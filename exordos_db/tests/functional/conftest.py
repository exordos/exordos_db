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
"""Functional tests run against an installation with the dbaas element.

The installation is reached through the Core API, the dbaas API and ssh to the
PostgreSQL nodes, configured by the environment:

- EXORDOS_ENDPOINT, EXORDOS_USERNAME, EXORDOS_PASSWORD, EXORDOS_PROJECT_ID:
  the Core API and the project the instances are created in;
- DBAAS_ENDPOINT: the dbaas user API, found from the dbaas-cp node if empty;
- PITR_S3_ENDPOINT, PITR_S3_BUCKET, PITR_S3_ACCESS_KEY, PITR_S3_SECRET_KEY:
  an existing bucket for backups (e.g. an s3aas instance);
- DBAAS_SSH_USER: the user to ssh to the nodes with, the key is ssh's default.
"""

from __future__ import annotations

import datetime
import json
import os
import shlex
import subprocess
import time
import typing as tp
import uuid as sys_uuid

import pytest
import requests

CORE_ENDPOINT = os.environ.get("EXORDOS_ENDPOINT", "http://10.20.0.2/api/core")
CORE_USERNAME = os.environ.get("EXORDOS_USERNAME", "admin")
CORE_PASSWORD = os.environ.get("EXORDOS_PASSWORD", "")
PROJECT_ID = os.environ.get(
    "EXORDOS_PROJECT_ID", "12345678-c625-4fee-81d5-f691897b8142"
)
DBAAS_ENDPOINT = os.environ.get("DBAAS_ENDPOINT", "")

S3_ENDPOINT = os.environ.get("PITR_S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("PITR_S3_BUCKET", "dbaas-backups")
S3_ACCESS_KEY = os.environ.get("PITR_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("PITR_S3_SECRET_KEY", "")

SSH_USER = os.environ.get("DBAAS_SSH_USER", "ubuntu")
SSH_OPTIONS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "LogLevel=ERROR",
]

KEEP_INSTANCES = bool(os.environ.get("DBAAS_KEEP_INSTANCES"))

POLL_TIMEOUT = int(os.environ.get("DBAAS_POLL_TIMEOUT", "1200"))
POLL_INTERVAL = int(os.environ.get("DBAAS_POLL_INTERVAL", "5"))

INSTANCES = "/v1/types/postgres/instances/"
REPOSITORIES = "/v1/types/postgres/backup_repositories/"
BACKUPS = "/v1/types/postgres/backups/"
WORK_DIR = "/var/lib/exordos/exordos_db"


def wait_for(check: tp.Callable[[], tp.Any], what: str, timeout: int = POLL_TIMEOUT):
    """Poll until `check` returns a truthy value, return that value."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if result := check():
                return result
        except Exception as e:  # noqa: BLE001 - the stand is converging
            last_error = e
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"Timed out waiting for {what} (last error: {last_error})")


class Api:
    def __init__(self) -> None:
        self._token: str | None = None
        self._endpoint = DBAAS_ENDPOINT

    def _authenticate(self) -> str:
        response = requests.post(
            f"{CORE_ENDPOINT}/v1/iam/clients/default/actions/get_token/invoke",
            data={
                "grant_type": "password",
                "username": CORE_USERNAME,
                "password": CORE_PASSWORD,
                "scope": f"project:{PROJECT_ID}",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _request(self, base: str, method: str, path: str, body=None):
        for _ in range(2):
            if self._token is None:
                self._token = self._authenticate()
            response = requests.request(
                method,
                f"{base}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                json=body,
                timeout=60,
            )
            if response.status_code != 401:
                return response
            self._token = None
        return response

    @property
    def endpoint(self) -> str:
        if not self._endpoint:
            nodes = self._request(
                CORE_ENDPOINT, "GET", "/v1/compute/nodes/?name=dbaas-cp"
            ).json()
            self._endpoint = f"http://{_find_ipv4(nodes[0])}:8080"
        return self._endpoint

    def call(self, method: str, path: str, body=None, expect: int | None = None):
        response = self._request(self.endpoint, method, path, body)
        if expect is not None and response.status_code != expect:
            raise AssertionError(
                f"{method} {path}: {response.status_code} {response.text}"
            )
        return response

    def get(self, path: str):
        return self.call("GET", path, expect=200).json()


def _find_ipv4(value: tp.Any) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("ipv4"), str):
            return value["ipv4"]
        values = value.values()
    elif isinstance(value, list):
        values = value
    else:
        return None
    for nested in values:
        if found := _find_ipv4(nested):
            return found
    return None


class Cluster:
    """A PostgreSQL instance: its API resources and its nodes."""

    def __init__(self, api: Api, uuid: str) -> None:
        self.api = api
        self.uuid = uuid

    @property
    def path(self) -> str:
        return f"{INSTANCES}{self.uuid}"

    def instance(self) -> dict:
        return self.api.get(self.path)

    def ips(self) -> list[str]:
        return self.instance()["ipsv4"]

    def ssh(self, ip: str, command: str, check: bool = True) -> str:
        result = subprocess.run(
            ["ssh", *SSH_OPTIONS, f"{SSH_USER}@{ip}", command],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"{ip}: {command} failed: {result.stderr.strip() or result.stdout}"
            )
        return result.stdout

    def members(self, ip: str | None = None) -> list[dict]:
        for node in [ip] if ip else self.ips():
            try:
                out = self.ssh(node, "curl -sf http://127.0.0.1:8008/cluster")
                return json.loads(out)["members"]
            except (AssertionError, ValueError):
                continue
        raise AssertionError("No node answers /cluster")

    def leader(self) -> str:
        leaders = [m["host"] for m in self.members() if m["role"] == "leader"]
        assert leaders, "The cluster has no leader"
        return leaders[0]

    def dcs(self) -> dict:
        return json.loads(
            self.ssh(self.ips()[0], "curl -sf http://127.0.0.1:8008/config")
        )

    def sql(self, query: str, db: str = "postgres", ip: str | None = None) -> str:
        command = f"sudo -u postgres psql -v ON_ERROR_STOP=1 -d {db} -Atc {shlex.quote(query)}"
        return self.ssh(ip or self.leader(), command).strip()

    def rows(self, ip: str | None = None) -> dict[str, int]:
        out = self.sql("select note, count(*) from orders group by note", "appdb", ip)
        return {n: int(c) for n, c in (line.split("|") for line in out.splitlines())}

    def table_exists(self, table: str, ip: str | None = None) -> bool:
        query = f"select to_regclass('{table}') is not null"
        return self.sql(query, "appdb", ip) == "t"

    def can_login(self, user: str, password: str, db: str) -> bool:
        command = (
            f"PGPASSWORD={shlex.quote(password)} psql "
            f"{shlex.quote(f'host=127.0.0.1 user={user} dbname={db}')} -Atc 'select 1'"
        )
        return self.ssh(self.leader(), command, check=False).strip() == "1"

    def now(self) -> str:
        return self.sql(
            "select to_char(clock_timestamp() at time zone 'utc', "
            '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
        )

    def timeline(self, ip: str) -> int:
        # A replica's checkpoint lags until its restartpoint
        return int(
            self.sql(
                "select case when pg_is_in_recovery() "
                "then (select received_tli from pg_stat_wal_receiver) "
                "else (select timeline_id from pg_control_checkpoint()) end",
                ip=ip,
            )
        )

    def wait_rows(self, expected: dict[str, int]) -> None:
        """Wait for every node to have the rows, replicas may lag a moment."""
        for ip in self.ips():
            wait_for(lambda ip=ip: self.rows(ip) == expected, f"{expected} on {ip}")

    def wait_status(self, status: str) -> None:
        wait_for(lambda: self.instance()["status"] == status, f"{self.uuid} {status}")

    def wait_ready(self, databases: tp.Collection[str]) -> str:
        """Wait for a leader archiving to its stanza with the databases."""

        def ready():
            leader = self.leader()
            marker = self.ssh(
                leader,
                f"sudo test -f {WORK_DIR}/backup_stanza.sha256 && echo ok",
                check=False,
            )
            archiving = "pgbackrest" in self.dcs()["postgresql"]["parameters"].get(
                "archive_command", ""
            )
            present = set(
                self.sql("select datname from pg_database", ip=leader).splitlines()
            )
            return marker.strip() == "ok" and archiving and set(databases) <= present

        wait_for(ready, f"{self.uuid} to archive with {databases}")
        return self.leader()

    def backup_now(self) -> None:
        self.ssh(self.leader(), "sudo systemctl start exordos-db-pg-backup")

    def backups(self, stanza: str | None = None) -> list[dict]:
        """The backups of a stanza of the instance as pgBackRest lists them."""
        out = self.ssh(
            self.leader(),
            f"sudo -u postgres pgbackrest --stanza={stanza or self.uuid} "
            "--output=json info",
        )
        return json.loads(out)[0].get("backup", [])

    def backup_rows(self) -> list[dict]:
        """The backups of the instance the API shows."""
        # A relationship is filtered by its URI
        return self.api.get(f"{BACKUPS}?instance={self.path}")

    def archive_now(self) -> None:
        """Switch the WAL segment and wait until it is archived."""
        current = self.sql("select pg_walfile_name(pg_current_wal_lsn())")
        self.sql("select pg_switch_wal()")
        wait_for(
            lambda: (
                self.sql(
                    "select coalesce(last_archived_wal, '') from pg_stat_archiver"
                )[:24]
                >= current
            ),
            f"{current} to be archived",
        )

    def applied_rollback(self, ip: str) -> str | None:
        out = self.ssh(ip, f"sudo cat {WORK_DIR}/rollback.json", check=False)
        return json.loads(out)["id"] if out.strip() else None

    def rollback_phase(self, ip: str) -> str | None:
        out = self.ssh(ip, f"sudo cat {WORK_DIR}/rollback_state.json", check=False)
        return json.loads(out)["phase"] if out.strip() else None

    def wait_rollback(self, revision: int) -> None:
        """Wait for every node and the control plane to finish a rollback."""

        def applied():
            ips = self.ips()
            return (
                all(self.applied_rollback(ip) == str(revision) for ip in ips)
                and self.instance()["status"] == "ACTIVE"
                and not self.dcs().get("pause")
            )

        try:
            wait_for(applied, f"rollback {revision} of {self.uuid}")
        except AssertionError as e:
            raise AssertionError(f"{e}\n{self.describe()}") from None

    def describe(self) -> str:
        """What the nodes say about a rollback, for a failed test."""
        lines = []
        try:
            config = self.dcs()
            lines.append(
                f"dcs: pause={config.get('pause')} "
                f"applied={config.get('exordos_rollback')} "
                f"owner={config.get('exordos_rollback_owner')}"
            )
            lines.extend(
                f"member {m['host']} {m['role']} {m['state']} "
                f"timeline={m.get('timeline')}"
                for m in self.members()
            )
        except Exception as e:  # noqa: BLE001 - describing a broken cluster
            lines.append(f"no cluster view: {e}")
        for ip in self.ips():
            lines.append(
                f"{ip}: phase={self.rollback_phase(ip)} "
                f"applied={self.applied_rollback(ip)}"
            )
            lines.append(
                self.ssh(
                    ip,
                    "sudo journalctl -u exordos-patroni --no-pager -n 400 | "
                    "grep -iE 'rewind|basebackup|reinit|bootstrap|FATAL|ERROR' | "
                    "tail -12",
                    check=False,
                )
            )
        return "\n".join(lines)

    def users(self) -> dict[str, str]:
        return {u["name"]: u["uuid"] for u in self.api.get(f"{self.path}/users/")}

    def databases(self) -> dict[str, tuple[str, str]]:
        names = {v: k for k, v in self.users().items()}
        return {
            d["name"]: (d["uuid"], names.get(d["owner"].rsplit("/", 1)[-1]))
            for d in self.api.get(f"{self.path}/databases/")
        }

    def create_user(self, name: str, password: str) -> str:
        body = {
            "name": name,
            "password": password,
            "project_id": PROJECT_ID,
            "instance": self.path,
        }
        return self.api.call("POST", f"{self.path}/users/", body, 201).json()["uuid"]

    def create_database(self, name: str, owner: str) -> str:
        body = {
            "name": name,
            "owner": f"{self.path}/users/{owner}",
            "project_id": PROJECT_ID,
            "instance": self.path,
        }
        return self.api.call("POST", f"{self.path}/databases/", body, 201).json()[
            "uuid"
        ]


@pytest.fixture(scope="session")
def api() -> Api:
    return Api()


@pytest.fixture(scope="session")
def pg_version(api: Api) -> str:
    return api.get("/v1/types/postgres/versions/")[0]["uuid"]


@pytest.fixture(scope="module")
def backup_repositories(api: Api):
    """Create repositories in the bucket, deleting them when the module is done."""
    created = []

    def create(**storage) -> str:
        body = {
            "name": f"functional-{sys_uuid.uuid4().hex[:6]}",
            "project_id": PROJECT_ID,
            "storage": {
                "kind": "s3",
                "endpoint": S3_ENDPOINT,
                "bucket": S3_BUCKET,
                "access_key": S3_ACCESS_KEY,
                "secret_key": S3_SECRET_KEY,
                **storage,
            },
            # Every run gets its own key, the bucket may be shared
            "encryption_key": f"functional-{sys_uuid.uuid4()}",
        }
        uuid = api.call("POST", REPOSITORIES, body, 201).json()["uuid"]
        created.append(uuid)
        return uuid

    yield create

    if KEEP_INSTANCES:
        return
    for uuid in created:
        # The instances using it are deleted first
        wait_for(
            lambda uuid=uuid: (
                api.call("DELETE", f"{REPOSITORIES}{uuid}").status_code in (204, 404)
            ),
            f"repository {uuid} to be deleted",
            timeout=300,
        )


@pytest.fixture(scope="module")
def s3_storage(backup_repositories) -> dict:
    """A reference to the repository of the module, for backup and restore_from."""
    return {"kind": "repository", "repository": backup_repositories()}


@pytest.fixture(scope="module")
def instances(api: Api, pg_version: str, backup_repositories):
    """Create instances, deleting them when the module is done."""
    created = []

    def create(name: str, nodes: int = 1, **fields) -> Cluster:
        body = {
            "name": f"{name}-{sys_uuid.uuid4().hex[:6]}",
            "project_id": PROJECT_ID,
            "cpu": 1,
            "ram": 2048,
            "disk_size": 15,
            "nodes_number": nodes,
            "sync_replica_number": 0,
            "version": f"/v1/types/postgres/versions/{pg_version}",
            **fields,
        }
        uuid = api.call("POST", INSTANCES, body, 201).json()["uuid"]
        created.append(uuid)
        return Cluster(api, uuid)

    yield create

    # Kept for a look at a failure on the nodes
    if KEEP_INSTANCES:
        return
    for uuid in created:
        api.call("DELETE", f"{INSTANCES}{uuid}")


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
