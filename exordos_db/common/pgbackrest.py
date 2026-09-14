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

import hashlib
import json
import logging
import os
import subprocess
import typing as tp

from exordos_db.common import constants as cc
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
# Fingerprint of the repository the stanza was created in on this node
STANZA_MARKER_FILE = f"{cc.WORK_DIR}/backup_stanza.sha256"

PG_DATA_DIR = f"{cc.PATRONI_DIR}/data"
PG_SOCKET_DIR = "/var/run/postgresql"

DISABLED_ARCHIVE_COMMAND = ":"

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
    options = {**FIXED_GLOBAL_OPTIONS, **spec["options"]}
    lines = ["[global]"]
    for key in sorted(options):
        _check_line(key, options[key])
        lines.append(f"{key}={options[key]}")

    _check_line("stanza", spec["stanza"])
    lines += [
        "",
        f"[{spec['stanza']}]",
        f"pg1-path={PG_DATA_DIR}",
        f"pg1-socket-path={PG_SOCKET_DIR}",
    ]
    return "\n".join(lines) + "\n"


def repo_fingerprint(spec: dict[str, tp.Any]) -> str:
    """Identify the repository and stanza, ignoring unrelated options."""
    repo = {k: v for k, v in spec["options"].items() if k.startswith("repo")}
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


def mark_stanza_ready(spec: dict[str, tp.Any] | None) -> None:
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
    # Written by the restore command running as postgres
    fd = os.open(RESTORE_CONF_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render_config(spec))


def remove_restore_config() -> bool:
    return files.remove(RESTORE_CONF_FILE)


def restore_args(spec: dict[str, tp.Any]) -> list[str]:
    args = [f"--config={RESTORE_CONF_FILE}"]
    if spec["target_time"] is not None:
        args += [
            "--type=time",
            f"--target={spec['target_time']}",
            "--target-action=promote",
        ]
    # Without a target the archive is replayed to its end
    return [*args, "restore"]


def choose_backup_type(
    backups: tp.Iterable[dict[str, tp.Any]],
    schedule: dict[str, int],
    now: float,
) -> str | None:
    """Decide which backup is due, if any.

    `backups` is the `backup` list of `pgbackrest info --output=json`.
    """
    done = [b for b in backups if not b.get("error")]

    fulls = [b["timestamp"]["stop"] for b in done if b["type"] == "full"]
    if not fulls or now - max(fulls) >= schedule["full_interval_hours"] * 3600:
        return "full"

    last = max(b["timestamp"]["stop"] for b in done)
    if now - last >= schedule["incr_interval_hours"] * 3600:
        return "incr"

    return None
