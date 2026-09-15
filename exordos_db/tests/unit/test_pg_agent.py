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


def test_found_roles_change_the_full_hash_only():
    # The agent reports a resource read from the data plane only while its
    # target hash matches; the control plane learns about the data plane
    # from the full hash. The found roles have to travel that way.
    resource = ua_models.Resource.from_value(UNMANAGED_ROLES, "pg_instance_node")
    empty = pg.PGInstance.from_ua_resource(resource)
    found = pg.PGInstance.from_ua_resource(resource)
    found.found_roles = {"users": {"app": {"pw_hash": "x"}}, "databases": {}}
    found.roles_unmanaged = True

    empty_resource = empty.to_ua_resource("pg_instance_node")
    found_resource = found.to_ua_resource("pg_instance_node")

    assert found_resource.hash == empty_resource.hash
    assert found_resource.full_hash != empty_resource.full_hash
    assert "roles_unmanaged" not in found_resource.value
    assert "roles_unmanaged" in found.get_meta_fields()


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
