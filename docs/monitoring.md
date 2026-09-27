# Monitoring PostgreSQL instances

The nodes of a PostgreSQL instance send their metrics to the platform
VictoriaMetrics of the `observability` element. Nothing is sent until that
element is deployed: the vmagent of the base image waits for
`victoria-storage.local.genesis-core.tech` to resolve before it starts.

The node image has to be built on `exordos_base` 1.3.1 or later, the first one
with vmagent and node_exporter. Nodes of an older image get them once they are
reinstalled from the new one.

## What is collected

vmagent scrapes local endpoints, every 15 s unless stated otherwise:

| Job | Endpoint | What it covers |
|---|---|---|
| `node_exporter` | `127.0.0.1:9100`, from the base image | the node, including the data disk |
| `patroni` | `127.0.0.1:8008/metrics`, the Patroni REST API | role, HA state, WAL positions, timeline (`patroni_*`) |
| `postgres_exporter` | `127.0.0.1:9187`, `exordos-postgres-exporter` | sessions, locks, database sizes, WAL, replication and slots, checkpoints, transaction ID age (`pg_*`) |
| `postgres_exporter_databases` | `127.0.0.1:9188/probe`, `exordos-postgres-exporter-databases`, every 60 s | per-table statistics of every database: sizes, rows, scans, vacuum (`pg_stat_user_tables_*`, `pg_statio_user_tables_*`, `pg_stat_progress_vacuum_*`), at most 30000 series a database |

Every series carries:

- `exordos_db_instance` — the instance uuid;
- `exordos_project` — the project of the instance;
- `exordos_db_type` — the engine, `postgres`: the dashboards and queries of
  each engine select their own series by it;
- `instance` — the node host name, `dbaas-dp-<instance uuid>-node-<suffix>`.

The series of `postgres_exporter_databases` also carry `database`, the
database probed, and the per-table ones `datname`, `schemaname` and `relname`.

Patroni adds `scope` (the instance name) and `name` (the node uuid, also the
`application_name` of a replica on the primary).

The node image replaces the vmagent scrape template of the base image,
`/etc/exordos_observability/vmagent_scrape.yml.tpl`, which only scrapes
node_exporter, with its own. The jobs read their targets from file_sd files
the pg agent writes with the labels:
`/var/lib/exordos/exordos_db/vmagent_node.json` for the jobs of the node and
`vmagent_databases.json` next to it for the databases. The control plane sends
the project to the agent with the rest of the node's resource. vmagent rereads
the files every minute, so a new node is scraped within a minute of the agent's
first run, without a restart.

Why two PostgreSQL sources. Patroni serves its metrics on the REST API it
already runs, so they cost nothing, but they only describe the cluster: role,
replication positions, timeline, pause. Sessions, database sizes and WAL need
queries to PostgreSQL, which is what postgres_exporter does. It is the Ubuntu
package (0.19.0 on 26.04), so there is no binary to pin, and it runs as
`postgres` over the local socket with peer authentication, so there is no
password to deliver. It listens on the loopback only.

Why two postgres_exporters. PostgreSQL shows the per-table statistics only to
a session of the same database, while the exporter keeps one connection. The
first one, connected to `postgres`, collects the instance-wide statistics;
the second one collects nothing on its own and is probed by vmagent once per
database, with only the per-table collectors and the vacuum progress on,
whose table names resolve in the database of the session only. The pg agent keeps the list
of databases in `/var/lib/exordos/exordos_db/vmagent_databases.json`, a
file_sd file vmagent rereads every minute, so a new database is picked up
without a restart. Every node writes it: a replica has the same databases.
The per-table statistics of a replica only count the queries run on it, the
dashboards read the ones of the primary.

## How full the disks are

The data disk is mounted at `/persist` and bind-mounted into
`/var/lib/postgresql/patroni/data` (PGDATA with `pg_wal`),
`/var/lib/postgresql/patroni/raft` and `/var/log`, so all of them count against
it, the node's logs included. As `df` counts it, root-reserved blocks left out,
for the fullest node of each instance:

```promql
max by (exordos_db_instance) (
  100 * (
    node_filesystem_size_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
    - node_filesystem_free_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
  ) / (
    node_filesystem_size_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
    - node_filesystem_free_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
    + node_filesystem_avail_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
  )
)
```

The fullest node is the one that breaks: PostgreSQL stops once it cannot write
WAL. The nodes do not fill evenly: the primary keeps the WAL that the
replication slot of a lagging or disconnected replica still needs, and a
replica keeps the WAL it has received but not replayed yet. Drop the `max` to see every node; the same expression
over a range gives the history, VictoriaMetrics keeps it for the retention of
the observability element.

## Replication

Replay lag of each replica in seconds, 0 when it has replayed everything it
received:

```promql
max by (exordos_db_instance) (pg_replication_lag_seconds)
```

Replay lag in bytes, as the primary sees each connected replica
(`application_name` is the node uuid of the replica):

```promql
max by (exordos_db_instance, application_name) (pg_stat_replication_pg_wal_lsn_diff)
```

A replica that has lost its connection is missing from the query above, but
not from Patroni's view:

```promql
max by (exordos_db_instance) (patroni_xlog_location)
- on (exordos_db_instance) group_right ()
(patroni_xlog_replayed_location and patroni_replica == 1)
```

Number of primaries, anything but 1 means the instance has no leader or is
split:

```promql
count by (exordos_db_instance) (patroni_primary == 1)
```

A timeline change, `changes(patroni_postgres_timeline[1h])`, marks a failover
or switchover.

## Sessions, databases and WAL

Share of `max_connections` in use on each node, in percent:

```promql
100 * sum by (exordos_db_instance, instance) (
  pg_stat_activity_count{backend_type="client backend"}
) / on (exordos_db_instance, instance)
max by (exordos_db_instance, instance) (pg_settings_max_connections)
```

Database sizes:

```promql
max by (exordos_db_instance, datname) (pg_database_size_bytes{datname!~"template[01]"})
```

Size of `pg_wal` on each node:

```promql
max by (exordos_db_instance, instance) (pg_wal_size_bytes)
```

## Backups

With backups on, the backup timer (`exordos-db-pg-backup`, every 15 min) reads
`pgbackrest info` on the primary and writes what it finds to
`/var/lib/exordos/node_exporter/exordos_db_backup.prom`, which the
node_exporter of the base image serves (its textfile collector, in
`exordos_base` 1.3.5 or later). A replica, and a node whose instance has backups off,
removes the file. The series come through the `node_exporter` job with the
labels above; a former primary keeps its file until its next run, so the
queries take the current primary only.

| Metric | Labels | Meaning |
|---|---|---|
| `exordos_db_backup_start_timestamp_seconds`, `exordos_db_backup_stop_timestamp_seconds` | `backup_name`, `backup_type` | when a backup in the repository started and finished |
| `exordos_db_backup_size_bytes` | `backup_name`, `backup_type` | size of the database in a backup |
| `exordos_db_backup_repo_delta_bytes` | `backup_name`, `backup_type` | what a backup added to the repository, compressed |
| `exordos_db_backup_error` | `backup_name`, `backup_type` | 1 if a backup has pages with checksum errors; it restores all the same |
| `exordos_db_backup_last_completion_timestamp_seconds` | `backup_type` | when the last backup of the type finished; a full one counts as `diff` and `incr` too, a `diff` one as `incr` |
| `exordos_db_backup_recovery_window_start_timestamp_seconds` | | the earliest moment a restore can reach, the end of the oldest backup |
| `exordos_db_backup_archive_missing_segments` | | WAL segments missing from the archive since the last backup; a restore past them needs a backup taken later, which the timer takes |
| `exordos_db_backup_run_timestamp_seconds`, `exordos_db_backup_run_success` | | the last run of the timer, and whether it read the repository and took the backup due |

A restore reaches from the start of the window to the last WAL archived,
`pg_stat_archiver_last_archive_age` ago. PostgreSQL switches a segment after
`archive_timeout` (300 s) only if something was written to it, so on an idle
instance the age grows while nothing is lost.

Time since the last backup of any type, of the current primary:

```promql
time() - max by (exordos_db_instance) (
  exordos_db_backup_last_completion_timestamp_seconds{backup_type="incr"}
  and on (instance) (patroni_primary == 1)
)
```

The recovery window, in seconds:

```promql
max by (exordos_db_instance) (
  time() - pg_stat_archiver_last_archive_age
  - on (instance) group_left exordos_db_backup_recovery_window_start_timestamp_seconds
  and on (instance) (patroni_primary == 1)
)
```

Worth an alert: no backup for longer than the incremental interval plus the
15 min of the timer, `exordos_db_backup_run_success == 0` for more than one
run, `exordos_db_backup_archive_missing_segments > 0`, and a growing
`pg_stat_archiver_failed_count`.

## Logs

The base image relays the journal of every node to the platform VictoriaLogs.
Patroni and PostgreSQL both write to the journal of `exordos-patroni`,
PostgreSQL through the standard error Patroni starts it with, so their lines
arrive with nothing added on the node. The node host name, in `_HOSTNAME`,
carries the instance uuid:

```logsql
_HOSTNAME:~"^dbaas-dp-<instance uuid>-node-" _SYSTEMD_UNIT:"exordos-patroni.service"
```

Errors only:

```logsql
_HOSTNAME:~"^dbaas-dp-<instance uuid>-node-" _SYSTEMD_UNIT:"exordos-patroni.service" _msg:~"(ERROR|FATAL|PANIC):"
```

pgBackRest writes its own files in `/var/log/pgbackrest` (WAL archiving,
backups, checks); rsyslog on the node reads them and sends their lines to
VictoriaLogs as syslog with the app name `pgbackrest`. They are kept on the
node too, rotated weekly. Archiving and backups happen on the primary:

```logsql
hostname:~"^dbaas-dp-<instance uuid>-node-" app_name:"pgbackrest"
```

Failed WAL pushes and backups:

```logsql
hostname:~"^dbaas-dp-<instance uuid>-node-" app_name:"pgbackrest" _msg:~"(ERROR|WARN):"
```

## Transactions, locks and maintenance

Age of the oldest open transaction of a client, per state; `idle in
transaction` is a client that opened a transaction and doesn't finish it:

```promql
max by (exordos_db_instance, state) (
  pg_stat_activity_max_tx_duration{backend_type="client backend", state!="idle"}
)
```

Transaction ID age as a share of `autovacuum_freeze_max_age`. Past 1
autovacuum freezes aggressively; PostgreSQL stops accepting writes near 2^31
transactions:

```promql
max by (exordos_db_instance) (
  max by (exordos_db_instance, instance) (pg_database_wraparound_age_datfrozenxid_seconds)
  / on (exordos_db_instance, instance)
  max by (exordos_db_instance, instance) (pg_settings_autovacuum_freeze_max_age)
)
```

WAL each replication slot keeps on the primary (Patroni keeps copies of the
slots on the replicas, inactive):

```promql
max by (exordos_db_instance, slot_name) (
  pg_replication_slots_pg_wal_lsn_diff
  and on (instance) (patroni_primary == 1)
)
```

Dead rows of the tables, on the primary:

```promql
sum by (exordos_db_instance, datname, schemaname, relname) (
  pg_stat_user_tables_n_dead_tup and on (instance) (patroni_primary == 1)
)
```

## Dashboards

The `dbaas_dashboard` element puts the dashboards into the **DBaaS** folder of
the shared observability Grafana, per project and instance. It depends on the
`observability` element; install it after that one.

- **PostgreSQL instance**: primaries, role of each node, replication lag and
  slots, oldest transaction, transaction ID age, locks, deadlocks and
  conflicts, sessions, transactions, rows, temporary files, checkpoints, WAL
  archiving, backups and the recovery window, disks, CPU, memory and network
  of the nodes, the main settings, and the Patroni and PostgreSQL logs with
  the error rate.
- **PostgreSQL tables**: every table of the chosen databases with its size,
  rows, dead rows, scans and last autovacuum, the largest tables, dead row
  share, writes, sequential scans, cache hit ratio and vacuums.

Every engine gets dashboards of its own, the metrics differ. Their uids are
`exordos-dbaas-<engine>-<view>` and their tags `dbaas` and the engine; the
links in the header lead to the other dashboards of the engine, the chosen
instance and time range carried over.

## Notes

- **A node older than the project label.** Its agent skips the field it
  doesn't know and doesn't match the target hash until the node is reinstalled
  from the new image; agents of exordos_db before 2.5.0 fail on it.
- **The template is the whole scrape config of the node.** It replaces the
  one of the base image, so a job the base image adds later is not scraped
  here until it is added to the template as well.
- **postgres_exporter reads as the superuser.** Peer authentication maps the
  `postgres` system user to the `postgres` role only.
- **Every probe of a database opens connections.** The per-database exporter
  connects anew on every probe, so with `log_connections` on each database
  adds a few lines a minute to the PostgreSQL log.
