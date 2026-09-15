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

PERMS_OWNER = [
    "exordos_db.pg_instance.create",
    "exordos_db.pg_instance.read",
    "exordos_db.pg_instance.update",
    "exordos_db.pg_instance.delete",
    "exordos_db.database.create",
    "exordos_db.database.read",
    "exordos_db.database.update",
    "exordos_db.database.delete",
    "exordos_db.user.create",
    "exordos_db.user.read",
    "exordos_db.user.update",
    "exordos_db.user.delete",
    "exordos_db.pg_version.read",
    "exordos_db.backup_repository.create",
    "exordos_db.backup_repository.read",
    "exordos_db.backup_repository.update",
    "exordos_db.backup_repository.delete",
    "exordos_db.backup.read",
]

ALL_PERMS = set(PERMS_OWNER)

ROLES = {
    "owner": PERMS_OWNER,
}
