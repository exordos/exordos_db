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
"""Point-in-time rollback of a running cluster in place.

The control plane sends every node the same spec, the restore spec of the
instance's source plus the revision that asked for the rollback:

    {"id": "<revision>", "stanza": ..., "options": {...}, "target_time": ...}

A node converges to it like to any other desired state: the id of the rollback
it has applied is kept in a marker, and until that is the id of the spec the
agent takes the next step of the procedure, chosen from the current state by
`decide`. Only the id, so that a spec rendered again with rotated repository
credentials reaches a rollback in progress instead of restarting it.

Nodes coordinate through the Patroni DCS only. Stopping Patroni itself isn't
an option: the DCS is Raft inside the Patroni processes, and a cluster that
lost the quorum demotes its leader. So the cluster is paused instead:

1. the leader pauses the cluster;
2. every replica stops its PostgreSQL (a paused Patroni leaves it stopped);
3. once all replicas are stopped, the leader runs the rollback job: it stops
   PostgreSQL, restores the backup with --delta, lets PostgreSQL recover to
   the target and promote on its own, and leaves the promoted server running;
4. the paused Patroni takes the leader lock for the running primary (it
   removes the lock of a stopped server, and a node that isn't a primary
   never races while paused);
5. the leader resumes the cluster, and Patroni rewinds the replicas to the
   new timeline;
6. the leader has Patroni restart its server once, so the server runs under
   Patroni rather than in the unit of the job that promoted it.

The job runs as a transient systemd unit, since the recovery may take far
longer than an agent iteration.
"""

from __future__ import annotations

import datetime
import enum
import logging
import subprocess
import typing as tp

from exordos_db.common import constants as cc
from exordos_db.common import files
from exordos_db.common import pgbackrest

LOG = logging.getLogger(__name__)

# The id of the rollback the cluster has applied, in the Patroni dynamic
# config, so a node that missed the rollback doesn't repeat it
DCS_KEY = "exordos_rollback"
# {"id": ..., "node": ...}: the node that paused the cluster for a rollback
OWNER_KEY = "exordos_rollback_owner"

PG_CTL = "/usr/sbin/pg_ctl"

MARKER_FILE = f"{cc.WORK_DIR}/rollback.json"
APPLIED_AT_FILE = f"{cc.WORK_DIR}/rollback_applied_at"
STATE_FILE = f"{cc.WORK_DIR}/rollback_state.json"

# A unit per rollback: the unit of a finished one may still hold the
# processes of the server it promoted
JOB_UNIT_PREFIX = "exordos-db-pg-rollback-"


def job_unit(rollback_id: str) -> str:
    return f"{JOB_UNIT_PREFIX}{rollback_id}"


JOB_COMMAND = "/usr/bin/exordos-db-pg-rollback"

LEADER_ROLES = ("leader", "standby_leader")


class Action(str, enum.Enum):
    WAIT = "wait"
    PAUSE = "pause"
    REPAUSE = "repause"
    STOP_POSTGRES = "stop_postgres"
    START_JOB = "start_job"
    RESTART_POSTGRES = "restart_postgres"
    RESUME = "resume"
    MARK_APPLIED = "mark_applied"
    FAILED = "failed"


class Phase(str, enum.Enum):
    # Replica
    STOPPED = "stopped"
    # Leader
    PAUSED = "paused"
    RESTORING = "restoring"
    RESTORED = "restored"
    RESUMED = "resumed"
    FAILED = "failed"


def load_marker() -> dict[str, tp.Any] | None:
    return files.read_json(MARKER_FILE)


def applied_id() -> str | None:
    """The rollback this node has applied, by id.

    Only the id: the spec is rendered from the source and the repository as
    they are, so the same rollback may be asked for with other credentials,
    and a spec kept here would be stale as soon as they are rotated.
    """
    marker = load_marker()
    return None if marker is None else str(marker["id"])


def applied_at() -> float | None:
    """When the last rollback was applied, readable by the backup timer."""
    content = files.read(APPLIED_AT_FILE)
    return None if content is None else float(content)


def mark_applied(rollback_id: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    # The marker is the agent's own state, root's only, while the backup
    # timer runs as postgres and needs just the time
    files.write_json(MARKER_FILE, {"id": rollback_id, "applied_at": now}, mode=0o600)
    files.write(APPLIED_AT_FILE, str(now))
    clear_state()


def load_state() -> dict[str, tp.Any] | None:
    return files.read_json(STATE_FILE)


def save_state(spec: dict[str, tp.Any], phase: Phase, **extra: tp.Any) -> None:
    # The job reads the spec from the state
    files.write_json(
        STATE_FILE,
        {"id": spec["id"], "phase": phase.value, "spec": spec, **extra},
        mode=0o600,
    )


def clear_state() -> None:
    files.remove(STATE_FILE)


def run_as_postgres(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    LOG.info("Running %s", " ".join(cmd))
    return subprocess.run(
        ["runuser", "-u", "postgres", "--", *cmd],
        capture_output=True,
        text=True,
        check=check,
    )


def stop_postgres() -> None:
    status = run_as_postgres(
        PG_CTL, "-D", pgbackrest.PG_DATA_DIR, "status", check=False
    )
    # 3 means not running: a replica's server may be down already
    if status.returncode == 3:
        return
    run_as_postgres(PG_CTL, "-D", pgbackrest.PG_DATA_DIR, "-m", "fast", "-w", "stop")


def decide(
    spec: dict[str, tp.Any],
    state: dict[str, tp.Any] | None,
    paused: bool,
    members: tp.Sequence[dict[str, tp.Any]],
    my_name: str,
    applied_id: str | None = None,
    job_active: bool = False,
    owner: dict[str, tp.Any] | None = None,
) -> Action:
    """Choose the next step of the rollback on this node.

    `members` is the `members` list of Patroni's /cluster, `state` the
    progress saved on this node, `applied_id` the rollback the cluster has
    applied according to the DCS, `job_active` whether a rollback job (maybe
    of a superseded rollback) still runs on this node, `owner` the node that
    paused the cluster for a rollback according to the DCS.
    """
    # The leader of a failed or unfinished rollback may not be the leader in
    # Patroni any more: a paused Patroni removes the lock of a stopped server
    was_leader = state is not None and Phase(state["phase"]) is not Phase.STOPPED

    if state is not None and state["id"] != spec["id"]:
        # A newer rollback replaces an unfinished or failed one, it starts
        # over; a job of the old one still running is waited for
        state = None

    if state is None and applied_id == spec["id"]:
        # The cluster has applied it while this node wasn't part of it: the
        # node was added or reinstalled later, or its agent was down. Rolling
        # back again would lose everything written since.
        return Action.MARK_APPLIED

    me = next((m for m in members if m["name"] == my_name), {})
    leader = next((m for m in members if m.get("role") in LEADER_ROLES), None)
    names = sorted(m["name"] for m in members)
    # Whoever pauses the cluster records itself; a later claim replaces the
    # record, and the node it replaced yields
    owner_node = owner.get("node") if owner and owner.get("id") == spec["id"] else None
    # The node that led the rollback is gone (removed or reinstalled) and
    # nobody can serve as the leader: the node with the lowest name takes
    # over, and its claim settles a race with anyone else doing the same
    take_over = (
        paused
        and leader is None
        and owner_node not in names
        and all(m.get("state") == "stopped" for m in members)
        and names[:1] == [my_name]
    )
    phase = None if state is None else Phase(state["phase"])

    if phase is Phase.FAILED:
        return Action.FAILED

    # Replica
    if phase is Phase.STOPPED:
        if take_over:
            return Action.PAUSE
        if paused:
            return Action.WAIT
        # Rewound and replicating the rolled back leader, not merely started:
        # a failed rewind leaves a running replica on the old timeline
        if (
            applied_id == spec["id"]
            and leader is not None
            and me.get("state") == "streaming"
            and me.get("timeline") == leader.get("timeline")
        ):
            return Action.MARK_APPLIED
        return Action.WAIT

    # Leader
    if phase is Phase.PAUSED:
        if owner_node not in (None, my_name):
            # Another node owns the rollback: a failover or a lost claim
            return Action.STOP_POSTGRES
        if not paused:
            # The pause didn't reach the DCS or was lifted meanwhile
            return Action.REPAUSE
        replicas = [m for m in members if m["name"] != my_name]
        if not job_active and all(m.get("state") == "stopped" for m in replicas):
            return Action.START_JOB
        return Action.WAIT
    if phase is Phase.RESTORING:
        # A cluster resumed during the restore would elect a leader
        return Action.WAIT if paused else Action.REPAUSE
    if phase is Phase.RESTORED:
        # Resume only once the paused Patroni has taken the leader lock for
        # the rolled back primary. A cluster resumed without a leader holds
        # an election, and a replica still on the old timeline is ahead.
        if me.get("role") in LEADER_ROLES and me.get("state") == "running":
            return Action.RESUME
        return Action.WAIT if paused else Action.REPAUSE
    if phase is Phase.RESUMED:
        if paused or me.get("state") != "running":
            return Action.WAIT
        # The promoted server runs in the unit of the job that started it.
        # Restarted by Patroni it becomes Patroni's own, and the unit goes.
        if state is None or not state.get("restarted"):
            return Action.RESTART_POSTGRES
        return Action.MARK_APPLIED

    # Not started on this node yet
    if me.get("role") in LEADER_ROLES:
        # Even on a paused cluster: it is left paused by a failed or a
        # superseded rollback, or by a restart between pausing and saving
        return Action.PAUSE
    if leader is None and (was_leader or owner_node == my_name or take_over):
        return Action.PAUSE
    if paused:
        return Action.STOP_POSTGRES
    # Wait for the leader to pause the cluster
    return Action.WAIT
