#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import uuid

from gcl_sdk.agents.universal.dm import models as ua_models
import pytest
import requests

from exordos_db.agent.universal.drivers import pg

ADOPTING = {
    "uuid": str(uuid.uuid4()),
    "name": "restored",
    "nodes_number": 2,
    "sync_replica_number": 0,
    "users": {},
    "databases": {},
    "adopt_roles": True,
}


def test_adopt_roles_is_reported_only_while_sent():
    # A node that isn't adopting reports what it did before the flag
    adopting = pg.PGInstance.from_ua_resource(
        ua_models.Resource.from_value(ADOPTING, "pg_instance_node")
    )
    managed = pg.PGInstance.from_ua_resource(
        ua_models.Resource.from_value(
            {k: v for k, v in ADOPTING.items() if k != "adopt_roles"},
            "pg_instance_node",
        )
    )

    assert adopting.to_ua_resource("pg_instance_node").value["adopt_roles"]
    assert "adopt_roles" not in managed.to_ua_resource("pg_instance_node").value
    assert "adopt_roles" in adopting.get_meta_fields()


@pytest.mark.parametrize(
    "field, value",
    [
        ("found_roles", {"users": {"app": {"pw_hash": "x"}}, "databases": {}}),
        ("backup_state", {"error": "HTTP request failed with 403 (Forbidden)"}),
    ],
)
def test_reports_change_the_full_hash_only(field, value):
    # The agent reports a resource read from the data plane only while its
    # target hash matches; the control plane learns about the data plane
    # from the full hash. The found roles have to travel that way.
    resource = ua_models.Resource.from_value(ADOPTING, "pg_instance_node")
    empty = pg.PGInstance.from_ua_resource(resource)
    found = pg.PGInstance.from_ua_resource(resource)
    setattr(found, field, value)

    empty_resource = empty.to_ua_resource("pg_instance_node")
    found_resource = found.to_ua_resource("pg_instance_node")

    assert found_resource.hash == empty_resource.hash
    assert found_resource.full_hash != empty_resource.full_hash


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]


class FakePsql:
    """Answers the queries of the agent from the state of a cluster."""

    def __init__(self, in_recovery=False, users=None, databases=None):
        self.in_recovery = in_recovery
        self.users = users or {}
        self.databases = databases or {}
        self.executed = []

    def execute(self, query):
        text = query if isinstance(query, str) else repr(query)
        self.executed.append(text)
        if "pg_is_in_recovery" in text:
            return FakeCursor([(self.in_recovery,)])
        if "FROM pg_authid" in text:
            return FakeCursor(list(self.users.items()))
        if "FROM pg_catalog.pg_database" in text:
            return FakeCursor(list(self.databases.items()))
        return FakeCursor([])


class FakePatroni:
    member_name = "node-a"

    def __init__(self, primary=True, down=False, leader=None):
        self.primary = primary
        self.down = down
        self.leader = leader
        self.patches = []

    def is_primary(self, ttl_hash=None):
        if self.down:
            raise requests.ConnectionError("Patroni is restarting")
        return self.primary

    def cluster(self):
        members = [{"name": self.member_name, "role": "replica"}]
        if self.leader is not None:
            members.append({"name": self.leader, "role": "leader"})
        return {"members": members}

    def config_get(self):
        return {"synchronous_node_count": 0, "postgresql": {"parameters": {}}}

    def get_full_state(self):
        return {"timeline": 3}

    def config_patch(self, config):
        self.patches.append(config)
        return config


class FakeClients:
    def __init__(self, psql, pclient):
        self.psql = psql
        self.pclient = pclient


def _restored_node(psql, pclient, monkeypatch, restore_state=None):
    FakeRepository(monkeypatch, restore_state=restore_state)
    resource = ua_models.Resource.from_value(ADOPTING, "pg_instance_node")
    instance = pg.PGInstance.from_ua_resource(resource)
    instance.c = FakeClients(psql, pclient)
    return instance


RESTORED = {
    "users": {"app": "SCRAM-SHA-256$..."},
    "databases": {"app": "app"},
}


def test_roles_are_found_once_the_recovery_is_over(monkeypatch):
    psql = FakePsql(in_recovery=False, **RESTORED)
    instance = _restored_node(psql, FakePatroni(primary=True), monkeypatch)

    instance.restore_from_dp()

    assert instance.found_roles == {
        "users": {"app": {"pw_hash": "SCRAM-SHA-256$..."}},
        "databases": {"app": {"owner": "app"}},
    }
    assert instance.users == {}
    assert instance.databases == {}


def test_roles_of_a_cluster_in_recovery_are_not_found(monkeypatch):
    # PostgreSQL serves reads while the restore replays WAL: the roles are
    # those of the replayed moment, the ones created later are missing and
    # would be dropped once imported
    psql = FakePsql(in_recovery=True, **RESTORED)
    instance = _restored_node(psql, FakePatroni(primary=True), monkeypatch)

    instance.restore_from_dp()

    assert instance.found_roles is None
    assert instance.users == {}
    assert instance.databases == {}


def test_roles_of_a_replica_are_not_found(monkeypatch):
    psql = FakePsql(in_recovery=True, **RESTORED)
    instance = _restored_node(psql, FakePatroni(primary=False), monkeypatch)

    instance.restore_from_dp()

    assert instance.found_roles is None


SPEC = {"stanza": str(uuid.uuid4()), "options": {"repo1-type": "s3"}}


class FakeRepository:
    """pgbackrest of the agent: files on the node and the repository."""

    def __init__(
        self,
        monkeypatch,
        reachable=True,
        stanza_ready=False,
        spec=None,
        restore_state=None,
    ):
        self.reachable = reachable
        self.ready = stanza_ready
        self.spec = spec
        self.restore_state = restore_state
        self.error = None
        self.calls = []
        for name in (
            "apply_spec",
            "mark_stanza_ready",
            "stanza_ready",
            "run",
            "remove_restore_config",
            "load_spec",
            "load_restore_state",
            "remove_restore_state",
            "load_backup_error",
            "save_backup_error",
            "clear_backup_error",
        ):
            monkeypatch.setattr(pg.pgbackrest, name, getattr(self, name))

    def apply_spec(self, spec):
        self.calls.append(("apply_spec", spec))
        return False

    def mark_stanza_ready(self, spec):
        self.ready = spec is not None

    def stanza_ready(self, spec):
        return self.ready

    def run(self, stanza, *args, timeout=None):
        self.calls.append(("run", args))
        if not self.reachable:
            raise pg.pgbackrest.PgBackRestError("stanza-create failed")
        return ""

    def remove_restore_config(self):
        return False

    def load_spec(self):
        return self.spec

    def load_restore_state(self):
        return self.restore_state

    def remove_restore_state(self):
        removed = self.restore_state is not None
        self.restore_state = None
        return removed

    def load_backup_error(self):
        return self.error

    def save_backup_error(self, error):
        self.error = str(error)

    def clear_backup_error(self):
        removed = self.error is not None
        self.error = None
        return removed


def _managed_node(psql, pclient, backup):
    value = {
        **ADOPTING,
        "adopt_roles": False,
        "users": {"app": {"pw_hash": "SCRAM-SHA-256$..."}},
        "databases": {"app": {"owner": "app"}},
        "sync_replica_number": 1,
        "backup": backup,
    }
    resource = ua_models.Resource.from_value(value, "pg_instance_node")
    instance = pg.PGInstance.from_ua_resource(resource)
    instance.c = FakeClients(psql, pclient)
    return instance


def test_unreachable_repository_holds_nothing_else(monkeypatch):
    repository = FakeRepository(monkeypatch, reachable=False)
    psql = FakePsql()
    patroni = FakePatroni(primary=True)
    instance = _managed_node(psql, patroni, SPEC)

    instance.dump_to_dp()

    # Users, databases and replication are applied, archiving isn't
    # touched until the stanza exists
    assert any("CREATE USER" in q for q in psql.executed)
    assert any("CREATE DATABASE" in q for q in psql.executed)
    assert patroni.patches == [
        {
            "synchronous_mode": True,
            "synchronous_mode_strict": True,
            "synchronous_node_count": 1,
        }
    ]
    assert not repository.ready


def test_archiving_is_turned_on_after_the_stanza(monkeypatch):
    repository = FakeRepository(monkeypatch)
    patroni = FakePatroni(primary=True)
    instance = _managed_node(FakePsql(), patroni, SPEC)

    instance.dump_to_dp()

    assert ("run", ("stanza-create",)) in repository.calls
    assert repository.ready
    replication, archiving = patroni.patches
    assert "postgresql" not in replication
    assert archiving["postgresql"]["parameters"]["archive_command"] == (
        pg.pgbackrest.archive_command(SPEC)
    )


def test_replication_is_patched_before_the_users(monkeypatch):
    # A single node is bootstrapped in the strict synchronous mode: a user
    # created before the patch waits for a standby that never comes
    FakeRepository(monkeypatch)
    psql = FakePsql()
    patroni = FakePatroni(primary=True)
    events = []
    patroni.config_patch = lambda config: events.append(("patch", config))
    execute = psql.execute

    def logged_execute(query):
        events.append(("sql", query))
        return execute(query)

    psql.execute = logged_execute
    instance = _managed_node(psql, patroni, SPEC)
    instance.nodes_number = 1

    instance.dump_to_dp()

    kind, patch = events[0]
    assert kind == "patch"
    assert patch["synchronous_mode_strict"] is False
    assert any(k == "sql" and "CREATE USER" in str(q) for k, q in events)


def test_archiving_stops_before_the_config_is_removed(monkeypatch):
    repository = FakeRepository(monkeypatch, stanza_ready=True)
    patroni = FakePatroni(primary=True)
    events = []
    patroni.config_patch = lambda config: events.append(("patch", config))
    repository.apply_spec = lambda spec: events.append(("apply_spec", spec))
    monkeypatch.setattr(pg.pgbackrest, "apply_spec", repository.apply_spec)
    instance = _managed_node(FakePsql(), patroni, None)

    instance.dump_to_dp()

    kinds = [kind for kind, _ in events]
    assert kinds.index("patch") < kinds.index("apply_spec")
    assert events[0][1]["postgresql"]["parameters"]["archive_command"] == (
        pg.pgbackrest.archive_command(None)
    )


def test_replica_leaves_the_repository_and_roles_alone(monkeypatch):
    repository = FakeRepository(monkeypatch)
    psql = FakePsql()
    patroni = FakePatroni(primary=False)
    instance = _managed_node(psql, patroni, SPEC)

    instance.dump_to_dp()

    # Every node keeps the config, only the primary talks to the repository
    assert repository.calls == [("apply_spec", SPEC)]
    assert patroni.patches == []
    assert psql.executed == []


def _archiving_config(spec, archive_timeout=None):
    if archive_timeout is None:
        archive_timeout = pg.pgbackrest.archive_timeout(spec)
    return {
        "synchronous_node_count": 0,
        "postgresql": {
            "parameters": {
                "archive_command": pg.pgbackrest.archive_command(spec),
                "archive_timeout": archive_timeout,
            }
        },
    }


def test_backup_settles_once_archiving_and_stanza_are_in_place(monkeypatch):
    FakeRepository(monkeypatch, stanza_ready=True, spec=SPEC)
    instance = _managed_node(FakePsql(), FakePatroni(primary=True), SPEC)

    instance._fill_backup(_archiving_config(SPEC))

    assert instance.backup == SPEC


def test_backup_is_unsettled_without_the_stanza_on_the_primary(monkeypatch):
    FakeRepository(monkeypatch, stanza_ready=False, spec=SPEC)
    instance = _managed_node(FakePsql(), FakePatroni(primary=True), SPEC)

    instance._fill_backup(_archiving_config(SPEC))

    assert instance.backup == pg.BACKUP_UNSETTLED


def test_backup_is_unsettled_until_archiving_is_on(monkeypatch):
    FakeRepository(monkeypatch, stanza_ready=True, spec=SPEC)
    instance = _managed_node(FakePsql(), FakePatroni(primary=True), SPEC)

    instance._fill_backup(_archiving_config(None))

    assert instance.backup == pg.BACKUP_UNSETTLED


def test_backup_is_unsettled_until_archive_timeout_is_set(monkeypatch):
    # A cluster archiving before archive_timeout was managed has 1800s
    FakeRepository(monkeypatch, stanza_ready=True, spec=SPEC)
    instance = _managed_node(FakePsql(), FakePatroni(primary=True), SPEC)

    instance._fill_backup(_archiving_config(SPEC, archive_timeout="1800s"))

    assert instance.backup == pg.BACKUP_UNSETTLED


FAILED = {"source": ["s", None], "attempts": 3, "phase": "failed", "error": "boom"}


def test_failed_bootstrap_is_reported_while_patroni_is_down(monkeypatch):
    psql = FakePsql()
    instance = _restored_node(
        psql, FakePatroni(down=True), monkeypatch, restore_state=FAILED
    )

    instance.restore_from_dp()

    # PostgreSQL isn't asked, it isn't there
    assert instance.restore_state == {"phase": "failed", "error": "boom"}
    assert instance.found_roles is None
    assert psql.executed == []


def test_failed_bootstrap_is_reported_by_the_update_too(monkeypatch):
    # The agent reports the target it applied to a node that hasn't
    # converged, not what it reads back
    FakeRepository(monkeypatch, restore_state=FAILED)
    psql = FakePsql()
    instance = _managed_node(psql, FakePatroni(down=True), None)
    instance.adopt_roles = True

    instance.dump_to_dp()

    assert instance.restore_state == {"phase": "failed", "error": "boom"}
    assert psql.executed == []


def test_promoted_primary_drops_the_bootstrap_state(monkeypatch):
    repository = FakeRepository(monkeypatch, restore_state=FAILED)
    instance = _managed_node(FakePsql(), FakePatroni(primary=True), None)

    instance.restore_from_dp()

    assert instance.restore_state is None
    assert repository.restore_state is None


def test_replica_of_another_bootstrap_drops_its_failed_one(monkeypatch):
    # Another node bootstrapped the cluster after this one failed to
    repository = FakeRepository(monkeypatch, restore_state=FAILED)
    instance = _managed_node(
        FakePsql(in_recovery=True), FakePatroni(primary=False, leader="node-b"), None
    )

    instance.restore_from_dp()

    assert instance.restore_state is None
    assert repository.restore_state is None


def test_stanza_error_is_reported_by_the_primary(monkeypatch):
    repository = FakeRepository(monkeypatch, reachable=False, spec=SPEC)
    instance = _managed_node(FakePsql(), FakePatroni(primary=True), SPEC)

    instance.dump_to_dp()

    assert instance.backup_state == {"error": "stanza-create failed", "timeline": 3}

    repository.reachable = True
    instance.dump_to_dp()

    assert instance.backup_state == {"error": None, "timeline": 3}


def test_replica_reports_no_backup_state(monkeypatch):
    repository = FakeRepository(monkeypatch, spec=SPEC)
    repository.error = "stale"
    instance = _managed_node(FakePsql(), FakePatroni(primary=False), SPEC)

    instance.dump_to_dp()

    assert instance.backup_state is None


def test_no_backup_state_without_backups(monkeypatch):
    FakeRepository(monkeypatch)
    instance = _managed_node(FakePsql(), FakePatroni(primary=True), None)

    instance.dump_to_dp()

    assert instance.backup_state is None
