"""Execute the shipped startup predicate without opening a real watchdog."""

import os
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).resolve().parents[3]


def run_guard(tmp_path, *, appears):
    service = (ROOT / "etc/systemd/exordos-patroni.service").read_text()
    commands = [
        line.split("=", 1)[1]
        for line in service.splitlines()
        if line.startswith("ExecStartPre=") and "/bin/sh" in line
    ]
    assert len(commands) == 1
    # systemd expands $$ to a literal $ before invoking the shell.
    command = (
        commands[0]
        .replace("$$", "$")
        .replace("/dev/watchdog", str(tmp_path / "device"))
    )
    sleep = tmp_path / "sleep"
    sleep.write_text(
        '#!/bin/sh\nprintf "wait\\n" >> "$WAIT_LOG"\n'
        + ('touch "$TEST_DEVICE"\n' if appears else "")
    )
    sleep.chmod(0o755)
    result = subprocess.run(
        shlex.split(command),
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "WAIT_LOG": str(tmp_path / "waits"),
            "TEST_DEVICE": str(tmp_path / "device"),
        },
        capture_output=True,
        text=True,
        timeout=3,
    )
    return result, (tmp_path / "waits").read_text().splitlines()


def test_missing_watchdog_blocks_patroni_instead_of_caching_failed_activation(tmp_path):
    result, waits = run_guard(tmp_path, appears=False)
    assert result.returncode != 0, "Patroni would start without its required watchdog"
    assert len(waits) == 25


def test_late_watchdog_allows_a_fresh_patroni_start(tmp_path):
    result, waits = run_guard(tmp_path, appears=True)
    assert result.returncode == 0, result.stderr
    assert waits == ["wait"]


def test_softdog_is_loaded_before_waiting_without_a_multiuser_ordering_cycle():
    service = (ROOT / "etc/systemd/exordos-patroni.service").read_text()
    assert service.index("ExecStartPre=-+/sbin/modprobe softdog") < service.index(
        "ExecStartPre=/bin/sh"
    )
    # watchdog.service itself is ordered after multi-user.target.
    assert "After=watchdog.service" not in service
