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

# The image replaces the scrape config template of vmagent of the base image
# with etc/exordos_observability/vmagent_scrape.yml.tpl. The agent labels
# the series with the instance, its project and the engine in the file_sd
# files the template reads, which vmagent rereads every minute: the one of
# the node and the one of the databases, which the second postgres_exporter
# probes one by one for the per-table statistics PostgreSQL shows only to a
# session of the same database.
VMAGENT_NODE_SD_FILE = f"{WORK_DIR}/vmagent_node.json"
VMAGENT_DATABASES_SD_FILE = f"{WORK_DIR}/vmagent_databases.json"
