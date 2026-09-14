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

import uuid

from gcl_sdk.agents.universal import utils as ua_utils
from gcl_sdk.agents.universal.dm import models as ua_models
import pytest

from exordos_db.agent.universal.drivers import pg
from exordos_db.paas.dm import models as paas_models
from exordos_db.user_api.dm import models

NODE = uuid.UUID("0c7a1f4e-2b5d-4c3a-9e8f-6d1b2a3c4e5f")
KIND = "pg_instance_node"

# The fields of a node resource as agents before backups know them
OLD_AGENT_FIELDS = {
    "uuid",
    "name",
    "nodes_number",
    "sync_replica_number",
    "users",
    "databases",
}

INSTANCE_FIELDS = {
    "name": "old",
    "cpu": 1,
    "ram": 1024,
    "disk_size": 8,
    "nodes_number": 2,
    "version": models.PGVersion(name="18", image="pg.raw"),
}


def _node(**fields):
    instance = paas_models.PGInstance(project_id=uuid.uuid4(), **INSTANCE_FIELDS)
    return paas_models.PGInstanceNode(
        uuid=NODE,
        name="old",
        instance=instance,
        nodes_number=2,
        sync_replica_number=1,
        users={"app": {"pw_hash": "SCRAM-SHA-256$..."}},
        databases={"app": {"owner": "app"}},
        **fields,
    )


def test_old_agent_matches_the_target_hash():
    # An old agent drops the fields it doesn't know and hashes the rest.
    # Anything more in the target and its resource never settles.
    target = _node().to_ua_resource()

    assert set(target.value) == OLD_AGENT_FIELDS
    old_agent_value = {k: v for k, v in target.value.items() if k in OLD_AGENT_FIELDS}
    assert target.hash == ua_utils.calculate_hash(old_agent_value)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"backup": {"stanza": "s", "options": {}}},
        {"rollback": {"id": "3", "stanza": "s", "options": {}}},
    ],
)
def test_agent_matches_the_target_hash(fields):
    target = _node(**fields).to_ua_resource()
    resource = ua_models.Resource.from_value(target.value, KIND)

    actual = pg.PGInstance.from_ua_resource(resource).to_ua_resource(KIND)

    assert actual.hash == target.hash
    assert set(fields) <= set(target.value)
