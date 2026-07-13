import configparser
from pathlib import Path
from unittest import mock

from exordos_db.services import gservice


@mock.patch.object(
    gservice.agent_service.UniversalAgentService,
    "__init__",
    return_value=None,
)
@mock.patch.object(gservice.core_drivers, "RestCoreCapabilityDriver")
@mock.patch.object(gservice.orch_db, "DatabaseOrchClient")
@mock.patch.object(gservice.ua_utils, "system_uuid")
def test_internal_agent_skips_local_node_verification(
    system_uuid,
    database_orch_client,
    rest_core_driver,
    universal_agent_init,
):
    gservice.UAgent(
        core_username="db-user",
        core_password="db-password",
        core_api_base_url="http://core.example/api/core",
        project_id="11111111-1111-1111-1111-111111111111",
    )

    universal_agent_init.assert_called_once()
    assert universal_agent_init.call_args.kwargs["verify_node_on_register"] is False


def test_postgres_agent_skips_local_node_verification():
    config = configparser.ConfigParser()
    config.read(
        Path(__file__).parents[4]
        / "etc"
        / "exordos_db"
        / "exordos_pg_agent.conf"
    )

    assert config.getboolean("universal_agent", "verify_node_on_register") is False
