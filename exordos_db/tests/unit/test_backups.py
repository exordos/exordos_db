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
            ("uri_style", "virtual"),
        ],
    )
    def test_invalid(self, field, value):
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _s3(**{field: value})

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
    def test_latest(self):
        spec = _restore_source(encryption_key="k3y").restore_spec()

        assert spec["stanza"] == SOURCE_UUID
        assert spec["target_time"] is None
        assert spec["options"]["repo1-s3-endpoint"] == "http://10.20.0.30:9000"
        assert spec["options"]["repo1-cipher-pass"] == "k3y"
        # Retention is a matter of the instance taking backups
        assert "repo1-retention-full" not in spec["options"]

    def test_target_time_in_utc(self):
        source = _restore_source(target_time="2026-01-14T13:30:15.000250+03:00")
        assert source.restore_spec()["target_time"] == "2026-01-14 10:30:15.000250+00"

    def test_future_target_time(self):
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=1
        )
        with pytest.raises((ra_exc.ParseError, ValueError, TypeError)):
            _restore_source(target_time=future.isoformat())

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

    def test_nothing_due(self):
        backups = [
            _backup("full", self.now - 30 * HOUR),
            _backup("diff", self.now - 2 * HOUR),
        ]
        assert self._choose(backups) is None
