# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Size the local shared buffer before Patroni reads its cached DCS profile."""

from pathlib import Path

import yaml

from exordos_db.common import constants
from exordos_db.common import pg_tuning

RUNTIME_DIR = Path("/run/exordos-db-patroni")


def prepare_config(managed: Path, directory: Path) -> None:
    # No PostgreSQL or DCS access: this runs before either is available.
    cpu, ram = pg_tuning.node_resources()
    shared_buffers = pg_tuning.calculate_settings(cpu, ram)["shared_buffers"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    link = directory / "00-managed.yml"
    if not link.is_symlink():
        link.symlink_to(managed)
    # Patroni loads a directory in order; only this node-local setting
    # overrides the cached profile. Keep Core's file linked for later reloads.
    target = directory / "10-memory.yml"
    temporary = directory / "10-memory.tmp"
    with temporary.open("w") as stream:
        temporary.chmod(0o600)
        yaml.safe_dump(
            {"postgresql": {"parameters": {"shared_buffers": shared_buffers}}}, stream
        )
    temporary.replace(target)


def main() -> int:
    prepare_config(Path(constants.PATRONI_CONFIG_FILE), RUNTIME_DIR)
    return 0
