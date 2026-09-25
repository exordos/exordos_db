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
        self._depends = ["0002-add-instance-restore-e7a2c9.py"]

    @property
    def migration_id(self):
        return "a11c730a-1be8-40c6-bd33-367fc781c5e6"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "ALTER TABLE postgres_instances ADD COLUMN IF NOT EXISTS backup_status JSONB;"
        )

    def downgrade(self, session):
        session.execute(
            "ALTER TABLE postgres_instances DROP COLUMN IF EXISTS backup_status;"
        )


migration_step = MigrationStep()
