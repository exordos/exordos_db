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

from functools import lru_cache
from functools import wraps
import logging
import time

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

LOG = logging.getLogger(__name__)

# NOTE: don't forget to update validation in controlplane
PG_SYSTEM_USERS_REGEX_TMPL = "'^(pg_|dbaas_|postgres$)'"
PG_SYSTEM_DATABASES_TMPL = "('postgres', 'template0', 'template1')"


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

    def _load_config(self):
        with open(constants.PATRONI_CONFIG_FILE, "r") as file:
            config = yaml.safe_load(file)
        self._config = config

    def get_full_state(self):
        return self._client.get(f"{self._endpoint}/").json()

    @lru_cache()
    def is_primary(self, ttl_hash=None):
        del ttl_hash
        return self._client.get(f"{self._endpoint}/primary").status_code == 200

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
        self.reinit_pclient()
        self.reinit_psql()

    def reinit_pclient(self):
        self._pclient = PatroniClient()

    def reinit_psql(self):
        # It's important to log all pg queries here
        logging.getLogger("psycopg").setLevel(logging.DEBUG)
        # We need to run this agent from Linux user with peer access to pg
        self._psql = psycopg.connect("user=postgres", autocommit=True)

    @property
    def pclient(self):
        return self._pclient

    @property
    def psql(self):
        if self._psql.broken or self._psql.closed:
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

    _meta_fields = {"uuid", "name", "nodes_number"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.c = ClientsSingleton()

    def get_meta_model_fields(self) -> set[str] | None:
        return self._meta_fields

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
        }
        LOG.info("DCS patch: %s", tconfig)
        self.c.pclient.config_patch(tconfig)

    def _fill_DCS(self):
        config = self.c.pclient.config_get()
        self.sync_replica_number = config["synchronous_node_count"]

    @on_primary_only
    def dump_to_dp(self) -> None:
        self._reconcile_DCS()
        self._reconcile_target_users()
        self._reconcile_target_databases()

    def restore_from_dp(self) -> None:
        self._fill_actual_users()
        self._fill_actual_databases()
        self._fill_DCS()

    @on_primary_only
    def delete_from_dp(self) -> None:
        # Instance exists along with nodes, so there's nothing to delete
        # TODO: maybe node draining on cluster shrink should be here?
        pass

    @on_primary_only
    def update_on_dp(self) -> None:
        self.dump_to_dp()


class PGCapabilityDriver(meta.MetaFileStorageAgentDriver):
    """PG capability driver."""

    PG_META_PATH = "/var/lib/exordos/exordos_db/pg_meta.json"

    __model_map__ = {
        "pg_instance_node": PGInstance,
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, meta_file=self.PG_META_PATH, **kwargs)
