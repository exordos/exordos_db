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

from exordos_db.paas.services import builder
from exordos_db.user_api.dm import backups
from exordos_db.user_api.dm import models

INSTANCE = uuid.UUID("38fc8bbb-0826-4287-9651-9745df402ded")
OTHER = uuid.UUID("7f1a7112-f649-4273-a7a6-8e9ac2c10dd7")


def _source(**kwargs):
    view = {
        "kind": "s3",
        "endpoint": "http://10.20.0.26:9000",
        "bucket": "dbaas-backups",
        "access_key": "backup",
        "secret_key": "secret",
        "stanza": str(INSTANCE),
        "target": {"kind": "time", "time": "2026-09-14T10:27:41Z"},
        **kwargs,
    }
    return backups.RESTORE_SOURCE_TYPE.from_simple_type(view)


def _backup(**kwargs):
    view = {
        "kind": "s3",
        "endpoint": "http://10.20.0.26:9000/",
        "bucket": "dbaas-backups",
        "access_key": "writer",
        "secret_key": "writer-secret",
        **kwargs,
    }
    return backups.BACKUP_TYPE.from_simple_type(view)


BACKUP = _backup()


def _rollback(old, new, applied=None, backup=BACKUP):
    return models.rollback_for_update(INSTANCE, old, new, applied, backup)


def _rendered(rollback_revision, source):
    """The spec the nodes get, as the builder renders it."""
    instance = types.SimpleNamespace(
        uuid=INSTANCE,
        rollback_revision=rollback_revision,
        restore_from=source,
    )
    return builder.PGInstanceBuilder._get_rollback(None, instance)


def test_first_source_on_an_existing_instance_rolls_back():
    assert _rollback(None, _source()) == 0


def test_the_spec_is_rendered_from_the_source():
    spec = _rendered(0, _source())

    assert spec["id"] == "0"
    assert spec["stanza"] == str(INSTANCE)
    assert spec["target_time"] == "2026-09-14 10:27:41.000000+00"
    assert spec["options"]["repo1-s3-bucket"] == "dbaas-backups"


def test_rotated_credentials_reach_a_rollback_in_progress():
    # Nothing of the spec is kept, so the nodes get the current credentials
    # and go on converging to the same rollback
    spec = _rendered(0, _source(access_key="rotated", secret_key="rotated-secret"))

    assert spec["id"] == "0"
    assert spec["options"]["repo1-s3-key"] == "rotated"


def test_no_spec_without_a_rollback_or_a_source():
    assert _rendered(None, _source()) is None
    # Cleared once the rollback was over: the data stays as it is
    assert _rendered(1, None) is None


def test_higher_revision_rolls_back_again():
    assert _rollback(_source(), _source(revision=1), applied=0) == 1


def test_same_target_again_with_higher_revision():
    old = _source(revision=1)
    assert _rollback(old, _source(revision=2), applied=1) == 2


def test_rotated_credentials_do_nothing():
    assert _rollback(_source(), _source(secret_key="rotated")) is None


def test_clearing_the_source_leaves_the_data():
    assert _rollback(_source(), None) is None
    models.check_restore_from_cleared(
        _source(), None, roles_imported=True, restore_status=None
    )


def test_source_of_a_restore_or_a_rollback_in_progress_cant_be_cleared():
    # The rows are empty until the roles are matched: without the source they
    # would be applied, and the agent would drop the restored databases. The
    # rollback the nodes converge to is rendered from the source too.
    with pytest.raises(models.RestoreSourceError):
        models.check_restore_from_cleared(
            _source(), None, roles_imported=False, restore_status=None
        )


def test_source_cant_be_cleared_while_a_replica_is_still_rolling_back():
    # The roles are matched as soon as the leader reports them, and the
    # instance is ACTIVE by then; a replica rewinding to the new timeline
    # keeps reporting its phase and still needs the spec to mark it applied
    with pytest.raises(models.RestoreSourceError):
        models.check_restore_from_cleared(
            _source(),
            None,
            roles_imported=True,
            restore_status={"revision": 1, "phase": "stopped", "error": None},
        )


@pytest.mark.parametrize(
    "change",
    [
        {"target": {"kind": "time", "time": "2026-09-14T09:00:00Z"}},
        {"target": {"kind": "latest"}},
    ],
)
def test_changed_target_without_revision_is_rejected(change):
    with pytest.raises(models.RestoreSourceError):
        _rollback(_source(), _source(**change))


@pytest.mark.parametrize("old", [None, "source"])
def test_rollback_to_the_latest_is_rejected(old):
    # The end of the archive is the state the instance already has, and by
    # then the cluster would already be paused and stopped
    old = _source() if old else None
    with pytest.raises(models.RestoreSourceError):
        _rollback(old, _source(target={"kind": "latest"}, revision=1))


def test_nodes_cant_be_removed_during_a_rollback():
    # The removed node may be the one leading the rollback
    with pytest.raises(models.InstanceUpdateError):
        models.check_nodes_change(3, 2, roles_managed=False)
    models.check_nodes_change(2, 3, roles_managed=False)
    models.check_nodes_change(3, 2, roles_managed=True)


def test_revision_below_an_applied_rollback_is_rejected():
    # The source was cleared after the rollback with revision 3, and
    # rollback_revision is the high-water mark that outlives it
    with pytest.raises(models.RestoreSourceError):
        _rollback(None, _source(revision=2), applied=3)


@pytest.mark.parametrize(
    "backup",
    [
        None,
        _backup(bucket="other-bucket"),
        _backup(path="/other"),
        _backup(endpoint="http://10.20.0.27:9000"),
    ],
)
def test_rollback_from_where_backups_dont_go_is_rejected(backup):
    # The WAL written since the last archived one is only in the repository
    # backups go to: recovering from another one ends before the target,
    # after the restore has replaced the data
    with pytest.raises(models.RestoreSourceError):
        _rollback(None, _source(), backup=backup)


def test_repository_credentials_may_differ():
    # E.g. a read-only key to restore with, a trailing slash in the endpoint
    assert _rollback(None, _source()) == 0


def test_another_instances_backups_are_rejected():
    with pytest.raises(models.RestoreSourceError):
        _rollback(None, _source(stanza=str(OTHER)))
