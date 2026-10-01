#    Copyright 2025 Genesis Corporation.
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

"""Exercise the bootstrap config refresh without touching host services."""

import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "persisted-password-must-not-be-traced"


@pytest.mark.parametrize("old_mode", [0o600, 0o644])
@pytest.mark.parametrize("render_fails", [False, True])
def test_config_refresh_keeps_credentials_private(tmp_path, old_mode, render_fails):
    config = tmp_path / "core_agent.conf"
    original = f"[db]\nconnection_url = postgresql://db:{PASSWORD}@localhost/db\n[models]\nold = model\n"
    config.write_text(original)
    config.chmod(old_mode)
    template = tmp_path / "etc/exordos_db/core_agent.conf.j2"
    template.parent.mkdir(parents=True)
    template.write_text("[db]\nconnection_url = placeholder\n[models]\nnew = model\n")
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    Path(str(config) + ".new").symlink_to(victim)
    source = (ROOT / "exordos/images/cp_bootstrap.sh").read_text()
    block = source[source.index("(\n    set +x") :].split("\n)\n", 1)[0] + "\n)\n"
    script = (
        """
set -euxo pipefail
umask 022
sudo() { "$@"; }
j2() {
    cat "$1"
    [[ "$RENDER_FAILS" == 0 ]]
}
"""
        + block
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env=dict(
            os.environ,
            CORE_AGENT_CONFIG=str(config),
            GC_PATH=str(tmp_path),
            RENDER_FAILS=str(int(render_fails)),
        ),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert PASSWORD not in result.stdout + result.stderr
    assert victim.read_text() == "untouched"
    assert not list(tmp_path.glob("core_agent.conf.??????"))
    if render_fails:
        assert result.returncode != 0
        assert config.read_text() == original
        assert config.stat().st_mode & 0o777 == old_mode
    else:
        assert result.returncode == 0, result.stderr
        assert (
            f"connection_url = postgresql://db:{PASSWORD}@localhost/db"
            in config.read_text()
        )
        assert "new = model" in config.read_text()
        assert "old = model" not in config.read_text()
        assert config.stat().st_mode & 0o777 == 0o600
