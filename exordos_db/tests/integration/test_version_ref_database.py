# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Opt-in migration checks in a disposable PostgreSQL schema."""

import importlib.util
import os
from pathlib import Path
from unittest import mock
import uuid

import gcl_sdk
import psycopg
from psycopg import sql
import pytest
from restalchemy.storage.sql import engines

from exordos_db.common import version_ref
from exordos_db.infra.services.builder import CoreInfraBuilder
from exordos_db.tests.unit.test_version_ref import _instance
from exordos_db.user_api.dm import models


@pytest.fixture
def database():
    url = os.environ.get("EXORDOS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set EXORDOS_TEST_POSTGRES_URL for isolated schema tests")
    schema = "version_ref_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    previous = engines.engine_factory._engines.get("default")
    engines.engine_factory.configure_factory(
        url,
        config={
            "min_size": 1,
            "max_size": 4,
            "kwargs": {"options": "-c search_path=" + schema},
        },
    )
    engine = engines.engine_factory.get_engine()
    try:
        with engine.session_manager() as session:
            for file in sorted(
                Path(__file__).parents[3].joinpath("migrations").glob("[0-9]*.py")
            ):
                spec = importlib.util.spec_from_file_location(
                    "migration_" + file.stem, file
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.migration_step.upgrade(session)
        yield engine
    finally:
        engine._pool.close()
        if previous is None:
            engines.engine_factory._engines.pop("default", None)
        else:
            engines.engine_factory._engines["default"] = previous
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.parametrize("image", ["urn:images:new", "https://host:8443/image.raw"])
def test_version_ref_migration_backfill_and_downgrade(image):
    url = os.environ.get("EXORDOS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set EXORDOS_TEST_POSTGRES_URL for isolated schema tests")
    # The entire schema, including DDL and downgrade, is rolled back on exit.
    with psycopg.connect(url) as conn:
        try:
            schema = "version_ref_test_" + uuid.uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            migration = None
            for path in sorted(
                Path(__file__).parents[3].joinpath("migrations").glob("[0-9]*.py")
            ):
                spec = importlib.util.spec_from_file_location(path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if path.name == "0004-version-ref-b4c31d.py":
                    migration = module.migration_step
                    break
                module.migration_step.upgrade(conn)
            assert migration is not None
            version_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO postgres_versions (uuid, name, image, created_at, updated_at) "
                "VALUES (%s, 'existing-version', %s, now(), now())",
                (version_id, image),
            )
            conn.execute(
                "INSERT INTO postgres_instances "
                "(uuid, name, project_id, cpu, ram, disk_size, nodes_number, "
                "sync_replica_number, version, created_at, updated_at) "
                "VALUES (%s, 'existing', %s, 1, 1024, 10, 1, 0, %s, now(), now())",
                (uuid.uuid4(), uuid.uuid4(), version_id),
            )
            migration.upgrade(conn)
            expected = version_ref.build_version_ref(str(version_id), image)
            assert conn.execute(
                "SELECT version_ref FROM postgres_versions"
            ).fetchone() == (expected,)
            assert conn.execute(
                "SELECT version_ref FROM postgres_instances"
            ).fetchone() == (expected,)
            migration.downgrade(conn)
            conn.execute(
                "UPDATE postgres_versions SET image = 'urn:images:after-downgrade'"
            )
            # Re-applying after downgrade must backfill the current image.
            migration.upgrade(conn)
            assert conn.execute(
                "SELECT version_ref FROM postgres_versions WHERE uuid = %s",
                (version_id,),
            ).fetchone() == (
                version_ref.build_version_ref(
                    str(version_id), "urn:images:after-downgrade"
                ),
            )
        finally:
            conn.rollback()


@pytest.mark.parametrize("image", ["urn:images:new", "https://host:8443/image.raw"])
def test_startup_repairs_downgraded_writes_without_changing_instances(database, image):
    with database.session_manager() as session:
        # Real audit writes are part of the repair transaction.
        for name in ("0007-init-audit-events-4f3a2b.py", "0008-audit-actor-a83d71.py"):
            path = Path(gcl_sdk.__file__).parent / "migrations" / name
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.migration_step.upgrade(session)
        stale = models.PGVersion(name="stale", image="urn:images:old")
        missing = models.PGVersion(name="missing", image=image)
        correct = models.PGVersion(name="correct", image=image)
        for version in (stale, missing, correct):
            version.insert(session=session)
        instance = _instance(version=stale)
        instance.insert(session=session)
        # Reproduce an old package writing after 0004 has already been applied.
        session.execute(
            "UPDATE postgres_versions SET image = %s WHERE uuid = %s",
            (image, stale.uuid),
        )
        session.execute(
            "UPDATE postgres_versions SET version_ref = '' WHERE uuid = %s",
            (missing.uuid,),
        )
        before = {
            v.uuid: v.updated_at
            for v in models.PGVersion.objects.get_all(session=session)
        }
        instance_before = session.execute("SELECT * FROM postgres_instances").fetchall()
        audits_before = session.execute(
            "SELECT count(*) AS n FROM gcl_sdk_audit_events"
        ).fetchone()["n"]

    service = object.__new__(CoreInfraBuilder)
    # Only registration needs Core; run the real startup repair and ORM writes.
    with mock.patch.object(CoreInfraBuilder, "register_ua_service"):
        service._setup()
        with database.session_manager() as session:
            versions = models.PGVersion.objects.get_all(session=session)
            for version in versions:
                assert version.image == image
                assert version.version_ref == version_ref.build_version_ref(
                    str(version.uuid), image
                )
                if version.uuid == correct.uuid:
                    assert version.updated_at == before[version.uuid]
                else:
                    assert version.updated_at > before[version.uuid]
            after = {v.uuid: v.updated_at for v in versions}
            assert (
                session.execute("SELECT * FROM postgres_instances").fetchall()
                == instance_before
            )
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM gcl_sdk_audit_events"
                ).fetchone()["n"]
                == audits_before + 2
            )

        service._setup()
        with database.session_manager() as session:
            assert {
                v.uuid: v.updated_at
                for v in models.PGVersion.objects.get_all(session=session)
            } == after
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM gcl_sdk_audit_events"
                ).fetchone()["n"]
                == audits_before + 2
            )
