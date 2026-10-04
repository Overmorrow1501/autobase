#!/usr/bin/env python3
"""proxysql-patroni: keep the ProxySQL PostgreSQL backends in sync with the Patroni cluster topology.

Discovers the cluster members with the Patroni REST API (GET /cluster), checks every member with the
same health endpoints used by the HAProxy configuration (/primary, /replica, /sync, /async) and
reconciles the ProxySQL 'pgsql_servers' table through the ProxySQL Admin interface (psql).

Managed by Autobase (vitabaks.autobase.proxysql role).
"""

import argparse
import collections
import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ONLINE = "ONLINE"
OFFLINE_SOFT = "OFFLINE_SOFT"
OFFLINE_HARD = "OFFLINE_HARD"

LOG = logging.getLogger("proxysql-patroni")

Server = collections.namedtuple(
    "Server", ["hostgroup_id", "hostname", "port", "status", "weight", "max_connections", "use_ssl", "comment"]
)


class Health(object):
    """Rise/fall hysteresis of one (member, hostgroup) check, like the HAProxy 'rise' and 'fall' options."""

    def __init__(self, rise, fall, up=False):
        self.rise = rise
        self.fall = fall
        self.up = up
        self.streak = 0

    def update(self, ok):
        """Record one check result and return the resulting state (True = up)."""
        if ok == self.up:
            self.streak = 0
        else:
            self.streak += 1
            if self.streak >= (self.fall if self.up else self.rise):
                self.up = ok
                self.streak = 0
        return self.up


def sql_quote(value):
    """Return value as a single-quoted SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def parse_members(cluster):
    """Return {member name: {"host": ..., "api": ...}} from a Patroni GET /cluster document."""
    members = {}
    if not isinstance(cluster, dict):
        return members
    for member in cluster.get("members") or []:
        name, host, api_url = member.get("name"), member.get("host"), member.get("api_url")
        if not (name and host and api_url):
            continue
        members[name] = {"host": host, "api": api_url.rsplit("/patroni", 1)[0].rstrip("/")}
    return members


def parse_servers(output):
    """Parse the tab-separated psql output of the pgsql_servers query."""
    servers = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        servers.add(
            Server(
                int(fields[0]), fields[1], int(fields[2]), fields[3], int(fields[4]), int(fields[5]), int(fields[6]),
                fields[7] if len(fields) > 7 else "",
            )
        )
    return servers


def servers_sql(servers):
    """Return the SQL that replaces pgsql_servers and loads it to runtime in one batch."""
    lines = ["DELETE FROM pgsql_servers;"]
    for row in sorted(servers):
        lines.append(
            "INSERT INTO pgsql_servers (hostgroup_id, hostname, port, status, weight, max_connections, use_ssl, comment) "
            "VALUES (%d, %s, %d, %s, %d, %d, %d, %s);"
            % (
                row.hostgroup_id, sql_quote(row.hostname), row.port, sql_quote(row.status), row.weight,
                row.max_connections, row.use_ssl, sql_quote(row.comment),
            )
        )
    lines.append("LOAD PGSQL SERVERS TO RUNTIME;")
    return "\n".join(lines) + "\n"


def http_get(url, timeout, context=None):
    """GET url and return (status code, body). The status is None if the request failed."""
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout, context=context) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, b""
    except (URLError, OSError, ValueError) as error:
        LOG.debug("GET %s failed: %s", url, error)
        return None, b""


def log_changes(current, desired):
    """Log every status change of a backend."""
    before = dict(((row.hostgroup_id, row.hostname, row.port), row.status) for row in current)
    after = dict(((row.hostgroup_id, row.hostname, row.port), row.status) for row in desired)
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            LOG.info("hostgroup %s, %s:%s: %s -> %s", key[0], key[1], key[2], before.get(key, "none"), after.get(key, "none"))


class ProxySQLAdmin(object):
    """Minimal client of the ProxySQL Admin interface (PostgreSQL protocol) based on psql."""

    def __init__(self, conf):
        self.command = [
            conf["psql"], "-h", conf["host"], "-p", str(conf["port"]), "-U", conf["user"], "-d", conf.get("dbname", "admin"),
            "-X", "-q", "-A", "-t", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-f", "-",
        ]
        self.env = dict(os.environ, PGPASSWORD=conf["password"], PGCONNECT_TIMEOUT="5")

    def run(self, sql):
        """Run SQL statements (one per line) and return the output of psql."""
        process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env, universal_newlines=True,
        )
        try:
            output, error = process.communicate(sql, timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("psql did not finish in 30 seconds")
        if process.returncode != 0:
            raise RuntimeError(error.strip() or "psql exited with code %d" % process.returncode)
        return output

    def servers(self):
        """Return the rows of the pgsql_servers table."""
        return parse_servers(
            self.run("SELECT hostgroup_id, hostname, port, status, weight, max_connections, use_ssl, comment FROM pgsql_servers;\n")
        )

    def replace_servers(self, servers):
        """Replace the rows of the pgsql_servers table and load them to runtime."""
        self.run(servers_sql(servers))


class Syncer(object):
    """Compute and apply the ProxySQL backends from the state of the Patroni cluster."""

    def __init__(self, conf, admin, get=http_get):
        self.conf = conf
        self.admin = admin
        self.get = get
        self.timeout = float(conf["timeout"])
        self.fall = int(conf["fall"])
        self.hostgroups = conf["hostgroups"]
        self.master_hostgroups = set(int(hostgroup["id"]) for hostgroup in self.hostgroups if hostgroup.get("master"))
        backend = conf["backend"]
        self.backend = (int(backend["port"]), int(backend["max_connections"]), 1 if backend.get("use_ssl") else 0)
        self.replica_down = OFFLINE_HARD if conf.get("on_marked_down", "shutdown-sessions") else OFFLINE_SOFT
        self.context = None
        if conf["patroni"]["protocol"] == "https":
            # Like the HAProxy checks ('check-ssl verify none'): encrypted, the certificate is not verified.
            self.context = ssl.create_default_context()
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.members = {}
        self.health = {}
        self.online = None
        self.executor = ThreadPoolExecutor(max_workers=16)

    def discover(self):
        """Refresh the members from the first Patroni REST API that answers GET /cluster."""
        urls = [member["api"] for member in self.members.values()] + list(self.conf["patroni"]["seeds"])
        for url in collections.OrderedDict.fromkeys(urls):
            status, body = self.get(url + "/cluster", self.timeout, self.context)
            if status != 200:
                continue
            try:
                members = parse_members(json.loads(body.decode("utf-8")))
            except ValueError:
                continue
            if members:
                if set(members) != set(self.members):
                    LOG.info("Patroni cluster members: %s", ", ".join(sorted(members)))
                self.members = members
                return True
        LOG.warning("The Patroni REST API is not available on any member, using the last known members")
        return False

    def check(self):
        """Run the health checks of all members in parallel and return {(member, hostgroup_id): passed}."""
        futures = {}
        for name, member in self.members.items():
            for hostgroup in self.hostgroups:
                url = member["api"] + hostgroup["check"]
                futures[(name, int(hostgroup["id"]))] = self.executor.submit(self.get, url, self.timeout, self.context)
        return dict((key, future.result()[0] == 200) for key, future in futures.items())

    def desired(self, results):
        """Update the health state with the check results and return the desired pgsql_servers rows."""
        state = {}
        for name in self.members:
            for hostgroup in self.hostgroups:
                key = (name, int(hostgroup["id"]))
                if key not in self.health:
                    self.health[key] = Health(int(hostgroup["rise"]), self.fall, up=key in (self.online or set()))
                state[key] = self.health[key].update(results.get(key, False))
        for key in list(self.health):
            if key[0] not in self.members:
                del self.health[key]
        masters = sorted(name for (name, hostgroup_id), up in state.items() if up and hostgroup_id in self.master_hostgroups)
        if len(masters) > 1:
            LOG.error("More than one member is reported as the primary (%s): writes are not routed", ", ".join(masters))
        port, max_connections, use_ssl = self.backend
        servers = set()
        for (name, hostgroup_id), up in state.items():
            master = hostgroup_id in self.master_hostgroups
            if up and not (master and len(masters) > 1):
                status = ONLINE
            else:
                status = OFFLINE_HARD if master else self.replica_down
            servers.add(Server(hostgroup_id, self.members[name]["host"], port, status, 1, max_connections, use_ssl, name))
        return servers

    def run_once(self):
        """Synchronize the ProxySQL backends once and return the desired rows."""
        current = self.admin.servers()
        if self.online is None:
            # Rows that are ONLINE when the service starts stay ONLINE until 'fall' failed checks (no outage on restart).
            self.online = set((row.comment, row.hostgroup_id) for row in current if row.status == ONLINE)
        self.discover()
        desired = self.desired(self.check())
        if desired != current:
            log_changes(current, desired)
            self.admin.replace_servers(desired)
        return desired


def main(argv=None):
    parser = argparse.ArgumentParser(description="Keep the ProxySQL backends in sync with the Patroni cluster topology.")
    parser.add_argument("-c", "--config", default="/etc/proxysql/patroni.json", help="configuration file (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every failed Patroni REST API request")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
    with open(args.config) as config_file:
        conf = json.load(config_file)
    syncer = Syncer(conf, ProxySQLAdmin(conf["admin"]))
    stopping = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.append(True))
    interval = float(conf["interval"])
    LOG.info("Started, checking the Patroni cluster every %s seconds", interval)
    while not stopping:
        started = time.time()
        try:
            syncer.run_once()
        except Exception as error:  # keep running, the next iteration retries
            LOG.error("Synchronization failed: %s", error)
        time.sleep(max(0.0, interval - (time.time() - started)))
    syncer.executor.shutdown(wait=False)
    LOG.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
