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

import pytest

from exordos_db.cmd import pg_restore
from exordos_db.common import pgbackrest

SPEC = {
    "stanza": "38fc8bbb-0826-4287-9651-9745df402ded",
    "options": {"repo1-s3-key-secret": "secret"},
    "target_time": "2026-09-14 10:27:41.638125+00",
}


class Restores(list):
    """The restores run, the backup set is chosen by `choose`."""


@pytest.fixture
def restores(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pgbackrest, "RESTORE_STATE_FILE", str(tmp_path / "restore_state.json")
    )
    monkeypatch.setattr(pgbackrest, "load_spec", lambda path: SPEC)
    monkeypatch.setattr(pgbackrest, "write_restore_config", lambda spec: None)
    restores = Restores()
    restores.choose = lambda stanza, target_time: "20260914-151105F"
    monkeypatch.setattr(
        pgbackrest,
        "restore_backup_set",
        lambda stanza, target_time: restores.choose(stanza, target_time),
    )
    monkeypatch.setattr(
        pgbackrest, "run", lambda stanza, *args, timeout: restores.append(args)
    )
    return restores


@pytest.fixture
def state():
    return pgbackrest.load_restore_state


def test_restored_cluster_is_left_to_recover(restores, state):
    assert pg_restore.main() == 0

    assert len(restores) == 1
    assert state() == {
        "source": [SPEC["stanza"], SPEC["target_time"]],
        "attempts": 1,
        "phase": "recovering",
        "error": None,
    }


def test_bootstrap_after_a_restore_is_another_attempt(restores, state):
    # PostgreSQL stops when the archive ends before the target, and Patroni
    # bootstraps again
    pg_restore.main()

    assert pg_restore.main() == 0

    assert len(restores) == 2
    assert state()["attempts"] == 2


def test_successful_attempt_clears_the_error_of_a_failed_one(restores, state):
    # E.g. the storage was unreachable for a moment: the instance mustn't be
    # ERROR while the retry restores it
    def unreachable(stanza, target_time):
        raise pgbackrest.PgBackRestError("unable to connect to storage")

    restores.choose = unreachable
    pg_restore.main()
    restores.choose = lambda stanza, target_time: "20260914-151105F"

    assert pg_restore.main() == 0

    assert state()["phase"] == "recovering"
    assert state()["error"] is None


def test_restore_error_is_recorded(restores, state):
    def no_backup(stanza, target_time):
        raise pgbackrest.PgBackRestError("No backup to recover to ... from")

    restores.choose = no_backup

    assert pg_restore.main() == 1

    assert restores == []
    assert state()["phase"] == "failed"
    assert state()["error"] == "No backup to recover to ... from"


def test_attempts_are_bounded(restores, state):
    for _ in range(pg_restore.ATTEMPTS):
        pg_restore.main()

    assert pg_restore.main() == 1

    assert len(restores) == pg_restore.ATTEMPTS
    assert state()["phase"] == "failed"
    assert state()["error"] == pg_restore.RECOVERY_FAILED


def test_another_source_starts_over(restores, state, monkeypatch):
    for _ in range(pg_restore.ATTEMPTS):
        pg_restore.main()
    monkeypatch.setattr(
        pgbackrest, "load_spec", lambda path: {**SPEC, "target_time": None}
    )

    assert pg_restore.main() == 0

    assert state()["attempts"] == 1
    assert state()["error"] is None
