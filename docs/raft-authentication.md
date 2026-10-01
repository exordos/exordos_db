# Patroni and Raft authentication

Each new PostgreSQL instance receives its own random 256-bit password shared by
Patroni REST API authentication and Raft transport.
The control plane keeps it in the instance database row, excludes it from
the user API and core resource reports, and sends it through the existing
encrypted node configuration channel. The resulting Patroni configuration
is owned by `postgres` with mode `0600`. Control-plane database backups and
administrative access remain sensitive; this does not add encryption at rest.

Patroni passes the password to PySyncObj, which encrypts and authenticates
Raft transport. `patroni[raft]` installs the required `cryptography` dependency.
Before each configuration PATCH, the node agent checks the config file
modification time (`st_mtime_ns`) and rereads REST API credentials only when
it changes, including when its client predates the upgrade.
A password change requires restarting Patroni; SIGHUP does not recreate
the Raft transport. Configuration delivery records only a password digest
in a root-owned file under `/run`. The first authenticated delivery restarts
Patroni; subsequent configuration deliveries reload it. A failed restart
does not record the digest, so delivery can retry.
The marker is cleared by a reboot, so the first configuration delivery after
a reboot also restarts Patroni, even when the password has not changed.

The migration authenticates existing instances only when both the requested
and reported node count are one and the reported Patroni configuration lists
only itself as a Raft peer. Other existing instances keep their current
authentication mode, including clusters that are scaling or have not yet
reported a configuration. There is no automatic retry of this migration for
those excluded instances. Concurrent scaling during the authentication
cutover is outside this migration's supported upgrade path.

The singleton restart preserves the PostgreSQL data directory, the Raft
journal and snapshots, and the Patroni scope. It requires a maintenance
interruption; it does not reinitialize or restore PostgreSQL. Passwords apply
to the network transport, not to the journal or snapshot format.

Existing multi-node clusters retain their legacy REST API credentials and
need a coordinated cutover. Passwordless and
authenticated PySyncObj peers cannot communicate, so changing passwords
one node at a time does not provide a compatible rolling upgrade. This
branch does not implement a cutover or a password rotation API for them.

The local regression tests exercise the generated delivery command and a
real singleton Raft journal restart. A separate test restarts real Patroni
and PostgreSQL, checks the original system identifier and stored rows, and
performs new writes. These tests require `patroni[raft]`, `pysyncobj`, and
PostgreSQL server binaries for the PostgreSQL test:

```sh
python -m pytest exordos_db/tests/unit/test_raft_auth.py \
  exordos_db/tests/functional/test_raft_upgrade.py
```
