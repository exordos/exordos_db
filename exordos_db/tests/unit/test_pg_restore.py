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

import json

import pytest

from exordos_db.cmd import pg_restore
from exordos_db.common import pgbackrest

SPEC = {
    "stanza": "38fc8bbb-0826-4287-9651-9745df402ded",
    "options": {"repo1-s3-key-secret": "secret"},
    "target_time": "2026-09-14 10:27:41.638125+00",
}


class Restores(list):
    """The restores run; `error` makes the next ones fail."""

    error = None


@pytest.fixture
def restores(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pgbackrest, "RESTORE_STATE_FILE", str(tmp_path / "restore_state.json")
    )
    monkeypatch.setattr(pgbackrest, "load_spec", lambda path: SPEC)
    monkeypatch.setattr(pgbackrest, "write_restore_config", lambda spec: None)
    monkeypatch.setattr(
        pg_restore, "PATRONI_CACHE_FILE", str(tmp_path / "patroni.dynamic.json")
    )
    restores = Restores()

    def run(stanza, *args, timeout):
        if restores.error is not None:
            raise pgbackrest.PgBackRestError(restores.error)
        restores.append(args)

    monkeypatch.setattr(pgbackrest, "run", run)
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


def test_successful_attempt_clears_the_error_of_a_failed_one(restores, state):
    # E.g. the storage was unreachable for a moment: the instance mustn't be
    # ERROR while the retry restores it
    restores.error = "unable to connect to storage"
    pg_restore.main()
    restores.error = None

    assert pg_restore.main() == 0

    assert state()["phase"] == "recovering"
    assert state()["error"] is None


def test_error_is_final_after_the_last_attempt(restores, state):
    restores.error = "No backup to recover to ... from"

    assert pg_restore.main() == 1
    # Patroni bootstraps again: nothing to report as failed yet
    assert state()["phase"] == "restoring"
    assert state()["error"] is None

    for _ in range(pg_restore.ATTEMPTS - 1):
        pg_restore.main()

    assert restores == []
    assert state()["phase"] == "failed"
    assert "No backup to recover to ... from" in state()["error"]


def test_attempts_are_bounded(restores, state):
    restores.error = "unable to connect to storage"
    for _ in range(pg_restore.ATTEMPTS):
        pg_restore.main()
    restores.error = None

    assert pg_restore.main() == 1

    assert restores == []
    assert state()["phase"] == "failed"
    assert "unable to connect to storage" in state()["error"]


def test_another_source_starts_over(restores, state, monkeypatch):
    restores.error = "No backup to recover to ... from"
    for _ in range(pg_restore.ATTEMPTS):
        pg_restore.main()
    restores.error = None
    monkeypatch.setattr(
        pgbackrest, "load_spec", lambda path: {**SPEC, "target_time": None}
    )

    assert pg_restore.main() == 0

    assert state()["attempts"] == 1
    assert state()["error"] is None


def test_source_archiving_is_disabled(restores):
    # The source's configuration, cached by its Patroni, comes with the data
    source = {
        "ttl": 30,
        "postgresql": {
            "parameters": {
                "archive_command": "pgbackrest --stanza=source archive-push %p",
                "max_connections": 500,
            }
        },
    }
    with open(pg_restore.PATRONI_CACHE_FILE, "w") as f:
        json.dump(source, f)

    assert pg_restore.main() == 0

    with open(pg_restore.PATRONI_CACHE_FILE) as f:
        restored = json.load(f)
    assert restored["postgresql"]["parameters"] == {
        "archive_command": ":",
        "max_connections": 500,
    }
    assert restored["ttl"] == 30
