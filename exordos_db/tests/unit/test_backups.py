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

import pytest
from restalchemy.common import exceptions as ra_exc

from exordos_db.common import pgbackrest
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
        assert pgbackrest.repo_fingerprint(queue) == base
        assert pgbackrest.repo_fingerprint(schedule) == base

        assert pgbackrest.repo_fingerprint(_spec(**{"repo1-path": "/x"})) != base
        stanza = _spec()
        stanza["stanza"] = "other"
        assert pgbackrest.repo_fingerprint(stanza) != base


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
