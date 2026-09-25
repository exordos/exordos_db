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

from functools import wraps
import logging
import subprocess
import time
import typing as tp

from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.infra import constants as pc
import psycopg
from psycopg import sql
import requests
from requests.auth import HTTPBasicAuth
from restalchemy.common import singletons
from restalchemy.dm import properties
from restalchemy.dm import types as ra_types
import yaml

from exordos_db.common import constants
from exordos_db.common import pgbackrest

LOG = logging.getLogger(__name__)

# NOTE: don't forget to update validation in controlplane
PG_SYSTEM_USERS_REGEX_TMPL = "'^(pg_|dbaas_|postgres$)'"
PG_SYSTEM_DATABASES_TMPL = "('postgres', 'template0', 'template1')"

# Reported instead of the backup spec while the data plane hasn't converged
# to it yet, so the agent keeps applying the spec
BACKUP_UNSETTLED = {"state": "unsettled"}

# stanza-create talks to the repository on every iteration until it
# succeeds, the rest of the node waits for it meanwhile
STANZA_CREATE_TIMEOUT = 60


def get_ttl_hash(seconds=600):
    """Return the same value withing `seconds` time period"""
    return round(time.time() / seconds)


class PatroniClient:
    def __init__(self):
        self._load_config()
        self._endpoint = constants.PATRONI_API_ENDPOINT
        # We don't need retries/etc because it's local and patroni loves to
        #  return 5XX codes with valid responses
        #  https://patroni.readthedocs.io/en/latest/rest_api.html
        self._client = requests.Session()
        creds = self._config["restapi"]["authentication"]
        # TODO: check for config changes?
        self._client.auth = HTTPBasicAuth(creds["username"], creds["password"])
        self._primary_cache: tuple[int | None, bool] | None = None

    def _load_config(self):
        with open(constants.PATRONI_CONFIG_FILE, "r") as file:
            config = yaml.safe_load(file)
        self._config = config

    def get_full_state(self):
        return self._client.get(f"{self._endpoint}/").json()

    def is_primary(self, ttl_hash: int | None = None) -> bool:
        if self._primary_cache is None or self._primary_cache[0] != ttl_hash:
            self._primary_cache = (
                ttl_hash,
                self._client.get(f"{self._endpoint}/primary").status_code == 200,
            )
        return self._primary_cache[1]

    @property
    def member_name(self) -> str:
        return self._config["name"]

    def cluster(self) -> dict[str, tp.Any]:
        response = self._client.get(f"{self._endpoint}/cluster")
        response.raise_for_status()
        return response.json()

    def config_get(self):
        response = self._client.get(f"{self._endpoint}/config")
        response.raise_for_status()
        return response.json()

    def config_patch(self, config):
        response = self._client.patch(f"{self._endpoint}/config", json=config)
        response.raise_for_status()
        return response.json()


class ClientsSingleton(singletons.InheritSingleton):
    def __init__(self):
        # Connect lazily: a model is built before PostgreSQL may be up
        self._pclient = None
        self._psql = None

    def reinit_pclient(self):
        self._pclient = PatroniClient()

    def reinit_psql(self):
        # It's important to log all pg queries here
        logging.getLogger("psycopg").setLevel(logging.DEBUG)
        # We need to run this agent from Linux user with peer access to pg
        self._psql = psycopg.connect("user=postgres", autocommit=True)

    @property
    def pclient(self):
        if self._pclient is None:
            self.reinit_pclient()
        return self._pclient

    @property
    def psql(self):
        if self._psql is None or self._psql.broken or self._psql.closed:
            self.reinit_psql()
        return self._psql


def on_primary_only(method):
    @wraps(method)
    def _impl(self, *method_args, **method_kwargs):
        if self.c.pclient.is_primary(get_ttl_hash(seconds=20)):
            return method(self, *method_args, **method_kwargs)
        LOG.debug("Not a primary node, skipping %s call.", method.__name__)

    return _impl


class PGInstance(meta.MetaDataPlaneModel):
    name = properties.property(
        ra_types.String(min_length=1, max_length=512),
        required=True,
    )
    databases = properties.property(ra_types.Dict(), default={})
    users = properties.property(ra_types.Dict(), default={})
    nodes_number = properties.property(ra_types.Integer(min_value=1, max_value=16))
    sync_replica_number = properties.property(
        ra_types.Integer(min_value=0, max_value=15)
    )
    status = properties.property(
        ra_types.Enum([s.value for s in pc.InstanceStatus]),
        default=pc.InstanceStatus.ACTIVE.value,
    )
    backup = properties.property(ra_types.AllowNone(ra_types.Dict()), default=None)
    # Sent by the control plane until it has imported the users and databases
    # of a restored cluster: the agent leaves them alone and reports them
    adopt_roles = properties.property(ra_types.Boolean(), default=False)
    # The roles found on the data plane while they are adopted. Not a target
    # field: it changes the full hash only, which is how the control plane
    # learns about changes on the data plane, while a differing target field
    # would make the agent apply the target instead of reporting it.
    found_roles = properties.property(ra_types.AllowNone(ra_types.Dict()), default=None)
    # {"phase": ..., "error": ...} while the restore a new cluster is
    # bootstrapped with is in progress. Not a target field, like found_roles.
    restore_state = properties.property(
        ra_types.AllowNone(ra_types.Dict()), default=None
    )
    # {"error": ..., "timeline": ...} of the repository as the primary uses
    # it, None on a replica or without backups. The timeline tells the
    # control plane the current primary from a former one that went down
    # before it could report again. Not a target field, like found_roles.
    backup_state = properties.property(
        ra_types.AllowNone(ra_types.Dict()), default=None
    )

    _meta_fields: tp.ClassVar[set[str]] = {
        "uuid",
        "name",
        "nodes_number",
        "adopt_roles",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.c = ClientsSingleton()

    def get_meta_model_fields(self) -> set[str] | None:
        return set(self._meta_fields)

    def get_resource_ignore_fields(self) -> list[str]:
        # Reported only as it's sent, a node that isn't adopting is as before
        ignored = super().get_resource_ignore_fields()
        return ignored if self.adopt_roles else [*ignored, "adopt_roles"]

    def _reconcile_target_users(self):
        actual_users = {
            r[0]: r[1]
            for r in self.c.psql.execute(
                f"SELECT rolname, rolpassword FROM pg_authid WHERE rolname !~ {PG_SYSTEM_USERS_REGEX_TMPL}"
            ).fetchall()
        }

        for tname, t in self.users.items():
            if tname not in actual_users:
                self.c.psql.execute(
                    sql.SQL("CREATE USER {username} WITH PASSWORD {password}").format(
                        username=sql.Identifier(tname),
                        password=sql.Literal(t["pw_hash"].replace("'", "''")),
                    )
                )

                LOG.info("User %s created", tname)
                continue

            if t["pw_hash"] != actual_users[tname]:
                self.c.psql.execute(
                    sql.SQL("ALTER USER {username} WITH PASSWORD {password}").format(
                        username=sql.Identifier(tname),
                        password=sql.Literal(t["pw_hash"].replace("'", "''")),
                    )
                )

                LOG.info("User %s: password updated", tname)
                continue

            LOG.info("User %s with actual password already exists", tname)

        # Clean up deleted users
        for aname in actual_users:
            if aname not in self.users:
                try:
                    self.c.psql.execute(
                        sql.SQL("DROP USER IF EXISTS {}").format(sql.Identifier(aname))
                    )
                except psycopg.errors.DependentObjectsStillExist:
                    LOG.warning(
                        "User %s can't be deleted now due to existing "
                        "dependencies, will try later",
                        aname,
                    )
                    continue

                LOG.info("User %s dropped", aname)

    def _fill_actual_users(self):
        actual_users = {
            r[0]: r[1]
            for r in self.c.psql.execute(
                f"SELECT rolname, rolpassword FROM pg_authid WHERE rolname !~ {PG_SYSTEM_USERS_REGEX_TMPL}"
            ).fetchall()
        }

        for aname, apass in actual_users.items():
            self.users[aname] = {"pw_hash": apass}

    def _reconcile_target_databases(self):
        actual_dbs = {
            r[0]: r[1]
            for r in self.c.psql.execute(
                """\
SELECT d.datname as "name",
pg_catalog.pg_get_userbyid(d.datdba) as "owner"
FROM pg_catalog.pg_database d
WHERE d.datname not in """
                + PG_SYSTEM_DATABASES_TMPL
            ).fetchall()
        }

        for tname, t in self.databases.items():
            if tname in actual_dbs:
                LOG.info("Database %s already exists", tname)

                if actual_dbs[tname] != t["owner"]:
                    self.c.psql.execute(
                        sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                            sql.Identifier(tname), sql.Identifier(t["owner"])
                        )
                    )
                    LOG.info("Owner of database %s altered to %s", tname, t["owner"])

                continue

            self.c.psql.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(tname), sql.Literal(t["owner"])
                )
            )

            self.c.psql.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(tname)
                )
            )

            LOG.info("Database %s created", tname)

        # Clean up deleted DBs
        for a in actual_dbs:
            if a not in self.databases:
                self.c.psql.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(a)
                    )
                )

                LOG.info("Database %s dropped", a)

    def _fill_actual_databases(self):
        actual_dbs = {
            r[0]: r[1]
            for r in self.c.psql.execute(
                """\
SELECT d.datname as "name",
pg_catalog.pg_get_userbyid(d.datdba) as "owner"
FROM pg_catalog.pg_database d
WHERE d.datname not in """
                + PG_SYSTEM_DATABASES_TMPL
            ).fetchall()
        }

        for aname, aowner in actual_dbs.items():
            self.databases[aname] = {"owner": aowner}

    def _reconcile_DCS(self, archiving: bool = True) -> None:
        sync_enabled = self.nodes_number > 1 and self.sync_replica_number
        tconfig: dict[str, tp.Any] = {
            "synchronous_mode": bool(sync_enabled),
            "synchronous_mode_strict": bool(sync_enabled),
            "synchronous_node_count": self.sync_replica_number,
        }
        # Left as it is otherwise: a primary that can't reach the
        # repository keeps archiving as it did, WAL waits in pg_wal
        if archiving:
            tconfig["postgresql"] = {
                "parameters": {
                    "archive_command": pgbackrest.archive_command(self.backup),
                    "archive_timeout": pgbackrest.archive_timeout(self.backup),
                },
            }
        LOG.info("DCS patch: %s", tconfig)
        self.c.pclient.config_patch(tconfig)

    def _fill_DCS(self, config: dict[str, tp.Any]) -> None:
        self.sync_replica_number = config["synchronous_node_count"]

    def _reconcile_backup_stanza(self) -> bool:
        """Create the stanza, return whether archiving may be set up."""
        # Idempotent, but talks to the repository, so run it only when the
        # repository changed or this node hasn't created the stanza yet
        # (e.g. it has just become the primary)
        if self.backup is None or pgbackrest.stanza_ready(self.backup):
            return True

        try:
            pgbackrest.run(
                self.backup["stanza"],
                "stanza-create",
                timeout=STANZA_CREATE_TIMEOUT,
            )
        except (pgbackrest.PgBackRestError, subprocess.TimeoutExpired) as e:
            # The repository doesn't hold the rest of the node up; the
            # backup stays unsettled, so this is retried
            LOG.exception("Failed to create stanza %s", self.backup["stanza"])
            pgbackrest.save_backup_error(e)
            return False

        pgbackrest.clear_backup_error()
        pgbackrest.mark_stanza_ready(self.backup)
        LOG.info("Stanza %s created", self.backup["stanza"])
        return True

    def _fill_backup(self, config: dict[str, tp.Any]) -> None:
        spec = pgbackrest.load_spec()
        parameters = config.get("postgresql", {}).get("parameters", {})

        # archive_timeout too: clusters archiving before it was set keep the
        # old value otherwise
        archiving = parameters.get("archive_command") == pgbackrest.archive_command(
            spec
        ) and parameters.get("archive_timeout") == pgbackrest.archive_timeout(spec)
        stanza_missing = (
            spec is not None
            and self.c.pclient.is_primary(get_ttl_hash(seconds=20))
            and not pgbackrest.stanza_ready(spec)
        )
        self.backup = spec if archiving and not stanza_missing else BACKUP_UNSETTLED

    def _patroni_down(self) -> bool:
        try:
            self.c.pclient.is_primary(get_ttl_hash(seconds=20))
        except requests.RequestException:
            return True
        return False

    def dump_to_dp(self) -> None:
        # Patroni restarts over and over after a failed bootstrap: there is
        # nothing to apply, but the failure has to be reported
        if pgbackrest.load_restore_state() is None or not self._patroni_down():
            self._apply()
        # A node that hasn't converged to the target isn't read back: the
        # agent reports the created or updated target, so the progress has
        # to be on it. A repository that fails keeps it from converging.
        self.restore_state = self._bootstrap_state()
        self.backup_state = self._backup_state()

    def _backup_state(self) -> dict[str, tp.Any] | None:
        if pgbackrest.load_spec() is None:
            return None
        pclient = self.c.pclient
        try:
            if not pclient.is_primary(get_ttl_hash(seconds=20)):
                return None
            timeline = pclient.get_full_state().get("timeline")
        except requests.RequestException:
            return None
        return {"error": pgbackrest.load_backup_error(), "timeline": timeline}

    def _apply(self) -> None:
        primary = self.c.pclient.is_primary(get_ttl_hash(seconds=20))

        # Stop archiving before the config it uses is removed
        if primary and self.backup is None:
            self._reconcile_DCS()

        # Every node keeps the config, any of them may become the primary
        if pgbackrest.apply_spec(self.backup):
            LOG.info("Backup config updated")
            # Of the repository used before; the new one is tried anew
            pgbackrest.clear_backup_error()
        if self.backup is None:
            pgbackrest.mark_stanza_ready(None)

        if not primary:
            LOG.debug("Not a primary node, skipping the rest of dump_to_dp.")
            return

        # A primary is out of recovery, restore_command is no longer used
        if pgbackrest.remove_restore_config():
            LOG.info("Restore config removed")

        if not self.adopt_roles:
            self._reconcile_target_users()
            self._reconcile_target_databases()
        # The stanza has to exist before archiving is turned on
        self._reconcile_DCS(archiving=self._reconcile_backup_stanza())

    def _bootstrap_state(self) -> dict[str, tp.Any] | None:
        """Return the progress of the restore the cluster is bootstrapped with."""
        state = pgbackrest.load_restore_state()
        if state is None:
            return None
        pclient = self.c.pclient
        try:
            primary = pclient.is_primary(get_ttl_hash(seconds=20))
            members = [] if primary else pclient.cluster().get("members", [])
        except requests.RequestException:
            # Patroni restarts after a failed bootstrap
            return self._report(state)
        if primary:
            # Recovered and promoted
            pgbackrest.remove_restore_state()
            return None
        leader = next((m for m in members if m.get("role") == "leader"), None)
        if leader is not None and leader["name"] != pclient.member_name:
            # Another node bootstrapped the cluster after this one failed to,
            # this one is its replica now
            pgbackrest.remove_restore_state()
            return None
        return self._report(state)

    @staticmethod
    def _report(state: dict[str, tp.Any]) -> dict[str, tp.Any]:
        return {"phase": state["phase"], "error": state["error"]}

    def restore_from_dp(self) -> None:
        # The restore in progress is all there is to report, PostgreSQL may
        # well be down
        self.restore_state = self._bootstrap_state()
        self.backup_state = self._backup_state()
        if self.restore_state is not None:
            self.found_roles = None
            return

        self.users = {}
        self.databases = {}
        self._fill_actual_users()
        self._fill_actual_databases()
        config = self.c.pclient.config_get()
        self._fill_DCS(config)
        self._fill_backup(config)

        self.found_roles = None
        if self.adopt_roles:
            # PostgreSQL serves reads while the restore still replays WAL,
            # with the roles as of the replayed moment: only those of the
            # promoted primary are the ones of the recovery target
            if self._recovery_over():
                self.found_roles = {"users": self.users, "databases": self.databases}
            # What the control plane sends until it has imported them
            self.users = {}
            self.databases = {}

    def _recovery_over(self) -> bool:
        if not self.c.pclient.is_primary(get_ttl_hash(seconds=20)):
            return False
        return not self.c.psql.execute("SELECT pg_is_in_recovery()").fetchone()[0]

    @on_primary_only
    def delete_from_dp(self) -> None:
        # Instance exists along with nodes, so there's nothing to delete
        # TODO: maybe node draining on cluster shrink should be here?
        pass

    def update_on_dp(self) -> None:
        self.dump_to_dp()


class PGCapabilityDriver(meta.MetaFileStorageAgentDriver):
    """PG capability driver."""

    PG_META_PATH = "/var/lib/exordos/exordos_db/pg_meta.json"

    __model_map__: tp.ClassVar[dict[str, type]] = {
        "pg_instance_node": PGInstance,
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, meta_file=self.PG_META_PATH, **kwargs)
