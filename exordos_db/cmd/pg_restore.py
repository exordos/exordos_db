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
"""Restore a pgBackRest backup into an empty data directory.

Run by Patroni as the custom bootstrap method of a restored cluster.

Patroni bootstraps again after a failed restore into the empty data
directory, so the progress is kept in a state file for the agent to report,
and the attempts are bounded.
"""

import json
import logging
import sys
import time

from exordos_db.common import pgbackrest

LOG = logging.getLogger(__name__)

# The spec comes as a separate config and may land after patroni.yml
SPEC_WAIT_SECONDS = 1800
# Patroni caches its dynamic configuration in the data directory
PATRONI_CACHE_FILE = f"{pgbackrest.PG_DATA_DIR}/patroni.dynamic.json"
ATTEMPTS = 3

RESTORING = "restoring"
RECOVERING = "recovering"
FAILED = "failed"


def disable_source_archiving() -> None:
    """Keep the restored cluster from archiving into the source's stanza.

    The backup brings the dynamic configuration of the source cached by its
    Patroni, archive_command included. Patroni loads it when the DCS has
    none, which is the case once a bootstrap failed after the restore: the
    recovery ending before the target does, and Patroni then runs the data
    it finds as is.
    """
    try:
        with open(PATRONI_CACHE_FILE) as f:
            config = json.load(f)
    except FileNotFoundError:
        return
    parameters = config.setdefault("postgresql", {}).setdefault("parameters", {})
    parameters["archive_command"] = pgbackrest.DISABLED_ARCHIVE_COMMAND
    with open(PATRONI_CACHE_FILE, "w") as f:
        json.dump(config, f)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    deadline = time.monotonic() + SPEC_WAIT_SECONDS
    while (spec := pgbackrest.load_spec(pgbackrest.RESTORE_SPEC_FILE)) is None:
        if time.monotonic() > deadline:
            LOG.error("No restore spec in %s", pgbackrest.RESTORE_SPEC_FILE)
            return 1
        LOG.info("Waiting for %s", pgbackrest.RESTORE_SPEC_FILE)
        time.sleep(5)

    source = [spec["stanza"], spec["target_time"]]
    state = pgbackrest.load_restore_state()
    if state is None or state.get("source") != source:
        state = {"source": source, "attempts": 0, "error": None}
    if state["attempts"] >= ATTEMPTS:
        LOG.error("Restore of %s failed %s times", spec["stanza"], state["attempts"])
        pgbackrest.save_restore_state({**state, "phase": FAILED})
        return 1

    # An error of an earlier attempt isn't reported while this one goes on
    state = {**state, "attempts": state["attempts"] + 1, "error": None}
    pgbackrest.save_restore_state({**state, "phase": RESTORING})
    LOG.info(
        "Restoring stanza %s to %s",
        spec["stanza"],
        spec["target_time"] or "the end of the archive",
    )
    try:
        pgbackrest.write_restore_config(spec)
        pgbackrest.run(spec["stanza"], *pgbackrest.restore_args(spec), timeout=None)
        disable_source_archiving()
    except Exception as e:
        LOG.exception("Restore of %s failed", spec["stanza"])
        # Patroni bootstraps again until the attempts are over, the failure
        # is final only then
        if state["attempts"] >= ATTEMPTS:
            error = pgbackrest.error_text(e)
            pgbackrest.save_restore_state({**state, "phase": FAILED, "error": error})
        return 1

    pgbackrest.save_restore_state({**state, "phase": RECOVERING})
    LOG.info("Restore of %s is done, recovery is up to PostgreSQL", spec["stanza"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
