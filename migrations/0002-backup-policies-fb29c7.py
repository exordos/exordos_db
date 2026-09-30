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
        self._depends = ["0001-add-instance-backup-and-restore-0cd16f.py"]

    @property
    def migration_id(self):
        return "fb29c79c-1a78-4142-af69-6b81cd0dcc9a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            # The backups set on instances are dropped, not moved: nobody
            # took them in production
            "ALTER TABLE postgres_instances DROP COLUMN IF EXISTS backup;",
            "ALTER TABLE postgres_instances DROP COLUMN IF EXISTS backup_status;",
            """\
CREATE TABLE postgres_backup_policies (
    uuid UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    project_id UUID NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    instance UUID NOT NULL,
    storage JSONB NOT NULL,
    full_interval_hours INTEGER NOT NULL,
    incr_interval_hours INTEGER NOT NULL,
    retention_full INTEGER NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'NEW',
    error TEXT,
    FOREIGN KEY (instance) REFERENCES postgres_instances(uuid)
);
""",
            # One policy per instance until they go to different
            # repositories
            """\
CREATE UNIQUE INDEX postgres_backup_policies_instance_idx
    ON postgres_backup_policies (instance);
""",
            """\
CREATE INDEX postgres_backup_policies_project_id_idx
    ON postgres_backup_policies (project_id);
""",
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "postgres_backup_policies")
        expressions = [
            "ALTER TABLE postgres_instances ADD COLUMN IF NOT EXISTS backup JSONB;",
            "ALTER TABLE postgres_instances ADD COLUMN IF NOT EXISTS backup_status JSONB;",
        ]

        for expression in expressions:
            session.execute(expression)


migration_step = MigrationStep()
