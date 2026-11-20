# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstarctMigrationStep):
    def __init__(self):
        self._depends = ["0003-add-patroni-password-d7e854.py"]

    @property
    def migration_id(self):
        return "b4c31de7-955a-4a12-b9f5-97bb45742558"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "ALTER TABLE postgres_versions ADD COLUMN version_ref VARCHAR(4096) NOT NULL DEFAULT '';"
        )
        session.execute(
            "ALTER TABLE postgres_instances ADD COLUMN version_ref VARCHAR(4096) NOT NULL DEFAULT '';"
        )
        session.execute("""
UPDATE postgres_versions SET version_ref = 'urn:exordos:postgres:' || uuid::text || ':' ||
    CASE WHEN image LIKE 'urn:images:%' THEN substring(image FROM 12) ELSE image END;
UPDATE postgres_instances AS i SET version_ref = v.version_ref, updated_at = now()
    FROM postgres_versions AS v WHERE i.version = v.uuid;
""")

    def downgrade(self, session):
        session.execute("ALTER TABLE postgres_instances DROP COLUMN version_ref;")
        session.execute("ALTER TABLE postgres_versions DROP COLUMN version_ref;")


migration_step = MigrationStep()
