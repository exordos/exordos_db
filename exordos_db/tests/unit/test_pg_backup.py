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

from exordos_db.cmd import pg_backup
from exordos_db.common import pgbackrest


class PausedPatroni:
    def is_primary(self):
        return True

    def config_get(self):
        return {"pause": True}


def test_no_backup_on_a_paused_cluster(monkeypatch):
    # A paused cluster may be rolled back: a backup would copy the data while
    # the rollback replaces it
    commands = []
    monkeypatch.setattr(pgbackrest, "load_spec", lambda: {"stanza": "s"})
    monkeypatch.setattr(pgbackrest, "stanza_ready", lambda spec: True)
    monkeypatch.setattr(
        pgbackrest, "run", lambda stanza, *args, **kwargs: commands.append(args)
    )
    monkeypatch.setattr(pg_backup.pg, "PatroniClient", PausedPatroni)

    assert pg_backup.main() == 0

    assert commands == []
