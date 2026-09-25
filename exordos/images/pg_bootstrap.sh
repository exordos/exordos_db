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

BOOTSTRAP_LIB=${EXORDOS_BOOTSTRAP_LIB:-/usr/local/lib/exordos/lib_bootstrap.sh}
source "$BOOTSTRAP_LIB"

# Keep Patroni stopped across BOTH migrations: restarting between data and
# Raft would leave open file descriptors on the filesystem being replaced.
sudo systemctl stop exordos-patroni

# A newer image may give postgres other ids than the ones the data on the
# persistent disk was written with, and PostgreSQL refuses to start then.
# What has the old ids of the data directory, the first path, gets the new
# ones; anything else keeps its owner.
restore_postgres_ownership() {
    local ids uid gid path paths=()
    ids=$(stat -c '%u %g' "$1" 2>/dev/null) || return 0
    read -r uid gid <<< "$ids"
    [[ "$uid:$gid" != "$(id -u postgres):$(id -g postgres)" ]] || return 0
    for path in "$@"; do
        if stat -c %u "$path" >/dev/null 2>&1; then
            paths+=("$path")
        fi
    done
    sudo find "${paths[@]}" -uid "$uid" -exec chown -h postgres {} +
    sudo find "${paths[@]}" -gid "$gid" -exec chgrp -h postgres {} +
}

# persistent data routines
PERSISTENT_DISK=$(find_persistent_disk)
prepare_persistent_disk "$PERSISTENT_DISK" "$PERSISTENT_MOUNT"

if [[ -n "$PERSISTENT_DISK" ]]; then
    restore_postgres_ownership \
        "${PERSISTENT_MOUNT}/var/lib/postgresql/patroni/data" \
        "${PERSISTENT_MOUNT}/var/lib/postgresql/patroni/raft" \
        "${PERSISTENT_MOUNT}/var/log/postgresql" \
        "${PERSISTENT_MOUNT}/var/log/pgbackrest"

    # Migrate logs first, some processes may be left writing to root disk until next reboot
    migrate_to_persistent_restart "/var/log" "${PERSISTENT_MOUNT}/var/log" "systemd-journald rsyslog"
    # The package creates it in the image, a /var/log kept from an image
    # without pgBackRest hides it. The files inherit adm, the group rsyslog
    # reads them as to send them to observability.
    sudo install -d -m 2750 -o postgres -g adm /var/log/pgbackrest
    sudo find /var/log/pgbackrest -type f -group postgres -exec chgrp adm {} +

    # Migrate Patroni data (raft, pg data)
    migrate_to_persistent "/var/lib/postgresql/patroni/data" "${PERSISTENT_MOUNT}/var/lib/postgresql/patroni/data"
    migrate_to_persistent "/var/lib/postgresql/patroni/raft" "${PERSISTENT_MOUNT}/var/lib/postgresql/patroni/raft"

    persist_migrate_complete
fi

# Enable exordos db services
sudo systemctl enable --now \
    exordos-patroni

echo "Bootstrap completed successfully."
