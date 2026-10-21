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

The required watchdog must be writable by `postgres` before Patroni starts.
The unit loads `softdog` and waits up to 25 seconds for udev permissions. An
expired wait fails the pre-start command; systemd retries after five seconds.
Starting Patroni without the device can leave it permanently in replica mode
even after the device appears, because failed watchdog activation is cached.
Keep watchdog mode `required`; repair the device and restart Patroni instead of
forcing promotion or reinitializing existing data. Confirm the cluster member
list and backups before recovery, then verify `pg_is_in_recovery()` is false
and a write transaction succeeds on the elected primary.

## Regression checks

`tox -e py312` runs the real PG bootstrap script with command stubs, covering
stop failures, disk discovery/preparation errors, failures in either migration,
and a successful retry. It also checks agent ordering, hard mount assertions,
and startup on first configuration. These tests do not replace a real image boot
with an attached persistent disk.

`exordos_db/tests/unit/test_watchdog_startup.py` executes the shipped shell
predicate with a late or absent synthetic device. It verifies that an absent
watchdog fails startup after the bounded wait and a late device allows startup.
The tests never open a real watchdog or change host services.
