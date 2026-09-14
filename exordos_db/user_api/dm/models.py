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
import typing as tp
import uuid

from gcl_sdk.agents.universal.dm import models as ua_models
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from exordos_db.common import utils as u
from exordos_db.common.pg_auth import passwd
from exordos_db.user_api.dm import backups


class RestoreSourceError(ra_exc.ValidationErrorException):
    message = "restore_from: %(reason)s"


class InstanceUpdateError(ra_exc.ValidationErrorException):
    message = "%(reason)s"


class RolesLockedError(ra_exc.RestAlchemyException):
    code = 409
    message = (
        "users and databases of instance %(instance)s can't be changed until "
        "its restore or rollback is over"
    )


def check_nodes_change(old: int, new: int, roles_managed: bool) -> None:
    # A removed node may be the one leading an unfinished rollback
    if new < old and not roles_managed:
        raise InstanceUpdateError(
            reason="nodes_number can't be decreased while the instance is "
            "restored or rolled back"
        )


def check_restore_from_cleared(
    old: backups.S3RestoreSource | None,
    new: backups.S3RestoreSource | None,
    roles_imported: bool,
    restore_status: dict[str, tp.Any] | None,
) -> None:
    # The source of a new instance is what leaves its roles unmanaged until
    # they are matched. Without it the empty rows would be applied, and the
    # agent would drop every user and database the restore brings back.
    #
    # The source of a rollback is also what its spec is rendered from, and
    # the roles are matched as soon as the leader reports them, while a
    # replica may still be rewinding to the new timeline. Taking the spec
    # away then leaves that replica with no rollback to mark as applied, and
    # it would take an incremental backup on top of one of the abandoned
    # timeline when it becomes the primary. Every node reports its phase
    # until it is done with the rollback, so nothing is left in
    # `restore_status` once they all are.
    over = roles_imported and restore_status is None
    if new is None and old is not None and not over:
        raise RestoreSourceError(
            reason="can't be cleared until the restore or the rollback is "
            "over on every node and the users and databases are matched"
        )


def rollback_for_update(
    instance_uuid: uuid.UUID,
    old: backups.S3RestoreSource | None,
    new: backups.S3RestoreSource | None,
    rollback_revision: int | None,
    backup: backups.S3Backup | None,
) -> int | None:
    """Return the revision an update of `restore_from` rolls the data back with.

    A different source rolls the data back in place only with a revision
    higher than any seen so far, so an edit of a manifest can't roll a
    database back by accident.
    """
    if new is None:
        # The data stays as it is
        return None
    if old is not None and new.identity() == old.identity():
        # E.g. rotated credentials of the same source
        return None
    if isinstance(new.target, backups.RestoreLatest):
        # The end of the archive is the state the instance already has
        raise RestoreSourceError(
            reason="target has to be a time to roll the data back in place"
        )

    revisions = [-1 if old is None else old.revision]
    if rollback_revision is not None:
        revisions.append(rollback_revision)
    if new.revision <= max(revisions):
        raise RestoreSourceError(
            reason=f"revision must be greater than {max(revisions)} "
            "to roll the data back in place"
        )
    if new.stanza != instance_uuid:
        raise RestoreSourceError(
            reason="an instance can be rolled back in place only to its own "
            "backups, create a new instance to restore another one's"
        )
    if backup is None or backup.repository() != new.repository():
        # The WAL written since the last archived one reaches only the
        # repository backups go to. Recovering from another one ends before
        # the target, after the restore has replaced the data.
        raise RestoreSourceError(
            reason="an instance is rolled back in place only from the "
            "repository its backup goes to"
        )
    return new.revision


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
        default=list,
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
    # Continuous WAL archiving and periodic backups, disabled when None
    backup = properties.property(backups.BACKUP_TYPE, default=None)
    # The backup the data comes from: the cluster is bootstrapped from it on
    # creation, and rolled back to it in place when a source with a higher
    # revision is set later
    restore_from = properties.property(backups.RESTORE_SOURCE_TYPE, default=None)
    # Users and databases of a restored cluster exist on the data plane
    # before the control plane knows them. They aren't managed (so aren't
    # dropped) until they are imported.
    roles_imported = properties.property(types.Boolean(), default=False)
    # The revision of the in-place rollback the nodes converge to, None when
    # `restore_from` is a bootstrap source instead. It is also the highest
    # revision used so far, which the next rollback has to exceed. The spec
    # the nodes get is rendered from `restore_from`, see
    # exordos_db.common.rollback.
    rollback_revision = properties.property(
        types.AllowNone(types.Integer(min_value=0, max_value=2**31 - 1)),
        default=None,
    )
    # {"revision": ..., "phase": ..., "error": ...} of the restore or the
    # rollback in progress as the nodes report it, None when there is none.
    # "revision" is None for the restore of a new instance.
    restore_status = properties.property(types.AllowNone(types.Dict()), default=None)

    def restore_failed(self) -> bool:
        return self.restore_status is not None and bool(self.restore_status["error"])

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

        nodes = self.properties["nodes_number"]
        if nodes.is_dirty():
            check_nodes_change(nodes.old_value, self.nodes_number, self.roles_managed())

        restore_from = self.properties["restore_from"]
        revision = None
        if restore_from.is_dirty():
            check_restore_from_cleared(
                restore_from.old_value,
                self.restore_from,
                self.roles_imported,
                self.restore_status,
            )
            revision = rollback_for_update(
                self.uuid,
                restore_from.old_value,
                self.restore_from,
                self.rollback_revision,
                self.backup,
            )
        if revision is not None:
            self.rollback_revision = revision
            # The rolled back cluster has the roles it had at the target time,
            # the rows are matched to them once the rollback is applied
            self.roles_imported = False

        super().update(session=session, force=force)

    def roles_managed(self) -> bool:
        """Whether users and databases are applied to the data plane.

        They aren't while a restored or rolled back cluster has roles the
        control plane hasn't matched its rows to yet.
        """
        return self.roles_imported or (
            self.restore_from is None and self.rollback_revision is None
        )

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
    # None for users imported from a restored cluster until it's set
    password = properties.property(
        types.AllowNone(types.String(min_length=8, max_length=99)),
        default=None,
    )
    password_hash = properties.property(types.String(min_length=1, max_length=512))

    def _update_pw_hash(self):
        if self.password is not None:
            self.password_hash = passwd.scram_sha_256(self.password)
        elif self.password_hash is None:
            raise ValueError("password is required")

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
