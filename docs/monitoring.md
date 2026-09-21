# Monitoring PostgreSQL instances

The nodes of a PostgreSQL instance send their metrics to the platform
VictoriaMetrics of the `observability` element. Nothing is sent until that
element is deployed: the vmagent of the base image waits for
`victoria-storage.local.genesis-core.tech` to resolve before it starts.

The node image has to be built on `exordos_base` 1.3.1 or later, the first one
with vmagent and node_exporter. Nodes of an older image get them once they are
reinstalled from the new one.

## What is collected

vmagent scrapes three local endpoints every 15 s:

| Job | Endpoint | What it covers |
|---|---|---|
| `node_exporter` | `127.0.0.1:9100`, from the base image | the node, including the data disk |
| `patroni` | `127.0.0.1:8008/metrics`, the Patroni REST API | role, HA state, WAL positions, timeline (`patroni_*`) |
| `postgres_exporter` | `127.0.0.1:9187`, `prometheus-postgres-exporter` | sessions, database sizes, WAL, replication, statistics (`pg_*`) |

Every series carries:

- `exordos_db_instance` — the instance uuid;
- `exordos_project` — the project of the instance;
- `instance` — the node host name, `dbaas-dp-<instance uuid>-node-<suffix>`.

Patroni adds `scope` (the instance name) and `name` (the node uuid, also the
`application_name` of a replica on the primary).

The control plane delivers the labels: it writes the vmagent scrape template
`/etc/exordos_observability/vmagent_scrape.yml.tpl` to every node, in place of
the one of the base image, which only scrapes node_exporter.

Why two PostgreSQL sources. Patroni serves its metrics on the REST API it
already runs, so they cost nothing, but they only describe the cluster: role,
replication positions, timeline, pause. Sessions, database sizes and WAL need
queries to PostgreSQL, which is what postgres_exporter does. It is the Ubuntu
package (0.19.0 on 26.04), so there is no binary to pin, and it runs as
`postgres` over the local socket with peer authentication, so there is no
password to deliver. It listens on the loopback only.

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

## Notes

- **Only vmagent restarts on a change of the labels or the jobs.** The
  template is a file of its own, apart from `patroni.yml`, and its delivery
  runs `systemctl --no-block try-restart exordos-vmagent`. vmagent waits a
  minute after it finds the observability host before it scrapes again, so
  each change leaves a gap of about a minute. A vmagent still waiting for the
  observability element is left alone and reads the template when it starts.
- **The template is the whole scrape config of the node.** It replaces the
  one of the base image, so a job the base image adds later is not scraped
  here until it is added to the template as well.
- **postgres_exporter reads as the superuser.** Peer authentication maps the
  `postgres` system user to the `postgres` role only.
