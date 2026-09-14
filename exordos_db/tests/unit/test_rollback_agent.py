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

from exordos_db.agent.universal.drivers import pg
from exordos_db.cmd import pg_rollback
from exordos_db.common import rollback
from exordos_db.common.rollback import Phase

SPEC = {
    "id": "1",
    "stanza": "38fc8bbb-0826-4287-9651-9745df402ded",
    "options": {"repo1-s3-key-secret": "secret"},
    "target_time": "2026-09-14 10:27:41.638125+00",
}


class FakePatroni:
    member_name = "a"

    def __init__(self, config=None, members=None, fail_patch=False):
        self.config = config or {}
        self.members = members or [{"name": "a", "role": "leader", "state": "running"}]
        self.fail_patch = fail_patch
        self.patches = []

    def config_get(self):
        return self.config

    def cluster(self):
        return {"members": self.members}

    def config_patch(self, patch):
        if self.fail_patch:
            raise RuntimeError("the agent died here")
        self.patches.append(patch)

    def is_primary(self, ttl_hash=None):
        return True

    def restart(self):
        self.patches.append("restart")


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(rollback, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(rollback, "MARKER_FILE", str(tmp_path / "marker.json"))
    monkeypatch.setattr(rollback, "APPLIED_AT_FILE", str(tmp_path / "applied_at"))


def _instance(monkeypatch, patroni, active_jobs=set):
    monkeypatch.setattr(
        pg.PGInstance, "_rollback_jobs_active", staticmethod(active_jobs)
    )
    instance = pg.PGInstance(
        uuid=uuid.uuid4(),
        name="restored",
        nodes_number=1,
        sync_replica_number=0,
        rollback=SPEC,
    )
    instance.c = types.SimpleNamespace(pclient=patroni)
    return instance


def test_job_gone_without_a_result_fails_the_rollback(monkeypatch):
    # E.g. the leader rebooted during the restore
    rollback.save_state(SPEC, Phase.RESTORING, started=True)

    assert _instance(monkeypatch, FakePatroni())._reconcile_rollback() is False
    assert rollback.load_state()["phase"] == "failed"


def test_job_finishing_meanwhile_isnt_taken_for_a_dead_one(monkeypatch):
    rollback.save_state(SPEC, Phase.RESTORING, started=True)

    def finishes():
        rollback.save_state(SPEC, Phase.RESTORED)
        return set()

    patroni = FakePatroni(config={"pause": True})
    _instance(monkeypatch, patroni, active_jobs=finishes)._reconcile_rollback()

    assert rollback.load_state()["phase"] == "resumed"
    assert patroni.patches == [{"pause": False, rollback.DCS_KEY: "1"}]


def test_state_is_saved_before_pausing(monkeypatch):
    # A restart right after pausing must find the state, or the leader would
    # face a paused cluster it knows nothing about
    patroni = FakePatroni(fail_patch=True)

    with pytest.raises(RuntimeError):
        _instance(monkeypatch, patroni)._reconcile_rollback()

    assert rollback.load_state()["phase"] == "paused"


def test_pausing_claims_the_rollback(monkeypatch):
    patroni = FakePatroni()

    _instance(monkeypatch, patroni)._reconcile_rollback()

    assert patroni.patches == [
        {"pause": True, rollback.OWNER_KEY: {"id": "1", "node": "a"}}
    ]


def test_job_that_never_started_is_started_again(monkeypatch):
    # The agent restarted between saving the state and starting the job:
    # nothing was touched, so it isn't a failure
    rollback.save_state(SPEC, Phase.RESTORING)
    patroni = FakePatroni(
        config={"pause": True, rollback.OWNER_KEY: {"id": "1", "node": "a"}}
    )
    instance = _instance(monkeypatch, patroni)
    started = []
    monkeypatch.setattr(instance, "_start_rollback_job", started.append)

    instance._reconcile_rollback()

    assert started == ["1"]
    assert rollback.load_state()["phase"] == "restoring"


def test_job_of_another_rollback_isnt_taken_for_this_one(monkeypatch):
    # The unit of a finished rollback may stay loaded with the server it
    # promoted; it says nothing about the job of this rollback
    rollback.save_state(SPEC, Phase.RESTORING, started=True)
    jobs = {rollback.job_unit("0")}

    _instance(
        monkeypatch, FakePatroni(), active_jobs=lambda: jobs
    )._reconcile_rollback()

    assert rollback.load_state()["phase"] == "failed"


def test_job_that_didnt_start_is_tried_again(monkeypatch):
    # E.g. systemd-run failed: that isn't a job that died without a result
    rollback.save_state(SPEC, Phase.PAUSED)
    patroni = FakePatroni(
        config={"pause": True, rollback.OWNER_KEY: {"id": "1", "node": "a"}}
    )
    instance = _instance(monkeypatch, patroni)

    def fails(rollback_id):
        raise pg.subprocess.CalledProcessError(1, "systemd-run", stderr="exists")

    monkeypatch.setattr(instance, "_start_rollback_job", fails)
    instance._reconcile_rollback()

    assert rollback.load_state()["phase"] == "paused"


def test_resumed_leader_restarts_its_server_once(monkeypatch):
    rollback.save_state(SPEC, Phase.RESUMED)
    patroni = FakePatroni()

    _instance(monkeypatch, patroni)._reconcile_rollback()
    assert patroni.patches == ["restart"]
    assert rollback.load_state()["restarted"] is True

    assert _instance(monkeypatch, patroni)._reconcile_rollback() is True
    assert patroni.patches == ["restart"]
    assert rollback.applied_id() == "1"


def test_lost_pause_is_restored(monkeypatch):
    rollback.save_state(SPEC, Phase.PAUSED)
    patroni = FakePatroni(config={rollback.OWNER_KEY: {"id": "1", "node": "a"}})

    _instance(monkeypatch, patroni)._reconcile_rollback()

    assert patroni.patches == [{"pause": True}]
    assert rollback.load_state()["phase"] == "paused"


def test_repository_errors_dont_hold_up_the_rest(monkeypatch):
    # Wrong keys, an unreachable storage or a changed encryption key
    backup = {"stanza": "s", "options": {}, "schedule": {}}
    applied = []

    def failing_run(*args, **kwargs):
        raise pg.pgbackrest.PgBackRestError("stanza-create failed")

    monkeypatch.setattr(pg.pgbackrest, "run", failing_run)
    monkeypatch.setattr(pg.pgbackrest, "stanza_ready", lambda spec: False)
    monkeypatch.setattr(pg.pgbackrest, "apply_spec", lambda spec: False)
    monkeypatch.setattr(pg.pgbackrest, "remove_restore_config", lambda: False)
    monkeypatch.setattr(
        pg.PGInstance, "_reconcile_target_users", lambda s: applied.append("users")
    )
    monkeypatch.setattr(
        pg.PGInstance,
        "_reconcile_target_databases",
        lambda s: applied.append("databases"),
    )
    patroni = FakePatroni()
    instance = pg.PGInstance(
        uuid=uuid.uuid4(),
        name="with-backup",
        nodes_number=1,
        sync_replica_number=0,
        backup=backup,
        users={},
        databases={},
    )
    instance.c = types.SimpleNamespace(pclient=patroni)

    instance.dump_to_dp()

    assert applied == ["users", "databases"]
    assert patroni.patches[-1]["synchronous_node_count"] == 0


def test_node_that_missed_the_rollback_doesnt_repeat_it(monkeypatch):
    patroni = FakePatroni(config={rollback.DCS_KEY: "1"})

    assert _instance(monkeypatch, patroni)._reconcile_rollback() is True
    assert rollback.applied_id() == "1"
    assert patroni.patches == []


def test_rotated_credentials_dont_roll_the_data_back_again(monkeypatch):
    # The control plane renders the spec for every target, so the applied
    # rollback comes back with the current credentials in it
    rollback.mark_applied("1")
    patroni = FakePatroni()
    instance = _instance(monkeypatch, patroni)
    instance.rollback = {**SPEC, "options": {"repo1-s3-key-secret": "rotated"}}

    assert instance._reconcile_rollback() is True
    assert patroni.patches == []
    # What the node reports has to be the target itself, or its resource
    # would never match the target hash
    assert instance._applied_rollback(instance.rollback) == instance.rollback


def test_rotated_credentials_dont_restart_a_rollback_in_progress(monkeypatch):
    rollback.save_state(SPEC, Phase.PAUSED)
    rotated = {**SPEC, "options": {"repo1-s3-key-secret": "rotated"}}
    patroni = FakePatroni(
        config={"pause": True, rollback.OWNER_KEY: {"id": "1", "node": "a"}}
    )
    instance = _instance(monkeypatch, patroni)
    instance.rollback = rotated
    started = []
    monkeypatch.setattr(instance, "_start_rollback_job", started.append)

    instance._reconcile_rollback()

    # Carried on from the phase it was in, and the job reads the fresh spec
    assert started == ["1"]
    assert rollback.load_state()["spec"] == rotated


@pytest.mark.parametrize(
    "applied, spec",
    [
        # Nothing applied yet, or an older rollback: the agent applies the
        # target and reports that instead of what it reads
        (None, SPEC),
        ("0", SPEC),
        # The source was cleared once the rollback was over, so none is asked
        # for any more; the marker of the old one isn't the target either
        ("1", None),
    ],
)
def test_only_the_applied_target_is_reported_back(monkeypatch, applied, spec):
    if applied is not None:
        rollback.mark_applied(applied)
    instance = _instance(monkeypatch, FakePatroni())

    assert instance._applied_rollback(spec) is None


def test_superseded_job_doesnt_overwrite_the_newer_rollback(monkeypatch):
    rollback.save_state(SPEC, Phase.RESTORING, started=True)
    newer = {**SPEC, "id": "2"}

    def run(spec):
        rollback.save_state(newer, Phase.PAUSED)

    monkeypatch.setattr(pg_rollback, "run", run)

    assert pg_rollback.main() == 0
    assert rollback.load_state()["id"] == "2"
    assert rollback.load_state()["phase"] == "paused"


def test_job_doesnt_log_the_credentials(monkeypatch, caplog):
    rollback.save_state(SPEC, Phase.FAILED)

    assert pg_rollback.main() == 1
    assert "secret" not in caplog.text


class ReplicaPatroni(FakePatroni):
    def is_primary(self, ttl_hash=None):
        return False


def test_rollback_progress_is_reported(monkeypatch):
    rollback.save_state(SPEC, Phase.FAILED, error="x" * 5000)
    instance = _instance(monkeypatch, FakePatroni())

    instance.restore_from_dp()

    assert instance.restore_state == {
        "id": "1",
        "phase": "failed",
        "error": "x" * pg.pgbackrest.ERROR_MAX_LENGTH + "...",
    }
    assert instance.users is None
    assert instance.found_roles is None


def test_progress_is_on_the_updated_target(monkeypatch):
    # While the node differs from the target the agent reports what the
    # update returns, not what is read back from the node
    rollback.save_state(SPEC, Phase.FAILED, error="No backup")
    instance = _instance(monkeypatch, FakePatroni())

    instance.update_on_dp()
    resource = instance.to_ua_resource("pg_instance_node")

    assert resource.value["restore_state"] == {
        "id": "1",
        "phase": "failed",
        "error": "No backup",
    }


@pytest.fixture
def bootstrap_state(tmp_path, monkeypatch):
    path = tmp_path / "restore_state.json"
    monkeypatch.setattr(pg.pgbackrest, "RESTORE_STATE_FILE", str(path))
    pg.pgbackrest.save_restore_state(
        {"phase": "failed", "error": "No backup", "attempts": 1}
    )
    return path


BOOTSTRAPPING = [{"name": "a", "role": "replica", "state": "starting"}]


def test_bootstrap_progress_is_reported(monkeypatch, bootstrap_state):
    # PostgreSQL isn't up while the cluster is restored, and no node leads it
    instance = _instance(monkeypatch, ReplicaPatroni(members=BOOTSTRAPPING))
    instance.rollback = None

    instance.restore_from_dp()

    assert instance.restore_state == {
        "id": None,
        "phase": "failed",
        "error": "No backup",
    }
    assert instance.users is None


class DownPatroni(FakePatroni):
    def is_primary(self, ttl_hash=None):
        raise pg.requests.ConnectionError("Connection refused")

    def cluster(self):
        raise pg.requests.ConnectionError("Connection refused")


def test_failed_bootstrap_is_reported_while_patroni_restarts(
    monkeypatch, bootstrap_state
):
    # Patroni restarts over and over after a failed bootstrap. The agent
    # failed to create the resource on the refused connection, and the
    # failure never reached the control plane.
    instance = _instance(monkeypatch, DownPatroni())
    instance.rollback = None

    instance.dump_to_dp()

    assert instance.restore_state == {
        "id": None,
        "phase": "failed",
        "error": "No backup",
    }


def test_patroni_down_without_a_bootstrap_still_fails(monkeypatch):
    # Nothing is known to be reported then: the agent retries the update
    instance = _instance(monkeypatch, DownPatroni())
    instance.rollback = None

    with pytest.raises(pg.requests.ConnectionError):
        instance.dump_to_dp()


def test_bootstrap_state_goes_once_the_node_is_a_primary(monkeypatch, bootstrap_state):
    instance = _instance(monkeypatch, FakePatroni())

    assert instance._bootstrap_state() is None
    assert not bootstrap_state.exists()


def test_bootstrap_state_goes_on_a_replica_of_another_leader(
    monkeypatch, bootstrap_state
):
    # The bootstrap failed here and another node bootstrapped the cluster: a
    # stale failure would keep a healthy instance ERROR for good
    members = [
        {"name": "a", "role": "replica", "state": "streaming"},
        {"name": "b", "role": "leader", "state": "running"},
    ]
    instance = _instance(monkeypatch, ReplicaPatroni(members=members))

    assert instance._bootstrap_state() is None
    assert not bootstrap_state.exists()
