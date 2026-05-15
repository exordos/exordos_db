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

import typing as tp
import uuid as sys_uuid

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.infra import constants as sdk_c
from gcl_sdk.infra.dm import models as sdk_models

from exordos_db.user_api.dm import models

ROOT_DISK_SIZE = 6


class PGInstance(models.PGInstance, ua_models.InstanceWithDerivativesMixin):
    __derivative_model_map__ = {
        "node_set": sdk_models.NodeSet,
        "node": sdk_models.Node,
        "config": sdk_models.Config,
    }

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "pg_instance_iaas"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "cpu",
                "ram",
                "disk_size",
                "nodes_number",
                "sync_replica_number",
                "version",
                "project_id",
            )
        )

    OnReloadFunc = sdk_models.OnChangeShell(
        command="systemctl try-reload-or-restart exordos-patroni"
    )

    def _create_config(self, node_uuid, project_id, content=""):
        config = sdk_models.Config(
            uuid=sys_uuid.uuid5(self.uuid, f"config-{node_uuid}"),
            name=str(node_uuid),
            project_id=project_id,
            status=sdk_c.InstanceStatus.NEW.value,
            target=sdk_models.NodeTarget(
                node=node_uuid,
            ),
            body=sdk_models.TextBodyConfig(
                content=content,
            ),
            path="/var/lib/postgresql/patroni/patroni.yml",
            owner="postgres",
            group="postgres",
            mode="0660",
            on_change=self.OnReloadFunc,
        )

        return config

    def get_infra(
        self,
        project_id: sys_uuid.UUID,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Return the infrastructure objects."""
        infra_objects = []

        node_set = sdk_models.NodeSet(
            uuid=self.uuid,
            name=f"dbaas-dp-{self.uuid}",
            cores=self.cpu,
            ram=self.ram,
            disk_spec=sdk_models.SetDisksSpec(
                disks=[
                    {
                        "size": ROOT_DISK_SIZE,
                        "image": self.version.image,
                        "label": "root",
                    },
                    {
                        "size": self.disk_size,
                        "label": "data",
                    },
                ]
            ),
            replicas=self.nodes_number,
            project_id=project_id,
            status=sdk_c.NodeStatus.NEW.value,
        )
        infra_objects.append(node_set)

        return infra_objects
