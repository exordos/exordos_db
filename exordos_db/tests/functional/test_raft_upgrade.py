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

"""Run with the node dependencies: patroni[raft] and pysyncobj.

These tests use real Raft listeners and journals, without a deployed cluster.
"""

import getpass
import importlib.util
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest
import yaml

raft = pytest.importorskip("patroni.dcs.raft")
utility = pytest.importorskip("pysyncobj.utility")


def test_legacy_singleton_keeps_dcs_state_when_authentication_is_enabled(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"

    def start(password):
        store = raft.KVStoreTTL(
            None,
            None,
            None,
            self_addr=address,
            partner_addrs=[],
            data_dir=str(tmp_path),
            password=password,
            retry_timeout=5,
        )
        store.startAutoTick()
        deadline = time.monotonic() + 15
        while not store.isReady():
            if time.monotonic() >= deadline:
                store.destroy()
                pytest.fail("Raft singleton did not elect a leader")
            time.sleep(0.05)
        return store

    initialize = "/db/scope/initialize"
    config = "/db/scope/config"
    original = start(None)
    try:
        assert original.set(initialize, "system-identifier")
        assert original.set(config, '{"ttl":30,"custom":"retained"}')
        previous = {key: original.get(key).copy() for key in (initialize, config)}
    finally:
        original.destroy()

    authenticated = start("a" * 64)
    try:
        assert authenticated.encryptor is not None
        assert isinstance(
            utility.TcpUtility("a" * 64, timeout=2).executeCommand(address, ["status"]),
            dict,
        )
        for password in (None, "b" * 64):
            with pytest.raises(utility.UtilityException):
                utility.TcpUtility(password, timeout=1).executeCommand(
                    address, ["status"]
                )
        for key, value in previous.items():
            assert authenticated.get(key) == value
        assert authenticated.set("/db/scope/after-upgrade", "new-write")
    finally:
        authenticated.destroy()


def check_singleton_migration(connection, config):
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    path = Path(__file__).parents[3] / "migrations/0003-add-patroni-password-d7e854.py"
    spec = importlib.util.spec_from_file_location("patroni_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Resource UUIDs identify objects; res_uuid identifies their kind/UUID pair.
    connection.execute(
        "CREATE TEMP TABLE ua_actual_resources (uuid uuid, "
        "kind text, res_uuid uuid PRIMARY KEY, value jsonb)"
    )
    connection.execute(
        "CREATE TEMP TABLE ua_target_resources (uuid uuid, "
        "kind text, res_uuid uuid PRIMARY KEY, master uuid)"
    )
    connection.execute(
        "CREATE TEMP TABLE postgres_instances (uuid uuid PRIMARY KEY, "
        "nodes_number integer, updated_at timestamp)"
    )
    singleton, unreported, multiple, config_uuid = [uuid.uuid4() for _ in range(4)]
    for identifier, nodes in [(singleton, 1), (unreported, 1), (multiple, 3)]:
        connection.execute(
            "INSERT INTO postgres_instances VALUES (%s, %s, %s)",
            (identifier, nodes, "2020-01-01"),
        )
    connection.execute(
        "INSERT INTO ua_actual_resources VALUES (%s, %s, %s, %s)",
        (singleton, "node_set", uuid.uuid4(), Jsonb({"nodes": {"node": {}}})),
    )
    resource_id = uuid.uuid4()
    connection.execute(
        "INSERT INTO ua_actual_resources VALUES (%s, %s, %s, %s)",
        (
            config_uuid,
            "config",
            resource_id,
            Jsonb(
                {
                    "path": "/var/lib/postgresql/patroni/patroni.yml",
                    "body": {"kind": "text", "content": yaml.safe_dump(config)},
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO ua_target_resources VALUES (%s, %s, %s, %s)",
        (config_uuid, "config", resource_id, singleton),
    )
    with connection.cursor(row_factory=dict_row) as session:
        module.MigrationStep().upgrade(session)
        first = session.execute(
            "SELECT patroni_password, updated_at FROM "
            "postgres_instances WHERE uuid = %s",
            (singleton,),
        ).fetchone()
        assert len(first["patroni_password"]) == 64
        assert first["updated_at"].year > 2020
        module.MigrationStep().upgrade(session)
        assert (
            session.execute(
                "SELECT patroni_password FROM postgres_instances WHERE uuid = %s",
                (singleton,),
            ).fetchone()["patroni_password"]
            == first["patroni_password"]
        )
        assert (
            session.execute(
                "SELECT count(*) AS count FROM postgres_instances "
                "WHERE patroni_password IS NULL"
            ).fetchone()["count"]
            == 2
        )


def test_singleton_patroni_restart_keeps_postgres_and_raft_state(tmp_path, monkeypatch):
    import requests

    from exordos_db.agent.universal.drivers import pg

    psycopg = pytest.importorskip("psycopg")
    postgres = shutil.which("postgres")
    if postgres is None:
        versions = sorted(Path("/usr/lib/postgresql").glob("*/bin/postgres"))
        postgres = str(versions[-1]) if versions else None
    if postgres is None:
        pytest.skip("PostgreSQL server binaries are required")

    def port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    pg_port, rest_port, raft_port = port(), port(), port()
    raft_address = f"127.0.0.1:{raft_port}"
    config = {
        "scope": "singleton-upgrade",
        "name": "singleton",
        "restapi": {
            "listen": f"127.0.0.1:{rest_port}",
            "connect_address": f"127.0.0.1:{rest_port}",
            "authentication": {"username": "patroni", "password": "patroni"},
        },
        "raft": {
            "self_addr": raft_address,
            "partner_addrs": [raft_address],
            "data_dir": str(tmp_path / "raft"),
        },
        "bootstrap": {
            "dcs": {"ttl": 20, "loop_wait": 2, "retry_timeout": 3},
            "initdb": [{"auth": "trust"}],
        },
        "postgresql": {
            "listen": f"127.0.0.1:{pg_port}",
            "connect_address": f"127.0.0.1:{pg_port}",
            "data_dir": str(tmp_path / "data"),
            "bin_dir": str(Path(postgres).parent),
            "pgpass": str(tmp_path / "pgpass"),
            "authentication": {
                "superuser": {"username": getpass.getuser()},
                "replication": {"username": "replicator", "password": "test"},
            },
            "parameters": {"unix_socket_directories": "/tmp"},
            "pg_hba": ["host all all 127.0.0.1/32 trust"],
        },
        "watchdog": {"mode": "off"},
    }
    config_path = tmp_path / "patroni.yml"
    log_path = tmp_path / "patroni.log"

    def start():
        config_path.write_text(yaml.safe_dump(config))
        config_path.chmod(0o600)
        with log_path.open("a") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "patroni", str(config_path)],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and process.poll() is None:
            try:
                connection = psycopg.connect(
                    host="127.0.0.1",
                    port=pg_port,
                    user=getpass.getuser(),
                    dbname="postgres",
                    connect_timeout=1,
                    autocommit=True,
                )
                ready = requests.get(f"http://127.0.0.1:{rest_port}/config", timeout=1)
                if (
                    not connection.execute("SELECT pg_is_in_recovery()").fetchone()[0]
                    and ready.status_code == 200
                    and "loop_wait" in ready.json()
                ):
                    return process, connection
                connection.close()
            except (psycopg.Error, requests.RequestException):
                pass
            time.sleep(0.1)
        process.terminate()
        process.wait(timeout=15)
        pytest.fail(f"Patroni did not start: {log_path.read_text()}")

    def stop(process, connection):
        connection.close()
        process.terminate()
        process.wait(timeout=15)
        assert process.returncode == 0

    original, connection = start()
    try:
        monkeypatch.setattr(pg.constants, "PATRONI_CONFIG_FILE", str(config_path))
        monkeypatch.setattr(
            pg.constants, "PATRONI_API_ENDPOINT", f"http://127.0.0.1:{rest_port}"
        )
        client = pg.PatroniClient()
        assert client.config_patch({"loop_wait": 2})["loop_wait"] == 2
        check_singleton_migration(connection, config)
        connection.execute("CREATE TABLE sentinel (value text)")
        connection.execute("INSERT INTO sentinel VALUES ('before-upgrade')")
        identifier = connection.execute(
            "SELECT system_identifier FROM pg_control_system()"
        ).fetchone()[0]
    finally:
        stop(original, connection)

    config["raft"]["password"] = "a" * 64
    config["restapi"]["authentication"]["password"] = "a" * 64
    authenticated, connection = start()
    try:
        endpoint = f"http://127.0.0.1:{rest_port}/config"
        for credentials in (None, ("patroni", "patroni"), ("patroni", "b" * 64)):
            assert (
                requests.patch(
                    endpoint, json={"loop_wait": 2}, auth=credentials, timeout=3
                ).status_code
                == 401
            )
        assert client.config_patch({"loop_wait": 2})["loop_wait"] == 2
        assert connection.execute("SELECT value FROM sentinel").fetchone() == (
            "before-upgrade",
        )
        assert (
            connection.execute(
                "SELECT system_identifier FROM pg_control_system()"
            ).fetchone()[0]
            == identifier
        )
        connection.execute("INSERT INTO sentinel VALUES ('after-upgrade')")
        assert connection.execute("SELECT COUNT(*) FROM sentinel").fetchone() == (2,)
    finally:
        stop(authenticated, connection)
