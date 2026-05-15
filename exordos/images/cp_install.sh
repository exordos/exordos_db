#!/usr/bin/env bash

# Copyright 2025 Genesis Corporation
#
# All Rights Reserved.
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

set -eu
set -x
set -o pipefail


GC_PATH="/opt/exordos_db"
GC_CFG_DIR=/etc/exordos_db
VENV_PATH="$GC_PATH/.venv"
BOOTSTRAP_PATH="/var/lib/exordos/bootstrap/scripts"

PG_VERSION="18"

SYSTEMD_SERVICE_DIR=/etc/systemd/system/

DEV_SDK_PATH="/opt/gcl_sdk"
SDK_DEV_MODE=$([ -d "$DEV_SDK_PATH" ] && echo "true" || echo "false")

# Install packages
sudo apt update
sudo apt install -y \
    postgresql-common \
    libev-dev \
    j2cli

sudo YES=1 /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt-get update
sudo apt -y install "postgresql-${PG_VERSION}"
sudo systemctl disable --now "postgresql"

# Note: PostgreSQL database and user creation is done in bootstrap.sh
# on the persistent disk to ensure data survives OS image updates

# Install exordos db
sudo mkdir -p $GC_CFG_DIR
sudo cp "$GC_PATH/etc/exordos_db/exordos_db.conf.j2" $GC_CFG_DIR/
sudo cp "$GC_PATH/etc/exordos_db/core_agent.conf.j2" $GC_CFG_DIR/
sudo cp "$GC_PATH/etc/exordos_db/logging.yaml" $GC_CFG_DIR/
sudo cp "$GC_PATH/exordos/images/cp_bootstrap.sh" $BOOTSTRAP_PATH/0100-ec-bootstrap.sh

cd "$GC_PATH"
uv sync
source "$GC_PATH"/.venv/bin/activate

# In the dev mode the gcl_sdk package is installed from the local machine
if [[ "$SDK_DEV_MODE" == "true" ]]; then
    uv pip uninstall -y gcl_sdk
    uv pip install -e "$DEV_SDK_PATH"
fi
deactivate

# Create links to venv
sudo ln -sf "$VENV_PATH/bin/exordos-db-gservice" "/usr/bin/exordos-db-gservice"
sudo ln -sf "$VENV_PATH/bin/exordos-db-user-api" "/usr/bin/exordos-db-user-api"
sudo ln -sf "$VENV_PATH/bin/exordos-db-status-api" "/usr/bin/exordos-db-status-api"
sudo ln -sf "$VENV_PATH/bin/exordos-db-orch-api" "/usr/bin/exordos-db-orch-api"
sudo ln -sf "$VENV_PATH/bin/genesis-universal-agent-db-back" "/usr/bin/exordos-universal-agent-db-back"

# Install Systemd service files
sudo cp "$GC_PATH/etc/systemd/exordos-db-gservice.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/exordos-db-user-api.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/exordos-db-status-api.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/exordos-db-orch-api.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/exordos-db-core-agent.service" $SYSTEMD_SERVICE_DIR
