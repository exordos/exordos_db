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
WORK_DIR="/var/lib/exordos/exordos_db"
VENV_PATH="$GC_PATH/.venv"
BOOTSTRAP_PATH="/var/lib/exordos/bootstrap/scripts"

SYSTEMD_SERVICE_DIR=/etc/systemd/system/

DEV_SDK_PATH="/opt/gcl_sdk"
SDK_DEV_MODE=$([ -d "$DEV_SDK_PATH" ] && echo "true" || echo "false")

PG_VERSION="18"

# Install packages
sudo apt update
sudo apt dist-upgrade -y
sudo apt install -y \
    libev-dev yq watchdog

# Install exordos db
sudo mkdir -p $GC_CFG_DIR
sudo mkdir -p $WORK_DIR
sudo cp "$GC_PATH/etc/exordos_db/exordos_pg_agent.conf" $GC_CFG_DIR/
sudo cp "$GC_PATH/etc/exordos_db/logging.yaml" $GC_CFG_DIR/
sudo cp "$GC_PATH/exordos/images/pg_bootstrap.sh" $BOOTSTRAP_PATH/0100-ec-bootstrap.sh

cd "$GC_PATH"
uv sync
source "$GC_PATH/.venv/bin/activate"

# In the dev mode the gcl_sdk package is installed from the local machine
if [[ "$SDK_DEV_MODE" == "true" ]]; then
    uv pip uninstall -y gcl_sdk
    uv pip install -e "$DEV_SDK_PATH"
fi

# Create links to venv
sudo ln -sf "$VENV_PATH/bin/exordos-universal-agent" "/usr/bin/exordos-db-pg-agent"

deactivate

# Install Systemd service files
sudo cp "$GC_PATH/etc/systemd/exordos-db-pg-agent.service" $SYSTEMD_SERVICE_DIR

# Enable exordos db services
sudo systemctl enable exordos-db-pg-agent


# Patroni

# Install packages
sudo apt-get update
sudo apt-get install postgresql-common -y
sudo YES=1 /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt-get update
sudo apt -y install "postgresql-${PG_VERSION}"
sudo systemctl disable --now "postgresql@${PG_VERSION}-main"
sudo systemctl disable --now postgresql
sudo ln -s /usr/lib/postgresql/$PG_VERSION/bin/* /usr/sbin/

# Setup watchdog
cat <<EOF | sudo tee /etc/udev/rules.d/99-watchdog.rules
KERNEL=="watchdog", OWNER="postgres", GROUP="postgres"
EOF
sudo sh -c 'echo "softdog" >> /etc/modules-load.d/softdog.conf'
sudo sed -i 's/watchdog_module="none"/watchdog_module="softdog"/' /etc/default/watchdog

# Prepare patroni
sudo su postgres <<'EOF'
cd $HOME
mkdir -p patroni
cd patroni
python3 -m venv venv
source venv/bin/activate
pip install "psycopg[binary]" "patroni[raft]>=4.1.0"

mkdir -p data
chmod 750 data
mkdir -p raft
chmod 770 raft
EOF

# Install Systemd service files
sudo cp "$GC_PATH/etc/systemd/exordos-patroni.service" $SYSTEMD_SERVICE_DIR

# Add some usability
sudo ln -sf "/var/lib/postgresql/patroni/venv/bin/patronictl" "/usr/bin/patronictl"
sudo ln -sf "/var/lib/postgresql/patroni/venv/bin/syncobj_admin" "/usr/bin/syncobj_admin"
sudo usermod -a -G postgres ubuntu
# TODO remove wrapper when this script will be executed with ubuntu user
sudo su ubuntu <<'EOF'
mkdir -p ~/.config/patroni
ln -sf "/var/lib/postgresql/patroni/patroni.yml" ~/.config/patroni/patronictl.yaml
ln -sf "/var/lib/postgresql/patroni" ~/patroni
EOF

# Lab mode
LAB_MODE=${LAB_MODE-}
if [ -n "$LAB_MODE" ]; then
    # set password for main user
    echo "ubuntu:ubuntu" | sudo chpasswd
    sudo rm /etc/ssh/sshd_config.d/60-cloudimg-settings.conf
    sudo yq -yi '.system_info.default_user.lock_passwd |= false' /etc/cloud/cloud.cfg

    # copy lab materials into home dir
    rsync -a "$GC_PATH/lab/" /home/ubuntu/

    # NOTE: don't use exordos build -i, it'll override these keys!
    # Add speakers' keys
    sudo -u ubuntu touch /home/ubuntu/.ssh/authorized_keys
    for user in gmelikov akremenetsky phantomii slashburygin; do
        curl -fsSL "https://github.com/${user}.keys" | sudo -u ubuntu tee -a /home/ubuntu/.ssh/authorized_keys >/dev/null
    done

    # set ssh key for ease of use
    sudo -u ubuntu ssh-keygen -q -f "/home/ubuntu/.ssh/lab" -N "" -C "lab"
    cat /home/ubuntu/.ssh/lab.pub >> /home/ubuntu/.ssh/authorized_keys

    sudo rm /etc/update-motd.d/91-* /etc/update-motd.d/92* /etc/update-motd.d/90* /etc/update-motd.d/00* /etc/update-motd.d/10*

    echo "@reboot root sleep 30 && hostname \$(hostname -I | awk '{print \$1}')" | sudo tee /etc/cron.d/lab_startup > /dev/null

    # Some beauty
    cat <<EOT | sudo tee -a /home/ubuntu/.bashrc

# Show ip in PS1
bold=\$(tput bold)
red=\$(tput setaf 1)
green=\$(tput setaf 2)
blue=\$(tput setaf 4)
reset=\$(tput sgr0)
IP=\$(hostname -I | awk '{print \$1}')
PS1="\[\$red\]\$IP\[\$reset\]:\w\$ "
EOT

    cat <<EOT | sudo tee /etc/motd
▄▖       ▌      ▄▖
▙▖▚▘▛▌▛▘▛▌▛▌▛▘  ▌ ▛▌▛▘█▌
▙▖▞▖▙▌▌ ▙▌▙▌▄▌  ▙▖▙▌▌ ▙▖


Welcome to Exordos DBaaS virtual machine!

If you see this - you'll be treated as the expert, so:
"With great power comes great responsibility".

All materials can be found here:
https://github.com/infraguys/exordos_db

Support chat:
https://t.me/infraguys_support

EOT

fi
