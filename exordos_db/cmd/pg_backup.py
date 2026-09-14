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

from exordos_db.agent.universal.drivers import pg
from exordos_db.common import pgbackrest

LOG = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    spec = pgbackrest.load_spec()
    if spec is None:
        LOG.info("Backups are disabled")
        return 0

    if not pg.PatroniClient().is_primary():
        LOG.info("Not a primary node, nothing to do")
        return 0

    # The agent creates the stanza and turns archiving on first
    if not pgbackrest.stanza_ready(spec):
        LOG.info("Stanza %s is not ready yet", spec["stanza"])
        return 0

    try:
        _backup_if_due(spec)
    except (pgbackrest.PgBackRestError, subprocess.TimeoutExpired) as e:
        LOG.error("Backup of %s failed: %s", spec["stanza"], e)
        # Reported to the API until a run succeeds
        pgbackrest.save_backup_error(e)
        return 1
    pgbackrest.clear_backup_error()
    return 0


def _backup_if_due(spec: dict) -> None:
    stanza = spec["stanza"]
    info = json.loads(pgbackrest.run(stanza, "--output=json", "info"))
    backups = info[0].get("backup", []) if info else []

    backup_type = pgbackrest.choose_backup_type(backups, spec["schedule"], time.time())
    # WAL lost to a failover or dropped while the storage was unreachable:
    # nothing past it can be restored until a backup is taken past it
    if backup_type is None and (missing := pgbackrest.archive_gap(stanza, info[0])):
        LOG.warning(
            "WAL %s is missing from the archive, taking a backup past it",
            ", ".join(missing),
        )
        backup_type = "incr"
    if backup_type is None:
        LOG.info("No backup is due")
        return

    LOG.info("Taking a %s backup of %s", backup_type, stanza)
    pgbackrest.run(stanza, f"--type={backup_type}", "backup", timeout=None)
    LOG.info("The %s backup of %s is done", backup_type, stanza)


if __name__ == "__main__":
    sys.exit(main())
