#!/usr/bin/env bash

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

set -eu
set -x
set -o pipefail

source /usr/local/lib/exordos/lib_bootstrap.sh

# persistent data routines
PERSISTENT_DISK=$(find_persistent_disk)
prepare_persistent_disk "$(find_persistent_disk)" "$PERSISTENT_MOUNT"

if [[ -n "$PERSISTENT_DISK" ]]; then
    # Migrate logs first, some processes may be left writing to root disk until next reboot
    migrate_to_persistent_restart "/var/log" "${PERSISTENT_MOUNT}/var/log" "systemd-journald rsyslog"

    # Migrate Patroni data (raft, pg data)
    migrate_to_persistent "/var/lib/postgresql/patroni/data" "${PERSISTENT_MOUNT}/var/lib/postgresql/patroni/data"
    migrate_to_persistent "/var/lib/postgresql/patroni/raft" "${PERSISTENT_MOUNT}/var/lib/postgresql/patroni/raft"

    persist_migrate_complete
fi

# Enable exordos db services
sudo systemctl enable --now \
    exordos-patroni

echo "Bootstrap completed successfully."
