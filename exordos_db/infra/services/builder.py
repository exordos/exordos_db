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
from gcl_sdk.agents.universal.drivers import core as core_drivers
from gcl_sdk.common.oslo import types as sdk_cfg_types
from gcl_sdk.infra import constants as sdk_c
from gcl_sdk.infra.dm import models as sdk_models
from gcl_sdk.infra.services import builder
from oslo_config import cfg
from restalchemy.dm import filters as dm_filters

from exordos_db.infra.dm import models

LOG = logging.getLogger(__name__)
NODE_KIND = sdk_models.Node.get_resource_kind()
NODE_SET_KIND = sdk_models.NodeSet.get_resource_kind()
CONFIG_KIND = sdk_models.Config.get_resource_kind()

PATRONI_RAFT_PORT = 5010


PATRONI_CONF_TEMPLATE = """\
scope: "{cluster_name}"
namespace: /db/
name: "{node_name}"

restapi:
  listen: "0.0.0.0:8008"
  connect_address: "{node_ip}:8008"
  authentication:
    username: patroni
    password: patroni

raft:
  data_dir: /var/lib/postgresql/patroni/raft/
  self_addr: "{node_ip}:5010"
  partner_addrs: {raft_partner_addrs}

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      use_slots: true
      parameters:
        # archive_mode is 'on' to allow enabling backups without a restart.
        # archive_command is set to a no-op to prevent WAL accumulation.
        archive_mode: "on"
        archive_command: ":"
        archive_timeout: 1800s
        wal_log_hints: 'on'
        wal_compression: 'lz4'
        max_connections: 500
        tcp_keepalives_idle: 900
        tcp_keepalives_interval: 100
        # More aggressive vacuum
        autovacuum_max_workers: 5
        autovacuum_vacuum_scale_factor: 0.05
        autovacuum_analyze_scale_factor: 0.02
        # Log user actions, may be too verbose
        log_line_prefix: '%t [%p]: [%l-1] %c %x %d %u %a %h '
        log_lock_waits: 'on'
        log_min_duration_statement: 500
        log_autovacuum_min_duration: 0
        log_connections: 'on'
        log_disconnections: 'on'
        log_statement: 'ddl'
        log_temp_files: 0
        track_functions: all
    synchronous_mode: {sync_mode}
    synchronous_mode_strict: {sync_mode}
    synchronous_node_count: {sync_replica_number}

  initdb:
  - encoding: UTF8
  - data-checksums
  - auth-local: peer
  - auth-host: scram-sha-256

postgresql:
  listen: "0.0.0.0:5432"
  connect_address: "{node_ip}:5432"
  data_dir: /var/lib/postgresql/patroni/data/
  bin_dir: /usr/sbin
  pgpass: /tmp/pgpass0
  authentication:
    replication:
      username: dbaas_replicator
      password: replicate_password
    superuser:
      username: postgres
      password: my-super-password
    rewind:
      username: dbaas_rewinder
      password: rewind_password
  parameters:
    unix_socket_directories: '/var/run/postgresql,/tmp'
    io_method: 'io_uring'
  pg_hba:
  - host replication dbaas_replicator 0.0.0.0/0 scram-sha-256
  - host all all 0.0.0.0/0 scram-sha-256
  - local all all peer map=exordos_map
  pg_ident:
   - exordos_map root postgres
   - exordos_map postgres postgres
watchdog:
  mode: required
  device: /dev/watchdog
  safety_margin: 5

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: false
  nosync: false
"""


class CoreInfraBuilder(builder.CoreInfraBuilder, oslo_base.OsloConfigurableService):
    def __init__(
        self,
        core_username,
        core_password,
        core_api_base_url,
        project_id: sys_uuid.UUID,
        instance_model: tp.Type[models.PGInstance] = models.PGInstance,
    ):
        super().__init__(instance_model)
        self._project_id = project_id
        # for agents' private keys
        self.core_driver = core_drivers.RestCoreCapabilityDriver(
            username=core_username,
            password=core_password,
            user_api_base_url=core_api_base_url,
            project_id=self._project_id,
            use_project_scope=True,
            node_set="/v1/compute/sets/",
            config="/v1/config/configs/",
        )
        self._cclient = self.core_driver._client._client

    @classmethod
    def svc_get_config_opts(cls) -> tp.Collection[cfg.Opt]:
        return [
            cfg.StrOpt(
                "core_username",
                default="exordos_db",
                help=("User to work with Core."),
            ),
            cfg.StrOpt(
                "core_password",
                default="exordos_db",
                help=("User password to work with Core."),
            ),
            cfg.StrOpt(
                "core_api_base_url",
                default="http://core.local.genesis-core.tech:11010",
                help=("Core's user api endpoint."),
            ),
            sdk_cfg_types.UuidOpt(
                "project_id",
                help=("Project id to work with Core."),
            ),
        ]

    def create_infra(
        self, instance: models.PGInstance
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Create a list of infrastructure objects.

        The method returns a list of infrastructure objects that are required
        for the instance. For example, nodes, sets, configs, etc.
        """
        return instance.get_infra(self._project_id)

    def actualize_infra(
        self,
        instance: models.PGInstance,
        infra: builder.InfraCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Actualize the infrastructure objects.

        The method is called when the instance is outdated. For example,
        the instance `Config` has derivative `Render`. Single `Config` may
        have multiple `Render` derivatives. If any of the derivatives is
        outdated, this method is called to reactualize this infrastructure.

        Args:
            instance: The instance to actualize.
            infra: The infrastructure objects.
        """
        nodeset = None
        configs = []

        for target, actual in infra.infra_objects:
            if target.get_resource_kind() == NODE_SET_KIND:
                nodeset = actual
            elif actual.get_resource_kind() == CONFIG_KIND:
                configs.append(actual)

        if nodeset.nodes:
            instance.ipsv4 = [node["ipv4"] for node in nodeset.nodes.values()]

        new_objects = []

        node_raft_members = [
            f"{node['ipv4']}:{PATRONI_RAFT_PORT}" for node in nodeset.nodes.values()
        ]

        # TODO: add mechanism to update rotated keys
        node_keys = self._cclient.do_action(
            "/v1/compute/sets/", "get_private_keys", nodeset.uuid
        )
        for u, v in node_keys.items():
            if nkey := ua_models.NodeEncryptionKey.objects.get_one_or_none(
                filters={"uuid": dm_filters.EQ(u)}
            ):
                nkey.private_key = v
                nkey.update()
            else:
                nkey = ua_models.NodeEncryptionKey(uuid=uuid.UUID(u), private_key=v)
                nkey.insert()

        # In case of shrink we still has all nodes but only lower nodes_number
        nodes_diff_num = instance.nodes_number - len(node_raft_members)
        if nodes_diff_num < 0:
            node_raft_members = node_raft_members[: instance.nodes_number]
            for idx, del_node_uuid in enumerate(nodeset.nodes.keys()):
                if idx < instance.nodes_number:
                    continue
                # node clean up routines
                for key in ua_models.NodeEncryptionKey.objects.get_all(
                    filters={"uuid": dm_filters.EQ(del_node_uuid)}
                ):
                    key.delete()

        sync_mode = "true" if instance.sync_replica_number else "false"

        # Just recreate configs, it'll be updated in DB if already exist
        for node_uuid, node in nodeset.nodes.items():
            content = PATRONI_CONF_TEMPLATE.format(
                cluster_name=instance.name,
                node_name=node_uuid,
                node_ip=node["ipv4"],
                raft_partner_addrs=node_raft_members,
                sync_mode=sync_mode,
                sync_replica_number=instance.sync_replica_number,
                on_change=instance.OnReloadFunc,
            )
            config = instance._create_config(
                uuid.UUID(node_uuid), self._project_id, content
            )
            new_objects.append(config)

        tgt_nodeset = None

        for target, _ in infra.infra_objects:
            if target.get_resource_kind() == CONFIG_KIND:
                # We already regenerated them earlier
                continue
            elif target.get_resource_kind() == NODE_SET_KIND:
                target.cores = instance.cpu
                target.ram = instance.ram
                target.disk_spec = sdk_models.SetDisksSpec(
                    disks=[
                        {
                            "size": models.ROOT_DISK_SIZE,
                            "image": instance.version.image,
                            "label": "root",
                        },
                        {
                            "size": instance.disk_size,
                            "label": "data",
                        },
                    ]
                )
                target.replicas = instance.nodes_number
                tgt_nodeset = target
            else:
                LOG.exception(
                    "%s kind is not supported here, ignoring...",
                    target.get_resource_kind(),
                )

        try:
            instance.status = sdk_c.InstanceStatus(nodeset.status).value
        except ValueError:
            instance.status = sdk_c.InstanceStatus.IN_PROGRESS.value

        return (tgt_nodeset, *new_objects)

    def pre_delete_instance_resource(self, resource):
        # Get actual nodeset to clean private keys of it's nodes
        target_resources = ua_models.TargetResource.objects.get_all(
            filters={
                "master": dm_filters.EQ(resource.uuid),
                "kind": dm_filters.EQ(NODE_SET_KIND),
            },
        )
        actual_resources = ua_models.Resource.objects.get_all(
            filters={
                "uuid": dm_filters.In(r.uuid for r in target_resources),
                "kind": dm_filters.EQ(NODE_SET_KIND),
            },
        )

        for ns in actual_resources:
            for key in ua_models.NodeEncryptionKey.objects.get_all(
                filters={"uuid": dm_filters.In(ns.value["nodes"].keys())}
            ):
                key.delete()
