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
"""Take a pgBackRest backup if one is due. Run periodically by a timer."""

import json
import logging
import subprocess
import sys
import time
import typing as tp

import psycopg

from exordos_db.agent.universal.drivers import pg
from exordos_db.common import pgbackrest
from exordos_db.common import rollback

LOG = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    spec = pgbackrest.load_spec()
    if spec is None:
        LOG.info("Backups are disabled")
        return 0

    patroni = pg.PatroniClient()
    if not patroni.is_primary():
        LOG.info("Not a primary node, nothing to do")
        # What it saw as the primary goes stale, and would be reported again
        # if it became the primary once more
        pgbackrest.remove_catalog()
        return 0
    # A paused cluster may be rolled back: its data is about to be replaced
    if patroni.config_get().get("pause"):
        LOG.info("The cluster is paused, no backup is taken")
        return 0

    # The agent creates the stanza and turns archiving on first
    if not pgbackrest.stanza_ready(spec):
        LOG.info("Stanza %s is not ready yet", spec["stanza"])
        return 0

    stanza = spec["stanza"]
    archive = archive_state()
    info = None
    error = None
    try:
        info = json.loads(pgbackrest.run(stanza, "--output=json", "info"))
        backups = info[0].get("backup", []) if info else []

        backup_type = pgbackrest.choose_backup_type(
            backups, spec["schedule"], time.time(), full_after=rollback.applied_at()
        )
        if backup_type is None:
            LOG.info("No backup is due")
        else:
            LOG.info("Taking a %s backup of %s", backup_type, stanza)
            pgbackrest.run(stanza, f"--type={backup_type}", "backup", timeout=None)
            LOG.info("The %s backup of %s is done", backup_type, stanza)
            info = json.loads(pgbackrest.run(stanza, "--output=json", "info"))
    except (pgbackrest.PgBackRestError, subprocess.TimeoutExpired) as e:
        LOG.error("Backup of %s failed: %s", stanza, e)
        error = pgbackrest.error_text(e)

    # The control plane learns about the backups and the failures from the
    # agent
    stanza_info = None if info is None else (info[0] if info else {})
    catalog = pgbackrest.collect_catalog(
        spec, stanza_info, time.time(), error=error, archive=archive
    )
    pgbackrest.save_catalog(catalog)
    return 0 if catalog["error"] is None else 1


def archive_state() -> dict[str, tp.Any] | None:
    """When WAL was archived and when archiving failed last, as epoch seconds."""
    try:
        with psycopg.connect("user=postgres", autocommit=True) as conn:
            row = conn.execute(
                "SELECT extract(epoch FROM last_archived_time), "
                "extract(epoch FROM last_failed_time) FROM pg_stat_archiver"
            ).fetchone()
    except psycopg.Error as e:
        LOG.error("Archiving state can't be read: %s", e)
        return None
    if row is None:
        return None
    last_archived, last_failed = row
    return {
        "last_archived_at": None if last_archived is None else float(last_archived),
        "last_failed_at": None if last_failed is None else float(last_failed),
    }


if __name__ == "__main__":
    sys.exit(main())
