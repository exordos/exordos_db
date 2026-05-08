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

import enum
import re

from gcl_sdk.agents.universal.dm import models as ua_models
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from exordos_db.common import utils as u
from exordos_db.common.pg_auth import passwd


class PGStatus(str, enum.Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


class PGNameType(types.BaseCompiledRegExpTypeFromAttr):
    # https://www.postgresql.org/docs/current/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS
    pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class PGRoleNameType(types.BaseCompiledRegExpTypeFromAttr):
    # NOTE: don't forget to update dataplane filters too!
    # pg_*, dbaas_*, postgres are reserved names (use dbaas_* for our needs)
    pattern = re.compile(
        r"^(?!pg_)(?!dbaas_)(?!postgres$)[a-zA-Z_][a-zA-Z0-9_$]{0,62}$"
    )


class PGVersion(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
    ua_models.TargetResourceMixin,
):
    __tablename__ = "postgres_versions"

    image = properties.property(types.String(max_length=2048))


class PGInstance(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "postgres_instances"

    name = properties.property(types.String(min_length=1, max_length=255))
    status = properties.property(
        types.Enum([status.value for status in PGStatus]),
        default=PGStatus.NEW.value,
    )
    ipsv4 = properties.property(
        types.TypedList(types.String(max_length=15)),
        default=lambda: [],
    )
    cpu = properties.property(types.Integer(min_value=1, max_value=128))
    ram = properties.property(types.Integer(min_value=512, max_value=1024**3))
    disk_size = properties.property(types.Integer(min_value=8, max_value=1024**3))
    # TODO: restrict shrink/support shrink
    nodes_number = properties.property(types.Integer(min_value=1, max_value=16))
    sync_replica_number = properties.property(
        types.Integer(min_value=0, max_value=15), default=1
    )
    # TODO: support version update
    version = relationships.relationship(PGVersion, required=True, read_only=True)

    def get_users(self, session=None):
        return PGUser.objects.get_all(
            session=session, filters={"instance": dm_filters.EQ(self)}
        )

    def get_databases(self, session=None):
        return PGDatabase.objects.get_all(
            session=session, filters={"instance": dm_filters.EQ(self)}
        )

    def _validate_update(self, session=None):
        disk_size = self.properties["disk_size"]
        if disk_size.is_dirty() and disk_size.old_value > self.disk_size:
            raise NotImplementedError("disk_size shrink is not supported yet")

    def update(self, session=None, force=False):
        self._validate_update(session=session)
        super().update(session=session, force=force)

    def delete(self, session=None, **kwargs):
        u.remove_nested_dm(PGDatabase, "instance", self, session=session)
        u.remove_nested_dm(PGUser, "instance", self, session=session)
        return super().delete(session=session, **kwargs)


class InstanceChildModel(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.ModelWithTimestamp,
    models.ModelWithProject,
    ua_models.TargetResourceMixin,
    orm.SQLStorableMixin,
):
    instance = relationships.relationship(PGInstance, required=True, read_only=True)

    def touch_parent(self, session=None):
        # Now we enforce dataplane updates via parent model, so we don't need
        #  to implement explicit child entities' resources on dataplane level
        # TODO: optimize and bump only updated_at
        self.instance.update(force=True)

    def insert(self, session=None):
        super().insert(session=session)
        self.touch_parent(session=session)

    def update(self, session=None, force=False):
        super().update(session=session, force=force)
        self.touch_parent(session=session)

    def delete(self, session=None, **kwargs):
        res = super().delete(session=session, **kwargs)
        self.touch_parent(session=session)
        return res


class PGUser(InstanceChildModel):
    __tablename__ = "postgres_users"

    name = properties.property(PGRoleNameType(), required=True, read_only=True)
    status = properties.property(
        types.Enum([status.value for status in PGStatus]),
        default=PGStatus.ACTIVE.value,
    )
    password = properties.property(types.String(min_length=8, max_length=99))
    password_hash = properties.property(types.String(min_length=1, max_length=512))

    def _update_pw_hash(self):
        self.password_hash = passwd.scram_sha_256(self.password)

    def insert(self, session=None):
        self._update_pw_hash()
        super().insert(session=session)

    def update(self, session=None, force=False):
        self._update_pw_hash()
        super().update(session=session, force=force)


class PGDatabase(InstanceChildModel):
    __tablename__ = "postgres_databases"

    name = properties.property(PGNameType(), required=True)
    status = properties.property(
        types.Enum([status.value for status in PGStatus]),
        default=PGStatus.ACTIVE.value,
    )
    owner = relationships.relationship(PGUser, required=True)


# class PGDatabasePrivilege(str, enum.Enum):
#     ALL = "ALL"
#     CREATE = "CREATE"
#     CONNECT = "CONNECT"
#     TEMPORARY = "TEMPORARY"


# class PGTablePrivilege(str, enum.Enum):
#     ALL = "ALL"
#     SELECT = "SELECT"
#     INSERT = "INSERT"
#     UPDATE = "UPDATE"
#     DELETE = "DELETE"
#     TRUNCATE = "TRUNCATE"
#     REFERENCES = "REFERENCES"
#     TRIGGER = "TRIGGER"
#     MAINTAIN = "MAINTAIN"


# class DatabaseEntity(types_dynamic.AbstractKindModel):
#     KIND = "DATABASE"

#     privileges = properties.property(
#         types.TypedList(types.Enum([v.value for v in PGDatabasePrivilege])),
#         default=[PGDatabasePrivilege.ALL.value],
#     )

#     @property
#     def name(self):
#         # TODO: different kinds (for ex. tables) will have `name` prop
#         return ""


# class PGUserPrivilege(
#     models.ModelWithUUID,
#     models.ModelWithTimestamp,
#     orm.SQLStorableMixin,
# ):

#     __tablename__ = "postgres_user_privileges"
#     user = relationships.relationship(PGUser, required=True)
#     database = relationships.relationship(PGDatabase, required=True)
#     entity = properties.property(
#         types_dynamic.KindModelSelectorType(
#             types_dynamic.KindModelType(DatabaseEntity),
#         ),
#         required=True,
#     )
