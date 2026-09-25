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
from exordos_db.user_api.dm import models as user_models

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


def restore_status(
    actuals: tp.Iterable[models.PGInstanceNode | None],
) -> dict[str, tp.Any] | None:
    """Sum up what the nodes report about the restore in progress."""
    reports = [
        actual.restore_state
        for actual in actuals
        if actual is not None and actual.restore_state is not None
    ]
    if not reports:
        return None
    # Another node may still be restoring after this one failed to
    return next((r for r in reports if not r["error"]), reports[0])


def backup_status(
    instance: models.PGInstance,
    actuals: tp.Iterable[models.PGInstanceNode | None],
) -> dict[str, tp.Any] | None:
    """Take what the primary reports about the backups."""
    if instance.backup is None:
        return None
    for actual in actuals:
        if actual is not None and actual.backup_state is not None:
            return actual.backup_state
    # No primary reports meanwhile, e.g. during a failover
    return instance.backup_status


class PGInstanceBuilder(PaaSBuilder, oslo_base.OsloConfigurableService):
    def __init__(
        self,
        instance_model: type[models.PGInstance] = models.PGInstance,
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

    def _get_backup(self, instance: models.PGInstance) -> dict[str, tp.Any] | None:
        if instance.backup is None:
            return None

        options = instance.backup.pgbackrest_repo_options()
        # WAL lives on the data disk. When the repository is unreachable
        # pgBackRest drops WAL past this size instead of filling the disk,
        # which breaks PITR but keeps the database running.
        options["archive-push-queue-max"] = f"{max(1, instance.disk_size // 4)}GiB"
        return {
            "stanza": str(instance.uuid),
            "options": options,
            "schedule": {
                "full_interval_hours": instance.backup.full_interval_hours,
                "incr_interval_hours": instance.backup.incr_interval_hours,
            },
        }

    @staticmethod
    def _roles_managed(instance: models.PGInstance) -> bool:
        return instance.restore_from is None or instance.roles_imported

    def _import_roles(
        self,
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> None:
        """Take users and databases of a restored cluster under management.

        The primary reports them once its recovery is over: they are
        imported once, what the replayed WAL does to them has to be in.
        """
        for actual in paas_collection.actuals():
            if actual is not None and actual.found_roles is not None:
                break
        else:
            return

        found_users = actual.found_roles["users"]
        found_databases = actual.found_roles["databases"]

        # By name: rows of an earlier, interrupted pass or created through
        # the API meanwhile are kept, not doubled
        users = {u.name: u for u in instance.get_users()}
        databases = {d.name for d in instance.get_databases()}
        for name, user in found_users.items():
            if name in users:
                continue
            if not user.get("pw_hash"):
                LOG.warning(
                    "User %s of the restored instance %s has no password, "
                    "it isn't imported and will be dropped",
                    name,
                    instance.uuid,
                )
                continue
            users[name] = user_models.PGUser(
                name=name,
                password_hash=user["pw_hash"],
                instance=instance,
                project_id=instance.project_id,
            )
            users[name].insert()

        for name, database in found_databases.items():
            if name in databases:
                continue
            if (owner := users.get(database["owner"])) is None:
                LOG.warning(
                    "Database %s of the restored instance %s is owned by %s "
                    "that isn't imported, it will be dropped",
                    name,
                    instance.uuid,
                    database["owner"],
                )
                continue
            user_models.PGDatabase(
                name=name,
                owner=owner,
                instance=instance,
                project_id=instance.project_id,
            ).insert()

        instance.roles_imported = True
        instance.update(force=True)
        LOG.info(
            "Imported %d users and %d databases of the restored instance %s",
            len(users),
            len(found_databases),
            instance.uuid,
        )

    @staticmethod
    def _update_restore_status(
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> None:
        status = restore_status(paas_collection.actuals())
        if status != instance.restore_status:
            instance.restore_status = status
            instance.update(force=True)

    @staticmethod
    def _update_backup_status(
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> None:
        status = backup_status(instance, paas_collection.actuals())
        if status != instance.backup_status:
            instance.backup_status = status
            instance.update(force=True)

    def actualize_paas_objects_source_data_plane(
        self,
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        self._update_restore_status(instance, paas_collection)
        self._update_backup_status(instance, paas_collection)
        if not self._roles_managed(instance):
            self._import_roles(instance, paas_collection)
        return super().actualize_paas_objects_source_data_plane(
            instance, paas_collection
        )

    def create_paas_objects(
        self, instance: models.PGInstance
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Create a list of PaaS objects.

        The method returns a list of PaaS objects that are required
        for the instance.
        """

        return self.actualize_paas_objects(
            instance, builder.PaaSCollection(paas_objects=())
        )

    def actualize_paas_objects(
        self,
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Basic update, all derivatives are non-unique"""

        actual_resources = []

        adopt_roles = not self._roles_managed(instance)
        users = {} if adopt_roles else self._get_users(instance)
        databases = {} if adopt_roles else self._get_databases(instance)

        backup = self._get_backup(instance)

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
                    backup=backup,
                    adopt_roles=adopt_roles,
                )
            )

        return actual_resources
