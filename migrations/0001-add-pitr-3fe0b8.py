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
        return "3fe0b8f6-6908-4efe-a498-d020f25ce143"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """\
ALTER TABLE postgres_instances
    ADD COLUMN backup JSONB,
    ADD COLUMN restore_from JSONB,
    ADD COLUMN roles_imported BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN rollback_revision INT;
""",
            # Users imported from a restored cluster have only a hash
            "ALTER TABLE postgres_users ALTER COLUMN password DROP NOT NULL;",
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        expressions = [
            "UPDATE postgres_users SET password = '' WHERE password IS NULL;",
            "ALTER TABLE postgres_users ALTER COLUMN password SET NOT NULL;",
            """\
ALTER TABLE postgres_instances
    DROP COLUMN IF EXISTS rollback_revision,
    DROP COLUMN IF EXISTS roles_imported,
    DROP COLUMN IF EXISTS restore_from,
    DROP COLUMN IF EXISTS backup;
""",
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
