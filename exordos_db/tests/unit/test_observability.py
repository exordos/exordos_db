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
import json
from pathlib import Path
import re
import uuid as sys_uuid

from gcl_sdk.agents.universal.dm import models as ua_models
import yaml

from exordos_db.agent.universal.drivers import pg
from exordos_db.common import constants
from exordos_db.paas.dm import models as paas_models

ROOT = Path(__file__).resolve().parents[3]
INSTANCE_UUID = sys_uuid.UUID("11111111-1111-1111-1111-111111111111")
PROJECT_ID = sys_uuid.UUID("22222222-2222-2222-2222-222222222222")
LABELS = {
    "exordos_db_instance": str(INSTANCE_UUID),
    "exordos_db_type": "postgres",
    "exordos_project": str(PROJECT_ID),
}


def _relabels(address):
    return [
        {"target_label": "instance", "replacement": "__HOSTNAME__"},
        {"target_label": "__address__", "replacement": address},
    ]


def test_scrape_config_reads_the_targets_of_the_agent():
    content = (ROOT / "etc/exordos_observability/vmagent_scrape.yml.tpl").read_text()

    *jobs, databases = yaml.safe_load(content)["scrape_configs"]

    node_sd = [{"files": [constants.VMAGENT_NODE_SD_FILE]}]
    assert [
        (j["job_name"], j["file_sd_configs"], j["relabel_configs"]) for j in jobs
    ] == [
        ("node_exporter", node_sd, _relabels("127.0.0.1:9100")),
        ("patroni", node_sd, _relabels(f"127.0.0.1:{constants.PATRONI_API_PORT}")),
        ("postgres_exporter", node_sd, _relabels("127.0.0.1:9187")),
    ]
    assert {j["scrape_interval"] for j in jobs} == {"15s"}
    assert databases == {
        "job_name": "postgres_exporter_databases",
        "scrape_interval": "60s",
        "series_limit": 30000,
        "metrics_path": "/probe",
        "file_sd_configs": [{"files": [constants.VMAGENT_DATABASES_SD_FILE]}],
        "relabel_configs": _relabels("127.0.0.1:9188"),
        # Vacuums of the whole instance come with every database, table
        # names only with the one of the probe
        "metric_relabel_configs": [
            {
                "if": '{__name__=~"pg_stat_progress_vacuum_.+"}',
                "action": "keep_if_equal",
                "source_labels": ["datname", "database"],
            }
        ],
    }
    install = (ROOT / "exordos/images/pg_install.sh").read_text()
    assert (
        'sudo cp "$GC_PATH/etc/exordos_observability/vmagent_scrape.yml.tpl" '
        "/etc/exordos_observability/\n"
    ) in install


def test_scrape_labels_leave_out_an_unknown_project():
    assert pg.scrape_labels(str(INSTANCE_UUID), str(PROJECT_ID)) == LABELS
    assert pg.scrape_labels(str(INSTANCE_UUID), None) == {
        "exordos_db_instance": str(INSTANCE_UUID),
        "exordos_db_type": "postgres",
    }


def test_database_scrape_targets_carry_a_url_dsn():
    targets = pg.database_scrape_targets(["b", "a"], LABELS)

    assert targets == [
        {
            "targets": [name],
            "labels": {
                **LABELS,
                "database": name,
                "__param_target": (
                    f"postgresql://postgres@/{name}"
                    "?host=/var/run/postgresql&sslmode=disable"
                ),
            },
        }
        for name in ("a", "b")
    ]


def test_scrape_targets_are_written_on_change_only(tmp_path):
    path = tmp_path / "vmagent_node.json"
    targets = pg.node_scrape_targets(LABELS)

    assert pg.write_scrape_targets(str(path), targets)
    assert json.loads(path.read_text()) == [{"targets": ["node"], "labels": LABELS}]
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    mtime = path.stat().st_mtime_ns

    assert not pg.write_scrape_targets(str(path), targets)
    assert path.stat().st_mtime_ns == mtime

    assert pg.write_scrape_targets(str(path), [])
    assert json.loads(path.read_text()) == []
    assert list(tmp_path.iterdir()) == [path]


def test_node_resource_carries_the_project():
    node = paas_models.PGInstanceNode(
        uuid=INSTANCE_UUID, name=str(INSTANCE_UUID), project_id=PROJECT_ID
    )
    assert "project_id" in node.get_resource_target_fields()

    agent = pg.PGInstance.from_ua_resource(
        ua_models.Resource(
            uuid=INSTANCE_UUID,
            kind="pg_instance_node",
            value={
                "uuid": str(INSTANCE_UUID),
                "name": str(INSTANCE_UUID),
                "project_id": str(PROJECT_ID),
            },
        )
    )
    assert agent._scrape_labels() == LABELS
    assert "project_id" in agent.get_meta_model_fields()


def test_postgres_exporter_listens_locally_and_is_enabled():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(ROOT / "etc/systemd/exordos-postgres-exporter.service")

    assert parser["Service"]["User"] == "postgres"
    assert "--web.listen-address=127.0.0.1:9187" in parser["Service"]["ExecStart"]
    install = (ROOT / "exordos/images/pg_install.sh").read_text()
    assert (
        "sudo systemctl enable exordos-postgres-exporter "
        "exordos-postgres-exporter-databases\n"
    ) in install


def test_database_exporter_only_collects_per_table_statistics():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(ROOT / "etc/systemd/exordos-postgres-exporter-databases.service")
    main = configparser.ConfigParser(interpolation=None, strict=False)
    main.optionxform = str  # type: ignore[assignment,method-assign]
    main.read(ROOT / "etc/systemd/exordos-postgres-exporter.service")

    args = parser["Service"]["ExecStart"].split()
    main_args = main["Service"]["ExecStart"].split()

    assert parser["Service"]["User"] == "postgres"
    assert "--web.listen-address=127.0.0.1:9188" in args
    assert {"--disable-default-metrics", "--disable-settings-metrics"} <= set(args)
    # What one exporter leaves out, the other one collects
    for collector in ("stat_user_tables", "statio_user_tables", "stat_progress_vacuum"):
        assert f"--no-collector.{collector}" in main_args
        assert f"--no-collector.{collector}" not in args
    for collector in ("database", "stat_database", "locks", "wal"):
        assert f"--no-collector.{collector}" in args
        assert f"--no-collector.{collector}" not in main_args


def _resources() -> dict:
    manifest = (ROOT / "exordos/manifests/dbaas_dashboard.yaml.j2").read_text()
    # The only Jinja in it: the version and the string literals of legends
    rendered = re.sub(
        r"\{\{ '([^'\n]*)' \}\}",
        r"\1",
        manifest.replace("{{ version }}", "0.0.0"),
    )
    return yaml.safe_load(rendered)["resources"]


def _dashboards() -> dict[str, dict]:
    resources = _resources()["$grafanaaas.types.grafana.dashboards"]
    return {name: r["source"]["content"] for name, r in resources.items()}


def _panels():
    for dashboard in _dashboards().values():
        for panel in dashboard["panels"]:
            if panel["type"] != "row":
                yield panel


def test_dashboards_are_grouped_by_engine():
    dashboards = _dashboards()
    bindings = _resources()["$dbaas_dashboard.imports.$grafana_instance.dashboards"]

    assert set(dashboards) == {"postgres_instance", "postgres_tables"}
    for dashboard in dashboards.values():
        assert dashboard["uid"].startswith("exordos-dbaas-postgres-")
        assert dashboard["tags"] == ["dbaas", "postgresql"]
        assert dashboard["links"][0]["tags"] == ["dbaas", "postgresql"]
        variables = {v["name"]: v for v in dashboard["templating"]["list"]}
        assert 'exordos_db_type="postgres"' in variables["project"]["definition"]
    assert {b["name"] for b in bindings.values()} == set(dashboards)
    assert {b["folder"] for b in bindings.values()} == {"DBaaS"}


def test_dashboard_panels_select_the_chosen_instance():
    panels = list(_panels())

    for panel in panels:
        for target in panel["targets"]:
            # Metrics by the label of the control plane or the node host
            # name, logs by the node host name
            assert (
                'exordos_db_instance="$db"' in target["expr"]
                or "dbaas-dp-$db-node-" in target["expr"]
            ), panel["title"]
    logs = [p for p in panels if "datasource" in p]
    assert {p["datasource"]["type"] for p in logs} == {
        "victoriametrics-logs-datasource"
    }
    # By name: the observability element doesn't export the datasource
    assert {p["datasource"]["uid"] for p in logs} == {"victoria-logs"}


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def test_dashboard_leaves_values_starting_with_dollar_to_manifest_links():
    # The element manager renders such a value as a link, a Grafana ${var}
    # there stops it
    starting = {s for s in _strings(_dashboards()) if s.startswith("$")}

    assert starting == set()


def test_dashboard_imports_only_what_the_observability_element_exports():
    manifest = (ROOT / "exordos/manifests/dbaas_dashboard.yaml.j2").read_text()
    imports = yaml.safe_load(manifest.replace("{{ version }}", "0"))["imports"]

    # The element manager refuses the whole element otherwise
    assert {i["link"] for i in imports.values()} == {
        "$grafanaaas.types.grafana.instances.$grafana"
    }


def test_dashboard_legends_survive_the_manifest_template():
    legends = {
        t["legendFormat"]
        for p in _panels()
        for t in p.get("targets", ())
        if t.get("legendFormat")
    }

    assert "{{instance}}" in legends
    assert "{{datname}}.{{schemaname}}.{{relname}}" in legends
