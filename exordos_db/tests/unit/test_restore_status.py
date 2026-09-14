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

import pytest

from exordos_db.infra.services import builder as infra_builder
from exordos_db.paas.services import builder
from exordos_db.user_api.dm import models

REVISION = 3


def _node(restore_state):
    return types.SimpleNamespace(restore_state=restore_state)


def _report(rollback_id, phase, error=None):
    return _node({"id": rollback_id, "phase": phase, "error": error})


def _status(rollback_revision, *actuals):
    instance = types.SimpleNamespace(rollback_revision=rollback_revision)
    return builder.restore_status(instance, actuals)


def test_nothing_in_progress():
    assert _status(REVISION, None, _node(None)) is None


def test_leader_phase_of_a_rollback():
    status = _status(REVISION, _report("3", "stopped"), _report("3", "restoring"), None)
    assert status == {"revision": 3, "phase": "restoring", "error": None}


def test_failure_on_any_node():
    status = _status(
        REVISION, _report("3", "restoring"), _report("3", "failed", "No backup")
    )
    assert status == {"revision": 3, "phase": "failed", "error": "No backup"}


def test_reports_of_a_replaced_rollback_dont_count():
    # A rollback with a higher revision replaces the failed one
    assert _status(REVISION, _report("2", "failed", "No backup")) is None


def test_restore_of_a_new_instance():
    status = _status(None, _report(None, "failed", "No backup"))
    assert status == {"revision": None, "phase": "failed", "error": "No backup"}


def _instance(**fields):
    return models.PGInstance(
        name="i",
        project_id=uuid.uuid4(),
        version=models.PGVersion(name="18", image="pg.raw"),
        cpu=1,
        ram=1024,
        disk_size=8,
        **fields,
    )


@pytest.mark.parametrize(
    "fields, nodeset, expected",
    [
        ({}, "ACTIVE", "ACTIVE"),
        ({"rollback_revision": REVISION}, "ACTIVE", "IN_PROGRESS"),
        (
            {
                "rollback_revision": REVISION,
                "restore_status": {
                    "revision": 3,
                    "phase": "failed",
                    "error": "No backup",
                },
            },
            "ACTIVE",
            "ERROR",
        ),
        (
            {
                "rollback_revision": REVISION,
                "restore_status": {"revision": 3, "phase": "paused", "error": None},
            },
            "ACTIVE",
            "IN_PROGRESS",
        ),
    ],
)
def test_instance_status(fields, nodeset, expected):
    assert infra_builder.instance_status(_instance(**fields), nodeset) == expected
