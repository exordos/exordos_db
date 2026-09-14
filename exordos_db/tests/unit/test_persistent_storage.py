"""First-boot storage and systemd contracts; never touch host services."""

import configparser
import os
from pathlib import Path
import subprocess

import pytest

from exordos_db.infra.dm import models

ROOT = Path(__file__).resolve().parents[3]
DATA = "/var/lib/postgresql/patroni/data"
RAFT = "/var/lib/postgresql/patroni/raft"


def unit(name):
    # systemd allows repeated assertions; ConfigParser keeps only the last one.
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(ROOT / "etc/systemd" / name)
    return parser


def test_agent_waits_but_survives_bootstrap_failure():
    agent = unit("exordos-db-pg-agent.service")
    assert "exordos-bootstrap.service" in agent["Unit"]["After"].split()
    assert "exordos-bootstrap.service" in agent["Unit"]["Wants"].split()
    assert "exordos-bootstrap.service" not in agent["Unit"].get("Requires", "")
    install = (ROOT / "exordos/images/pg_install.sh").read_text()
    assert "sudo systemctl enable exordos-db-pg-agent\n" in install


def test_config_delivery_agent_also_waits_for_bootstrap():
    dropin = "exordos-universal-agent.service.d/pg-bootstrap.conf"
    config_agent = unit(dropin)
    assert "exordos-bootstrap.service" in config_agent["Unit"]["After"].split()
    assert "exordos-bootstrap.service" in config_agent["Unit"]["Wants"].split()
    assert "Requires" not in config_agent["Unit"]
    install = (ROOT / "exordos/images/pg_install.sh").read_text()
    assert f"$GC_PATH/etc/systemd/{dropin}" in install
    assert f"${{SYSTEMD_SERVICE_DIR}}/{dropin}" in install


def test_patroni_refuses_missing_mounts_instead_of_silently_skipping():
    service = unit("exordos-patroni.service")
    paths = {"/persist", DATA, RAFT}
    assert set(service["Unit"]["RequiresMountsFor"].split()) == paths
    text = (ROOT / "etc/systemd/exordos-patroni.service").read_text()
    assertions = {
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith("AssertPathIsMountPoint=")
    }
    assert assertions == paths
    assert "ConditionPathIsMountPoint" not in text
    assert service["Unit"]["ConditionPathExists"].endswith("/patroni.yml")
    assert "ConditionPathExists" not in service["Service"]
    # Bootstrap starts Patroni synchronously after migration. Ordering Patroni
    # after bootstrap would deadlock this call.
    assert "exordos-bootstrap.service" not in service["Unit"]["After"]


def test_first_configuration_starts_previously_skipped_patroni():
    assert models.PGInstance.OnReloadFunc.command == (
        "systemctl reload-or-restart exordos-patroni"
    )


@pytest.fixture
def bootstrap(tmp_path):
    log = tmp_path / "calls"
    lib = tmp_path / "lib.sh"
    lib.write_text(
        """
PERSISTENT_MOUNT=/persist
record() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
find_persistent_disk() {
    record find_disk
    [[ "${FAIL_AT:-}" != find ]] || return 1
    printf '/dev/test-disk\\n'
}
prepare_persistent_disk() {
    record prepare "$@"
    [[ "${FAIL_AT:-}" != prepare ]]
}
migrate_to_persistent_restart() { record logs "$@"; }
migrate_to_persistent() {
    record migrate "$@"
    [[ "$1" != "${FAIL_AT:-}" ]]
}
persist_migrate_complete() { record complete; }
sudo() {
    record "$@"
    [[ "${FAIL_AT:-}" != stop || "$*" != 'systemctl stop exordos-patroni' ]]
}
"""
    )

    def run(fail_at=""):
        log.write_text("")
        env = dict(
            os.environ,
            EXORDOS_BOOTSTRAP_LIB=str(lib),
            CALL_LOG=str(log),
            FAIL_AT=fail_at,
        )
        result = subprocess.run(
            ["bash", str(ROOT / "exordos/images/pg_bootstrap.sh")],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result, log.read_text().splitlines()

    return run


def test_bootstrap_keeps_patroni_stopped_until_both_paths_are_migrated(bootstrap):
    result, calls = bootstrap()
    assert result.returncode == 0, result.stderr
    assert calls == [
        "systemctl stop exordos-patroni",
        "find_disk",
        "prepare /dev/test-disk /persist",
        "logs /var/log /persist/var/log systemd-journald rsyslog",
        f"migrate {DATA} /persist{DATA}",
        f"migrate {RAFT} /persist{RAFT}",
        "complete",
        "systemctl enable --now exordos-patroni",
    ]


@pytest.mark.parametrize("fail_at", ["stop", "find", "prepare", DATA, RAFT])
def test_failed_bootstrap_does_not_start_patroni_and_can_be_retried(bootstrap, fail_at):
    result, calls = bootstrap(fail_at)
    assert result.returncode != 0
    assert "complete" not in calls
    assert "systemctl enable --now exordos-patroni" not in calls
    result, calls = bootstrap()
    assert result.returncode == 0, result.stderr
    assert calls[-1] == "systemctl enable --now exordos-patroni"
