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
"""Backups the nodes find in a repository and the rows the API shows."""

import contextlib
import datetime
import json
import subprocess
import types
import uuid

from gcl_sdk.agents.universal.dm import models as ua_models
import pytest
from restalchemy.storage.sql import orm

from exordos_db.agent.universal.drivers import pg
from exordos_db.cmd import pg_backup
from exordos_db.common import endpoints
from exordos_db.common import pgbackrest
from exordos_db.paas.services import builder
from exordos_db.user_api.dm import backups
from exordos_db.user_api.dm import models

STANZA = "84022dd1-a9db-41de-9490-9ab07976d5a2"
SNAPSHOTS = f"{STANZA}-rollbacks"
REPOSITORY = uuid.UUID("5a0e2c1b-7d3f-4e8a-9b6c-1f2e3d4c5b6a")

# Timeline 2 forked from timeline 1 after the first backup was taken, and
# timeline 3 from timeline 2 before the second one
HISTORY_3 = "1\t0/501ACA0\tbefore 2026-09-14 15:11:18+00\n2\t0/5017830\tbefore x\n"


def _backup(label, timeline, stop, error=False, backup_type="full"):
    return {
        "label": label,
        "type": backup_type,
        "error": error,
        "archive": {"start": f"{timeline:08X}0000000000000004"},
        "lsn": {"start": "0/4000028", "stop": "0/7000028"},
        "timestamp": {"start": stop - 60, "stop": stop},
        "database": {"id": 1, "repo-key": 1},
        "info": {"size": 1000, "repository": {"size": 100}},
    }


BEFORE_ROLLBACKS = {
    **_backup("20260914-151105F", 1, 1789398667),
    "lsn": {"start": "0/4000028", "stop": "0/4000100"},
}
ABANDONED = _backup("20260914-151438F", 2, 1789398881)
ON_LATEST = _backup("20260914-152000I", 3, 1789399100, backup_type="incr")
STANZA_INFO = {
    "archive": [{"database": {"id": 1}, "id": "18-1"}],
    "backup": [ON_LATEST, BEFORE_ROLLBACKS, ABANDONED],
}
SNAPSHOT = {
    "label": "20260914-151500F",
    "type": "full",
    "error": False,
    "annotation": {"exordos-before-revision": "2"},
    "timestamp": {"start": 1789398800, "stop": 1789398900},
    "database": {"id": 1, "repo-key": 1},
    "info": {"size": 2000, "repository": {"size": 200}},
}


@pytest.fixture
def repo(monkeypatch):
    """The repository as pgBackRest shows it, recording the commands."""
    state = types.SimpleNamespace(calls=[], snapshots=[{"backup": [SNAPSHOT]}])

    def run(stanza, *args, timeout=600):
        state.calls.append((stanza, args))
        if "info" in args:
            if stanza == SNAPSHOTS:
                if isinstance(state.snapshots, Exception):
                    raise state.snapshots
                return json.dumps(state.snapshots)
            return json.dumps([STANZA_INFO])
        if "repo-ls" in args:
            return json.dumps({"00000002.history": {}, "00000003.history": {}})
        if "repo-get" in args:
            return HISTORY_3
        return ""

    monkeypatch.setattr(pgbackrest, "run", run)
    return state


class TestCatalog:
    def test_backups_off_the_latest_history_arent_restorable(self, repo):
        entries = pgbackrest.catalog_backups(STANZA, STANZA_INFO)

        assert [(e["label"], e["restorable"]) for e in entries] == [
            (BEFORE_ROLLBACKS["label"], True),
            (ABANDONED["label"], False),
            (ON_LATEST["label"], True),
        ]
        assert entries[2] == {
            "label": ON_LATEST["label"],
            "type": "incr",
            "started_at": 1789399040,
            "finished_at": 1789399100,
            "size": 1000,
            "stored_size": 100,
            "before_revision": None,
            "error": False,
            "restorable": True,
        }
        # The history is read with the default config, once
        assert [args[-2] for _, args in repo.calls] == ["repo-ls", "repo-get"]

    def test_failed_backup_isnt_restorable(self, repo):
        failed = _backup("20260914-152500F", 3, 1789399400, error=True)

        [entry] = pgbackrest.catalog_backups(
            STANZA, {**STANZA_INFO, "backup": [failed]}
        )

        assert entry["error"] is True
        assert entry["restorable"] is False

    def test_newest_backups_only(self, repo, monkeypatch):
        monkeypatch.setattr(pgbackrest, "CATALOG_MAX_BACKUPS", 2)

        entries = pgbackrest.catalog_backups(STANZA, STANZA_INFO)

        assert [e["label"] for e in entries] == [
            ABANDONED["label"],
            ON_LATEST["label"],
        ]

    def test_state_kept_before_a_rollback(self, repo):
        [entry] = pgbackrest.catalog_backups(
            SNAPSHOTS, {"backup": [SNAPSHOT]}, snapshots=True
        )

        # An offline copy replays none of the archive, so no history is read
        assert entry["before_revision"] == 2
        assert entry["restorable"] is True
        assert repo.calls == []

    def test_collect(self, repo):
        spec = {"stanza": STANZA, "repository": str(REPOSITORY)}

        catalog = pgbackrest.collect_catalog(spec, STANZA_INFO, 1789399200.5)

        assert catalog["repository"] == str(REPOSITORY)
        assert catalog["stanza"] == STANZA
        assert catalog["collected_at"] == 1789399200.5
        assert len(catalog["backups"][STANZA]) == 3
        assert [e["label"] for e in catalog["backups"][SNAPSHOTS]] == [
            SNAPSHOT["label"]
        ]

    def test_missing_snapshot_stanza_is_empty(self, repo):
        repo.snapshots = [{"status": {"code": 1, "message": "missing stanza path"}}]
        spec = {"stanza": STANZA, "repository": str(REPOSITORY)}

        catalog = pgbackrest.collect_catalog(spec, STANZA_INFO, 1)

        assert catalog["backups"][SNAPSHOTS] == []

    def test_unreadable_stanza_is_left_out(self, repo):
        # Reported empty, it would make the control plane forget the backups
        repo.snapshots = pgbackrest.PgBackRestError("unable to list")
        spec = {"stanza": STANZA, "repository": str(REPOSITORY)}

        catalog = pgbackrest.collect_catalog(spec, STANZA_INFO, 1)

        assert list(catalog["backups"]) == [STANZA]
        assert "unable to list" in catalog["error"]

    def test_hanging_repository_is_reported(self, repo):
        repo.snapshots = subprocess.TimeoutExpired(["pgbackrest", "info"], 600)
        spec = {"stanza": STANZA, "repository": str(REPOSITORY)}

        catalog = pgbackrest.collect_catalog(spec, STANZA_INFO, 1)

        assert list(catalog["backups"]) == [STANZA]
        assert "timed out" in catalog["error"]

    def test_stanza_not_read_lists_nothing(self, repo):
        spec = {"stanza": STANZA, "repository": str(REPOSITORY)}

        catalog = pgbackrest.collect_catalog(
            spec, None, 1, error="info failed", archive={"last_archived_at": 5}
        )

        assert catalog["backups"] == {}
        assert catalog["error"] == "info failed"
        assert catalog["archive"] == {"last_archived_at": 5}
        assert repo.calls == []


class Patroni:
    def is_primary(self):
        return True

    def config_get(self):
        return {}


class Replica(Patroni):
    def is_primary(self):
        return False


def test_replica_drops_the_catalog_it_collected_as_the_primary(monkeypatch):
    removed = []
    monkeypatch.setattr(pgbackrest, "load_spec", lambda: {"stanza": STANZA})
    monkeypatch.setattr(pgbackrest, "remove_catalog", lambda: removed.append(True))
    monkeypatch.setattr(pg_backup.pg, "PatroniClient", Replica)

    assert pg_backup.main() == 0

    assert removed == [True]


class TestBackupTimer:
    @pytest.fixture
    def timer(self, repo, monkeypatch):
        saved = []
        spec = {
            "stanza": STANZA,
            "repository": str(REPOSITORY),
            "schedule": {"full_interval_hours": 168, "incr_interval_hours": 24},
        }
        monkeypatch.setattr(pgbackrest, "load_spec", lambda: spec)
        monkeypatch.setattr(pgbackrest, "stanza_ready", lambda spec: True)
        monkeypatch.setattr(pgbackrest, "save_catalog", saved.append)
        monkeypatch.setattr(pg_backup.rollback, "applied_at", lambda: None)
        monkeypatch.setattr(pg_backup.pg, "PatroniClient", Patroni)
        monkeypatch.setattr(
            pg_backup, "archive_state", lambda: {"last_archived_at": 1789399000}
        )
        return saved

    def test_catalog_is_saved_when_no_backup_is_due(self, timer, repo, monkeypatch):
        monkeypatch.setattr(pg_backup.time, "time", lambda: 1789399200)

        assert pg_backup.main() == 0

        [catalog] = timer
        assert catalog["collected_at"] == 1789399200
        assert not any("backup" in args for _, args in repo.calls)

    def test_catalog_is_read_again_after_a_backup(self, timer, repo, monkeypatch):
        monkeypatch.setattr(pg_backup.time, "time", lambda: 1789399200 + 30 * 3600)

        assert pg_backup.main() == 0

        commands = [(s, args[-1]) for s, args in repo.calls if s == STANZA]
        assert commands[:3] == [(STANZA, "info"), (STANZA, "backup"), (STANZA, "info")]
        [catalog] = timer
        assert catalog["error"] is None
        assert catalog["archive"] == {"last_archived_at": 1789399000}

    def test_failed_backup_is_reported(self, timer, repo, monkeypatch):
        monkeypatch.setattr(pg_backup.time, "time", lambda: 1789399200 + 30 * 3600)
        run = pgbackrest.run

        def failing_backup(stanza, *args, **kwargs):
            if "backup" in args:
                raise pgbackrest.PgBackRestError("backup failed with code 45")
            return run(stanza, *args, **kwargs)

        monkeypatch.setattr(pgbackrest, "run", failing_backup)

        assert pg_backup.main() == 1

        [catalog] = timer
        assert "code 45" in catalog["error"]
        # What was listed before the backup is still reported
        assert len(catalog["backups"][STANZA]) == 3

    def test_unreachable_repository_is_reported(self, timer, monkeypatch):
        def unreachable(stanza, *args, **kwargs):
            raise pgbackrest.PgBackRestError("unable to connect")

        monkeypatch.setattr(pgbackrest, "run", unreachable)

        assert pg_backup.main() == 1

        [catalog] = timer
        assert "unable to connect" in catalog["error"]
        # Rather than an empty list the control plane would take for no backups
        assert catalog["backups"] == {}


SPEC = {"stanza": STANZA, "repository": str(REPOSITORY)}
CATALOG = {"stanza": STANZA, "repository": str(REPOSITORY), "backups": {}}


def _node(primary=True, node_uuid=None):
    value = {
        "uuid": str(node_uuid or uuid.uuid4()),
        "name": "demo",
        "nodes_number": 1,
        "sync_replica_number": 0,
        "users": {},
        "databases": {},
        "backup": None,
    }
    node = pg.PGInstance.from_ua_resource(
        ua_models.Resource.from_value(value, "pg_instance_node")
    )
    node.c = types.SimpleNamespace(
        pclient=types.SimpleNamespace(is_primary=lambda ttl_hash=None: primary)
    )
    return node


class TestAgentReport:
    @pytest.mark.parametrize(
        "primary, spec, catalog, reported",
        [
            (True, SPEC, CATALOG, True),
            # A former primary may keep an old one
            (False, SPEC, CATALOG, False),
            (True, None, CATALOG, False),
            (True, SPEC, None, False),
            # Collected before the backups were moved to another repository
            (True, SPEC, {**CATALOG, "repository": str(uuid.uuid4())}, False),
            (True, SPEC, {**CATALOG, "stanza": "other"}, False),
        ],
    )
    def test_catalog_of_the_current_repository(
        self, monkeypatch, primary, spec, catalog, reported
    ):
        monkeypatch.setattr(pgbackrest, "load_catalog", lambda: catalog)
        monkeypatch.setattr(pgbackrest, "stanza_ready", lambda spec: True)

        result = _node(primary)._backup_catalog(spec)

        assert result == (catalog if reported else None)

    def test_stanza_error_is_reported(self, monkeypatch):
        monkeypatch.setattr(pgbackrest, "load_catalog", lambda: CATALOG)
        monkeypatch.setattr(pgbackrest, "stanza_ready", lambda spec: False)
        monkeypatch.setattr(pgbackrest, "load_stanza_error", lambda: "403 Forbidden")

        result = _node()._backup_catalog(SPEC)

        assert result["error"] == "403 Forbidden"
        # The catalog left from before isn't reported with it
        assert result["backups"] == {}
        assert result["repository"] == str(REPOSITORY)

    def test_catalog_changes_the_full_hash_only(self):
        # A differing target field would make the agent apply the target
        # instead of reporting what it reads
        node_uuid = uuid.uuid4()
        idle = _node(node_uuid=node_uuid)
        reporting = _node(node_uuid=node_uuid)
        reporting.backup_catalog = CATALOG

        idle_resource = idle.to_ua_resource("pg_instance_node")
        reporting_resource = reporting.to_ua_resource("pg_instance_node")

        assert reporting_resource.hash == idle_resource.hash
        assert reporting_resource.full_hash != idle_resource.full_hash


def _entry(label, **fields):
    return {
        "label": label,
        "type": "full",
        "started_at": 1789398600,
        "finished_at": 1789398667,
        "size": 1000,
        "stored_size": 100,
        "before_revision": None,
        "error": False,
        "restorable": True,
        **fields,
    }


class FakeBackup:
    """A row of PGBackup recording what happens to it."""

    def __init__(self, journal, **fields):
        self._journal = journal
        self.deleted = False
        self.__dict__.update(fields)

    def insert(self):
        self._journal.append(("insert", self.stanza, self.label))

    def update(self):
        self._journal.append(("update", self.stanza, self.label))

    def delete(self):
        self.deleted = True
        self._journal.append(("delete", self.stanza, self.label))


COLLECTED_AT = 1789400000


def _repository_row(repository_uuid, path="/exordos_db", same_place=()):
    repository = types.SimpleNamespace(
        uuid=repository_uuid,
        storage=types.SimpleNamespace(location=lambda: ("s3", "e", "b", path)),
    )
    repository.same_place = lambda: [repository, *same_place]
    return repository


class TestSync:
    @pytest.fixture
    def rows(self, monkeypatch):
        state = types.SimpleNamespace(journal=[], rows=[], filters=None)

        def get_all(filters):
            state.filters = filters
            return [
                r
                for r in state.rows
                if r.stanza in filters["stanza"].value
                and r.repository.uuid in filters["repository"].value
            ]

        backup_model = lambda **kw: FakeBackup(state.journal, **kw)
        backup_model.objects = types.SimpleNamespace(get_all=get_all)
        monkeypatch.setattr(
            builder, "user_models", types.SimpleNamespace(PGBackup=backup_model)
        )
        state.row = lambda stanza, entry, repository=None: FakeBackup(
            state.journal,
            repository=repository or _repository_row(REPOSITORY),
            stanza=stanza,
            label=entry["label"],
            **builder.backup_fields(entry),
        )
        return state

    @staticmethod
    def _instance(repository=REPOSITORY, same_place=()):
        instance = types.SimpleNamespace(
            uuid=uuid.UUID(STANZA),
            project_id=uuid.uuid4(),
            backup=types.SimpleNamespace(repository=repository),
            get_backup_repository=lambda: _repository_row(
                repository, same_place=same_place
            ),
            backup_status=None,
            updates=[],
        )
        instance.update = lambda force=False: instance.updates.append(
            instance.backup_status
        )
        return instance

    @staticmethod
    def _sync(instance, *catalogs):
        actuals = [
            None if c is None else types.SimpleNamespace(backup_catalog=c)
            for c in catalogs
        ]
        collection = types.SimpleNamespace(actuals=lambda: actuals)
        builder.PGInstanceBuilder._sync_backups(instance, collection)

    @staticmethod
    def _catalog(backups_by_stanza, collected_at=COLLECTED_AT, repository=REPOSITORY):
        return {
            "repository": str(repository),
            "stanza": STANZA,
            "collected_at": collected_at,
            "backups": backups_by_stanza,
        }

    def test_rows_follow_the_catalog(self, rows):
        kept = _entry("F1")
        changed = _entry("F2")
        rows.rows = [
            rows.row(STANZA, kept),
            rows.row(STANZA, changed),
            rows.row(STANZA, _entry("F0")),
        ]
        catalog = self._catalog(
            {
                STANZA: [kept, _entry("F2", restorable=False), _entry("F3")],
                SNAPSHOTS: [_entry("S1", before_revision=1)],
            }
        )

        self._sync(self._instance(), None, catalog)

        assert sorted(rows.journal) == [
            ("delete", STANZA, "F0"),
            ("insert", STANZA, "F3"),
            ("insert", SNAPSHOTS, "S1"),
            ("update", STANZA, "F2"),
        ]
        assert set(rows.filters["stanza"].value) == {STANZA, SNAPSHOTS}

    def test_new_row(self, rows, monkeypatch):
        inserted = []
        monkeypatch.setattr(FakeBackup, "insert", lambda self: inserted.append(self))
        instance = self._instance()

        self._sync(instance, self._catalog({STANZA: [_entry("F1")]}))

        [row] = inserted
        assert row.uuid == uuid.uuid5(REPOSITORY, f"{STANZA}/F1")
        assert row.instance is instance
        assert row.project_id == instance.project_id
        assert row.finished_at == datetime.datetime(
            2026, 9, 14, 15, 11, 7, tzinfo=datetime.timezone.utc
        )

    def test_backup_newer_than_the_catalog_is_kept(self, rows):
        # E.g. a primary that took over reports what it saw before, until its
        # timer runs
        rows.rows = [rows.row(STANZA, _entry("F9", finished_at=COLLECTED_AT + 60))]

        self._sync(self._instance(), self._catalog({STANZA: []}))

        assert rows.journal == []

    def test_rows_of_another_repository_object_for_the_place_are_dropped(self, rows):
        same_place = _repository_row(uuid.uuid4())
        elsewhere = _repository_row(uuid.uuid4(), path="/other")
        rows.rows = [
            rows.row(STANZA, _entry("F1"), repository=same_place),
            rows.row(STANZA, _entry("F1"), repository=elsewhere),
        ]

        self._sync(
            self._instance(same_place=[same_place]),
            self._catalog({STANZA: [_entry("F1")]}),
        )

        # The rows of another place are frozen as they were
        assert sorted(rows.journal) == [
            ("delete", STANZA, "F1"),
            ("insert", STANZA, "F1"),
        ]
        assert rows.rows[0].deleted and not rows.rows[1].deleted

    def test_unreported_stanza_is_kept(self, rows):
        # Its backups couldn't be read on the node
        rows.rows = [rows.row(SNAPSHOTS, _entry("S1"))]

        self._sync(self._instance(), self._catalog({STANZA: []}))

        assert rows.journal == []

    def test_newest_catalog_wins(self, rows):
        old = self._catalog({STANZA: [_entry("F1")]}, collected_at=COLLECTED_AT - 5)
        new = self._catalog({STANZA: [_entry("F2")]}, collected_at=COLLECTED_AT)

        self._sync(self._instance(), new, old)

        assert rows.journal == [("insert", STANZA, "F2")]

    @pytest.mark.parametrize(
        "catalog",
        [
            # Of the repository the instance backed up to before
            {
                "repository": str(uuid.uuid4()),
                "stanza": STANZA,
                "collected_at": 1,
                "backups": {STANZA: []},
            },
            {
                "repository": str(REPOSITORY),
                "stanza": str(uuid.uuid4()),
                "collected_at": 1,
                "backups": {STANZA: []},
            },
        ],
    )
    def test_catalog_of_another_repository_is_ignored(self, rows, catalog):
        rows.rows = [rows.row(STANZA, _entry("F1"))]

        self._sync(self._instance(), catalog)

        assert rows.journal == []
        assert rows.filters is None

    def test_no_backups_nothing_to_sync(self, rows):
        instance = self._instance()
        instance.backup = None
        instance.backup_status = {"error": None}

        self._sync(instance, self._catalog({STANZA: []}))

        assert rows.filters is None
        assert instance.updates == [None]

    def test_status_follows_the_catalog(self, rows):
        instance = self._instance()
        catalog = self._catalog({STANZA: [_entry("F1")]})
        catalog["archive"] = {"last_archived_at": 1789399000}

        self._sync(instance, catalog)
        self._sync(instance, catalog)

        # Written once, the same report changes nothing
        assert instance.updates == [
            {
                "error": None,
                "last_backup_at": "2026-09-14T15:11:07Z",
                "last_archived_at": "2026-09-14T15:16:40Z",
                "last_archive_failed_at": None,
                "restore_window": {
                    "earliest": "2026-09-14T15:11:08Z",
                    "latest": "2026-09-14T15:16:40Z",
                },
            }
        ]


class TestSummary:
    @staticmethod
    def _catalog(entries, archive=None, error=None):
        backups = {} if entries is None else {STANZA: entries}
        return {"backups": backups, "archive": archive, "error": error}

    def test_window_starts_after_the_oldest_restorable_backup(self):
        entries = [
            _entry("F0", finished_at=100, restorable=False),
            _entry("F1", finished_at=200),
            _entry("I2", finished_at=300, error=True, restorable=False),
        ]

        status = builder.summarize_backups(
            self._catalog(entries, {"last_archived_at": 400}), STANZA, None
        )

        assert status["restore_window"] == {
            "earliest": "1970-01-01T00:03:21Z",
            "latest": "1970-01-01T00:06:40Z",
        }
        # A backup with errors isn't the last one taken
        assert status["last_backup_at"] == "1970-01-01T00:03:20Z"

    def test_no_restorable_backup_no_window(self):
        entries = [_entry("F0", restorable=False)]

        status = builder.summarize_backups(self._catalog(entries), STANZA, None)

        assert status["restore_window"] is None
        assert status["last_backup_at"] is not None

    def test_failing_archive_and_timer(self):
        status = builder.summarize_backups(
            self._catalog(
                [_entry("F1", finished_at=200)],
                {"last_archived_at": 300, "last_failed_at": 500},
                error="archive-push failed",
            ),
            STANZA,
            None,
        )

        assert status["error"] == "archive-push failed"
        assert status["last_archive_failed_at"] == "1970-01-01T00:08:20Z"
        # Recoverable only up to what was archived
        assert status["restore_window"]["latest"] == "1970-01-01T00:05:00Z"

    def test_unread_backups_keep_what_was_known(self):
        before = {
            "error": None,
            "last_backup_at": "1970-01-01T00:03:20Z",
            "last_archived_at": "1970-01-01T00:05:00Z",
            "last_archive_failed_at": None,
            "restore_window": {
                "earliest": "1970-01-01T00:03:21Z",
                "latest": "1970-01-01T00:05:00Z",
            },
        }

        # After a failover the new primary hasn't archived anything yet
        status = builder.summarize_backups(
            self._catalog(None, {"last_archived_at": None}, error="unreachable"),
            STANZA,
            before,
        )

        assert status == {**before, "error": "unreachable"}


def _storage(**kwargs):
    view = {
        "kind": "s3",
        "endpoint": "http://10.20.0.30:9000",
        "bucket": "dbaas-backups",
        "access_key": "backup",
        "secret_key": "secret",
        **kwargs,
    }
    return backups.STORAGE_TYPE.from_simple_type(view)


class FakeEngine:
    """Hands out one session, as the engine does within a request."""

    def __init__(self, journal):
        self.journal = journal

    @contextlib.contextmanager
    def session_manager(self, session=None):
        yield session or "session"


class TestRepository:
    @pytest.fixture
    def saved(self, monkeypatch):
        journal = []
        monkeypatch.setattr(
            models.engines.engine_factory,
            "get_engine",
            lambda name="default": FakeEngine(journal),
        )
        monkeypatch.setattr(
            models,
            "get_repository",
            lambda *a, locked=False, **kw: journal.append(f"lock {locked}"),
        )
        monkeypatch.setattr(
            orm.SQLStorableMixin,
            "update",
            lambda self, session=None, force=False: journal.append("update"),
        )
        monkeypatch.setattr(
            orm.SQLStorableMixin,
            "delete",
            lambda self, session=None, **kw: journal.append("delete"),
        )
        return journal

    @staticmethod
    def _repository(monkeypatch, instances=(), **storage):
        repository = models.PGBackupRepository(
            uuid=REPOSITORY,
            name="backups",
            project_id=uuid.uuid4(),
            storage=_storage(**storage),
        )
        monkeypatch.setattr(
            models.PGBackupRepository,
            "get_instances",
            lambda self, session=None: list(instances),
        )
        return repository

    @staticmethod
    def _instance(journal):
        return types.SimpleNamespace(
            uuid="i",
            update=lambda session=None, force=False: journal.append(f"touch {force}"),
        )

    def test_rotated_credentials_reach_the_instances(self, monkeypatch, saved):
        repository = self._repository(monkeypatch, [self._instance(saved)])

        repository.storage = _storage(
            endpoint="http://10.20.0.30:9000/", secret_key="rotated"
        )
        repository.name = "renamed"
        repository.update()

        assert saved == ["update", "touch True"]

    @pytest.fixture
    def resolved_to_loopback(self, monkeypatch):
        monkeypatch.setattr(
            endpoints.socket,
            "getaddrinfo",
            lambda host, port, proto=0: [(None, None, proto, "", ("127.0.0.1", 0))],
        )

    def test_endpoint_resolved_on_insert(
        self, monkeypatch, saved, resolved_to_loopback
    ):
        monkeypatch.setattr(
            orm.SQLStorableMixin,
            "insert",
            lambda self, session=None: saved.append("insert"),
        )
        repository = self._repository(monkeypatch)
        repository.storage = _storage(endpoint="http://s3.example.com")

        with pytest.raises(models.EndpointError):
            repository.insert()
        assert saved == []

    def test_endpoint_resolved_on_update(
        self, monkeypatch, saved, resolved_to_loopback
    ):
        endpoint = "http://s3.example.com"
        repository = self._repository(monkeypatch, endpoint=endpoint)
        repository.storage = _storage(endpoint=endpoint, secret_key="rotated")

        with pytest.raises(models.EndpointError):
            repository.update()
        assert saved == []

    def test_endpoint_not_resolved_without_storage_change(
        self, monkeypatch, saved, resolved_to_loopback
    ):
        repository = self._repository(monkeypatch, endpoint="http://s3.example.com")
        repository.name = "renamed"

        repository.update()
        assert saved == ["update"]

    @pytest.mark.parametrize(
        "change",
        [
            {"storage": {"bucket": "other-bucket"}},
            {"storage": {"path": "/other"}},
            {"storage": {"endpoint": "http://10.20.0.31:9000"}},
            {"encryption_key": "k3y"},
        ],
    )
    def test_data_of_the_repository_cant_change(self, monkeypatch, saved, change):
        repository = self._repository(monkeypatch)

        if "storage" in change:
            repository.storage = _storage(**change["storage"])
        else:
            repository.encryption_key = change["encryption_key"]
        with pytest.raises(models.RepositoryUpdateError):
            repository.update()

        assert saved == []

    def test_repository_in_use_isnt_deleted(self, monkeypatch, saved):
        repository = self._repository(monkeypatch, [self._instance(saved)])

        with pytest.raises(models.RepositoryInUseError) as e:
            repository.delete()

        assert e.value.get_code() == 409
        # Checked under the lock of the repository row
        assert saved == ["lock True"]

    def test_unused_repository_is_deleted(self, monkeypatch, saved):
        self._repository(monkeypatch).delete()
        assert saved == ["lock True", "delete"]


class TestRestoreTarget:
    @staticmethod
    def _source(target_time):
        target = (
            {"kind": "latest"}
            if target_time is None
            else {"kind": "time", "time": target_time}
        )
        return backups.RESTORE_SOURCE_TYPE.from_simple_type(
            {
                "kind": "repository",
                "repository": str(REPOSITORY),
                "stanza": STANZA,
                "target": target,
            }
        )

    @staticmethod
    def _backup(finished_at, restorable=True):
        return types.SimpleNamespace(
            finished_at=datetime.datetime.fromisoformat(finished_at),
            restorable=restorable,
        )

    def test_target_before_every_backup_is_rejected(self):
        known = [
            self._backup("2026-09-14T10:00:00+00:00"),
            # On a timeline abandoned by a rollback
            self._backup("2026-09-14T09:00:00+00:00", restorable=False),
        ]

        with pytest.raises(models.RestoreSourceError) as e:
            models.check_restore_target(
                self._source("2026-09-14T09:30:00Z"), known, 500
            )

        assert "2026-09-14T10:00:00" in str(e.value)

    def test_backup_finished_within_the_target_second_doesnt_count(self):
        known = [self._backup("2026-09-14T10:00:00+00:00")]
        with pytest.raises(models.RestoreSourceError):
            models.check_restore_target(
                self._source("2026-09-14T10:00:00.500Z"), known, 500
            )
        models.check_restore_target(self._source("2026-09-14T10:00:01Z"), known, 500)

    @pytest.mark.parametrize(
        "target_time, known, max_known",
        [
            (None, [], 500),
            # Nothing listed: the data plane finds out
            ("2026-09-14T09:30:00Z", [], 500),
            # The oldest backups may be past what a node lists
            ("2026-09-14T09:30:00Z", ["2026-09-14T10:00:00+00:00"], 1),
        ],
    )
    def test_not_checked(self, target_time, known, max_known):
        models.check_restore_target(
            self._source(target_time), [self._backup(k) for k in known], max_known
        )


class TestInstanceRepositories:
    def _instance(self, **fields):
        return models.PGInstance(
            name="demo",
            project_id=uuid.uuid4(),
            cpu=1,
            ram=1024,
            disk_size=10,
            nodes_number=1,
            version=models.PGVersion(name="18", image="pg18"),
            **fields,
        )

    @pytest.mark.parametrize("field", ["backup", "restore_from"])
    def test_repository_of_another_project_isnt_found(self, monkeypatch, field):
        asked = []

        def get_repository(uuid_, project_id=None, session=None, locked=False):
            asked.append((uuid_, project_id))

        monkeypatch.setattr(models, "get_repository", get_repository)
        view = {"kind": "repository", "repository": str(REPOSITORY)}
        if field == "restore_from":
            view["stanza"] = STANZA
        prop_type = (
            backups.BACKUP_TYPE if field == "backup" else backups.RESTORE_SOURCE_TYPE
        )
        instance = self._instance(**{field: prop_type.from_simple_type(view)})

        with pytest.raises(models.RepositoryNotFoundError):
            instance._check_repositories()

        assert asked == [(REPOSITORY, instance.project_id)]

    def test_repositories_of_the_project(self, monkeypatch):
        monkeypatch.setattr(models, "get_repository", lambda *a, **kw: object())
        backup = backups.BACKUP_TYPE.from_simple_type(
            {"kind": "repository", "repository": str(REPOSITORY)}
        )
        instance = self._instance(backup=backup)

        instance._check_repositories()

        assert instance.repository_uuids() == {REPOSITORY}
