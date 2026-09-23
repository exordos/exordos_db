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
import time
import types

import pytest

from exordos_db.cmd import pg_backup
from exordos_db.common import pgbackrest

SPEC = {
    "stanza": "st",
    "schedule": {"full_interval_hours": 168, "incr_interval_hours": 24},
}


@pytest.fixture
def backups(monkeypatch):
    """The backups taken; `gap` is what the archive lacks."""
    taken = types.SimpleNamespace(types=[], gap=[])
    info = [
        {
            "backup": [
                {"type": "full", "timestamp": {"stop": time.time() - 3600}},
            ]
        }
    ]

    def run(stanza, *args, timeout=600):
        if args[-1] == "info":
            return json.dumps(info)
        taken.types.append(args[0])
        return ""

    monkeypatch.setattr(pgbackrest, "load_spec", lambda: SPEC)
    monkeypatch.setattr(pgbackrest, "stanza_ready", lambda spec: True)
    monkeypatch.setattr(pgbackrest, "run", run)
    monkeypatch.setattr(pgbackrest, "archive_gap", lambda stanza, info: taken.gap)
    monkeypatch.setattr(
        pg_backup.pg,
        "PatroniClient",
        lambda: types.SimpleNamespace(is_primary=lambda: True),
    )
    return taken


def test_no_backup_is_due(backups):
    assert pg_backup.main() == 0

    assert backups.types == []


def test_missing_wal_takes_a_backup(backups):
    backups.gap = ["00000003000000000000005E"]

    assert pg_backup.main() == 0

    assert backups.types == ["--type=incr"]
