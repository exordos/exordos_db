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

from gcl_sdk.agents.universal.dm import models as ua_models

from exordos_db.agent.universal.drivers import pg

UNMANAGED_ROLES = {
    "uuid": str(uuid.uuid4()),
    "name": "restored",
    "nodes_number": 2,
    "sync_replica_number": 0,
    "users": None,
    "databases": None,
    "backup": None,
}


def test_unmanaged_roles_stay_unmanaged():
    # A restored cluster's roles aren't imported yet. Turning None into an
    # empty dict made the agent drop every database of the restored cluster.
    resource = ua_models.Resource.from_value(UNMANAGED_ROLES, "pg_instance_node")

    instance = pg.PGInstance.from_ua_resource(resource)

    assert instance.users is None
    assert instance.databases is None


def test_managed_roles():
    value = {
        **UNMANAGED_ROLES,
        "users": {"app": {"pw_hash": "SCRAM-SHA-256$..."}},
        "databases": {},
    }
    resource = ua_models.Resource.from_value(value, "pg_instance_node")

    instance = pg.PGInstance.from_ua_resource(resource)

    assert instance.users == {"app": {"pw_hash": "SCRAM-SHA-256$..."}}
    assert instance.databases == {}
