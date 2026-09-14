# PostgreSQL dataplane bootstrap

Both the PG agent and the base configuration-delivery agent are ordered after
`exordos-bootstrap.service`. The dependency is `Wants`, not `Requires`: a failed
bootstrap must not permanently remove the agent from service. After a failed
bootstrap the agent can report configuration errors; it must not silently start
Patroni on the root filesystem.

The PG image installs a drop-in for `exordos-universal-agent.service`, which
delivers `patroni.yml`. Ordering only `exordos-db-pg-agent` would leave that
configuration path unprotected. Control-plane images are not changed.

Bootstrap stops Patroni before preparing storage and keeps it stopped until
both the PostgreSQL data and Raft directories have been migrated. Only then is
Patroni enabled and started. A failed migration exits without starting Patroni.

Patroni requires `/persist` and both bind-mounted data directories. Missing
mounts fail the service start through systemd assertions, rather than skipping
it through a condition. An absent configuration file still skips startup; the
first delivered configuration uses `reload-or-restart` to start an inactive
service and propagate a failed start back to the configuration agent.

## Recovery

Inspect `journalctl -u exordos-bootstrap -u exordos-patroni` and the storage
devices before retrying. Repair the underlying disk or mount problem without
formatting existing data. Retry the installed PG bootstrap script:

```sh
sudo systemctl stop exordos-universal-agent exordos-db-pg-agent
sudo /var/lib/exordos/bootstrap/scripts/0100-ec-bootstrap.sh
sudo systemctl start exordos-universal-agent exordos-db-pg-agent
```

During a manual retry, stop both agents first so a concurrent configuration
update cannot restart Patroni between migrations. Start the agents again even
if the retry fails, so they can report the configuration failure.

The shared bootstrap completion marker alone is not proof that the PG storage
step succeeded. Verify `findmnt --mountpoint` for `/persist`,
`/var/lib/postgresql/patroni/data`, and `/var/lib/postgresql/patroni/raft`, then
check Patroni and the control-plane configuration status.

## Regression checks

`tox -e py312` runs the real PG bootstrap script with command stubs, covering
stop failures, disk discovery/preparation errors, failures in either migration,
and a successful retry. It also checks agent ordering, hard mount assertions,
and startup on first configuration. These tests do not replace a real image boot
with an attached persistent disk.
