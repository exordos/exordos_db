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
from exordos_db.common import rollback

LOG = logging.getLogger(__name__)

# NOTE: don't forget to update validation in controlplane
PG_SYSTEM_USERS_REGEX_TMPL = "'^(pg_|dbaas_|postgres$)'"
PG_SYSTEM_DATABASES_TMPL = "('postgres', 'template0', 'template1')"

# Reported instead of the backup spec while the data plane hasn't converged
# to it yet, so the agent keeps applying the spec
BACKUP_UNSETTLED = {"state": "unsettled"}

# Bounds an agent iteration against an unreachable repository
STANZA_TIMEOUT = 120


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

    def config_get(self):
        response = self._client.get(f"{self._endpoint}/config")
        response.raise_for_status()
        return response.json()

    def config_patch(self, config):
        response = self._client.patch(f"{self._endpoint}/config", json=config)
        response.raise_for_status()
        return response.json()

    def restart(self) -> None:
        response = self._client.post(f"{self._endpoint}/restart", json={})
        response.raise_for_status()

    @property
    def member_name(self) -> str:
        return str(self._config["name"])

    def cluster(self) -> dict[str, tp.Any]:
        response = self._client.get(f"{self._endpoint}/cluster")
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
    # None leaves them unmanaged, e.g. until a restored cluster's are imported.
    # The default has to be None too: restalchemy replaces a None value with
    # the default, so any other default would turn "unmanaged" into "empty"
    # and drop everything the cluster has.
    databases = properties.property(ra_types.AllowNone(ra_types.Dict()), default=None)
    users = properties.property(ra_types.AllowNone(ra_types.Dict()), default=None)
    nodes_number = properties.property(ra_types.Integer(min_value=1, max_value=16))
    sync_replica_number = properties.property(
        ra_types.Integer(min_value=0, max_value=15)
    )
    status = properties.property(
        ra_types.Enum([s.value for s in pc.InstanceStatus]),
        default=pc.InstanceStatus.ACTIVE.value,
    )
    backup = properties.property(ra_types.AllowNone(ra_types.Dict()), default=None)
    rollback = properties.property(ra_types.AllowNone(ra_types.Dict()), default=None)
    # Whether the control plane left the roles unmanaged (users and databases
    # are None in the target), remembered to report them the same way
    roles_unmanaged = properties.property(ra_types.Boolean(), default=False)
    # The roles found on the data plane while they are unmanaged. Not a
    # target field: it changes the full hash only, which is how the control
    # plane learns about changes on the data plane, while a differing target
    # field would make the agent apply the target instead of reporting it.
    found_roles = properties.property(ra_types.AllowNone(ra_types.Dict()), default=None)

    # The requested rollback is kept to know one is in progress while
    # PostgreSQL is down
    _meta_fields: tp.ClassVar[set[str]] = {
        "uuid",
        "name",
        "nodes_number",
        "rollback",
        "roles_unmanaged",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.c = ClientsSingleton()

    def get_meta_model_fields(self) -> set[str] | None:
        return set(self._meta_fields)

    def get_resource_ignore_fields(self) -> list[str]:
        return [*super().get_resource_ignore_fields(), "roles_unmanaged"]

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

    def _reconcile_DCS(self):
        sync_enabled = self.nodes_number > 1 and self.sync_replica_number
        tconfig = {
            "synchronous_mode": bool(sync_enabled),
            "synchronous_mode_strict": bool(sync_enabled),
            "synchronous_node_count": self.sync_replica_number,
            "postgresql": {
                "parameters": {
                    "archive_command": pgbackrest.archive_command(self.backup),
                },
                # A replica whose rewind fails, e.g. after a rollback to a
                # moment older than the WAL it keeps, is cloned again rather
                # than started on its old timeline
                "remove_data_directory_on_rewind_failure": True,
            },
        }
        LOG.info("DCS patch: %s", tconfig)
        self.c.pclient.config_patch(tconfig)

    def _fill_DCS(self, config: dict[str, tp.Any]) -> None:
        self.sync_replica_number = config["synchronous_node_count"]

    def _reconcile_backup_stanza(self) -> None:
        # Idempotent, but talks to the repository, so run it only when the
        # repository changed or this node hasn't created the stanza yet
        # (e.g. it has just become the primary)
        if self.backup is None or pgbackrest.stanza_ready(self.backup):
            return

        # A repository that can't be reached or used mustn't hold up the
        # users, the databases and the DCS: the stanza is retried on the next
        # iteration, which the unsettled backup triggers
        try:
            pgbackrest.run(
                self.backup["stanza"], "stanza-create", timeout=STANZA_TIMEOUT
            )
        except (pgbackrest.PgBackRestError, subprocess.TimeoutExpired) as e:
            LOG.error("Stanza %s can't be created: %s", self.backup["stanza"], e)
            return
        pgbackrest.mark_stanza_ready(self.backup)
        LOG.info("Stanza %s created", self.backup["stanza"])

    def _fill_backup(self, config: dict[str, tp.Any]) -> None:
        spec = pgbackrest.load_spec()
        parameters = config.get("postgresql", {}).get("parameters", {})

        archiving = parameters.get("archive_command") == pgbackrest.archive_command(
            spec
        )
        stanza_missing = (
            spec is not None
            and self.c.pclient.is_primary(get_ttl_hash(seconds=20))
            and not pgbackrest.stanza_ready(spec)
        )
        self.backup = spec if archiving and not stanza_missing else BACKUP_UNSETTLED

    def _start_rollback_job(self, rollback_id: str) -> None:
        unit = rollback.job_unit(rollback_id)
        # A failed earlier attempt leaves the unit loaded under the same name
        subprocess.run(
            ["systemctl", "reset-failed", unit], capture_output=True, check=False
        )
        subprocess.run(
            [
                "systemd-run",
                f"--unit={unit}",
                "--collect",
                # A oneshot unit is "started" when it finishes, don't wait
                "--no-block",
                "--property=Type=oneshot",
                # The job leaves the promoted PostgreSQL running for Patroni
                "--property=KillMode=process",
                rollback.JOB_COMMAND,
            ],
            capture_output=True,
            text=True,
            check=True,
        )

    @staticmethod
    def _rollback_jobs_active() -> set[str]:
        """Return the rollback jobs running on the node, as their units."""
        result = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--all",
                "--plain",
                "--no-legend",
                f"{rollback.JOB_UNIT_PREFIX}*",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        active = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            # A running oneshot unit is "activating", not "active"
            if len(fields) > 2 and fields[2] in (
                "activating",
                "active",
                "deactivating",
            ):
                active.add(fields[0].removesuffix(".service"))
        return active

    def _reconcile_rollback(self) -> bool:
        """Take the next step of an in-place rollback, if one is due.

        Return whether the node has converged to the requested rollback.
        """
        spec = self.rollback
        if spec is None or rollback.applied_id() == spec["id"]:
            return True

        state = rollback.load_state()
        active_jobs = self._rollback_jobs_active()
        # Including a job of a superseded rollback, which is waited for
        job_active = bool(active_jobs)
        if (
            state is not None
            and state["id"] == spec["id"]
            and state["phase"] == rollback.Phase.RESTORING.value
            and rollback.job_unit(spec["id"]) not in active_jobs
        ):
            # The job records its outcome, a job that is gone without one
            # was killed, e.g. by a reboot. One that never started, e.g. the
            # agent restarted before starting it, is started again.
            state = rollback.load_state()
            if state is not None and state["phase"] == rollback.Phase.RESTORING.value:
                if state.get("started"):
                    rollback.save_state(
                        spec,
                        rollback.Phase.FAILED,
                        error="the rollback job stopped without a result",
                    )
                else:
                    rollback.save_state(spec, rollback.Phase.PAUSED)
                state = rollback.load_state()

        pclient = self.c.pclient
        config = pclient.config_get()
        action = rollback.decide(
            spec,
            state,
            bool(config.get("pause")),
            pclient.cluster().get("members", []),
            pclient.member_name,
            applied_id=config.get(rollback.DCS_KEY),
            job_active=job_active,
            owner=config.get(rollback.OWNER_KEY),
        )
        LOG.info("Rollback %s: %s", spec["id"], action.value)

        if action is rollback.Action.PAUSE:
            # Saved first: pausing again is harmless, a pause without a
            # saved state is what a restart in between would leave
            rollback.save_state(spec, rollback.Phase.PAUSED)
            owner = {"id": spec["id"], "node": pclient.member_name}
            pclient.config_patch({"pause": True, rollback.OWNER_KEY: owner})
        elif action is rollback.Action.REPAUSE:
            pclient.config_patch({"pause": True})
        elif action is rollback.Action.STOP_POSTGRES:
            rollback.stop_postgres()
            rollback.save_state(spec, rollback.Phase.STOPPED)
        elif action is rollback.Action.START_JOB:
            rollback.save_state(spec, rollback.Phase.RESTORING)
            try:
                self._start_rollback_job(spec["id"])
            except subprocess.CalledProcessError as e:
                # Not started: tried again, rather than taken for a job that
                # died without a result
                rollback.save_state(spec, rollback.Phase.PAUSED)
                LOG.error("Rollback %s job didn't start: %s", spec["id"], e.stderr)
        elif action is rollback.Action.RESUME:
            # Recorded for the whole cluster along with resuming it
            pclient.config_patch({"pause": False, rollback.DCS_KEY: spec["id"]})
            rollback.save_state(spec, rollback.Phase.RESUMED)
        elif action is rollback.Action.RESTART_POSTGRES:
            pclient.restart()
            rollback.save_state(spec, rollback.Phase.RESUMED, restarted=True)
        elif action is rollback.Action.MARK_APPLIED:
            rollback.mark_applied(spec["id"])
            pgbackrest.remove_restore_config()
            LOG.info("Rollback %s applied", spec["id"])
            return True
        elif action is rollback.Action.FAILED:
            LOG.error(
                "Rollback %s failed, the cluster stays paused: %s",
                spec["id"],
                state.get("error") if state else None,
            )
        return False

    def dump_to_dp(self) -> None:
        self.roles_unmanaged = self.users is None

        # Nothing else can be applied to a cluster being rolled back
        if not self._reconcile_rollback():
            return

        primary = self.c.pclient.is_primary(get_ttl_hash(seconds=20))

        # Stop archiving before the config it uses is removed
        if primary and self.backup is None:
            self._reconcile_DCS()

        # Every node keeps the config, any of them may become the primary
        if pgbackrest.apply_spec(self.backup):
            LOG.info("Backup config updated")
        if self.backup is None:
            pgbackrest.mark_stanza_ready(None)

        if not primary:
            LOG.debug("Not a primary node, skipping the rest of dump_to_dp.")
            return

        # A primary is out of recovery, restore_command is no longer used
        if pgbackrest.remove_restore_config():
            LOG.info("Restore config removed")

        # The stanza has to exist before archiving is turned on
        self._reconcile_backup_stanza()
        self._reconcile_DCS()
        if self.users is not None:
            self._reconcile_target_users()
        if self.databases is not None:
            self._reconcile_target_databases()

    @staticmethod
    def _applied_rollback(spec: dict[str, tp.Any] | None) -> dict[str, tp.Any] | None:
        """Report the requested rollback back once this node has applied it.

        The node keeps the id only, so the spec itself has to come from the
        target: reporting anything else would leave the resource with a hash
        the target never matches. A node that hasn't applied it, or one with
        a marker of an older rollback, reports none, and the agent applies
        the target and reports that instead.
        """
        return (
            spec if spec is not None and spec["id"] == rollback.applied_id() else None
        )

    def restore_from_dp(self) -> None:
        # The rollback in progress is all there is to report, PostgreSQL may
        # well be down
        spec = self.rollback
        self.rollback = self._applied_rollback(spec)
        if rollback.in_progress(spec):
            self.users = None
            self.databases = None
            self.found_roles = None
            return

        self.users = {}
        self.databases = {}
        self._fill_actual_users()
        self._fill_actual_databases()
        config = self.c.pclient.config_get()
        self._fill_DCS(config)
        self._fill_backup(config)

        if self.roles_unmanaged:
            self.found_roles = {"users": self.users, "databases": self.databases}
            self.users = None
            self.databases = None
        else:
            self.found_roles = None

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
