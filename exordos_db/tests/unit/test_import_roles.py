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

import types

import pytest

from exordos_db.paas.services import builder

REVISION = 1
# What a node that has applied it reports back, the rendered spec
ROLLBACK = {"id": "1", "stanza": "s", "options": {}, "target_time": "t"}


class Journal(list):
    """Operations on the rows in the order they happened."""


class FakeRow:
    def __init__(self, journal, kind, name, owner=None, **kwargs):
        self._journal = journal
        self.kind = kind
        self.name = name
        self.owner = owner

    def insert(self):
        owner = f" owner={self.owner.name}" if self.owner is not None else ""
        self._journal.append(f"create {self.kind} {self.name}{owner}")

    def update(self):
        self._journal.append(f"owner {self.kind} {self.name}={self.owner.name}")

    def delete(self):
        self._journal.append(f"delete {self.kind} {self.name}")


@pytest.fixture
def journal(monkeypatch):
    journal = Journal()
    fake_models = types.SimpleNamespace(
        PGUser=lambda **kw: FakeRow(journal, "user", **kw),
        PGDatabase=lambda **kw: FakeRow(journal, "database", **kw),
    )
    monkeypatch.setattr(builder, "user_models", fake_models)
    return journal


def _instance(journal, users, databases, rollback_revision=REVISION):
    rows = {n: FakeRow(journal, "user", n) for n in users}
    dbs = [FakeRow(journal, "database", n, owner=rows[o]) for n, o in databases.items()]

    def update(force=False):
        journal.append(f"instance roles_imported={instance.roles_imported}")

    instance = types.SimpleNamespace(
        uuid="i",
        project_id="p",
        rollback_revision=rollback_revision,
        roles_imported=False,
        get_users=lambda: list(rows.values()),
        get_databases=lambda: dbs,
        update=update,
    )
    return instance


def _actual(rollback, users, databases):
    return types.SimpleNamespace(
        rollback=rollback,
        found_roles={
            "users": {n: {"pw_hash": f"hash-{n}"} for n in users},
            "databases": {n: {"owner": o} for n, o in databases.items()},
        },
    )


def _import(instance, *actuals):
    collection = types.SimpleNamespace(actuals=lambda: actuals)
    builder.PGInstanceBuilder._import_roles(None, instance, collection)


def test_node_that_hasnt_applied_the_rollback_is_ignored(journal):
    # Its roles are the ones after the target time. Taking them would keep
    # the rows of roles created later, and the agent would then drop the ones
    # the rollback brought back.
    instance = _instance(journal, ["app", "late"], {"appdb": "app"})
    not_rolled_back = _actual(None, ["app", "late"], {"appdb": "app"})

    _import(instance, not_rolled_back)

    assert journal == []
    assert instance.roles_imported is False


def test_rows_follow_the_node_that_applied_the_rollback(journal):
    instance = _instance(
        journal,
        ["app", "late", "late_owner"],
        {"appdb": "late_owner", "latedb": "late"},
    )
    not_rolled_back = _actual(None, ["app", "late"], {"appdb": "app"})
    rolled_back = _actual(ROLLBACK, ["app", "old"], {"appdb": "app", "olddb": "old"})

    _import(instance, None, not_rolled_back, rolled_back)

    assert journal == [
        # Databases go first: they refer to their owners
        "delete database latedb",
        "create user old",
        "create database olddb owner=old",
        # The owner changes before the user it had is deleted
        "owner database appdb=app",
        "delete user late",
        "delete user late_owner",
        "instance roles_imported=True",
    ]


def test_no_report_yet(journal):
    instance = _instance(journal, ["app"], {})
    reporting_nothing = types.SimpleNamespace(rollback=ROLLBACK, found_roles=None)

    _import(instance, None, reporting_nothing)

    assert journal == []


def test_the_rollback_is_matched_by_its_id(journal):
    # The spec is rendered for every target, so a node that applied the
    # rollback may report it with credentials rotated since
    instance = _instance(journal, ["app"], {})
    rotated = {**ROLLBACK, "options": {"repo1-s3-key": "rotated"}}
    rolled_back = _actual(rotated, ["app"], {})

    _import(instance, rolled_back)

    assert journal == ["instance roles_imported=True"]
