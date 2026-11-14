"""A resized VM must not start PostgreSQL with the previous RAM profile."""

from pathlib import Path

import pytest
import yaml

from exordos_db.common import pg_tuning

ROOT = Path(__file__).resolve().parents[3]


def test_startup_uses_current_ram_before_cached_cluster_settings(tmp_path, monkeypatch):
    from exordos_db.cmd import pg_startup

    managed = tmp_path / "patroni.yml"
    original = {
        "name": "node",
        "restapi": {"authentication": {"password": "fixture"}},
        "postgresql": {"parameters": {"io_method": "io_uring"}},
    }
    managed.write_text(yaml.safe_dump(original))
    directory = tmp_path / "runtime"
    monkeypatch.setattr(pg_tuning, "node_resources", lambda: (8, 3908))
    pg_startup.prepare_config(managed, directory)
    merged = {"postgresql": {"parameters": {"shared_buffers": "1984MB"}}}
    for path in sorted(directory.glob("*.yml")):
        config = yaml.safe_load(path.read_text())
        if "name" in config:
            assert config == original
        merged["postgresql"]["parameters"].update(
            config.get("postgresql", {}).get("parameters", {})
        )
    assert merged["postgresql"]["parameters"]["shared_buffers"] == "977MB"
    assert merged["postgresql"]["parameters"]["io_method"] == "io_uring"
    assert managed.read_text() == yaml.safe_dump(original)
    assert (directory / "00-managed.yml").resolve() == managed
    assert (directory / "10-memory.yml").stat().st_mode & 0o777 == 0o600
    # Core may replace its managed file later; Patroni must read the new file.
    replacement = tmp_path / "new.yml"
    replacement.write_text("name: updated\n")
    replacement.replace(managed)
    assert (
        yaml.safe_load((directory / "00-managed.yml").read_text())["name"] == "updated"
    )


def test_startup_recalculates_on_each_service_start(tmp_path, monkeypatch):
    from exordos_db.cmd import pg_startup

    managed = tmp_path / "patroni.yml"
    managed.write_text("name: node\n")
    directory = tmp_path / "runtime"
    for ram, expected in [(7936, "1984MB"), (3908, "977MB")]:
        monkeypatch.setattr(pg_tuning, "node_resources", lambda ram=ram: (8, ram))
        pg_startup.prepare_config(managed, directory)
        config = yaml.safe_load((directory / "10-memory.yml").read_text())
        assert config["postgresql"]["parameters"]["shared_buffers"] == expected


def test_service_starts_patroni_with_generated_directory():
    service = (ROOT / "etc/systemd/exordos-patroni.service").read_text()
    assert "RuntimeDirectory=exordos-db-patroni" in service
    assert "ExecStartPre=/usr/bin/exordos-db-pg-startup" in service
    assert (
        "ExecStart=/var/lib/postgresql/patroni/venv/bin/patroni /run/exordos-db-patroni\n"
        in service
    )
    installer = (ROOT / "exordos/images/pg_install.sh").read_text()
    assert '"$VENV_PATH/bin/exordos-db-pg-startup"' in installer


def test_resource_detection_failure_does_not_publish_old_profile(tmp_path, monkeypatch):
    from exordos_db.cmd import pg_startup

    def unavailable():
        raise OSError("resources unavailable")

    monkeypatch.setattr(pg_tuning, "node_resources", unavailable)
    with pytest.raises(OSError, match="resources unavailable"):
        pg_startup.prepare_config(tmp_path / "managed.yml", tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()
