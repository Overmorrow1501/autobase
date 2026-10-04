"""Unit tests of proxysql-patroni (automation/roles/proxysql/files/proxysql_patroni.py).

Run: python3 -m unittest discover -s automation/molecule/tests/roles/proxysql -p 'test_*.py' -v
"""

import importlib.util
import json
import os
import unittest

SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "roles", "proxysql", "files", "proxysql_patroni.py"
)
SPEC = importlib.util.spec_from_file_location("proxysql_patroni", SCRIPT)
pp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pp)

ADDRESSES = {"pgnode01": "10.0.0.1", "pgnode02": "10.0.0.2", "pgnode03": "10.0.0.3"}

CONF = {
    "interval": 1,
    "timeout": 2,
    "fall": 3,
    "on_marked_down": "shutdown-sessions",
    "admin": {"psql": "/usr/bin/psql", "host": "127.0.0.1", "port": 6132, "user": "admin", "password": "x", "dbname": "admin"},
    "patroni": {"protocol": "http", "seeds": ["http://10.0.0.1:8008"]},
    "backend": {"port": 5432, "max_connections": 100, "use_ssl": False},
    "hostgroups": [
        {"id": 10, "check": "/primary", "rise": 4, "master": True},
        {"id": 20, "check": "/replica?lag=100MB", "rise": 2, "master": False},
    ],
}


class FakeAdmin(object):
    """In-memory pgsql_servers table."""

    def __init__(self, servers=()):
        self.rows = set(servers)
        self.writes = 0

    def servers(self):
        return set(self.rows)

    def replace_servers(self, servers):
        self.rows = set(servers)
        self.writes += 1


class FakePatroni(object):
    """Patroni REST API of the members in 'roles': primary, replica, down (listed, unreachable) or gone."""

    def __init__(self, roles):
        self.roles = roles

    def get(self, url, timeout, context=None):
        address, _, path = url.split("://", 1)[1].partition("/")
        name = [member for member, ip in ADDRESSES.items() if ip == address.split(":")[0]][0]
        role = self.roles.get(name, "gone")
        if role in ("down", "gone"):
            return None, b""
        path = "/" + path
        if path == "/cluster":
            members = [
                {"name": member, "host": ADDRESSES[member], "port": 5432, "api_url": "http://%s:8008/patroni" % ADDRESSES[member]}
                for member, member_role in sorted(self.roles.items())
                if member_role != "gone"
            ]
            return 200, json.dumps({"members": members}).encode("utf-8")
        if path.startswith("/primary"):
            return (200 if role == "primary" else 503), b""
        if path.startswith("/replica"):
            return (200 if role == "replica" else 503), b""
        return 503, b""


def online(servers, hostgroup_id):
    return sorted(server.comment for server in servers if server.hostgroup_id == hostgroup_id and server.status == pp.ONLINE)


def server(hostgroup_id, name, status):
    return pp.Server(hostgroup_id, ADDRESSES[name], 5432, status, 1, 100, 0, name)


class HealthTest(unittest.TestCase):
    def test_rise_and_fall(self):
        health = pp.Health(rise=2, fall=3)
        self.assertFalse(health.update(True))
        self.assertTrue(health.update(True))
        self.assertTrue(health.update(False))
        self.assertTrue(health.update(False))
        self.assertFalse(health.update(False))

    def test_streak_resets_when_the_result_flaps(self):
        health = pp.Health(rise=2, fall=2, up=True)
        self.assertTrue(health.update(False))
        self.assertTrue(health.update(True))
        self.assertTrue(health.update(False))
        self.assertFalse(health.update(False))


class ParsingTest(unittest.TestCase):
    def test_parse_members(self):
        cluster = {
            "members": [
                {"name": "pgnode01", "host": "10.0.0.1", "api_url": "https://10.0.0.1:8008/patroni"},
                {"name": "pgnode02", "host": "10.0.0.2"},
            ]
        }
        self.assertEqual(pp.parse_members(cluster), {"pgnode01": {"host": "10.0.0.1", "api": "https://10.0.0.1:8008"}})

    def test_parse_members_of_an_invalid_document(self):
        self.assertEqual(pp.parse_members([]), {})
        self.assertEqual(pp.parse_members({"members": None}), {})

    def test_parse_servers(self):
        output = "10\t10.0.0.1\t5432\tONLINE\t1\t100\t0\tpgnode01\n20\t10.0.0.2\t5432\tOFFLINE_HARD\t1\t100\t1\t\n"
        self.assertEqual(
            pp.parse_servers(output),
            {
                pp.Server(10, "10.0.0.1", 5432, "ONLINE", 1, 100, 0, "pgnode01"),
                pp.Server(20, "10.0.0.2", 5432, "OFFLINE_HARD", 1, 100, 1, ""),
            },
        )

    def test_servers_sql_quotes_values_and_loads_to_runtime(self):
        sql = pp.servers_sql({pp.Server(10, "db'1", 5432, "ONLINE", 1, 100, 0, "pg'node")})
        self.assertTrue(sql.startswith("DELETE FROM pgsql_servers;\n"))
        self.assertIn("'db''1'", sql)
        self.assertIn("'pg''node'", sql)
        self.assertTrue(sql.endswith("LOAD PGSQL SERVERS TO RUNTIME;\n"))


class SyncerTest(unittest.TestCase):
    def make(self, roles, servers=(), conf=None):
        self.patroni = FakePatroni(roles)
        self.admin = FakeAdmin(servers)
        return pp.Syncer(conf or CONF, self.admin, get=self.patroni.get)

    @staticmethod
    def run_times(syncer, count):
        servers = None
        for _ in range(count):
            servers = syncer.run_once()
        return servers

    def test_new_primary_is_online_after_rise_checks(self):
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica"})
        self.assertEqual(online(self.run_times(syncer, 3), 10), [])
        self.assertEqual(online(syncer.run_once(), 10), ["pgnode01"])
        self.assertEqual(online(self.admin.rows, 20), ["pgnode02"])

    def test_existing_online_rows_are_kept_after_restart(self):
        current = {
            server(10, "pgnode01", pp.ONLINE),
            server(20, "pgnode01", pp.OFFLINE_HARD),
            server(10, "pgnode02", pp.OFFLINE_HARD),
            server(20, "pgnode02", pp.ONLINE),
        }
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica"}, current)
        servers = syncer.run_once()
        self.assertEqual(online(servers, 10), ["pgnode01"])
        self.assertEqual(online(servers, 20), ["pgnode02"])
        self.assertEqual(self.admin.writes, 0)

    def test_failover_moves_writes_to_the_new_primary(self):
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica"})
        self.run_times(syncer, 4)
        self.patroni.roles.update({"pgnode01": "down", "pgnode02": "primary"})
        servers = self.run_times(syncer, 3)
        self.assertEqual(online(servers, 10), [])
        self.assertIn(server(10, "pgnode01", pp.OFFLINE_HARD), servers)
        self.assertEqual(online(syncer.run_once(), 10), ["pgnode02"])

    def test_split_brain_routes_no_writes(self):
        syncer = self.make({"pgnode01": "primary", "pgnode02": "primary"})
        servers = self.run_times(syncer, 4)
        self.assertEqual(online(servers, 10), [])
        self.assertEqual({row.status for row in servers if row.hostgroup_id == 10}, {pp.OFFLINE_HARD})

    def test_last_known_members_are_kept_when_patroni_is_unreachable(self):
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica"})
        self.run_times(syncer, 4)
        self.patroni.roles.update({"pgnode01": "down", "pgnode02": "down"})
        servers = self.run_times(syncer, 3)
        self.assertEqual(len(servers), 4)
        self.assertEqual({row.status for row in servers}, {pp.OFFLINE_HARD})

    def test_member_removed_from_the_cluster_is_removed(self):
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica", "pgnode03": "replica"})
        self.run_times(syncer, 4)
        self.patroni.roles["pgnode03"] = "gone"
        self.assertNotIn("pgnode03", {row.comment for row in syncer.run_once()})

    def test_new_member_is_discovered(self):
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica"})
        self.run_times(syncer, 4)
        self.patroni.roles["pgnode03"] = "replica"
        self.assertEqual(online(self.run_times(syncer, 2), 20), ["pgnode02", "pgnode03"])

    def test_replica_marked_down_keeps_sessions_when_configured(self):
        conf = dict(CONF, on_marked_down="")
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica"}, conf=conf)
        self.run_times(syncer, 4)
        self.patroni.roles["pgnode02"] = "down"
        self.assertIn(server(20, "pgnode02", pp.OFFLINE_SOFT), self.run_times(syncer, 3))

    def test_no_write_when_nothing_changes(self):
        syncer = self.make({"pgnode01": "primary", "pgnode02": "replica"})
        self.run_times(syncer, 4)
        writes = self.admin.writes
        self.run_times(syncer, 5)
        self.assertEqual(self.admin.writes, writes)


if __name__ == "__main__":
    unittest.main()
