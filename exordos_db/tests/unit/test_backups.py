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

import datetime
import json
import types
import uuid

import pytest
from restalchemy.common import exceptions as ra_exc
import yaml

from exordos_db.common import pgbackrest
from exordos_db.infra.services import builder as infra_builder
from exordos_db.paas.dm import models as paas_models
from exordos_db.paas.services import builder as paas_builder
from exordos_db.user_api.dm import backups
from exordos_db.user_api.dm import models

HOUR = 3600

S3_VIEW = {
    "kind": "s3",
    "endpoint": "http://10.20.0.30:9000",
    "bucket": "dbaas-backups",
    "access_key": "backup",
    "secret_key": "s3cr3t/key+",
}


def _storage(**kwargs):
    return backups.STORAGE_TYPE.from_simple_type({**S3_VIEW, **kwargs})


def _policy(**kwargs):
    instance = models.PGInstance(
        project_id=uuid.uuid4(),
        name="backed-up",
        cpu=1,
        ram=1024,
        disk_size=8,
        nodes_number=1,
        version=models.PGVersion(name="18", image="pg.raw"),
    )
    return models.PGBackupPolicy(
        project_id=instance.project_id,
        instance=instance,
        storage=_storage(),
        **kwargs,
    )


def _spec(**options):
    return {
        "stanza": "1b1bc0de-0000-4000-8000-000000000001",
        "options": {"repo1-type": "s3", "repo1-path": "/exordos_db", **options},
        "schedule": {"full_interval_hours": 168, "incr_interval_hours": 24},
    }


def _backup(backup_type, stop, error=False):
    return {
        "label": f"{stop}{backup_type[0].upper()}",
        "type": backup_type,
        "error": error,
        "timestamp": {"start": stop - 60, "stop": stop},
        "info": {"size": 1000, "repository": {"delta": 100}},
    }


class TestBackupPolicy:
    def test_defaults(self):
        policy = _policy()

        assert policy.pgbackrest_repo_options() == {
            "repo1-type": "s3",
            "repo1-s3-endpoint": "http://10.20.0.30:9000",
            "repo1-s3-bucket": "dbaas-backups",
            "repo1-s3-region": "us-east-1",
            "repo1-s3-key": "backup",
            "repo1-s3-key-secret": "s3cr3t/key+",
            "repo1-s3-uri-style": "path",
            "repo1-storage-verify-tls": "y",
            "repo1-path": "/exordos_db",
            "repo1-retention-full": "2",
        }
        assert policy.full_interval_hours == 168
        assert policy.incr_interval_hours == 24
        assert policy.status == "NEW"
        assert policy.error is None

    def test_retention(self):
        options = _policy(retention_full=7).pgbackrest_repo_options()
        assert options["repo1-retention-full"] == "7"

    @pytest.mark.parametrize(
        "field, value",
        [
            ("full_interval_hours", 0),
            ("incr_interval_hours", 8785),
            ("retention_full", 0),
        ],
    )
    def test_invalid_schedule(self, field, value):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _policy(**{field: value})

    def test_spec_of_the_instance(self, monkeypatch):
        policy = _policy(full_interval_hours=48, incr_interval_hours=6)
        instance = policy.instance
        get_policy = lambda: policy
        monkeypatch.setattr(instance, "get_backup_policy", get_policy, raising=False)

        spec = paas_builder.PGInstanceBuilder._get_backup(None, instance)

        assert spec["stanza"] == str(instance.uuid)
        assert spec["schedule"] == {"full_interval_hours": 48, "incr_interval_hours": 6}
        assert spec["options"]["repo1-s3-bucket"] == "dbaas-backups"
        # A quarter of the 8 GiB data disk
        assert spec["options"]["archive-push-queue-max"] == "2GiB"

    def test_no_policy_no_backups(self, monkeypatch):
        instance = _policy().instance
        get_policy = lambda: None
        monkeypatch.setattr(instance, "get_backup_policy", get_policy, raising=False)

        assert paas_builder.PGInstanceBuilder._get_backup(None, instance) is None


class TestS3Storage:
    def test_encryption_and_trailing_slash(self):
        options = _storage(
            endpoint="https://s3.example.com/",
            encryption_key="k3y",
            verify_tls=False,
        ).storage_repo_options()

        assert options["repo1-s3-endpoint"] == "https://s3.example.com"
        assert options["repo1-storage-verify-tls"] == "n"
        assert options["repo1-cipher-type"] == "aes-256-cbc"
        assert options["repo1-cipher-pass"] == "k3y"

    def test_roundtrip(self):
        view = backups.STORAGE_TYPE.to_simple_type(_storage())
        assert backups.STORAGE_TYPE.from_simple_type(view) == _storage()

    def test_schedule_is_not_accepted(self):
        # It's a matter of the policy
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _storage(retention_full=2)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("endpoint", "ftp://10.20.0.30:9000"),
            ("endpoint", "http://10.20.0.30:9000/bucket"),
            ("bucket", "Upper"),
            # Would inject an option into pgbackrest.conf
            ("secret_key", "secret\nrepo1-path=/other"),
            ("path", "relative"),
            ("uri_style", "virtual"),
        ],
    )
    def test_invalid(self, field, value):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _storage(**{field: value})

    @pytest.mark.parametrize("field", ["endpoint", "bucket", "access_key"])
    def test_required(self, field):
        view = {k: v for k, v in S3_VIEW.items() if k != field}
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            backups.STORAGE_TYPE.from_simple_type(view)


class TestConfig:
    def test_render(self):
        spec = _spec(**{"repo1-s3-key": "backup"})

        config = pgbackrest.render_config(spec)

        assert config.startswith("[global]\narchive-async=y\n")
        assert "repo1-s3-key=backup\n" in config
        assert config.endswith(
            "\n[1b1bc0de-0000-4000-8000-000000000001]\n"
            "pg1-path=/var/lib/postgresql/patroni/data\n"
            "pg1-socket-path=/var/run/postgresql\n"
        )

    def test_spec_overrides_fixed_options(self):
        spec = _spec(**{"process-max": "4"})
        assert "process-max=4\n" in pgbackrest.render_config(spec)
        assert "process-max=2\n" not in pgbackrest.render_config(spec)

    def test_render_rejects_line_breaks(self):
        with pytest.raises(ValueError):
            pgbackrest.render_config(_spec(**{"repo1-s3-key": "a\nb"}))

    def test_archive_command(self):
        assert pgbackrest.archive_command(None) == ":"
        assert pgbackrest.archive_command(_spec()) == (
            "pgbackrest --stanza=1b1bc0de-0000-4000-8000-000000000001 archive-push %p"
        )

    def test_archive_timeout_only_with_archiving(self):
        assert pgbackrest.archive_timeout(None) == "0"
        assert pgbackrest.archive_timeout(_spec()) == "300s"

    def test_fingerprint_follows_repository_only(self):
        base = pgbackrest.repo_fingerprint(_spec())

        queue = _spec(**{"archive-push-queue-max": "4GiB"})
        schedule = _spec()
        schedule["schedule"]["incr_interval_hours"] = 1
        assert pgbackrest.repo_fingerprint(queue) == base
        assert pgbackrest.repo_fingerprint(schedule) == base

        assert pgbackrest.repo_fingerprint(_spec(**{"repo1-path": "/x"})) != base
        stanza = _spec()
        stanza["stanza"] = "other"
        assert pgbackrest.repo_fingerprint(stanza) != base


SOURCE_UUID = "1b1bc0de-0000-4000-8000-000000000001"


def _restore_source(**kwargs):
    view = {**S3_VIEW, "stanza": SOURCE_UUID, **kwargs}
    return backups.RESTORE_SOURCE_TYPE.from_simple_type(view)


class TestS3RestoreSource:
    @pytest.mark.parametrize("target", [{}, {"target": {"kind": "latest"}}])
    def test_latest(self, target):
        # The end of the archive is what a source without a target replays to
        spec = _restore_source(encryption_key="k3y", **target).restore_spec()

        assert spec["stanza"] == SOURCE_UUID
        assert spec["target_time"] is None
        assert spec["options"]["repo1-s3-endpoint"] == "http://10.20.0.30:9000"
        assert spec["options"]["repo1-cipher-pass"] == "k3y"
        # Retention is a matter of the instance taking backups
        assert "repo1-retention-full" not in spec["options"]

    def test_target_time_in_utc(self):
        source = _restore_source(
            target={"kind": "time", "time": "2026-01-14T13:30:15.000250+03:00"}
        )
        assert source.restore_spec()["target_time"] == "2026-01-14 10:30:15.000250+00"

    def test_future_target_time(self):
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=1
        )
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _restore_source(target={"kind": "time", "time": future.isoformat()})

    @pytest.mark.parametrize(
        "target",
        [
            # A target carries the fields of its kind and nothing else
            {"kind": "latest", "time": "2026-01-14T10:30:15Z"},
            {"kind": "time"},
            {"kind": "whenever"},
        ],
    )
    def test_unknown_targets_are_rejected(self, target):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _restore_source(target=target)

    def test_schedule_is_not_accepted(self):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _restore_source(retention_full=2)

    def test_stanza_required(self):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            backups.RESTORE_SOURCE_TYPE.from_simple_type(S3_VIEW)


class TestRestore:
    def test_args_latest(self):
        assert pgbackrest.restore_args({"target_time": None}) == [
            "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf",
            "restore",
        ]

    def test_args_target_time(self):
        spec = {"target_time": "2026-09-14 10:30:15.000250+00"}
        assert pgbackrest.restore_args(spec) == [
            "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf",
            "--type=time",
            "--target=2026-09-14 10:30:15.000250+00",
            "--target-action=promote",
            "restore",
        ]

    @pytest.mark.parametrize("restore", [False, True])
    def test_patroni_config(self, restore):
        instance = types.SimpleNamespace(
            restore_from=_restore_source() if restore else None
        )

        config = yaml.safe_load(
            infra_builder.PATRONI_CONF_TEMPLATE.format(
                cluster_name="demo",
                node_name=str(uuid.uuid4()),
                node_ip="10.20.0.40",
                raft_partner_addrs=["10.20.0.40:5010"],
                raft_auth="",
                sync_mode="false",
                sync_replica_number=0,
                bootstrap_method=infra_builder.bootstrap_method(instance),
            )
        )

        bootstrap = config["bootstrap"]
        assert "dcs" in bootstrap
        if restore:
            assert bootstrap["method"] == "pgbackrest"
            assert bootstrap["pgbackrest"] == {
                "command": "/usr/bin/exordos-db-pg-restore",
                "keep_existing_recovery_conf": True,
                "no_params": True,
            }
        else:
            assert "method" not in bootstrap


class TestChooseBackupType:
    now = 1_800_000_000

    def _choose(self, backups):
        return pgbackrest.choose_backup_type(backups, _spec()["schedule"], self.now)

    def test_no_backups(self):
        assert self._choose([]) == "full"

    def test_backup_with_page_errors_counts(self):
        # pgBackRest lists only backups that succeeded, `error` flags pages
        # with checksum errors: taking another backup wouldn't fix them and
        # the retention would expire the backups from before
        assert self._choose([_backup("full", self.now - HOUR, error=True)]) is None

    def test_full_expired(self):
        backups = [
            _backup("full", self.now - 168 * HOUR),
            _backup("incr", self.now - HOUR),
        ]
        assert self._choose(backups) == "full"

    def test_incr_due(self):
        backups = [
            _backup("full", self.now - 48 * HOUR),
            _backup("incr", self.now - 24 * HOUR),
        ]
        assert self._choose(backups) == "incr"

    def test_nothing_due(self):
        backups = [
            _backup("full", self.now - 30 * HOUR),
            _backup("diff", self.now - 2 * HOUR),
        ]
        assert self._choose(backups) is None


class TestMetrics:
    now = 1_800_000_000

    def _samples(self, info, missing=None, succeeded=True):
        text = pgbackrest.render_metrics(info, missing, succeeded, self.now)
        return dict(
            line.rsplit(" ", 1)
            for line in text.splitlines()
            if not line.startswith("#")
        )

    def test_backups(self):
        full = _backup("full", self.now - 48 * HOUR, error=True)
        incr = _backup("incr", self.now - HOUR)
        samples = self._samples({"backup": [full, incr]}, missing=[])

        labels = f'{{backup_name="{full["label"]}",backup_type="full"}}'
        assert samples[f"exordos_db_backup_stop_timestamp_seconds{labels}"] == str(
            full["timestamp"]["stop"]
        )
        assert samples[f"exordos_db_backup_size_bytes{labels}"] == "1000"
        assert samples[f"exordos_db_backup_repo_delta_bytes{labels}"] == "100"
        assert samples[f"exordos_db_backup_error{labels}"] == "1"
        assert samples[
            "exordos_db_backup_recovery_window_start_timestamp_seconds"
        ] == str(full["timestamp"]["stop"])
        assert samples["exordos_db_backup_archive_missing_segments"] == "0"
        assert samples["exordos_db_backup_run_success"] == "1"

    def test_larger_backup_counts_as_a_smaller_one(self):
        full = _backup("full", self.now - 48 * HOUR)
        diff = _backup("diff", self.now - 2 * HOUR)
        samples = self._samples({"backup": [full, diff]})

        last = "exordos_db_backup_last_completion_timestamp_seconds"
        assert samples[f'{last}{{backup_type="full"}}'] == str(
            full["timestamp"]["stop"]
        )
        assert samples[f'{last}{{backup_type="diff"}}'] == str(
            diff["timestamp"]["stop"]
        )
        assert samples[f'{last}{{backup_type="incr"}}'] == str(
            diff["timestamp"]["stop"]
        )

    def test_unreachable_repository(self):
        samples = self._samples(None, succeeded=False)

        assert samples == {
            "exordos_db_backup_run_timestamp_seconds": str(self.now),
            "exordos_db_backup_run_success": "0",
        }

    def test_labels_are_escaped(self):
        backup = {**_backup("full", self.now), "label": 'a"b\\c'}
        text = pgbackrest.render_metrics({"backup": [backup]}, None, True, self.now)

        assert 'backup_name="a\\"b\\\\c"' in text


def test_error_starts_at_its_cause(monkeypatch):
    stderr = (
        "2026-09-23 14:01:21.652 P00   WARN: --delta or --force specified but ...\n"
        "2026-09-23 14:01:21.671 P00  ERROR: [075]: no backup set found to restore"
    )
    monkeypatch.setattr(
        pgbackrest.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=75, stderr=stderr, stdout=""),
    )

    with pytest.raises(pgbackrest.PgBackRestError) as e:
        pgbackrest.run("stanza", "restore")

    assert str(e.value) == (
        "restore failed with code 75: "
        "2026-09-23 14:01:21.671 P00  ERROR: [075]: no backup set found to restore"
    )


def test_restore_fields_are_sent_to_a_node_only_when_set():
    # The agent of a node created before them drops the fields it doesn't
    # know and would never match the target hash
    instance = paas_models.PGInstance(
        project_id=uuid.uuid4(),
        name="old",
        cpu=1,
        ram=1024,
        disk_size=8,
        nodes_number=1,
        version=models.PGVersion(name="18", image="pg.raw"),
    )
    node = paas_models.PGInstanceNode(
        uuid=uuid.uuid4(),
        name="old",
        instance=instance,
        nodes_number=1,
        sync_replica_number=0,
        users={},
        databases={},
    )

    assert set(node.to_ua_resource().value) == {
        "uuid",
        "name",
        "nodes_number",
        "sync_replica_number",
        "users",
        "databases",
        # Always: the metrics of every node need it
        "project_id",
    }
    node.backup = {"stanza": "s"}
    assert node.to_ua_resource().value["backup"] == {"stanza": "s"}
    node.adopt_roles = True
    assert node.to_ua_resource().value["adopt_roles"] is True


# The stand's failover: timeline 3 ended in segment 5F, the old primary went
# down before it archived 5E
HISTORY = "1\t0/20000A0\tno recovery target\n\n2\t0/5A0000A0\tno recovery target\n\n3\t0/5F0000A0\tno recovery target\n"
SEGMENTS = [
    "00000003000000000000005B",
    "00000003000000000000005C",
    "00000003000000000000005D",
    "00000003000000000000005E",
    "00000004000000000000005F",
]


class TestArchiveGap:
    def test_needed_wal_follows_the_timelines(self):
        switches = pgbackrest.parse_history(HISTORY)

        assert pgbackrest.needed_wal(SEGMENTS[0], SEGMENTS[-1], switches) == SEGMENTS

    def test_needed_wal_of_the_first_timeline_crosses_the_log_id(self):
        assert pgbackrest.needed_wal(
            "0000000100000000000000FF", "000000010000000100000000", []
        ) == ["0000000100000000000000FF", "000000010000000100000000"]

    def _info(self):
        return {
            "backup": [
                {
                    "timestamp": {"stop": 100},
                    "database": {"id": 1},
                    "archive": {"start": SEGMENTS[0]},
                },
            ],
            "archive": [{"database": {"id": 1}, "id": "18-1", "max": SEGMENTS[-1]}],
        }

    @pytest.fixture
    def repository(self, tmp_path, monkeypatch):
        (tmp_path / "pg_wal").mkdir()
        (tmp_path / "pg_wal" / "00000004.history").write_text(HISTORY)
        monkeypatch.setattr(pgbackrest, "PG_DATA_DIR", str(tmp_path))
        files = {}
        listed = []

        def run(stanza, *args, timeout=None):
            listed.append(args[-1])
            directory = args[-1].rsplit("/", 1)[-1]
            return json.dumps(
                {f"{n}-sha1.zst": {} for n in files if n.startswith(directory)}
            )

        monkeypatch.setattr(pgbackrest, "run", run)
        return files, listed

    def test_missing_segment_is_found(self, repository):
        files, listed = repository
        files.update(dict.fromkeys(s for s in SEGMENTS if not s.endswith("5E")))

        assert pgbackrest.archive_gap("st", self._info()) == [
            "00000003000000000000005E"
        ]
        assert listed == [
            "archive/st/18-1/0000000300000000",
            "archive/st/18-1/0000000400000000",
        ]

    def test_segment_still_being_archived_isnt_missing(self, repository, tmp_path):
        files, _ = repository
        files.update(dict.fromkeys(s for s in SEGMENTS if not s.endswith("5E")))
        status = tmp_path / "pg_wal" / "archive_status"
        status.mkdir()
        (status / "00000003000000000000005E.ready").touch()

        assert pgbackrest.archive_gap("st", self._info()) == []

    def test_segment_archived_during_the_listing_isnt_missing(
        self, repository, tmp_path, monkeypatch
    ):
        # Sent after its directory was listed: .ready is gone by the end
        files, _ = repository
        files.update(dict.fromkeys(s for s in SEGMENTS if not s.endswith("5E")))
        status = tmp_path / "pg_wal" / "archive_status"
        status.mkdir()
        ready = status / "00000003000000000000005E.ready"
        ready.touch()
        listing = pgbackrest.run
        monkeypatch.setattr(
            pgbackrest,
            "run",
            lambda *a, **k: (ready.unlink(missing_ok=True), listing(*a, **k))[1],
        )

        assert pgbackrest.archive_gap("st", self._info()) == []

    def test_continuous_archive_has_no_gap(self, repository):
        files, _ = repository
        files.update(dict.fromkeys(SEGMENTS))

        assert pgbackrest.archive_gap("st", self._info()) == []

    def test_nothing_to_check_without_a_backup(self, repository):
        assert pgbackrest.archive_gap("st", {"backup": [], "archive": []}) == []


def test_error_is_reported_without_the_http_exchange():
    error = pgbackrest.PgBackRestError(
        "restore failed with code 39: ERROR: [039]: HTTP request failed with 403\n"
        "*** Response Content ***:\n<secret>"
    )

    assert pgbackrest.error_text(error) == (
        "restore failed with code 39: ERROR: [039]: HTTP request failed with 403"
    )
