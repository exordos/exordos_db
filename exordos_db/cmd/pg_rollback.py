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
"""Roll the paused cluster's leader back to a point in time.

Run by the agent as a transient systemd unit, see exordos_db.common.rollback.
"""

import logging
import os
import subprocess
import sys
import time
import typing as tp

from exordos_db.common import pgbackrest
from exordos_db.common import rollback

LOG = logging.getLogger(__name__)

PG_CTL = rollback.PG_CTL
PG_CONTROLDATA = "/usr/sbin/pg_controldata"
PSQL = "/usr/sbin/psql"
LOG_FILE = "/var/log/postgresql/exordos-rollback.log"
ARCHIVE_TIMEOUT = 600
# Recovery replays WAL from the start of the chosen backup, it may be long
RECOVERY_TIMEOUT = 7 * 24 * 3600
# `pg_ctl -W start` returns before the server writes its pid file
START_TIMEOUT = 60
POLL_INTERVAL = 5
# A backup command line ends with the command, its workers with backup:local
BACKUP_COMMAND = " backup(:local)?$"
BACKUP_STOP_TIMEOUT = 120
# pgBackRest keeps its manifest in the data directory until a restore is
# done, and the restore leaves the recovery to PostgreSQL
RESTORE_LEFTOVERS = ("backup.manifest", "recovery.signal", "backup_label")

_as_postgres = rollback.run_as_postgres


def run(spec: dict) -> None:
    pgbackrest.write_restore_config(spec)

    # The restore replaces the data and the WAL of the leader. Everything it
    # needs is checked while nothing is lost yet: the WAL up to now is in the
    # archive, and a backup to start the recovery from exists.
    _archive_the_tail()
    stanza, backup_set = pgbackrest.restore_set(spec)

    # The backup timer doesn't start one on a paused cluster, but one started
    # before the pause would copy the data while it is replaced
    _stop_backups()
    rollback.stop_postgres()
    _keep_the_current_state(spec)
    pgbackrest.run(
        stanza,
        *pgbackrest.restore_args(spec, backup_set, in_place=True),
        timeout=None,
    )

    # Recover outside of Patroni: it would drop the recovery target. `pg_ctl
    # -w` can't wait for the promotion, a server with hot_standby off reports
    # itself ready in standby already, so the control file is watched instead.
    _as_postgres(
        PG_CTL,
        "-D",
        pgbackrest.PG_DATA_DIR,
        "-W",
        "-l",
        LOG_FILE,
        "-o",
        "-c hot_standby=off",
        "start",
    )
    _wait_for_promotion()
    # PostgreSQL is left running as the primary: a paused Patroni takes the
    # leader lock only for a running primary, and removes it for a stopped one


def _psql(query: str) -> str:
    return _as_postgres(PSQL, "-Atc", query).stdout.strip()


def _archive_the_tail() -> None:
    """Archive the WAL written up to now, the target may be in it."""
    status = _as_postgres(PG_CTL, "-D", pgbackrest.PG_DATA_DIR, "status", check=False)
    if status.returncode == 3:
        # Stopped by an earlier attempt, which archived the tail first
        return

    # The file holding the last byte before the switch; when nothing was
    # written since the previous switch it is the previous, archived one
    last = _psql("select pg_walfile_name(pg_switch_wal() - 1)")
    deadline = time.monotonic() + ARCHIVE_TIMEOUT
    while (
        _psql("select coalesce(last_archived_wal, '') from pg_stat_archiver")[:24]
        < last
    ):
        if time.monotonic() > deadline:
            raise RuntimeError(f"WAL {last} isn't archived, the target may be lost")
        time.sleep(POLL_INTERVAL)


def _stop_backups() -> None:
    # pgBackRest leaves an interrupted backup to be resumed or expired
    pattern = f"pgbackrest .*{BACKUP_COMMAND}"
    subprocess.run(["pkill", "-u", "postgres", "-f", pattern], check=False)
    deadline = time.monotonic() + BACKUP_STOP_TIMEOUT
    while (
        subprocess.run(
            ["pgrep", "-u", "postgres", "-f", pattern],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    ):
        if time.monotonic() > deadline:
            raise RuntimeError("A backup is still running")
        time.sleep(1)


def _keep_the_current_state(spec: dict) -> None:
    """Back the stopped leader up, so the rollback can be undone."""
    if any(
        os.path.exists(os.path.join(pgbackrest.PG_DATA_DIR, name))
        for name in RESTORE_LEFTOVERS
    ):
        # The data of a rollback that failed after its restore had started:
        # not a state the instance had, the one before that rollback is kept
        LOG.warning("Not keeping the data of an unfinished restore")
        return
    if pgbackrest.find_snapshot(spec["stanza"], spec["id"]) is not None:
        # Kept by an earlier attempt of the job
        return

    _record(spec, rollback.Phase.SAVING, started=True)
    if _cluster_state() != "shut down":
        # A server that crashed before the job: an offline backup of it
        # would recover to its last checkpoint only
        _as_postgres(
            PG_CTL, "-D", pgbackrest.PG_DATA_DIR, "-w", "-l", LOG_FILE, "start"
        )
        rollback.stop_postgres()
    pgbackrest.take_snapshot(spec["stanza"], spec["id"])
    _record(spec, rollback.Phase.RESTORING, started=True)


def _cluster_state() -> str:
    control = _as_postgres(PG_CONTROLDATA, "-D", pgbackrest.PG_DATA_DIR).stdout
    for line in control.splitlines():
        if line.startswith("Database cluster state:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("No cluster state in pg_controldata")


def _wait_for_promotion() -> None:
    started = time.monotonic()
    deadline = started + RECOVERY_TIMEOUT
    running = False
    while (state := _cluster_state()) != "in production":
        status = _as_postgres(
            PG_CTL, "-D", pgbackrest.PG_DATA_DIR, "status", check=False
        )
        if status.returncode != 3:
            running = True
        elif running or time.monotonic() > started + START_TIMEOUT:
            raise RuntimeError(
                f"PostgreSQL stopped during the recovery ({state}), see {LOG_FILE}"
            )
        if time.monotonic() > deadline:
            raise RuntimeError(f"The recovery didn't finish in time ({state})")
        time.sleep(POLL_INTERVAL)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    state = rollback.load_state()
    if state is None or state.get("phase") != rollback.Phase.RESTORING.value:
        # Not the whole state: it carries the repository credentials
        LOG.error(
            "No rollback to run: %s",
            None if state is None else (state.get("id"), state.get("phase")),
        )
        return 1

    spec = state["spec"]
    # The agent tells a job that died from one that never started by it
    rollback.save_state(spec, rollback.Phase.RESTORING, started=True)
    try:
        run(spec)
    except Exception as e:
        stderr = getattr(e, "stderr", "") or ""
        LOG.exception("Rollback %s failed %s", spec["id"], stderr)
        _record(spec, rollback.Phase.FAILED, error=f"{e} {stderr}".strip())
        return 1

    _record(spec, rollback.Phase.RESTORED)
    LOG.info("Rolled back to %s", pgbackrest.describe_target(spec))
    return 0


def _record(spec: dict, phase: rollback.Phase, **extra: tp.Any) -> None:
    # A rollback with a higher revision may have replaced this one meanwhile,
    # its progress must not be overwritten with this outcome
    current = rollback.load_state()
    if current is None or current["id"] != spec["id"]:
        LOG.warning("Rollback %s was superseded, not recording %s", spec["id"], phase)
        return
    rollback.save_state(spec, phase, **extra)


if __name__ == "__main__":
    sys.exit(main())
