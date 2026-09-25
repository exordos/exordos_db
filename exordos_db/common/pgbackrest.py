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
"""pgBackRest on the data plane.

The control plane sends a backup spec:

    {
        "stanza": "<instance uuid>",
        "options": {"repo1-type": "s3", ...},
        "schedule": {"full_interval_hours": 168, "incr_interval_hours": 24},
    }

The agent renders it into pgbackrest.conf and keeps the spec next to it, so
both the agent and the backup timer read the applied state from one place.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import typing as tp

from exordos_db.common import constants as cc
from exordos_db.common import endpoints
from exordos_db.common import files

LOG = logging.getLogger(__name__)

CONF_DIR = "/etc/pgbackrest"
CONF_FILE = f"{CONF_DIR}/pgbackrest.conf"
SPEC_FILE = f"{CONF_DIR}/exordos_backup.json"
# Delivered by the control plane before the cluster is bootstrapped
RESTORE_SPEC_FILE = f"{CONF_DIR}/exordos_restore.json"
# The restore config is referenced by restore_command until the recovery
# ends, so it lives next to the data until the agent sees a primary
RESTORE_CONF_FILE = f"{cc.PATRONI_DIR}/pgbackrest-restore.conf"
# The progress of the restore a cluster is bootstrapped with, see
# exordos_db.cmd.pg_restore; removed once the node is a primary
RESTORE_STATE_FILE = f"{cc.PATRONI_DIR}/exordos_restore_state.json"
# Errors reach the API, a pgBackRest error may be long
ERROR_MAX_LENGTH = 1024
# The backups the primary last found in the repository, written by the
# backup timer and reported by the agent, see collect_catalog
CATALOG_FILE = f"{cc.PATRONI_DIR}/exordos_backup_catalog.json"
# Of a stanza, bounds what a node reports to the control plane
CATALOG_MAX_BACKUPS = 500
# Why the stanza can't be created, reported while it isn't
STANZA_ERROR_FILE = f"{cc.WORK_DIR}/backup_stanza_error.txt"
# Fingerprint of the repository the stanza was created in on this node
STANZA_MARKER_FILE = f"{cc.WORK_DIR}/backup_stanza.sha256"

PG_DATA_DIR = f"{cc.PATRONI_DIR}/data"
PG_SOCKET_DIR = "/var/run/postgresql"

# Revision of the rollback that kept the state
SNAPSHOT_ANNOTATION = "exordos-before-revision"

DISABLED_ARCHIVE_COMMAND = ":"

S3_ENDPOINT_OPTION = "repo1-s3-endpoint"
VERIFY_TLS_OPTION = "repo1-storage-verify-tls"

FIXED_GLOBAL_OPTIONS = {
    "archive-async": "y",
    "spool-path": "/var/spool/pgbackrest",
    "log-path": "/var/log/pgbackrest",
    "log-level-console": "warn",
    "log-level-file": "info",
    "compress-type": "zst",
    "process-max": "2",
    "start-fast": "y",
    "delta": "y",
}


class PgBackRestError(RuntimeError):
    pass


def archive_command(spec: dict[str, tp.Any] | None) -> str:
    if spec is None:
        return DISABLED_ARCHIVE_COMMAND
    return f"pgbackrest --stanza={spec['stanza']} archive-push %p"


def _check_line(key: str, value: str) -> None:
    if any(c in f"{key}{value}" for c in "\r\n"):
        raise ValueError(f"Line break in pgBackRest option {key}")


def render_config(spec: dict[str, tp.Any]) -> str:
    """Render pgbackrest.conf, pinning the endpoint to a checked address.

    The control plane checks the endpoint when the repository is saved, but
    the name behind it can be pointed at a node's own address afterwards.
    Whatever the name resolves to here is checked and passed as the host to
    connect to, so the address can't change before the request.
    """
    options = {**FIXED_GLOBAL_OPTIONS, **spec["options"], **_endpoint_options(spec)}
    lines = ["[global]"]
    for key in sorted(options):
        _check_line(key, options[key])
        lines.append(f"{key}={options[key]}")

    _check_line("stanza", spec["stanza"])
    for stanza in (spec["stanza"], snapshot_stanza(spec["stanza"])):
        lines += [
            "",
            f"[{stanza}]",
            f"pg1-path={PG_DATA_DIR}",
            f"pg1-socket-path={PG_SOCKET_DIR}",
        ]
    return "\n".join(lines) + "\n"


def _endpoint_options(spec: dict[str, tp.Any]) -> dict[str, str]:
    """Pin the endpoint's name to an address checked here, where possible.

    With TLS verified, the certificate is checked against the host connected
    to, so pinning the address would reject every valid https endpoint. There
    the name in the certificate is what ties the endpoint to its storage, and
    a name pointed elsewhere fails the handshake instead.
    """
    options = spec["options"]
    endpoint = options.get(S3_ENDPOINT_OPTION)
    if endpoint is None:
        return {}
    try:
        pinned = endpoints.pinned_address(endpoint)
    except ValueError as e:
        raise PgBackRestError(str(e)) from e

    verified_tls = (
        endpoint.startswith("https://") and options.get(VERIFY_TLS_OPTION, "y") != "n"
    )
    if pinned is None or verified_tls:
        return {}
    address, port = pinned
    scheme = endpoint.split("://", 1)[0]
    return {
        "repo1-storage-host": f"{scheme}://{address}",
        "repo1-storage-port": str(port),
    }


def snapshot_stanza(stanza: str) -> str:
    """The stanza of the states kept before rollbacks, offline backups."""
    return f"{stanza}-rollbacks"


def repo_fingerprint(spec: dict[str, tp.Any]) -> str:
    """Identify the repository and stanza, ignoring unrelated options."""
    # Retention is applied by backups, not by creating the stanza
    repo = {
        k: v
        for k, v in spec["options"].items()
        if k.startswith("repo")
        and k not in ("repo1-retention-full", "repo1-retention-full-type")
    }
    data = json.dumps({"stanza": spec["stanza"], "repo": repo}, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def load_spec(path: str = SPEC_FILE) -> dict[str, tp.Any] | None:
    return files.read_json(path)


def apply_spec(spec: dict[str, tp.Any] | None) -> bool:
    """Bring the config files to the spec, return whether anything changed."""
    if spec is None:
        removed = files.remove(CONF_FILE)
        return files.remove(SPEC_FILE) or removed

    config = render_config(spec)
    if load_spec() == spec and files.read(CONF_FILE) == config:
        return False

    os.makedirs(CONF_DIR, exist_ok=True)
    # Both carry repository credentials
    files.write(CONF_FILE, config, 0o640, "postgres")
    files.write_json(SPEC_FILE, spec, mode=0o640, group="postgres")
    return True


def stanza_ready(spec: dict[str, tp.Any]) -> bool:
    marker = files.read(STANZA_MARKER_FILE)
    return marker is not None and marker.strip() == repo_fingerprint(spec)


def save_stanza_error(error: BaseException | str) -> None:
    files.write(STANZA_ERROR_FILE, error_text(error), group="root")


def load_stanza_error() -> str | None:
    return files.read(STANZA_ERROR_FILE)


def mark_stanza_ready(spec: dict[str, tp.Any] | None) -> None:
    files.remove(STANZA_ERROR_FILE)
    if spec is None:
        files.remove(STANZA_MARKER_FILE)
        return
    files.write(STANZA_MARKER_FILE, repo_fingerprint(spec), group="root")


def run(
    stanza: str,
    *args: str,
    timeout: float | None = 600,
) -> str:
    cmd = ["pgbackrest", f"--stanza={stanza}", *args]
    # The agent runs as root, the backup timer as postgres
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--", *cmd]

    LOG.info("Running %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise PgBackRestError(
            f"{' '.join(args)} failed with code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def write_restore_config(spec: dict[str, tp.Any]) -> None:
    # Read by pgbackrest running as postgres, written either by the restore
    # command running as postgres or by the rollback job running as root
    fd = os.open(RESTORE_CONF_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render_config(spec))
    if os.geteuid() == 0:
        shutil.chown(RESTORE_CONF_FILE, user="postgres", group="postgres")


def remove_restore_config() -> bool:
    return files.remove(RESTORE_CONF_FILE)


def error_text(error: BaseException | str) -> str:
    """Shorten an error to report it to the control plane."""
    text = str(error).strip()
    return text if len(text) <= ERROR_MAX_LENGTH else f"{text[:ERROR_MAX_LENGTH]}..."


def load_restore_state() -> dict[str, tp.Any] | None:
    return files.read_json(RESTORE_STATE_FILE)


def save_restore_state(state: dict[str, tp.Any]) -> None:
    # Written by the bootstrap running as postgres, no credentials in it
    files.write_json(RESTORE_STATE_FILE, state)


def remove_restore_state() -> bool:
    return files.remove(RESTORE_STATE_FILE)


def restore_args(
    spec: dict[str, tp.Any], backup_set: str, in_place: bool = False
) -> list[str]:
    """Return the restore arguments, `in_place` for the cluster's own nodes.

    delta=y comes from the rendered config: only files that differ are
    fetched from the repository.
    """
    args = [f"--config={RESTORE_CONF_FILE}", f"--set={backup_set}"]
    if spec.get("before_revision") is not None:
        # A copy of the stopped cluster replays its own WAL only. The type is
        # explicit: for an offline backup it defaults to none, which starts the
        # server without a recovery and keeps the timeline of the copy.
        args += ["--type=default", "--target-timeline=current"]
        if in_place:
            # The kept state has no archive, and the timeline history is in
            # the cluster's: the promotion has to choose a timeline no
            # rollback has used
            args.append(
                "--recovery-option=restore_command=pgbackrest "
                f"--config={RESTORE_CONF_FILE} "
                f'--stanza={spec["stanza"]} archive-get %f "%p"'
            )
        return [*args, "restore"]
    if spec["target_time"] is not None:
        args += [
            "--type=time",
            f"--target={spec['target_time']}",
            "--target-action=promote",
        ]
    # Without a target the archive is replayed to its end
    return [*args, "restore"]


def target_timestamp(target_time: str) -> float:
    return (
        datetime.datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S.%f+00")
        .replace(tzinfo=datetime.timezone.utc)
        .timestamp()
    )


def _lsn(text: str) -> int:
    high, low = text.split("/")
    return int(high, 16) << 32 | int(low, 16)


def parse_history(content: str) -> dict[int, int]:
    """Map the ancestors of a timeline to the LSNs it forked from them at."""
    forks = {}
    for line in content.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit():
            forks[int(fields[0])] = _lsn(fields[1])
    return forks


class Timelines(tp.NamedTuple):
    # The timeline a recovery follows by default
    latest: int
    # The ancestors of the latest timeline and where it forked from each
    forks: dict[int, int]


def choose_backup_set(
    backups: tp.Iterable[dict[str, tp.Any]],
    target_time: str | None,
    timelines: tp.Callable[[dict[str, tp.Any]], Timelines],
) -> str | None:
    """Choose the backup to recover to the target from.

    `backups` is the `backup` list of `pgbackrest info --output=json`,
    `timelines` gives the timelines of the archive a backup belongs to.

    pgBackRest picks the newest backup finished before the target and fails
    when it is off the latest timeline's history: after a rollback to an
    earlier point, a backup taken before the rollback but after the point it
    forked at is on an abandoned branch.

    A backup on an ancestor has to end before the fork, not only start: the
    record of its end is in the WAL of the abandoned branch otherwise, and
    the recovery never becomes consistent.
    """
    # The stop time of a backup is in whole seconds: compared as pgBackRest
    # does, a backup ending within the target's second doesn't count
    target = None if target_time is None else math.floor(target_timestamp(target_time))
    for backup in sorted(backups, key=lambda b: b["timestamp"]["stop"], reverse=True):
        if backup.get("error"):
            continue
        if target is not None and backup["timestamp"]["stop"] >= target:
            continue
        if on_latest_history(backup, timelines):
            return backup["label"]
    return None


def on_latest_history(
    backup: dict[str, tp.Any],
    timelines: tp.Callable[[dict[str, tp.Any]], Timelines],
) -> bool:
    """Whether a recovery following the latest timeline can start from it."""
    timeline = int(backup["archive"]["start"][:8], 16)
    latest, forks = timelines(backup)
    return timeline >= latest or (
        timeline in forks and _lsn(backup["lsn"]["stop"]) <= forks[timeline]
    )


def timelines_loader(
    stanza: str, stanza_info: dict[str, tp.Any], *config: str
) -> tp.Callable[[dict[str, tp.Any]], Timelines]:
    """Return the timelines of the archive a backup is in, read once each."""
    archive_ids = {a["database"]["id"]: a["id"] for a in stanza_info.get("archive", [])}
    loaded: dict[str, Timelines] = {}

    def timelines(backup: dict[str, tp.Any]) -> Timelines:
        archive_id = archive_ids[backup["database"]["id"]]
        if archive_id not in loaded:
            path = f"archive/{stanza}/{archive_id}"
            listed = json.loads(
                run(
                    stanza,
                    *config,
                    "--output=json",
                    "--filter=\\.history$",
                    "repo-ls",
                    path,
                )
            )
            histories = sorted(int(name[:8], 16) for name in listed)
            if not histories:
                loaded[archive_id] = Timelines(1, {})
            else:
                latest = histories[-1]
                content = run(
                    stanza, *config, "repo-get", f"{path}/{latest:08X}.history"
                )
                loaded[archive_id] = Timelines(latest, parse_history(content))
        return loaded[archive_id]

    return timelines


def restore_backup_set(stanza: str, target_time: str | None) -> str:
    """Choose the backup to restore with the restore config."""
    config = f"--config={RESTORE_CONF_FILE}"
    info = json.loads(run(stanza, config, "--output=json", "info"))
    stanza_info = info[0] if info else {}

    backup_set = choose_backup_set(
        stanza_info.get("backup", []),
        target_time,
        timelines_loader(stanza, stanza_info, config),
    )
    if backup_set is None:
        raise PgBackRestError(
            f"No backup to recover to {target_time or 'the end of the archive'} from"
        )
    return backup_set


def _catalog_entry(backup: dict[str, tp.Any], restorable: bool) -> dict[str, tp.Any]:
    annotation = str((backup.get("annotation") or {}).get(SNAPSHOT_ANNOTATION, ""))
    info = backup.get("info") or {}
    return {
        "label": backup["label"],
        "type": backup["type"],
        "started_at": backup["timestamp"]["start"],
        "finished_at": backup["timestamp"]["stop"],
        "size": info.get("size", 0),
        "stored_size": (info.get("repository") or {}).get("size", 0),
        "before_revision": int(annotation) if annotation.isdigit() else None,
        "error": bool(backup.get("error")),
        "restorable": restorable,
    }


def catalog_backups(
    stanza: str, stanza_info: dict[str, tp.Any], snapshots: bool = False
) -> list[dict[str, tp.Any]]:
    """Describe the backups of a stanza, the newest CATALOG_MAX_BACKUPS."""
    backups = sorted(
        stanza_info.get("backup", []), key=lambda b: b["timestamp"]["stop"]
    )
    backups = backups[-CATALOG_MAX_BACKUPS:]
    timelines = timelines_loader(stanza, stanza_info)
    entries = []
    for backup in backups:
        # A copy kept before a rollback replays its own WAL only
        restorable = not backup.get("error") and (
            snapshots or on_latest_history(backup, timelines)
        )
        entries.append(_catalog_entry(backup, restorable))
    return entries


def collect_catalog(
    spec: dict[str, tp.Any],
    stanza_info: dict[str, tp.Any] | None,
    now: float,
    error: str | None = None,
    archive: dict[str, tp.Any] | None = None,
) -> dict[str, tp.Any]:
    """Describe the backups of the spec's stanzas for the control plane.

    `stanza_info` is the info of the spec's stanza, already read, None when
    it couldn't be. A stanza whose backups can't be read is left out rather
    than reported empty, so the control plane doesn't forget its backups.
    `error` is what failed in the run of the backup timer, `archive` the
    state of WAL archiving.
    """
    stanza = spec["stanza"]
    backups = {}
    if stanza_info is not None:
        snapshots = snapshot_stanza(stanza)
        try:
            backups[stanza] = catalog_backups(stanza, stanza_info)
            info = json.loads(run(snapshots, "--output=json", "info"))
            backups[snapshots] = catalog_backups(
                snapshots, info[0] if info else {}, snapshots=True
            )
        except (PgBackRestError, subprocess.TimeoutExpired) as e:
            LOG.error("Backups of %s can't be read: %s", stanza, e)
            error = error or error_text(e)
    return {
        "repository": spec.get("repository"),
        "stanza": stanza,
        "collected_at": now,
        "backups": backups,
        "error": error,
        "archive": archive,
    }


def save_catalog(catalog: dict[str, tp.Any]) -> None:
    # Written by the backup timer running as postgres, no credentials in it
    files.write_json(CATALOG_FILE, catalog)


def remove_catalog() -> bool:
    return files.remove(CATALOG_FILE)


def load_catalog() -> dict[str, tp.Any] | None:
    return files.read_json(CATALOG_FILE)


def find_snapshot(stanza: str, revision: str | int) -> str | None:
    """Return the state kept before the rollback with the revision, if any."""
    info = json.loads(
        run(
            snapshot_stanza(stanza),
            f"--config={RESTORE_CONF_FILE}",
            "--output=json",
            "info",
        )
    )
    labels = [
        backup["label"]
        for backup in (info[0].get("backup", []) if info else [])
        if not backup.get("error")
        and (backup.get("annotation") or {}).get(SNAPSHOT_ANNOTATION) == str(revision)
    ]
    return labels[-1] if labels else None


def take_snapshot(stanza: str, revision: str) -> None:
    """Back the stopped cluster up, incrementally, annotated with the revision."""
    name = snapshot_stanza(stanza)
    config = f"--config={RESTORE_CONF_FILE}"
    run(name, config, "--no-online", "stanza-create")
    # delta=y compares files by checksum: a restore resets their timestamps
    run(
        name,
        config,
        "--no-online",
        "--type=incr",
        f"--annotation={SNAPSHOT_ANNOTATION}={revision}",
        "backup",
        timeout=None,
    )


def restore_set(spec: dict[str, tp.Any]) -> tuple[str, str]:
    """Return the stanza and the backup to restore the spec from."""
    revision = spec.get("before_revision")
    if revision is None:
        return spec["stanza"], restore_backup_set(spec["stanza"], spec["target_time"])
    label = find_snapshot(spec["stanza"], revision)
    if label is None:
        raise PgBackRestError(f"No state kept before rollback {revision}")
    return snapshot_stanza(spec["stanza"]), label


def describe_target(spec: dict[str, tp.Any]) -> str:
    if spec.get("before_revision") is not None:
        return f"the state before rollback {spec['before_revision']}"
    return spec["target_time"] or "the end of the archive"


def choose_backup_type(
    backups: tp.Iterable[dict[str, tp.Any]],
    schedule: dict[str, int],
    now: float,
    full_after: float | None = None,
) -> str | None:
    """Decide which backup is due, if any.

    `backups` is the `backup` list of `pgbackrest info --output=json`.
    `full_after` forces a full backup until one finishes after that moment,
    e.g. after a rollback, when older backups belong to another timeline.
    """
    done = [b for b in backups if not b.get("error")]

    fulls = [b["timestamp"]["stop"] for b in done if b["type"] == "full"]
    if not fulls or now - max(fulls) >= schedule["full_interval_hours"] * 3600:
        return "full"
    if full_after is not None and max(fulls) < full_after:
        return "full"

    last = max(b["timestamp"]["stop"] for b in done)
    if now - last >= schedule["incr_interval_hours"] * 3600:
        return "incr"

    return None
