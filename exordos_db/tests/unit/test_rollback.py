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

import pytest

from exordos_db.common import pgbackrest
from exordos_db.common import rollback
from exordos_db.common.rollback import Action
from exordos_db.common.rollback import Phase

SPEC = {
    "id": "0d3c1b2a-0000-4000-8000-000000000001",
    "target_time": "2026-09-14 10:27:41.638125+00",
    "stanza": "38fc8bbb-0826-4287-9651-9745df402ded",
}


def _member(name, role, state, timeline=None):
    return {"name": name, "role": role, "state": state, "timeline": timeline}


LEADER = _member("a", "leader", "running")
REPLICA = _member("b", "replica", "streaming")
STOPPED_REPLICA = _member("b", "replica", "stopped")


def _state(phase, rollback_id=SPEC["id"]):
    return {"id": rollback_id, "phase": phase.value}


def _decide(state, paused, members, me):
    return rollback.decide(SPEC, state, paused, members, me)


class TestLeader:
    def test_pauses_first(self):
        assert _decide(None, False, [LEADER, REPLICA], "a") is Action.PAUSE

    def test_waits_for_replicas_to_stop(self):
        state = _state(Phase.PAUSED)
        assert _decide(state, True, [LEADER, REPLICA], "a") is Action.WAIT

    def test_restores_once_replicas_stopped(self):
        state = _state(Phase.PAUSED)
        members = [LEADER, STOPPED_REPLICA]
        assert _decide(state, True, members, "a") is Action.START_JOB

    def test_single_node(self):
        assert _decide(_state(Phase.PAUSED), True, [LEADER], "a") is Action.START_JOB

    def test_waits_for_job(self):
        state = _state(Phase.RESTORING)
        stopped_leader = _member("a", "leader", "stopped")
        members = [stopped_leader, STOPPED_REPLICA]
        assert _decide(state, True, members, "a") is Action.WAIT

    def test_resumes_once_it_holds_the_lock(self):
        state = _state(Phase.RESTORED)
        members = [LEADER, STOPPED_REPLICA]
        assert _decide(state, True, members, "a") is Action.RESUME

    @pytest.mark.parametrize(
        "me",
        [
            # Patroni removed the lock while PostgreSQL was stopped
            _member("a", "replica", "running"),
            _member("a", "leader", "stopped"),
        ],
    )
    def test_doesnt_resume_without_the_lock(self, me):
        # Resuming would start an election a stale replica wins
        state = _state(Phase.RESTORED)
        assert _decide(state, True, [me, STOPPED_REPLICA], "a") is Action.WAIT

    def test_restarts_the_promoted_server_under_patroni(self):
        # It still runs in the unit of the job that promoted it
        state = _state(Phase.RESUMED)
        assert _decide(state, False, [LEADER, REPLICA], "a") is Action.RESTART_POSTGRES

    def test_applied_once_restarted(self):
        state = {**_state(Phase.RESUMED), "restarted": True}
        assert _decide(state, False, [LEADER, REPLICA], "a") is Action.MARK_APPLIED

    def test_not_applied_while_starting(self):
        state = _state(Phase.RESUMED)
        starting = _member("a", "leader", "starting")
        assert _decide(state, False, [starting, REPLICA], "a") is Action.WAIT

    def test_failed_job_stops_the_rollback(self):
        state = _state(Phase.FAILED)
        assert _decide(state, True, [LEADER, REPLICA], "a") is Action.FAILED


class TestReplica:
    def test_waits_for_pause(self):
        assert _decide(None, False, [LEADER, REPLICA], "b") is Action.WAIT

    def test_stops_postgres_when_paused(self):
        assert _decide(None, True, [LEADER, REPLICA], "b") is Action.STOP_POSTGRES

    def test_stays_stopped_while_paused(self):
        state = _state(Phase.STOPPED)
        members = [LEADER, STOPPED_REPLICA]
        assert _decide(state, True, members, "b") is Action.WAIT

    @pytest.mark.parametrize(
        "replica, applied_id, expected",
        [
            (_member("b", "replica", "streaming", 3), SPEC["id"], Action.MARK_APPLIED),
            # A failed rewind leaves the replica running on its old timeline
            (_member("b", "replica", "running", 2), SPEC["id"], Action.WAIT),
            (_member("b", "replica", "streaming", 2), SPEC["id"], Action.WAIT),
            (_member("b", "replica", "starting", None), SPEC["id"], Action.WAIT),
            # The leader hasn't recorded the rollback for the cluster yet
            (_member("b", "replica", "streaming", 3), None, Action.WAIT),
        ],
    )
    def test_applied_once_replicating_the_new_timeline(
        self, replica, applied_id, expected
    ):
        members = [_member("a", "leader", "running", 3), replica]
        action = rollback.decide(
            SPEC, _state(Phase.STOPPED), False, members, "b", applied_id=applied_id
        )
        assert action is expected


OWNED_BY_A = {"id": SPEC["id"], "node": "a"}
OWNED_BY_B = {"id": SPEC["id"], "node": "b"}


@pytest.mark.parametrize(
    "phase, me",
    [
        (Phase.PAUSED, LEADER),
        (Phase.RESTORING, _member("a", "leader", "stopped")),
        (Phase.RESTORED, _member("a", "replica", "running")),
    ],
)
def test_lost_pause_is_restored(phase, me):
    # The pause didn't reach the DCS, or somebody lifted it
    action = rollback.decide(
        SPEC, _state(phase), False, [me, STOPPED_REPLICA], "a", owner=OWNED_BY_A
    )
    assert action is Action.REPAUSE


def test_rolled_back_leader_resumes_even_if_the_pause_was_lifted():
    members = [LEADER, STOPPED_REPLICA]
    action = rollback.decide(SPEC, _state(Phase.RESTORED), False, members, "a")
    assert action is Action.RESUME


def test_node_that_lost_the_ownership_yields():
    # A failover after pausing, or another node's later claim
    members = [_member("a", "replica", "running"), _member("b", "leader", "running")]
    action = rollback.decide(
        SPEC, _state(Phase.PAUSED), True, members, "a", owner=OWNED_BY_B
    )
    assert action is Action.STOP_POSTGRES


def test_job_starts_only_for_the_owner():
    members = [LEADER, STOPPED_REPLICA]
    action = rollback.decide(
        SPEC, _state(Phase.PAUSED), True, members, "a", owner=OWNED_BY_B
    )
    assert action is not Action.START_JOB


ALL_STOPPED = [_member("a", "replica", "stopped"), _member("b", "replica", "stopped")]
GONE = {"id": SPEC["id"], "node": "c"}


@pytest.mark.parametrize("state", [None, _state(Phase.STOPPED)])
def test_lowest_node_takes_over_when_the_owner_is_gone(state):
    # The node leading the rollback was removed or reinstalled
    assert (
        rollback.decide(SPEC, state, True, ALL_STOPPED, "a", owner=GONE) is Action.PAUSE
    )
    assert (
        rollback.decide(SPEC, state, True, ALL_STOPPED, "b", owner=GONE)
        is not Action.PAUSE
    )


def test_no_take_over_while_the_owner_is_there():
    action = rollback.decide(
        SPEC, _state(Phase.STOPPED), True, ALL_STOPPED, "a", owner=OWNED_BY_B
    )
    assert action is Action.WAIT


def test_no_take_over_while_a_leader_is_there():
    members = [_member("a", "replica", "stopped"), _member("b", "leader", "running")]
    action = rollback.decide(
        SPEC, _state(Phase.STOPPED), True, members, "a", owner=GONE
    )
    assert action is Action.WAIT


@pytest.mark.parametrize("me", ["a", "b"])
def test_node_that_missed_the_rollback_marks_it_applied(me):
    # E.g. added after the rollback: repeating it on a new leader would lose
    # everything written since
    members = [LEADER, REPLICA]
    action = rollback.decide(SPEC, None, False, members, me, applied_id=SPEC["id"])
    assert action is Action.MARK_APPLIED


def test_older_applied_rollback_doesnt_count():
    action = rollback.decide(SPEC, None, False, [LEADER, REPLICA], "a", applied_id="0")
    assert action is Action.PAUSE


@pytest.mark.parametrize(
    "state",
    [
        None,  # restarted between pausing and saving the state
        _state(Phase.FAILED, rollback_id="older"),
        _state(Phase.RESTORING, rollback_id="older"),
    ],
)
def test_leader_starts_over_on_a_paused_cluster(state):
    assert _decide(state, True, [LEADER, STOPPED_REPLICA], "a") is Action.PAUSE


@pytest.mark.parametrize("phase", [Phase.FAILED, Phase.RESTORING, Phase.RESTORED])
def test_leader_of_a_failed_rollback_starts_the_next_one(phase):
    # Its server was stopped, so the paused Patroni removed its lock and
    # lists it as a stopped replica. Without remembering it led, every node
    # would wait for a leader and a higher revision would never proceed.
    state = _state(phase, rollback_id="older")
    members = [_member("a", "replica", "stopped"), STOPPED_REPLICA]
    assert _decide(state, True, members, "a") is Action.PAUSE


def test_replica_of_a_failed_rollback_stays_a_replica():
    state = _state(Phase.STOPPED, rollback_id="older")
    members = [_member("a", "replica", "stopped"), STOPPED_REPLICA]
    assert _decide(state, True, members, "b") is Action.STOP_POSTGRES


def test_superseded_job_is_waited_for():
    state = _state(Phase.PAUSED)
    members = [LEADER, STOPPED_REPLICA]
    action = rollback.decide(SPEC, state, True, members, "a", job_active=True)
    assert action is Action.WAIT


def test_newer_rollback_replaces_stale_state():
    stale = _state(Phase.FAILED, rollback_id="older")
    assert _decide(stale, False, [LEADER, REPLICA], "a") is Action.PAUSE


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rollback, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(rollback, "MARKER_FILE", str(tmp_path / "marker.json"))
    monkeypatch.setattr(rollback, "APPLIED_AT_FILE", str(tmp_path / "applied_at"))
    assert rollback.applied_at() is None

    rollback.save_state(SPEC, Phase.FAILED, error="boom")
    assert rollback.load_state() == {
        "id": SPEC["id"],
        "phase": "failed",
        "spec": SPEC,
        "error": "boom",
    }
    assert rollback.in_progress(SPEC)
    assert not rollback.in_progress({**SPEC, "id": "other"})

    rollback.mark_applied(SPEC["id"])
    assert rollback.applied_id() == SPEC["id"]
    assert rollback.load_state() is None
    assert not rollback.in_progress(SPEC)
    # The spec is rendered again for every target, so nothing of it, and none
    # of the repository credentials in it, is kept on the node
    assert set(rollback.load_marker()) == {"id", "applied_at"}

    # The backup timer runs as postgres: the time must be readable by it
    # without the marker, which is the agent's own state
    assert rollback.applied_at() == rollback.load_marker()["applied_at"]
    assert (tmp_path / "marker.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "applied_at").stat().st_mode & 0o777 == 0o644


def test_restore_args():
    assert pgbackrest.restore_args(SPEC, "20260914-151105F") == [
        "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf",
        "--set=20260914-151105F",
        "--type=time",
        "--target=2026-09-14 10:27:41.638125+00",
        "--target-action=promote",
        "restore",
    ]
