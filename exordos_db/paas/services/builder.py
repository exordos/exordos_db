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

from exordos_db.common import rollback
from exordos_db.paas.dm import models
from exordos_db.paas.services import roles
from exordos_db.user_api.dm import models as user_models

LOG = logging.getLogger(__name__)
NODE_KIND = sdk_models.Node.get_resource_kind()
CONFIG_KIND = sdk_models.Config.get_resource_kind()
AGENT_UUID5_NAME = "dbaas"


def rollback_id(instance: models.PGInstance) -> str | None:
    """The id the nodes know the instance's in-place rollback by."""
    if instance.rollback_revision is None:
        return None
    return str(instance.rollback_revision)


def restore_status(
    instance: models.PGInstance,
    actuals: tp.Iterable[models.PGInstanceNode | None],
) -> dict[str, tp.Any] | None:
    """Sum up what the nodes report about the restore or rollback in progress.

    Only reports of the current request count: of the rollback the instance
    asks for, or of the restore of a new instance when it asks for none.
    """
    requested = rollback_id(instance)
    reports = [
        actual.restore_state
        for actual in actuals
        if actual is not None
        and actual.restore_state is not None
        and actual.restore_state.get("id") == requested
    ]
    if not reports:
        return None
    # A failure on any node, then the node leading a rollback: replicas only
    # report they are stopped
    report = next(
        (r for r in reports if r.get("error")),
        next(
            (r for r in reports if r.get("phase") != rollback.Phase.STOPPED.value),
            reports[0],
        ),
    )
    return {
        "revision": instance.rollback_revision,
        "phase": report.get("phase"),
        "error": report.get("error"),
    }


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

    def _get_rollback(self, instance: models.PGInstance) -> dict[str, tp.Any] | None:
        """Render the in-place rollback the nodes converge to, if any.

        Rendered from the source as it is now, like the backup is, so new
        credentials of the source reach a rollback in progress. Once the
        source is cleared the rollback is done with and none is asked for any
        more.
        """
        if instance.rollback_revision is None or instance.restore_from is None:
            return None
        return {"id": rollback_id(instance), **instance.restore_from.restore_spec()}

    @staticmethod
    def _roles_managed(instance: models.PGInstance) -> bool:
        return instance.roles_managed()

    def _import_roles(
        self,
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> None:
        """Match users and databases to the roles of a restored cluster.

        The agent reports them once PostgreSQL accepts connections, i.e. the
        recovery is over and the node is promoted. After an in-place rollback
        only a node that has applied it reports the rolled back state, which
        it does by reporting the rollback back by its id.
        """
        requested = rollback_id(instance)
        for actual in paas_collection.actuals():
            if (
                actual is not None
                and actual.found_roles is not None
                and (actual.rollback or {}).get("id") == requested
            ):
                break
        else:
            return

        users = {u.name: u for u in instance.get_users()}
        databases = {d.name: d for d in instance.get_databases()}
        plan = roles.plan_roles(
            users.keys(),
            {name: d.owner.name for name, d in databases.items()},
            actual.found_roles,
        )

        for name in plan.unmanageable_users:
            LOG.warning(
                "User %s of the restored instance %s has no password, "
                "it isn't managed and will be dropped",
                name,
                instance.uuid,
            )
        for name in plan.unmanageable_databases:
            LOG.warning(
                "Database %s of the restored instance %s is owned by a role "
                "that isn't managed, it will be dropped",
                name,
                instance.uuid,
            )

        for name in plan.delete_databases:
            databases.pop(name).delete()
        for name, pw_hash in plan.create_users.items():
            users[name] = user_models.PGUser(
                name=name,
                password_hash=pw_hash,
                instance=instance,
                project_id=instance.project_id,
            )
            users[name].insert()
        for name, owner in plan.create_databases.items():
            user_models.PGDatabase(
                name=name,
                owner=users[owner],
                instance=instance,
                project_id=instance.project_id,
            ).insert()
        for name, owner in plan.change_owners.items():
            databases[name].owner = users[owner]
            databases[name].update()
        for name in plan.delete_users:
            users.pop(name).delete()

        instance.roles_imported = True
        instance.update(force=True)
        # Names only: the plan carries password hashes
        LOG.info(
            "Roles of the restored instance %s matched: created users %s and "
            "databases %s, changed owners of %s, deleted databases %s and users %s",
            instance.uuid,
            sorted(plan.create_users),
            sorted(plan.create_databases),
            sorted(plan.change_owners),
            plan.delete_databases,
            plan.delete_users,
        )

    @staticmethod
    def _update_restore_status(
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> None:
        status = restore_status(instance, paas_collection.actuals())
        if status != instance.restore_status:
            instance.restore_status = status
            instance.update(force=True)

    def actualize_paas_objects_source_data_plane(
        self,
        instance: models.PGInstance,
        paas_collection: builder.PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        self._update_restore_status(instance, paas_collection)
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

        users = databases = None
        if self._roles_managed(instance):
            users = self._get_users(instance)
            databases = self._get_databases(instance)

        backup = self._get_backup(instance)
        rollback_spec = self._get_rollback(instance)

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
                    rollback=rollback_spec,
                )
            )

        return actual_resources
