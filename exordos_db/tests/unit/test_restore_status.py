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
import uuid

from gcl_sdk.infra import constants as sdk_c
import pytest

from exordos_db.infra.services import builder as infra_builder
from exordos_db.paas.dm import models as paas_models
from exordos_db.paas.services import builder as paas_builder
from exordos_db.user_api.api import controllers
from exordos_db.user_api.dm import backups
from exordos_db.user_api.dm import models

SOURCE = backups.RESTORE_SOURCE_TYPE.from_simple_type(
    {
        "kind": "s3",
        "endpoint": "http://10.20.0.30:9000",
        "bucket": "backups",
        "access_key": "key",
        "secret_key": "secret",
        "stanza": str(uuid.uuid4()),
    }
)


def _node(restore_state):
    return paas_models.PGInstanceNode(
        uuid=uuid.uuid4(), name="restored", restore_state=restore_state
    )


def test_no_reports_no_status():
    assert paas_builder.restore_status([None, _node(None)]) is None


def test_restore_in_progress_wins_over_a_failed_one():
    # Another node takes the bootstrap over after one fails to
    failed = _node({"phase": "failed", "error": "unable to connect"})
    restoring = _node({"phase": "restoring", "error": None})

    assert paas_builder.restore_status([failed, restoring]) == {
        "phase": "restoring",
        "error": None,
    }
    assert paas_builder.restore_status([failed]) == {
        "phase": "failed",
        "error": "unable to connect",
    }


def _instance(**fields):
    return models.PGInstance(
        name="restored",
        project_id=uuid.uuid4(),
        cpu=1,
        ram=1024,
        disk_size=8,
        nodes_number=1,
        version=models.PGVersion(name="18", image="pg.raw"),
        **fields,
    )


@pytest.mark.parametrize(
    ("fields", "nodeset", "expected"),
    [
        ({}, "ACTIVE", "ACTIVE"),
        ({}, "unknown", "IN_PROGRESS"),
        # The users and databases of a restored instance aren't there yet
        ({"restore_from": SOURCE}, "ACTIVE", "IN_PROGRESS"),
        ({"restore_from": SOURCE, "roles_imported": True}, "ACTIVE", "ACTIVE"),
        (
            {
                "restore_from": SOURCE,
                "restore_status": {"phase": "failed", "error": "no backup"},
            },
            "IN_PROGRESS",
            "ERROR",
        ),
        (
            {
                "restore_from": SOURCE,
                "restore_status": {"phase": "restoring", "error": None},
            },
            "IN_PROGRESS",
            "IN_PROGRESS",
        ),
    ],
)
def test_instance_status(fields, nodeset, expected):
    status = infra_builder.instance_status(_instance(**fields), nodeset)

    assert status == sdk_c.InstanceStatus(expected).value


def test_import_of_roles_keeps_the_rows_there_are(monkeypatch):
    # An interrupted pass left `app` behind, or it was created meanwhile
    inserted = []
    monkeypatch.setattr(models.PGUser, "insert", lambda self: inserted.append(self))
    monkeypatch.setattr(models.PGDatabase, "insert", lambda self: inserted.append(self))
    instance = _instance(restore_from=SOURCE)
    app = models.PGUser(
        name="app", password_hash="h", instance=instance, project_id=uuid.uuid4()
    )
    monkeypatch.setattr(instance, "get_users", lambda: [app])
    monkeypatch.setattr(instance, "get_databases", list)
    monkeypatch.setattr(instance, "update", lambda force: None)
    node = _node(None)
    node.found_roles = {
        "users": {"app": {"pw_hash": "h"}, "report": {"pw_hash": "r"}},
        "databases": {"app": {"owner": "app"}},
    }
    collection = types.SimpleNamespace(actuals=lambda: [node])

    paas_builder.PGInstanceBuilder._import_roles(None, instance, collection)

    assert [(type(r).__name__, r.name) for r in inserted] == [
        ("PGUser", "report"),
        ("PGDatabase", "app"),
    ]
    assert inserted[1].owner is app
    assert instance.roles_imported


@pytest.mark.parametrize(
    ("call", "kwargs"),
    [
        ("create", {"parent_resource": None, "name": "app"}),
        ("create", {"parent_resource": None, "name": "app", "password": None}),
        ("update", {"parent_resource": None, "uuid": None, "password": None}),
    ],
)
def test_api_asks_for_a_password(call, kwargs):
    # Only users imported from a restored cluster have none
    with pytest.raises(controllers.PasswordRequiredError):
        getattr(controllers.PGUserController, call)(None, **kwargs)
