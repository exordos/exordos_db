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

Patroni bootstraps again after a failed attempt, including one whose restore
succeeded but whose recovery didn't: PostgreSQL stops when the archive ends
before the target. The progress is kept in a state file for the agent to
report, and the attempts are bounded.
"""

import logging
import sys
import time

from exordos_db.common import pgbackrest

LOG = logging.getLogger(__name__)

# The spec comes as a separate config and may land after patroni.yml
SPEC_WAIT_SECONDS = 1800
ATTEMPTS = 3

RESTORING = "restoring"
RECOVERING = "recovering"
FAILED = "failed"

RECOVERY_FAILED = (
    "PostgreSQL stopped before it recovered to the target, the target may "
    "be past the end of the archive"
)


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
        error = state.get("error")
        if state.get("phase") == RECOVERING:
            # The restore of the last attempt succeeded
            error = RECOVERY_FAILED
        pgbackrest.save_restore_state({**state, "phase": FAILED, "error": error})
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
        backup_set = pgbackrest.restore_backup_set(spec["stanza"], spec["target_time"])
        pgbackrest.run(
            spec["stanza"], *pgbackrest.restore_args(spec, backup_set), timeout=None
        )
    except Exception as e:
        LOG.exception("Restore of %s failed", spec["stanza"])
        error = pgbackrest.error_text(e)
        pgbackrest.save_restore_state({**state, "phase": FAILED, "error": error})
        return 1

    # A recovery that doesn't reach the target shows up as the next attempt
    pgbackrest.save_restore_state({**state, "phase": RECOVERING})
    LOG.info("Restore of %s is done, recovery is up to PostgreSQL", spec["stanza"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
