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
from exordos_db.user_api.dm import backups

HOUR = 3600

S3_VIEW = {
    "kind": "s3",
    "endpoint": "http://10.20.0.30:9000",
    "bucket": "dbaas-backups",
    "access_key": "backup",
    "secret_key": "s3cr3t/key+",
}


def _s3(**kwargs):
    return backups.BACKUP_TYPE.from_simple_type({**S3_VIEW, **kwargs})


def _spec(**options):
    return {
        "stanza": "1b1bc0de-0000-4000-8000-000000000001",
        "options": {"repo1-type": "s3", "repo1-path": "/exordos_db", **options},
        "schedule": {"full_interval_hours": 168, "incr_interval_hours": 24},
    }


def _backup(backup_type, stop, error=False):
    return {
        "type": backup_type,
        "error": error,
        "timestamp": {"start": stop - 60, "stop": stop},
    }


class TestS3Backup:
    def test_defaults(self):
        backup = _s3()

        assert backup.pgbackrest_repo_options() == {
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
        assert backup.full_interval_hours == 168
        assert backup.incr_interval_hours == 24

    def test_encryption_and_trailing_slash(self):
        options = _s3(
            endpoint="https://s3.example.com/",
            encryption_key="k3y",
            verify_tls=False,
        ).pgbackrest_repo_options()

        assert options["repo1-s3-endpoint"] == "https://s3.example.com"
        assert options["repo1-storage-verify-tls"] == "n"
        assert options["repo1-cipher-type"] == "aes-256-cbc"
        assert options["repo1-cipher-pass"] == "k3y"

    def test_none_disables(self):
        assert backups.BACKUP_TYPE.from_simple_type(None) is None

    def test_roundtrip(self):
        view = backups.BACKUP_TYPE.to_simple_type(_s3())
        assert backups.BACKUP_TYPE.from_simple_type(view) == _s3()

    @pytest.mark.parametrize(
        "field, value",
        [
            ("endpoint", "ftp://10.20.0.30:9000"),
            ("endpoint", "http://10.20.0.30:9000/bucket"),
            ("bucket", "Upper"),
            # Would inject an option into pgbackrest.conf
            ("secret_key", "secret\nrepo1-path=/other"),
            ("path", "relative"),
            ("path", "/exordos_db/../other"),
            ("path", "/exordos_db/.."),
            # Reached from the nodes, with errors reported through the API
            ("endpoint", "http://127.0.0.1:9000"),
            ("endpoint", "http://localhost:9000"),
            ("endpoint", "http://169.254.169.254"),
            ("endpoint", "http://[::1]:9000"),
            ("endpoint", "http://0.0.0.0:9000"),
            ("uri_style", "virtual"),
        ],
    )
    def test_invalid(self, field, value):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _s3(**{field: value})

    @pytest.mark.parametrize(
        "field, value",
        [
            ("path", "/exordos_db/a..b"),
            # The storage is often in the same private network
            ("endpoint", "http://10.20.0.26:9000"),
            ("endpoint", "https://s3.example.com"),
        ],
    )
    def test_valid(self, field, value):
        assert _s3(**{field: value}).storage_repo_options()

    def test_restore_source_endpoint_is_checked(self):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _restore_source(endpoint="http://169.254.169.254")

    @pytest.mark.parametrize("field", ["endpoint", "bucket", "access_key"])
    def test_required(self, field):
        view = {k: v for k, v in S3_VIEW.items() if k != field}
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            backups.BACKUP_TYPE.from_simple_type(view)


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
            "\n[1b1bc0de-0000-4000-8000-000000000001-rollbacks]\n"
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

    def test_apply_spec_keeps_credentials_for_postgres(self, tmp_path, monkeypatch):
        conf = tmp_path / "pgbackrest.conf"
        spec_file = tmp_path / "exordos_backup.json"
        monkeypatch.setattr(pgbackrest, "CONF_DIR", str(tmp_path))
        monkeypatch.setattr(pgbackrest, "CONF_FILE", str(conf))
        monkeypatch.setattr(pgbackrest, "SPEC_FILE", str(spec_file))
        monkeypatch.setattr(pgbackrest, "load_spec", lambda: None)
        owners = {}
        monkeypatch.setattr(
            pgbackrest.files.shutil,
            "chown",
            lambda path, user, group: owners.update({path: (user, group)}),
        )

        assert pgbackrest.apply_spec(_spec()) is True

        # Read by the backup timer and archive_command running as postgres
        for path in (conf, spec_file):
            assert path.stat().st_mode & 0o777 == 0o640
            assert owners[f"{path}.tmp"] == ("root", "postgres")

    def test_archive_command(self):
        assert pgbackrest.archive_command(None) == ":"
        assert pgbackrest.archive_command(_spec()) == (
            "pgbackrest --stanza=1b1bc0de-0000-4000-8000-000000000001 archive-push %p"
        )

    def test_fingerprint_follows_repository_only(self):
        base = pgbackrest.repo_fingerprint(_spec())

        queue = _spec(**{"archive-push-queue-max": "4GiB"})
        schedule = _spec()
        schedule["schedule"]["incr_interval_hours"] = 1
        # Changing it mustn't recreate the stanza against the repository
        retention = _spec(**{"repo1-retention-full": "7"})
        assert pgbackrest.repo_fingerprint(queue) == base
        assert pgbackrest.repo_fingerprint(schedule) == base
        assert pgbackrest.repo_fingerprint(retention) == base

        assert pgbackrest.repo_fingerprint(_spec(**{"repo1-path": "/x"})) != base
        stanza = _spec()
        stanza["stanza"] = "other"
        assert pgbackrest.repo_fingerprint(stanza) != base


SOURCE_UUID = "1b1bc0de-0000-4000-8000-000000000001"


def _restore_source(**kwargs):
    view = {**S3_VIEW, "stanza": SOURCE_UUID, **kwargs}
    return backups.RESTORE_SOURCE_TYPE.from_simple_type(view)


def _before_revision(revision):
    return {"kind": "before_revision", "revision": revision}


class TestS3RestoreSource:
    @pytest.mark.parametrize("target", [{}, {"target": {"kind": "latest"}}])
    def test_latest(self, target):
        # The end of the archive is what a source without a target replays to
        spec = _restore_source(encryption_key="k3y", **target).restore_spec()

        assert spec["stanza"] == SOURCE_UUID
        assert spec["target_time"] is None
        assert spec["before_revision"] is None
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
            # A target carries the fields of its kind and nothing else, so a
            # time and a state before a rollback can't be asked for together
            {"kind": "time", "time": "2026-01-14T10:30:15Z", "revision": 2},
            {"kind": "before_revision", "revision": 2, "time": "2026-01-14T10:30:15Z"},
            {"kind": "latest", "time": "2026-01-14T10:30:15Z"},
            {"kind": "time"},
            {"kind": "whenever"},
        ],
    )
    def test_unknown_targets_are_rejected(self, target):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _restore_source(target=target)

    def test_state_before_a_rollback(self):
        source = _restore_source(target=_before_revision(2))
        assert source.restore_spec()["before_revision"] == 2
        other = _restore_source(target=_before_revision(3))
        assert source.identity() != other.identity()

    def test_schedule_is_not_accepted(self):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _restore_source(retention_full=2)

    def test_stanza_required(self):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            backups.RESTORE_SOURCE_TYPE.from_simple_type(S3_VIEW)


def _repo_backup(label, timeline, lsn, stop, error=False, stop_lsn=None):
    return {
        "label": label,
        "error": error,
        "archive": {"start": f"{timeline:08X}0000000000000004"},
        "lsn": {"start": lsn, "stop": stop_lsn or lsn},
        "timestamp": {"start": stop - 2, "stop": stop},
        "database": {"id": 1, "repo-key": 1},
    }


# Taken from a cluster rolled back twice: timeline 2 forked from timeline 1
# after the first backup, a full backup was taken on timeline 2, and the
# second rollback forked timeline 3 from timeline 2 before that backup
HISTORY_3 = (
    "1\t0/501ACA0\tbefore 2026-09-14 15:11:18.586686+00\n\n\n"
    "2\t0/5017830\tbefore 2026-09-14 15:11:13.744683+00\n"
)
BEFORE_ROLLBACKS = _repo_backup("20260914-151105F", 1, "0/4000028", 1789398667)
ABANDONED = _repo_backup("20260914-151438F", 2, "0/7000028", 1789398881)
TARGET_AFTER_BOTH = "2026-09-14 15:20:01.330951+00"  # 1789399201.33


class TestBackupSet:
    @staticmethod
    def _timelines(latest=3, history=HISTORY_3):
        return lambda backup: pgbackrest.Timelines(
            latest, pgbackrest.parse_history(history)
        )

    def test_backup_on_an_abandoned_branch_is_skipped(self):
        chosen = pgbackrest.choose_backup_set(
            [BEFORE_ROLLBACKS, ABANDONED], TARGET_AFTER_BOTH, self._timelines()
        )
        assert chosen == BEFORE_ROLLBACKS["label"]

    def test_newest_backup_on_the_latest_timeline(self):
        on_latest = _repo_backup("20260914-152000F", 3, "0/9000028", 1789399100)
        chosen = pgbackrest.choose_backup_set(
            [BEFORE_ROLLBACKS, ABANDONED, on_latest],
            TARGET_AFTER_BOTH,
            self._timelines(),
        )
        assert chosen == on_latest["label"]

    def test_backup_has_to_finish_before_the_target(self):
        chosen = pgbackrest.choose_backup_set(
            [BEFORE_ROLLBACKS],
            "2026-09-14 15:11:06.000000+00",  # before its stop
            self._timelines(),
        )
        assert chosen is None

    def test_failed_backup_is_skipped(self):
        failed = _repo_backup("20260914-152000F", 3, "0/9000028", 1789399100, True)
        chosen = pgbackrest.choose_backup_set(
            [BEFORE_ROLLBACKS, failed], None, self._timelines()
        )
        assert chosen == BEFORE_ROLLBACKS["label"]

    def test_end_of_the_archive_without_rollbacks(self):
        newer = _repo_backup("20260914-152000I", 1, "0/9000028", 1789399100)
        chosen = pgbackrest.choose_backup_set(
            [BEFORE_ROLLBACKS, newer], None, self._timelines(1, "")
        )
        assert chosen == newer["label"]

    def test_no_backup_in_the_latest_history(self):
        chosen = pgbackrest.choose_backup_set(
            [ABANDONED], TARGET_AFTER_BOTH, self._timelines()
        )
        assert chosen is None

    def test_backup_running_over_the_fork_is_skipped(self):
        # Started on timeline 2 before timeline 3 forked from it, ended after:
        # the end of the backup is on the abandoned branch
        over_the_fork = _repo_backup(
            "20260914-151200F", 2, "0/5000028", 1789398800, stop_lsn="0/5020000"
        )
        chosen = pgbackrest.choose_backup_set(
            [BEFORE_ROLLBACKS, over_the_fork], TARGET_AFTER_BOTH, self._timelines()
        )
        assert chosen == BEFORE_ROLLBACKS["label"]

    def test_backup_ending_within_the_target_second_is_skipped(self):
        # The stop time has whole seconds only, the backup may have ended
        # after the target
        on_latest = _repo_backup("20260914-152000F", 3, "0/9000028", 1789399201)
        chosen = pgbackrest.choose_backup_set(
            [BEFORE_ROLLBACKS, on_latest], TARGET_AFTER_BOTH, self._timelines()
        )
        assert chosen == BEFORE_ROLLBACKS["label"]


class TestRestoreBackupSet:
    STANZA = "84022dd1-a9db-41de-9490-9ab07976d5a2"
    ARCHIVE = f"archive/{STANZA}/18-1"

    @pytest.fixture
    def calls(self, monkeypatch):
        outputs = {
            "info": json.dumps(
                [
                    {
                        "archive": [{"database": {"id": 1}, "id": "18-1"}],
                        "backup": [BEFORE_ROLLBACKS, ABANDONED],
                    }
                ]
            ),
            "repo-ls": json.dumps(
                {
                    "00000002.history": {"type": "file"},
                    "00000003.history": {"type": "file"},
                }
            ),
            "repo-get": HISTORY_3,
        }
        calls = []

        def run(stanza, *args, timeout=600):
            calls.append((stanza, args))
            command = next(a for a in args if not a.startswith("--"))
            return outputs[command]

        monkeypatch.setattr(pgbackrest, "run", run)
        return calls

    def test_history_of_the_latest_timeline_is_read(self, calls):
        chosen = pgbackrest.restore_backup_set(self.STANZA, TARGET_AFTER_BOTH)

        assert chosen == BEFORE_ROLLBACKS["label"]
        config = "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf"
        assert calls == [
            (self.STANZA, (config, "--output=json", "info")),
            (
                self.STANZA,
                (
                    config,
                    "--output=json",
                    "--filter=\\.history$",
                    "repo-ls",
                    self.ARCHIVE,
                ),
            ),
            (self.STANZA, (config, "repo-get", f"{self.ARCHIVE}/00000003.history")),
        ]

    def test_no_backup_fails(self, calls):
        with pytest.raises(pgbackrest.PgBackRestError):
            pgbackrest.restore_backup_set(self.STANZA, "2026-09-14 15:11:00.000000+00")


def _snapshot(label, revision, error=False):
    backup = {"label": label, "error": error, "type": "incr"}
    if revision is not None:
        backup["annotation"] = {"exordos-before-revision": revision}
    return backup


class TestSnapshots:
    STANZA = "84022dd1-a9db-41de-9490-9ab07976d5a2"

    @pytest.fixture
    def calls(self, monkeypatch):
        calls = []
        backups_listed = [
            _snapshot("F1", "1"),
            _snapshot("F1_I2", "2", error=True),
            _snapshot("F1_I3", None),
            # A retried job keeps the state again
            _snapshot("F1_I4", "1"),
        ]

        def run(stanza, *args, timeout=600):
            calls.append((stanza, args))
            return json.dumps([{"backup": backups_listed}]) if "info" in args else ""

        monkeypatch.setattr(pgbackrest, "run", run)
        return calls

    def test_newest_state_before_the_revision(self, calls):
        assert pgbackrest.find_snapshot(self.STANZA, 1) == "F1_I4"
        assert calls[0][0] == f"{self.STANZA}-rollbacks"

    @pytest.mark.parametrize("revision", ["2", "3"])
    def test_failed_or_missing_state_isnt_found(self, calls, revision):
        assert pgbackrest.find_snapshot(self.STANZA, revision) is None
        spec = {"stanza": self.STANZA, "target_time": None, "before_revision": revision}
        with pytest.raises(pgbackrest.PgBackRestError):
            pgbackrest.restore_set(spec)

    def test_empty_stanza(self, monkeypatch):
        monkeypatch.setattr(pgbackrest, "run", lambda *a, **kw: json.dumps([{}]))
        assert pgbackrest.find_snapshot(self.STANZA, 1) is None

    def test_state_is_restored_from_its_stanza(self, calls):
        spec = {"stanza": self.STANZA, "target_time": None, "before_revision": 1}
        assert pgbackrest.restore_set(spec) == (f"{self.STANZA}-rollbacks", "F1_I4")

    def test_state_is_kept_offline_and_annotated(self, calls):
        pgbackrest.take_snapshot(self.STANZA, "1")

        config = "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf"
        stanza = f"{self.STANZA}-rollbacks"
        assert calls == [
            (stanza, (config, "--no-online", "stanza-create")),
            (
                stanza,
                (
                    config,
                    "--no-online",
                    "--type=incr",
                    "--annotation=exordos-before-revision=1",
                    "backup",
                ),
            ),
        ]


class TestRestore:
    def test_args_before_a_rollback(self):
        spec = {"stanza": "s", "target_time": None, "before_revision": 2}
        assert pgbackrest.restore_args(spec, "F1") == [
            "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf",
            "--set=F1",
            "--type=default",
            "--target-timeline=current",
            "restore",
        ]

    def test_args_latest(self):
        assert pgbackrest.restore_args({"target_time": None}, "F1") == [
            "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf",
            "--set=F1",
            "restore",
        ]

    def test_args_target_time(self):
        spec = {"target_time": "2026-09-14 10:30:15.000250+00"}
        assert pgbackrest.restore_args(spec, "F1") == [
            "--config=/var/lib/postgresql/patroni/pgbackrest-restore.conf",
            "--set=F1",
            "--type=time",
            "--target=2026-09-14 10:30:15.000250+00",
            "--target-action=promote",
            "restore",
        ]

    @pytest.mark.parametrize(
        "restore, rolled_back", [(False, False), (True, False), (True, True)]
    )
    def test_patroni_config(self, restore, rolled_back):
        instance = types.SimpleNamespace(
            restore_from=_restore_source() if restore else None,
            rollback_revision=1 if rolled_back else None,
        )
        # A source of an in-place rollback isn't a bootstrap source
        restore = restore and not rolled_back

        config = yaml.safe_load(
            infra_builder.PATRONI_CONF_TEMPLATE.format(
                cluster_name="demo",
                node_name=str(uuid.uuid4()),
                node_ip="10.20.0.40",
                raft_partner_addrs=["10.20.0.40:5010"],
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

    def test_only_failed_full(self):
        assert self._choose([_backup("full", self.now - HOUR, error=True)]) == "full"

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

    def test_failed_incr_doesnt_count(self):
        backups = [
            _backup("full", self.now - 30 * HOUR),
            _backup("incr", self.now - HOUR, error=True),
        ]
        assert self._choose(backups) == "incr"

    def test_full_after_rollback(self):
        backups = [
            _backup("full", self.now - 30 * HOUR),
            _backup("incr", self.now - 2 * HOUR),
        ]
        rolled_back = self.now - HOUR
        assert (
            pgbackrest.choose_backup_type(
                backups, _spec()["schedule"], self.now, full_after=rolled_back
            )
            == "full"
        )

        backups.append(_backup("full", self.now - 10))
        assert (
            pgbackrest.choose_backup_type(
                backups, _spec()["schedule"], self.now, full_after=rolled_back
            )
            is None
        )

    def test_nothing_due(self):
        backups = [
            _backup("full", self.now - 30 * HOUR),
            _backup("diff", self.now - 2 * HOUR),
        ]
        assert self._choose(backups) is None
