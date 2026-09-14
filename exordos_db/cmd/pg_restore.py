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
"""

import logging
import sys
import time

from exordos_db.common import pgbackrest

LOG = logging.getLogger(__name__)

# The spec comes as a separate config and may land after patroni.yml
SPEC_WAIT_SECONDS = 1800


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

    pgbackrest.write_restore_config(spec)
    LOG.info(
        "Restoring stanza %s to %s",
        spec["stanza"],
        spec["target_time"] or "the end of the archive",
    )
    pgbackrest.run(spec["stanza"], *pgbackrest.restore_args(spec), timeout=None)
    LOG.info("Restore of %s is done, recovery is up to PostgreSQL", spec["stanza"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
