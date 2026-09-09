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
from __future__ import annotations

import logging
import os
import typing as tp

from gcl_looper.services.oslo import base as oslo_base
from gcl_sdk.agents.universal import constants as ua_c
from gcl_sdk.agents.universal import utils as ua_utils
from gcl_sdk.agents.universal.clients.orch import db as orch_db
from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.agents.universal.drivers import core as core_drivers
from gcl_sdk.agents.universal.services import agent as agent_service
from gcl_sdk.agents.universal.services import scheduler as scheduler_service
from gcl_sdk.common.oslo import types as sdk_cfg_types
from oslo_config import cfg
from restalchemy.common import contexts

from exordos_db.common import constants as cc

LOG = logging.getLogger(__name__)


class InfraScheduler(
    scheduler_service.UniversalAgentSchedulerService,
    oslo_base.OsloConfigurableService,
):
    @classmethod
    def svc_get_config_opts(cls) -> tp.Collection[cfg.Opt]:
        return [
            cfg.ListOpt(
                "capabilities",
                default=(),
                help=("List of capabilities to run."),
            )
        ]


class UAgent(agent_service.UniversalAgentService, oslo_base.OsloConfigurableService):
    def __init__(
        self,
        *args,
        core_username,
        core_password,
        core_api_base_url,
        project_id,
        **kwargs,
    ):
        agent_uuid = ua_utils.system_uuid()
        orch_client = orch_db.DatabaseOrchClient()
        core_driver = core_drivers.RestCoreCapabilityDriver(
            username=core_username,
            password=core_password,
            user_api_base_url=core_api_base_url,
            project_id=project_id,
            use_project_scope=True,
            node_set="/v1/compute/sets/",
            config="/v1/config/configs/",
        )

        caps_drivers = [
            core_driver,
        ]

        facts_drivers = []
        payload_path = os.path.join(cc.WORK_DIR, "infra_agent_payload.json")
        super().__init__(
            *args,
            agent_uuid=agent_uuid,
            orch_client=orch_client,
            caps_drivers=caps_drivers,
            facts_drivers=facts_drivers,
            payload_path=payload_path,
            **kwargs,
        )

    def _setup(self):
        # The agent registers itself only if its node is known, i.e. the
        # node has an encryption key. The CP host is not a managed node,
        # so nothing mirrors a key for it from Core the way
        # CoreInfraBuilder does for the PG instance nodes - provision one
        # here so registration can go through. Seed it with the key
        # already deployed on this host, otherwise the DB copy would
        # differ from the one the CP host encrypts with.
        with contexts.Context().session_manager() as session:
            ua_models.NodeEncryptionKey.get_or_create(
                ua_utils.system_uuid(),
                private_key=self._read_private_key(),
                session=session,
            )

        super()._setup()

    @staticmethod
    def _read_private_key() -> str:
        """Return the key deployed on this host.

        A missing key file means the host was never provisioned with
        one, so any key put in the DB would differ from what the CP
        host encrypts with - fail loudly instead.
        """
        with open(ua_c.PRIVATE_KEY_PATH) as f:
            return f.read().strip()

    @classmethod
    def svc_get_config_opts(cls) -> tp.Collection[cfg.Opt]:
        return [
            cfg.StrOpt(
                "core_username",
                default="exordos_db",
                help="User to work with Core.",
            ),
            cfg.StrOpt(
                "core_password",
                default="exordos_db",
                help="User password to work with Core.",
            ),
            cfg.StrOpt(
                "core_api_base_url",
                default="http://core.local.genesis-core.tech:80/api/core",
                help="Core's user api endpoint.",
            ),
            sdk_cfg_types.UuidOpt(
                "project_id",
                help="Project id to work with Core.",
            ),
        ]
