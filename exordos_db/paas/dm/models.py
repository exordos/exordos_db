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

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.infra import constants as pc
from gcl_sdk.infra.dm import models as sdk_models
from restalchemy.dm import filters as ra_filters
from restalchemy.dm import models as ra_models
from restalchemy.dm import properties
from restalchemy.dm import types as ra_types

from exordos_db.user_api.dm import models

LOG = logging.getLogger(__name__)


class PGInstanceNode(
    ra_models.ModelWithUUID,
    ua_models.TargetResourceKindAwareMixin,
):
    status = properties.property(
        ra_types.Enum([s.value for s in pc.InstanceStatus]),
        default=pc.InstanceStatus.NEW.value,
    )
    # TODO(akremenetsky): We already have name in the parent model
    name = properties.property(ra_types.String(min_length=1, max_length=64))
    databases = properties.property(ra_types.Dict())
    users = properties.property(ra_types.Dict())
    nodes_number = properties.property(ra_types.Integer(min_value=1, max_value=16))
    sync_replica_number = properties.property(
        ra_types.Integer(min_value=0, max_value=15)
    )

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "pg_instance_node"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "sync_replica_number",
                "nodes_number",
                "databases",
                "users",
            )
        )


class PGInstance(
    models.PGInstance,
    ua_models.InstanceWithDerivativesMixin,
):
    __master_model__ = sdk_models.NodeSet
    __derivative_model_map__ = {
        "pg_instance_node": PGInstanceNode,
    }

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "pg_instance"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "sync_replica_number",
            )
        )

    def get_actual_nodeset(self):
        res = ua_models.Resource.objects.get_one(
            filters={
                "uuid": ra_filters.EQ(self.uuid),
                "kind": ra_filters.EQ("node_set"),
            }
        )
        return self.__master_model__.from_ua_resource(res)
