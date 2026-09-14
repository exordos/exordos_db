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

from exordos_db.agent.universal.drivers import pg

UNMANAGED_ROLES = {
    "uuid": str(uuid.uuid4()),
    "name": "restored",
    "nodes_number": 2,
    "sync_replica_number": 0,
    "users": None,
    "databases": None,
    "backup": None,
}


def test_unmanaged_roles_stay_unmanaged():
    # A restored cluster's roles aren't imported yet. Turning None into an
    # empty dict made the agent drop every database of the restored cluster.
    resource = ua_models.Resource.from_value(UNMANAGED_ROLES, "pg_instance_node")

    instance = pg.PGInstance.from_ua_resource(resource)

    assert instance.users is None
    assert instance.databases is None


def test_found_roles_change_the_full_hash_only():
    # The agent reports a resource read from the data plane only while its
    # target hash matches; the control plane learns about the data plane
    # from the full hash. The found roles have to travel that way.
    resource = ua_models.Resource.from_value(UNMANAGED_ROLES, "pg_instance_node")
    empty = pg.PGInstance.from_ua_resource(resource)
    found = pg.PGInstance.from_ua_resource(resource)
    found.found_roles = {"users": {"app": {"pw_hash": "x"}}, "databases": {}}
    found.roles_unmanaged = True

    empty_resource = empty.to_ua_resource("pg_instance_node")
    found_resource = found.to_ua_resource("pg_instance_node")

    assert found_resource.hash == empty_resource.hash
    assert found_resource.full_hash != empty_resource.full_hash
    assert "roles_unmanaged" not in found_resource.value
    assert "roles_unmanaged" in found.get_meta_fields()


def test_managed_roles():
    value = {
        **UNMANAGED_ROLES,
        "users": {"app": {"pw_hash": "SCRAM-SHA-256$..."}},
        "databases": {},
    }
    resource = ua_models.Resource.from_value(value, "pg_instance_node")

    instance = pg.PGInstance.from_ua_resource(resource)

    assert instance.users == {"app": {"pw_hash": "SCRAM-SHA-256$..."}}
    assert instance.databases == {}


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
    def __init__(self, primary=True):
        self.primary = primary
        self.patches = []

    def is_primary(self, ttl_hash=None):
        return self.primary

    def config_get(self):
        return {"synchronous_node_count": 0, "postgresql": {"parameters": {}}}

    def config_patch(self, config):
        self.patches.append(config)
        return config


class FakeClients:
    def __init__(self, psql, pclient):
        self.psql = psql
        self.pclient = pclient


def _restored_node(psql, pclient, monkeypatch):
    monkeypatch.setattr(pg.pgbackrest, "load_spec", lambda: None)
    resource = ua_models.Resource.from_value(UNMANAGED_ROLES, "pg_instance_node")
    instance = pg.PGInstance.from_ua_resource(resource)
    instance.roles_unmanaged = True
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
    assert instance.users is None
    assert instance.databases is None


def test_roles_of_a_cluster_in_recovery_are_not_found(monkeypatch):
    # PostgreSQL serves reads while the restore replays WAL: the roles are
    # those of the replayed moment, the ones created later are missing and
    # would be dropped once imported
    psql = FakePsql(in_recovery=True, **RESTORED)
    instance = _restored_node(psql, FakePatroni(primary=True), monkeypatch)

    instance.restore_from_dp()

    assert instance.found_roles is None
    assert instance.users is None
    assert instance.databases is None


def test_roles_of_a_replica_are_not_found(monkeypatch):
    psql = FakePsql(in_recovery=True, **RESTORED)
    instance = _restored_node(psql, FakePatroni(primary=False), monkeypatch)

    instance.restore_from_dp()

    assert instance.found_roles is None
