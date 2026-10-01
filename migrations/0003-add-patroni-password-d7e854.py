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

from secrets import token_hex

from restalchemy.storage.sql import migrations
import yaml


def stable_singleton(session, identifier):
    nodesets = session.execute(
        "SELECT value FROM ua_resources WHERE uuid = %s AND kind = 'node_set';",
        (identifier,),
    ).fetchall()
    if len(nodesets) != 1 or len(nodesets[0]["value"].get("nodes", {})) != 1:
        return False
    configs = session.execute(
        "SELECT actual.value FROM ua_resources actual "
        "JOIN ua_target_resources target ON actual.uuid = target.uuid "
        "AND actual.kind = target.kind WHERE target.master = %s "
        "AND actual.kind = 'config' AND actual.value->>'path' = "
        "'/var/lib/postgresql/patroni/patroni.yml';",
        (identifier,),
    ).fetchall()
    if len(configs) != 1:
        return False
    content = configs[0]["value"].get("body", {}).get("content", "")
    try:
        patroni = yaml.safe_load(content) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(patroni, dict):
        return False
    raft = patroni.get("raft", {})
    return set(raft.get("partner_addrs", ())) == {raft.get("self_addr")}


class MigrationStep(migrations.AbstarctMigrationStep):
    def __init__(self):
        self._depends = ["0002-backup-policies-fb29c7.py"]

    @property
    def migration_id(self):
        return "d7e8548a-c2fb-44c9-b513-8edf562e7831"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "ALTER TABLE postgres_instances ADD COLUMN IF NOT EXISTS "
            "patroni_password VARCHAR(64);"
        )
        # A singleton can change transport credentials with one restart.
        # Existing multi-node clusters require a coordinated cutover.
        instances = session.execute(
            "SELECT uuid FROM postgres_instances "
            "WHERE nodes_number = 1 "
            "AND patroni_password IS NULL;"
        ).fetchall()
        for instance in instances:
            if not stable_singleton(session, instance["uuid"]):
                continue
            session.execute(
                "UPDATE postgres_instances SET patroni_password = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE uuid = %s "
                "AND patroni_password IS NULL;",
                (token_hex(32), instance["uuid"]),
            )

    def downgrade(self, session):
        raise RuntimeError(
            "Raft authentication requires a coordinated downgrade of the "
            "control plane and node configurations; do not discard the secrets."
        )


migration_step = MigrationStep()
