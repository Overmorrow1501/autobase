# Ansible Role: proxysql

This role installs and configures [ProxySQL](https://proxysql.com) ([source](https://github.com/sysown/proxysql)), a high-performance proxy with connection pooling that provides load balancing for PostgreSQL database connections. It is an alternative to HAProxy (`with_haproxy_load_balancing` and `with_proxysql_load_balancing` are mutually exclusive) and runs on the `balancers` hosts.

## How it works

- Listeners and hostgroups. Each listener port is bound to one hostgroup, whose members are selected with the Patroni REST API health check:

  | Listener         | Port   | Hostgroup | Patroni check                                   |
  | ---------------- | ------ | --------- | ----------------------------------------------- |
  | `master`         | `5000` | `10`      | `/primary`                                      |
  | `replicas`       | `5001` | `20`      | `/replica?lag=<patroni_maximum_lag_on_replica>` |
  | `replicas_sync`  | `5002` | `30`      | `/sync`                                         |
  | `replicas_async` | `5003` | `40`      | `/async?lag=<patroni_maximum_lag_on_replica>`   |

  The checks are the same as for HAProxy, including the `balancer_tags` filters. The `replicas_sync` and `replicas_async` listeners can be disabled. When `cluster_vip` is set, the listeners bind to the VIP, otherwise to `proxysql_bind_address` (if defined) or `bind_address`.

- Routing. `pgsql_query_rules` route the connections by the listener port (`proxy_port`). Additional rules from `proxysql_query_rules` (`rule_id` 1-999) are applied before them.
- `proxysql-patroni`. A systemd service (`/usr/local/bin/proxysql-patroni`) discovers the cluster members with `GET /cluster` of the Patroni REST API, checks every member with the endpoints above and reconciles the ProxySQL `pgsql_servers` table. A member is marked up after `rise_master` / `rise_replica` successful checks and down after `fall` failed checks. If two members report as primary, no writes are routed (fail closed) and the condition is logged. When `proxysql-patroni` restarts, the servers that are already ONLINE stay ONLINE.
- Direct connections. ProxySQL connects directly to PostgreSQL (`proxysql_backend_port`, default `postgresql_port`) and provides the connection pool itself, PgBouncer is not used on this path.
- Admin interface. The ProxySQL Admin listens on `127.0.0.1:6132` (PostgreSQL protocol). The password is in `/etc/proxysql.cnf`:

  ```bash
  psql -h 127.0.0.1 -p 6132 -U admin -d admin
  ```

## Role Variables

### Core Configuration

| Variable                       | Default                | Description                                                                                          |
| ------------------------------ | ---------------------- | ---------------------------------------------------------------------------------------------------- |
| `with_proxysql_load_balancing` | `false`                | Enable ProxySQL installation and configuration                                                       |
| `proxysql_install_repo`        | `true`                 | Add the ProxySQL repository. Set to `false` to install ProxySQL from your own repository             |
| `proxysql_repo_series`         | `"3.0.x"`              | Repository series (the Stable tier with PostgreSQL support), see https://repo.proxysql.com/ProxySQL/ |
| `proxysql_version`             | `"latest"`             | `latest` or a specific version (e.g. `3.0.11`)                                                       |
| `proxysql_bind_address`        | `"{{ bind_address }}"` | Address to bind the listeners when no VIP is used. Optional, not declared in the defaults            |

### Port Configuration

| Variable                              | Default | Description                                                               |
| ------------------------------------- | ------- | ------------------------------------------------------------------------- |
| `proxysql_listen_port.master`         | `5000`  | Port for primary database connections                                     |
| `proxysql_listen_port.replicas`       | `5001`  | Port for read-only replica connections                                    |
| `proxysql_listen_port.replicas_sync`  | `5002`  | Port for synchronous replica connections                                  |
| `proxysql_listen_port.replicas_async` | `5003`  | Port for asynchronous replica connections                                 |
| `proxysql_listen_port.admin`          | `6132`  | ProxySQL Admin interface (PostgreSQL protocol), listens on 127.0.0.1 only |

### Replica Listeners

| Variable                          | Default | Description                          |
| --------------------------------- | ------- | ------------------------------------ |
| `proxysql_replicas_sync_enabled`  | `true`  | Enable the `replicas_sync` listener  |
| `proxysql_replicas_async_enabled` | `true`  | Enable the `replicas_async` listener |

Set a flag to `false` to disable the listener, for example in a cluster with a single replica, where one of the two would be permanently empty.

### Hostgroups

| Variable                            | Default | Description                            |
| ----------------------------------- | ------- | -------------------------------------- |
| `proxysql_hostgroup.master`         | `10`    | Hostgroup of the primary               |
| `proxysql_hostgroup.replicas`       | `20`    | Hostgroup of the replicas              |
| `proxysql_hostgroup.replicas_sync`  | `30`    | Hostgroup of the synchronous replicas  |
| `proxysql_hostgroup.replicas_async` | `40`    | Hostgroup of the asynchronous replicas |

### Backend Connections

| Variable                           | Default                   | Description                                                                |
| ---------------------------------- | ------------------------- | -------------------------------------------------------------------------- |
| `proxysql_backend_port`            | `"{{ postgresql_port }}"` | PostgreSQL port used by ProxySQL (direct connection)                       |
| `proxysql_backend_max_connections` | `100`                     | Maximum number of connections from each ProxySQL to each PostgreSQL server |

### Credentials

| Variable                    | Default              | Description                                                                                   |
| --------------------------- | -------------------- | --------------------------------------------------------------------------------------------- |
| `proxysql_admin_username`   | `"admin"`            | ProxySQL Admin user                                                                           |
| `proxysql_admin_password`   | `""`                 | ProxySQL Admin password. If not defined, it is generated automatically during deployment      |
| `proxysql_monitor_username` | `"proxysql_monitor"` | PostgreSQL user for the ProxySQL health checks (created automatically)                        |
| `proxysql_monitor_password` | `""`                 | Password of the monitor user. If not defined, it is generated automatically during deployment |

### Users and Advanced Configuration

| Variable                   | Default | Description                                                                                                                                                                                            |
| -------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `proxysql_users`           | `[]`    | Additional users allowed to connect through ProxySQL, see [Users and passwords](#users-and-passwords)                                                                                                  |
| `proxysql_pgsql_variables` | `[]`    | ProxySQL `pgsql-` global variables (without the prefix), e.g. `{ option: "max_connections", value: "10000" }`                                                                                          |
| `proxysql_query_rules`     | `[]`    | Additional query rules (`rule_id` 1-999), e.g. `{ rule_id: 1, proxy_port: 5000, match_digest: "^SELECT", destination_hostgroup: 20, apply: 1 }`. The routing by listener port uses `rule_id` 1001-1003 |

### proxysql-patroni

| Variable                          | Default               | Description                                                                                                                                                                                  |
| --------------------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `proxysql_patroni.interval`       | `1`                   | Seconds between checks                                                                                                                                                                       |
| `proxysql_patroni.timeout`        | `2`                   | Patroni REST API request timeout (seconds)                                                                                                                                                   |
| `proxysql_patroni.fall`           | `3`                   | Failed checks before a server is marked down                                                                                                                                                 |
| `proxysql_patroni.rise_master`    | `4`                   | Successful checks before the primary is marked up                                                                                                                                            |
| `proxysql_patroni.rise_replica`   | `2`                   | Successful checks before a replica is marked up                                                                                                                                              |
| `proxysql_patroni.on_marked_down` | `"shutdown-sessions"` | Action when a replica is marked down. The default closes its active sessions (`OFFLINE_HARD`). Set to `""` to keep them (`OFFLINE_SOFT`). The sessions of a former primary are always closed |

`proxysql-patroni` uses the Python interpreter that Ansible discovered on the host (standard library only, Python 3.6+).

## Configuration management

- File ownership. `/etc/proxysql.cnf` carries the marker line `Managed by Autobase (vitabaks.autobase.proxysql)`. On the first configuration of a host whose configuration lacks the marker (for example the packaged default), the role stops ProxySQL and removes `/var/lib/proxysql/proxysql.db`, so ProxySQL bootstraps from the Autobase configuration. Any non-Autobase ProxySQL configuration on that host is discarded.
- Later runs. The configuration is applied through the Admin interface without a restart. ProxySQL is restarted only when `pgsql-interfaces` changes, which includes a change of the listener ports, of `cluster_vip` or of the bind address.
- `proxysql_pgsql_variables`. Removing an entry does not reset the runtime value: it stays until ProxySQL is restarted from a fresh configuration or the value is set explicitly.
- `pgsql_servers`. The table is fully owned by `proxysql-patroni`. It is not saved to disk, and `proxysql-patroni` repopulates it within one interval after a ProxySQL restart.
- Monitor. The ProxySQL monitor is enabled from the start. The monitor user is created on the primary. On a standby cluster it is created on the first host of the `source_cluster` group if one is defined, otherwise a warning reminds you to create it on the source cluster with `proxysql_monitor_password`.
- keepalived. When `cluster_vip` is set, the default `check_status_command` of keepalived is `/usr/libexec/keepalived/proxysql_check.sh`. If you override `keepalived_instances`, set `check_status_command` yourself.

## Users and passwords

- Provisioning. ProxySQL users are merged by name (later sources override earlier ones) from: the superuser (`patroni_superuser_username`), the `postgresql_users`, and `proxysql_users`. Users in `proxysql_users` accept `name`, `password`, `default_hostgroup` (default `proxysql_hostgroup.master`), `transaction_persistent` (default `1`), `max_connections` (default `10000`) and `state`.
- Removal. Users are only removed from ProxySQL with `state: absent`. Users that disappear from every list stay in ProxySQL.
- Cleartext storage. ProxySQL stores the passwords in clear text (in `/var/lib/proxysql/proxysql.db`). ProxySQL 3.0.11 needs the cleartext password to authenticate with SCRAM to the PostgreSQL backend, see the [ProxySQL PostgreSQL authentication](https://www.proxysql.com/documentation/users-management/postgresql-authentication) documentation.
- `postgresql_users_password_generation: on_create`. Users whose password is not known (existing users) are left unchanged in ProxySQL, they are not removed or blanked. To make such a user available through ProxySQL, add it to `proxysql_users` with its password.
- Admin password. Changing `proxysql_admin_password` later is applied by `config_pgcluster.yml`. If the password is not defined, the role reuses the one from the existing Autobase configuration, or generates a new one.
- `add_node.yml`. When `postgresql_users_password_generation` is `on_create` or `always`, a new balancer added with `add_node.yml` only receives the users whose passwords are known in the inventory (`postgresql_users` with a `password`, and `proxysql_users`), not the generated ones. With `on_create`, add the users that must be available through the new balancer to `proxysql_users` with their passwords (`config_pgcluster.yml` cannot recover the passwords of existing users). With `always`, `config_pgcluster.yml` configures them, but it also rotates every generated password, which affects the applications.

## TLS

Client TLS uses the certificate that ProxySQL generates automatically in `/var/lib/proxysql`. Backend TLS (`use_ssl` on the servers) follows `tls_cert_generate`.

## Limitations

- `cloud_resources` and the Console do not support ProxySQL yet (on-premises / "Your Own Machines" only).
- `pgbouncer_pool_pause` does not apply during `pg_upgrade`, ProxySQL connects directly to PostgreSQL.
- If the PostgreSQL client of the configured version is removed (for example by `pg_upgrade`), `proxysql-patroni` falls back to `/usr/bin/psql`; `pgsql-server_version` keeps showing the old major version until `config_pgcluster.yml` (or `deploy_pgcluster.yml`) is re-run.
- The ProxySQL 3.0.x packages are unavailable for Debian 11 and Ubuntu 26.04.
- EL8 packages stop at 3.0.9.
- `pgsql_servers` is fully managed by `proxysql-patroni`, manual changes are overwritten.
- Switching an existing HAProxy cluster to ProxySQL is not supported: HAProxy keeps ports 5000-5003 and `config_pgcluster.yml` does not remove it.
- `balancers.yml` on its own only installs ProxySQL. The users and the monitor user are configured by `deploy_pgcluster.yml`, `config_pgcluster.yml` and `add_node.yml`.
- Do not limit runs (`--limit`) to the balancer hosts: the facts of the database servers are not gathered, so `/etc/proxysql/patroni.json` uses the inventory hostnames of the backends instead of their IP addresses.
- The RPM path (AlmaLinux/Rocky 8-10 repositories, `/usr/pgsql-N/bin/psql`) and RHEL-family dedicated balancers are implemented but were not exercised in the integration tests (only Ubuntu 24.04 was).
- SELinux: not covered (tested in containers without SELinux).

## Dependencies

This role depends on:

- `vitabaks.autobase.common` - Provides common variables and configurations
