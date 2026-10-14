#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from pathlib import Path
from unittest import mock
import uuid

from gcl_sdk.audit import opts
from oslo_config import cfg
import pytest
from restalchemy.storage.sql import orm
import yaml

from exordos_db.common import audit
from exordos_db.user_api.dm import backups
from exordos_db.user_api.dm import models

ROOT = Path(__file__).parents[3]


def instance() -> models.PGInstance:
    return models.PGInstance(
        name="test-instance",
        cpu=1,
        ram=512,
        disk_size=8,
        nodes_number=1,
        version=models.PGVersion(name="v", image="image"),
        project_id=uuid.uuid4(),
        patroni_password="a" * 64,
    )


@pytest.mark.parametrize(
    "model",
    [
        models.PGVersion,
        models.PGInstance,
        models.PGUser,
        models.PGDatabase,
        models.PGBackupPolicy,
    ],
)
def test_business_models_record_dbaas_events(model: type) -> None:
    assert issubclass(model, audit.AuditLogSQLStorableMixin)
    assert model.get_audit_service_name() == "dbaas"
    assert model.get_audit_resource_type() == model.__tablename__


@pytest.mark.parametrize("action", ["insert", "update", "delete"])
def test_audit_event_shares_resource_transaction(action: str) -> None:
    version = models.PGVersion(name="v", image="image")
    session = mock.Mock()
    engine = mock.MagicMock()
    engine.session_manager.return_value.__enter__.return_value = session
    with (
        mock.patch.object(version, "_get_engine", return_value=engine),
        mock.patch.object(version, "is_dirty", return_value=True),
        mock.patch.object(version, "_write_audit_event") as write,
        mock.patch.object(orm.SQLStorableMixin, action) as mutation,
    ):
        getattr(version, action)()
    assert mutation.call_args.kwargs["session"] is session
    assert write.call_args.kwargs["session"] is session
    assert (
        write.call_args.kwargs["action"]
        == {
            "insert": "create",
            "update": "update",
            "delete": "delete",
        }[action]
    )
    assert (write.call_args.kwargs["snapshot"] is None) == (action == "delete")


def test_passwords_and_backup_keys_are_absent_from_snapshots() -> None:
    parent = instance()
    user = models.PGUser(
        name="user",
        instance=parent,
        project_id=parent.project_id,
        password="test-password",
        password_hash="test-hash",
    )
    assert "patroni_password" not in parent.dump_to_audit_snapshot()
    assert {"password", "password_hash"}.isdisjoint(user.dump_to_audit_snapshot())
    storage = backups.S3Storage(
        endpoint="https://s3.example",
        bucket="test-bucket",
        access_key="access-canary",
        secret_key="secret-canary",
        encryption_key="encryption-canary",
    )
    policy = models.PGBackupPolicy(
        instance=parent, project_id=parent.project_id, storage=storage
    )
    snapshot = policy.dump_to_audit_snapshot()
    assert snapshot["storage"]["bucket"] == "test-bucket"
    assert {"access_key", "secret_key", "encryption_key"}.isdisjoint(
        snapshot["storage"]
    )


def test_restore_source_credentials_are_absent_from_snapshot() -> None:
    parent = instance()
    source = backups.S3RestoreSource(
        endpoint="https://s3.example",
        bucket="test-bucket",
        stanza=uuid.uuid4(),
        access_key="access-canary",
        secret_key="secret-canary",
        encryption_key="encryption-canary",
    )
    parent.properties["restore_from"].set_value_force(source)
    snapshot = parent.dump_to_audit_snapshot()["restore_from"]
    assert snapshot["stanza"] == str(source.stanza)
    assert {"access_key", "secret_key", "encryption_key"}.isdisjoint(snapshot)


@pytest.mark.parametrize("actor", [None, uuid.uuid4()])
def test_audit_event_keeps_user_actor_and_resource_project(actor) -> None:
    from types import SimpleNamespace

    from gcl_sdk.audit.dm import models as sdk_audit_models
    from restalchemy.common import contexts

    parent = instance()
    context = SimpleNamespace(
        iam_context=SimpleNamespace(token_info=SimpleNamespace(user_uuid=actor))
    )
    session = mock.Mock()
    with (
        mock.patch.object(contexts, "get_context", return_value=context),
        mock.patch.object(
            sdk_audit_models.AuditDeliveryEvent, "insert", autospec=True
        ) as insert,
    ):
        parent._write_audit_event("create", parent.dump_to_audit_snapshot(), session)
    event = insert.call_args.args[0]
    assert event.actor_user_uuid == actor
    assert event.project_id == parent.project_id
    assert event.resource_uuid == parent.uuid
    assert insert.call_args.args[1] is session


@pytest.mark.parametrize("enabled", [False, True])
def test_api_starts_sender_only_when_enabled(enabled: bool) -> None:
    with mock.patch.object(cfg, "CONF", cfg.ConfigOpts()):
        from exordos_db.cmd import user_api
    conf = mock.MagicMock()
    conf.__getitem__.return_value.workers = 1
    with (
        mock.patch.object(user_api, "CONF", conf),
        mock.patch.object(user_api.config, "parse"),
        mock.patch.object(user_api.infra_log, "configure"),
        mock.patch.object(user_api.app, "build_wsgi_application"),
        mock.patch.object(user_api.drivers, "HttpDriver"),
        mock.patch.object(user_api.bjoern_service, "BjoernService"),
        mock.patch.object(user_api.hub, "ProcessHubService") as hub,
        mock.patch.object(
            user_api.audit_opts,
            "get_audit_delivery_config",
            return_value=mock.Mock(enabled=enabled),
        ),
        mock.patch.object(
            user_api.audit_senders.AuditSenderService, "build_from_config"
        ) as build,
        mock.patch.object(
            user_api.engines.engine_factory, "configure_postgresql_factory"
        ) as database,
    ):
        user_api.main()
        if enabled:
            sender = build.return_value
            hub.return_value.add_service.assert_any_call(sender)
            database.assert_not_called()
            sender.add_setup.call_args.args[0]()
            database.assert_called_once_with(conf=conf)
        else:
            build.assert_not_called()
        hub.return_value.start.assert_called_once()


def test_optional_manifest_config_and_token_scope(tmp_path: Path) -> None:
    text = (ROOT / "exordos/manifests/dbaas_audit.yaml.j2").read_text()
    # Both template expressions are quoted strings before their values are rendered.
    manifest = yaml.safe_load(text.replace("{{ version }}", "test"))
    resources = manifest["resources"]
    assert "audit" in manifest["requirements"]
    main = yaml.safe_load((ROOT / "exordos/manifests/dbaas.yaml.j2").read_text())
    assert "audit" not in main["requirements"]
    config = resources["$core.config.configs"]["dbaas_audit_config"]
    assert config["project_id"] == "12345678-c625-4fee-81d5-f691897b8142"
    token = resources["$core.iam.tokens"]["dbaas_audit_token"]
    assert token["auto_renew"] is True
    assert token["scope"] == 'f"project:{$dbaas_audit.imports.$dbaas_project:uuid}"'
    content = config["body"]["content"].strip().removeprefix('f"').removesuffix('"')
    content = content.replace(
        "{{ audit_endpoint | default('http://audit.local.genesis-core.tech:8080/') }}",
        "http://audit.example/",
    )
    content = content.replace(
        "{$core.iam.tokens.$dbaas_audit_token:access_token}", "test-token"
    )
    directory = tmp_path / "exordos_db.d"
    directory.mkdir()
    (directory / "audit.conf").write_text(content)
    conf = cfg.ConfigOpts()
    opts.register_audit_delivery_opts(conf)
    assert conf.audit_delivery.enabled is False
    conf(["--config-dir", str(directory)])
    assert conf.audit_delivery.enabled is True
    assert conf.audit_delivery.auth_token == "test-token"
    assert conf.audit_delivery.endpoint == "http://audit.example/"
    for name in ["user-api", "orch-api", "status-api", "core-agent", "gservice"]:
        unit = (ROOT / f"etc/systemd/exordos-db-{name}.service").read_text()
        assert "--config-dir /etc/exordos_db/exordos_db.d" in unit
