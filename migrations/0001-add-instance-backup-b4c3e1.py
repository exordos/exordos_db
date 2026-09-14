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

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstarctMigrationStep):
    def __init__(self):
        self._depends = ["0000-init-63a338.py"]

    @property
    def migration_id(self):
        return "b4c3e1d2-7f5a-4e8b-9c1d-2a6f8e0b3d57"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "ALTER TABLE postgres_instances ADD COLUMN IF NOT EXISTS backup JSONB;"
        )

    def downgrade(self, session):
        session.execute("ALTER TABLE postgres_instances DROP COLUMN IF EXISTS backup;")


migration_step = MigrationStep()
