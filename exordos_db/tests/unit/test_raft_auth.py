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

import importlib.util
from pathlib import Path
import subprocess
import types
import uuid

import pytest
from restalchemy.api import constants as api_c
from restalchemy.api import field_permissions
from restalchemy.storage.sql import orm

from exordos_db.infra.dm import models as infra_models
from exordos_db.infra.services import builder
from exordos_db.user_api.api import controllers
from exordos_db.user_api.dm import models


def instance(password=None):
    return infra_models.PGInstance(
        name="test",
        cpu=1,
        ram=512,
        disk_size=8,
        nodes_number=1,
        version=models.PGVersion(name="v", image="image"),
        project_id=uuid.uuid4(),
        raft_password=password,
    )


def test_creation_generates_independent_credentials_and_loading_keeps_them(monkeypatch):
    monkeypatch.setattr(orm.SQLStorableMixin, "insert", lambda *a, **kw: None)
    first, second = instance(), instance()
    first.insert()
    second.insert()
    assert len(first.raft_password) == 64
    assert first.raft_password != second.raft_password
    restored = instance(first.raft_password)
    assert restored.raft_password == first.raft_password
    assert instance(None).raft_password is None
    assert "raft_password" not in first.to_ua_resource().value


@pytest.mark.parametrize("method", api_c.ALL_RA_METHODS)
def test_credentials_are_hidden_in_the_user_api(method):
    permissions = controllers.PGInstanceController.__resource__._fields_permissions
    request = types.SimpleNamespace(
        api_context=types.SimpleNamespace(get_active_method=lambda: method)
    )
    assert permissions.permission_of("raft_password", request) == (
        field_permissions.Permissions.HIDDEN
    )


def test_config_with_credentials_is_private():
    config = instance("a" * 64)._create_config(uuid.uuid4(), uuid.uuid4())
    assert config.mode == "0600"
    assert config.owner == config.group == "postgres"


def test_authentication_cutover_restarts_once_and_retries_failure(tmp_path):
    marker = tmp_path / "marker"
    log = tmp_path / "calls"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n[ ! -f "$FAIL" ]\n'
    )
    systemctl.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "CALLS": str(log),
        "FAIL": str(tmp_path / "fail"),
    }
    password = "a" * 64
    command = builder.patroni_on_change(password).command.replace(
        "/run/exordos-db-raft-auth.sha256", str(marker)
    )
    assert password not in command
    Path(env["FAIL"]).touch()
    assert subprocess.run(command, shell=True, env=env, check=False).returncode != 0
    assert not marker.exists()
    Path(env["FAIL"]).unlink()
    subprocess.run(command, shell=True, env=env, check=True)
    subprocess.run(command, shell=True, env=env, check=True)
    assert log.read_text().splitlines() == [
        "restart exordos-patroni",
        "restart exordos-patroni",
        "reload-or-restart exordos-patroni",
    ]
    assert marker.stat().st_mode & 0o777 == 0o600


def migration_module():
    path = Path(__file__).parents[3] / "migrations/0003-add-raft-password-d7e854.py"
    spec = importlib.util.spec_from_file_location("raft_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "count,content,expected",
    [
        (0, "", False),
        (2, "raft: {self_addr: n1, partner_addrs: [n1, n2]}", False),
        (1, "raft: {self_addr: n1, partner_addrs: [n1, n2]}", False),
        (1, "", False),
        (1, "[invalid", False),
        (1, "raft: {self_addr: n1, partner_addrs: [n1]}", True),
    ],
)
def test_migration_excludes_incomplete_and_scaling_clusters(count, content, expected):
    class Session:
        def execute(self, sql, values=None):
            if "kind = 'node_set'" in sql:
                self.rows = [
                    {"value": {"nodes": {str(index): {} for index in range(count)}}}
                ]
            else:
                self.rows = [{"value": {"body": {"content": content}}}]
            return self

        def fetchall(self):
            return self.rows

    assert migration_module().stable_singleton(Session(), uuid.uuid4()) is expected


def test_migration_assigns_secrets_only_to_singletons_and_retries_safely():
    module = migration_module()
    rows = [
        {"uuid": uuid.uuid4(), "nodes_number": nodes, "raft_password": None}
        for nodes in (1, 1, 3)
    ]

    class Session:
        def execute(self, sql, values=None):
            if sql.startswith("SELECT uuid"):
                assert "nodes_number = 1" in sql
                self.selected = [
                    row.copy()
                    for row in rows
                    if row["nodes_number"] == 1 and row["raft_password"] is None
                ]
            elif "kind = 'node_set'" in sql:
                self.selected = [{"value": {"nodes": {"node": {}}}}]
            elif "SELECT actual.value" in sql:
                self.selected = [
                    {
                        "value": {
                            "body": {
                                "content": "raft:\n  self_addr: node:5010\n"
                                "  partner_addrs: ['node:5010']\n"
                            }
                        }
                    }
                ]
            elif sql.startswith("UPDATE"):
                assert "updated_at = CURRENT_TIMESTAMP" in sql
                password, identifier = values
                for row in rows:
                    if row["uuid"] == identifier and row["raft_password"] is None:
                        row["raft_password"] = password
            return self

        def fetchall(self):
            return self.selected

    migration = module.MigrationStep()
    migration.upgrade(Session())
    passwords = [row["raft_password"] for row in rows]
    assert len(passwords[0]) == len(passwords[1]) == 64
    assert passwords[0] != passwords[1]
    assert passwords[2] is None
    migration.upgrade(Session())
    assert [row["raft_password"] for row in rows] == passwords
    with pytest.raises(RuntimeError, match="coordinated downgrade"):
        migration.downgrade(Session())
