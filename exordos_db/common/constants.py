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

# project
GLOBAL_SERVICE_NAME = "exordos_db"
WORK_DIR = "/var/lib/exordos/exordos_db"

PATRONI_DIR = "/var/lib/postgresql/patroni"
PATRONI_CONFIG_FILE = f"{PATRONI_DIR}/patroni.yml"
PATRONI_API_PORT = 8008
PATRONI_API_ENDPOINT = f"http://127.0.0.1:{PATRONI_API_PORT}"

# vmagent of the base image renders its scrape config from this template at
# every start, with the node host name in place of __HOSTNAME__, and relays
# the metrics to the platform VictoriaMetrics. It only runs once the
# observability element is deployed.
VMAGENT_SCRAPE_TEMPLATE = "/etc/exordos_observability/vmagent_scrape.yml.tpl"
NODE_EXPORTER_ENDPOINT = "127.0.0.1:9100"
POSTGRES_EXPORTER_ENDPOINT = "127.0.0.1:9187"
# A second postgres_exporter probes every database of the instance for the
# per-table statistics, which PostgreSQL only shows to a session of the same
# database. The agent keeps the list of databases in a file_sd file of
# vmagent, which rereads it every minute.
POSTGRES_DB_EXPORTER_ENDPOINT = "127.0.0.1:9188"
VMAGENT_DATABASES_SD_FILE = f"{WORK_DIR}/vmagent_databases.json"
