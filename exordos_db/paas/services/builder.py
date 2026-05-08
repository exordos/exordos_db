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

import logging
import typing as tp
import uuid
import uuid as sys_uuid

from gcl_looper.services.oslo import base as oslo_base
from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.infra.dm import models as sdk_models
from gcl_sdk.paas.services import builder

from exordos_db.paas.dm import models

LOG = logging.getLogger(__name__)
NODE_KIND = sdk_models.Node.get_resource_kind()
CONFIG_KIND = sdk_models.Config.get_resource_kind()
AGENT_UUID5_NAME = "dbaas"


class PaaSBuilder(builder.PaaSBuilder):
    @classmethod
    def agent_uuid_by_node(cls, node_uuid: sys_uuid.UUID) -> sys_uuid.UUID:
        return sys_uuid.uuid5(node_uuid, AGENT_UUID5_NAME)

    def schedule_paas_objects(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        paas_objects: tp.Collection[ua_models.TargetResourceKindAwareMixin],
    ) -> dict[sys_uuid.UUID, tp.Collection[ua_models.TargetResourceKindAwareMixin]]:
        """Schedule the PaaS objects.

        The method schedules the PaaS objects. The result is a dictionary
        where the key is a UUID of a agent and the value is a list of PaaS
        objects that should be scheduled on this agent.
        """

        scheduled = {}
        for entity in paas_objects:
            # We hardcode entity's uuid the same as agents's uuid
            scheduled[entity.uuid] = [entity]
        return scheduled


class PGInstanceBuilder(PaaSBuilder, oslo_base.OsloConfigurableService):
    def __init__(
        self,
        instance_model: tp.Type[models.PGInstance] = models.PGInstance,
    ):
        super().__init__(instance_model)

    def _get_users(self, instance):
        return {
            u.name: {
                # Don't give actual password to dataplane, just hash it
                "pw_hash": u.password_hash,
            }
            for u in instance.get_users()
        }

    def _get_databases(self, instance):
        return {d.name: {"owner": d.owner.name} for d in instance.get_databases()}

    def create_paas_objects(
        self, instance: models.PGInstance
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Create a list of PaaS objects.

        The method returns a list of PaaS objects that are required
        for the instance.
        """

        return self.actualize_paas_objects(
            instance, builder.PaaSCollection(paas_objects=tuple())
        )

    def actualize_paas_objects(
        self,
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Basic update, all derivatives are non-unique"""

        actual_resources = []

        users = self._get_users(instance)

        databases = self._get_databases(instance)

        nodeset = instance.get_actual_nodeset()
        nodes_by_idx = list(nodeset.nodes.keys())

        # Just recreate entities, it'll be updated in DB if already exist
        for i in range(instance.nodes_number):
            actual_resources.append(
                models.PGInstanceNode(
                    uuid=PaaSBuilder.agent_uuid_by_node(uuid.UUID(nodes_by_idx[i])),
                    name=instance.name,
                    instance=instance,
                    nodes_number=instance.nodes_number,
                    sync_replica_number=instance.sync_replica_number,
                    users=users,
                    databases=databases,
                )
            )

        return actual_resources
