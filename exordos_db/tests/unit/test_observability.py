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

import configparser
from pathlib import Path
import uuid as sys_uuid

import yaml

from exordos_db.infra.dm import models
from exordos_db.infra.services import builder

ROOT = Path(__file__).resolve().parents[3]
INSTANCE_UUID = sys_uuid.UUID("11111111-1111-1111-1111-111111111111")
PROJECT_ID = sys_uuid.UUID("22222222-2222-2222-2222-222222222222")
NODE_UUID = sys_uuid.UUID("33333333-3333-3333-3333-333333333333")
LABELS = {
    "instance": "__HOSTNAME__",
    "exordos_db_instance": str(INSTANCE_UUID),
    "exordos_project": str(PROJECT_ID),
}


def test_scrape_config_labels_every_job_with_the_instance():
    content = builder.render_vmagent_scrape(INSTANCE_UUID, PROJECT_ID)

    jobs = yaml.safe_load(content)["scrape_configs"]

    assert [(j["job_name"], j["static_configs"]) for j in jobs] == [
        ("node_exporter", [{"targets": ["127.0.0.1:9100"], "labels": LABELS}]),
        ("patroni", [{"targets": ["127.0.0.1:8008"], "labels": LABELS}]),
        ("postgres_exporter", [{"targets": ["127.0.0.1:9187"], "labels": LABELS}]),
    ]
    assert {j["scrape_interval"] for j in jobs} == {"15s"}


def test_scrape_config_replaces_the_base_image_template():
    instance = models.PGInstance(
        uuid=INSTANCE_UUID,
        project_id=PROJECT_ID,
        version=models.models.PGVersion(image="image"),
    )

    config = instance._create_vmagent_config(NODE_UUID, PROJECT_ID, "content")

    assert config.path == "/etc/exordos_observability/vmagent_scrape.yml.tpl"
    assert config.target.node == NODE_UUID
    assert config.body.content == "content"
    assert (config.owner, config.group, config.mode) == ("root", "root", "0644")
    # Neither the config nor its hook may touch Patroni or PostgreSQL
    assert config.uuid != instance._create_config(NODE_UUID, PROJECT_ID).uuid
    assert config.on_change.command == (
        "if systemctl cat exordos-vmagent >/dev/null 2>&1; then "
        "systemctl --no-block try-restart exordos-vmagent; fi"
    )


def test_postgres_exporter_listens_locally_and_is_enabled():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(ROOT / "etc/systemd/exordos-postgres-exporter.service")

    assert parser["Service"]["User"] == "postgres"
    assert "--web.listen-address=127.0.0.1:9187" in parser["Service"]["ExecStart"]
    install = (ROOT / "exordos/images/pg_install.sh").read_text()
    assert "sudo systemctl enable exordos-postgres-exporter\n" in install
