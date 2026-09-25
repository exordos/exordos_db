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
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import orm

from exordos_db.common import utils as u
from exordos_db.common.pg_auth import passwd
from exordos_db.user_api.dm import backups


class RestoreSourceError(ra_exc.ValidationErrorException):
    message = "restore_from: %(reason)s"


class InstanceUpdateError(ra_exc.ValidationErrorException):
    message = "%(reason)s"


# As many backups of a stanza as the nodes list, see
# exordos_db.common.pgbackrest.CATALOG_MAX_BACKUPS
CATALOG_MAX_BACKUPS = 500


class RepositoryNotFoundError(ra_exc.ValidationErrorException):
    message = "%(field)s: repository %(repository)s not found"


class RepositoryUpdateError(ra_exc.ValidationErrorException):
    message = "%(reason)s"


class EndpointError(ra_exc.ValidationErrorException):
    message = "storage.endpoint: %(reason)s"


class RepositoryInUseError(ra_exc.RestAlchemyException):
    code = 409
    message = "repository %(repository)s is used by instances %(instances)s"


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
    old: backups.RepositoryRestoreSource | None,
    new: backups.RepositoryRestoreSource | None,
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


def check_restore_target(
    source: backups.RepositoryRestoreSource,
    known_backups: tp.Collection["PGBackup"],
    max_known: int,
) -> None:
    """Reject a target time no known backup of the source can recover to.

    The backups are the ones listed for the stanza in the place of the
    source. A stanza nobody listed, or with more backups than a node lists,
    isn't checked: the data plane finds out.
    """
    if not isinstance(source.target, backups.RestoreTime) or not known_backups:
        return
    if len(known_backups) >= max_known:
        return
    # The stop time of a backup is in whole seconds, as in
    # pgbackrest.choose_backup_set
    target = source.target.time.replace(microsecond=0)
    restorable = [b.finished_at for b in known_backups if b.restorable]
    if any(finished_at < target for finished_at in restorable):
        return
    earliest = (
        f"the earliest is after {min(restorable).isoformat()}"
        if restorable
        else "none of its backups is restorable"
    )
    raise RestoreSourceError(
        reason=f"no backup of {source.stanza} finished before target.time, {earliest}"
    )


def restore_spec(
    source: backups.RepositoryRestoreSource,
    repository: "PGBackupRepository",
) -> dict[str, tp.Any]:
    return {**source.target_spec(), "options": repository.pgbackrest_options()}


def rollback_for_update(
    instance_uuid: uuid.UUID,
    old: backups.RepositoryRestoreSource | None,
    new: backups.RepositoryRestoreSource | None,
    rollback_revision: int | None,
    backup_repository: "PGBackupRepository | None",
    source_repository: "PGBackupRepository | None",
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
        # E.g. another repository object for the same storage
        return None
    if isinstance(new.target, backups.RestoreLatest):
        # The end of the archive is the state the instance already has
        raise RestoreSourceError(
            reason="target has to be a time or the state before a rollback to "
            "roll the data back in place"
        )

    revisions = [-1 if old is None else old.revision]
    if rollback_revision is not None:
        revisions.append(rollback_revision)
    if new.revision <= max(revisions):
        raise RestoreSourceError(
            reason=f"revision must be greater than {max(revisions)} "
            "to roll the data back in place"
        )
    target = new.target
    if isinstance(target, backups.RestoreBeforeRevision) and target.revision > max(
        revisions
    ):
        raise RestoreSourceError(
            reason=f"target.revision must not be greater than {max(revisions)}"
        )
    if new.stanza != instance_uuid:
        raise RestoreSourceError(
            reason="an instance can be rolled back in place only to its own "
            "backups, create a new instance to restore another one's"
        )
    if (
        backup_repository is None
        or source_repository is None
        or backup_repository.storage.location() != source_repository.storage.location()
    ):
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


class PGBackupRepository(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """A pgBackRest repository instances back up to and restore from.

    Instances keep their backups in it under their own stanzas, so it may be
    shared. It outlives the instances: backups of a deleted one are restored
    from it.
    """

    __tablename__ = "postgres_backup_repositories"

    name = properties.property(
        types.String(min_length=1, max_length=255), required=True
    )
    storage = properties.property(backups.STORAGE_TYPE, required=True)
    # pgBackRest encrypts the whole repository with it when set. Losing it
    # makes the backups unrecoverable.
    encryption_key = properties.property(
        types.AllowNone(backups.OptionValueType()),
        default=None,
    )

    def _check_endpoint(self) -> None:
        try:
            backups.check_endpoint_resolved(self.storage.endpoint)
        except ValueError as e:
            raise EndpointError(reason=str(e))

    def pgbackrest_options(self) -> dict[str, str]:
        options = self.storage.repo_options()
        if self.encryption_key is not None:
            options["repo1-cipher-type"] = "aes-256-cbc"
            options["repo1-cipher-pass"] = self.encryption_key
        return options

    def get_instances(self, session: tp.Any = None) -> list["PGInstance"]:
        """Return the instances backing up to or restoring from it."""
        expression = (
            "SELECT uuid FROM postgres_instances "
            "WHERE backup->>'repository' = %s OR restore_from->>'repository' = %s;"
        )
        engine = engines.engine_factory.get_engine()
        with engine.session_manager(session=session) as s:
            rows = s.execute(expression, (str(self.uuid), str(self.uuid))).fetchall()
        if not rows:
            return []
        return PGInstance.objects.get_all(
            session=session,
            filters={"uuid": dm_filters.In([str(r["uuid"]) for r in rows])},
        )

    def same_place(self, session: tp.Any = None) -> list["PGBackupRepository"]:
        """Return the repositories of the project for the same place."""
        return [
            repository
            for repository in PGBackupRepository.objects.get_all(
                session=session,
                filters={"project_id": dm_filters.EQ(self.project_id)},
            )
            if repository.storage.location() == self.storage.location()
        ]

    def insert(self, session: tp.Any = None) -> None:
        self._check_endpoint()
        super().insert(session=session)

    def update(self, session: tp.Any = None, force: bool = False) -> None:
        storage = self.properties["storage"]
        if storage.is_dirty():
            self._check_endpoint()
        if storage.is_dirty() and (
            storage.old_value.location() != self.storage.location()
        ):
            raise RepositoryUpdateError(
                reason="the storage of a repository can't be moved, only its "
                "credentials changed: create another repository"
            )
        if self.properties["encryption_key"].is_dirty():
            raise RepositoryUpdateError(
                reason="encryption_key can't be changed: the repository is "
                "encrypted with it"
            )
        super().update(session=session, force=force)
        # The credentials reach the nodes with the instances
        for instance in self.get_instances(session=session):
            instance.update(session=session, force=True)

    def delete(self, session: tp.Any = None, **kwargs: tp.Any) -> tp.Any:
        engine = engines.engine_factory.get_engine()
        with engine.session_manager(session=session) as s:
            # An instance starting to use it waits for the lock, see
            # PGInstance._check_repositories
            get_repository(self.uuid, session=s, locked=True)
            instances = self.get_instances(session=s)
            if instances:
                raise RepositoryInUseError(
                    repository=self.uuid,
                    instances=", ".join(str(i.uuid) for i in instances),
                )
            # The backups known in it are forgotten along with it by the
            # database, the storage is left as it is
            return super().delete(session=s, **kwargs)


def get_repository(
    uuid_: uuid.UUID,
    project_id: uuid.UUID | None = None,
    session: tp.Any = None,
    locked: bool = False,
) -> PGBackupRepository | None:
    filters = {"uuid": dm_filters.EQ(uuid_)}
    if project_id is not None:
        filters["project_id"] = dm_filters.EQ(project_id)
    return PGBackupRepository.objects.get_one_or_none(
        filters=filters, session=session, locked=locked
    )


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
    # the nodes get is rendered from `restore_from` and its repository, see
    # exordos_db.common.rollback.
    rollback_revision = properties.property(
        types.AllowNone(types.Integer(min_value=0, max_value=2**31 - 1)),
        default=None,
    )
    # {"revision": ..., "phase": ..., "error": ...} of the restore or the
    # rollback in progress as the nodes report it, None when there is none.
    # "revision" is None for the restore of a new instance.
    restore_status = properties.property(types.AllowNone(types.Dict()), default=None)
    # {"error", "last_backup_at", "last_archived_at", "last_archive_failed_at",
    # "restore_window"} of the backups as the primary last reported them, None
    # without backup. "restore_window" is the {"earliest", "latest"} moments
    # they can recover to, None when none is known to be restorable.
    backup_status = properties.property(types.AllowNone(types.Dict()), default=None)

    def repository_uuids(self) -> set[uuid.UUID]:
        return {
            ref.repository
            for ref in (self.backup, self.restore_from)
            if ref is not None
        }

    def get_backup_repository(
        self, session: tp.Any = None
    ) -> PGBackupRepository | None:
        if self.backup is None:
            return None
        return get_repository(self.backup.repository, session=session)

    def get_source_repository(
        self, session: tp.Any = None
    ) -> PGBackupRepository | None:
        if self.restore_from is None:
            return None
        return get_repository(self.restore_from.repository, session=session)

    def _check_repositories(self, session: tp.Any = None) -> None:
        # Only the repositories of the instance's project may be used. They
        # are locked until the instance is saved in the session, so a
        # repository isn't deleted meanwhile.
        for field in ("backup", "restore_from"):
            ref = getattr(self, field)
            if ref is not None and (
                get_repository(
                    ref.repository, self.project_id, session=session, locked=True
                )
                is None
            ):
                raise RepositoryNotFoundError(field=field, repository=ref.repository)

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

    def _check_restore_target(self, session: tp.Any = None) -> None:
        if self.restore_from is None or not isinstance(
            self.restore_from.target, backups.RestoreTime
        ):
            return
        repository = self.get_source_repository(session=session)
        if repository is None:
            return
        places = [r.uuid for r in repository.same_place(session=session)]
        known = PGBackup.objects.get_all(
            session=session,
            filters={
                "repository": dm_filters.In(places),
                "stanza": dm_filters.EQ(str(self.restore_from.stanza)),
            },
        )
        check_restore_target(self.restore_from, known, CATALOG_MAX_BACKUPS)

    def insert(self, session: tp.Any = None) -> None:
        engine = engines.engine_factory.get_engine()
        with engine.session_manager(session=session) as s:
            self._check_repositories(session=s)
            self._check_restore_target(session=s)
            super().insert(session=s)

    def update(self, session=None, force=False):
        engine = engines.engine_factory.get_engine()
        with engine.session_manager(session=session) as s:
            self._update(session=s, force=force)

    def _update(self, session: tp.Any, force: bool) -> None:
        self._validate_update(session=session)
        if (
            self.properties["backup"].is_dirty()
            or self.properties["restore_from"].is_dirty()
        ):
            self._check_repositories(session=session)
        restore_from = self.properties["restore_from"]
        if restore_from.is_dirty() and (
            restore_from.old_value is None
            or self.restore_from is None
            or restore_from.old_value.identity() != self.restore_from.identity()
        ):
            # Another repository object for the same source restores nothing
            self._check_restore_target(session=session)

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
                self.get_backup_repository(session=session),
                self.get_source_repository(session=session),
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


class PGBackupType(str, enum.Enum):
    FULL = "full"
    DIFF = "diff"
    INCR = "incr"


class PGBackup(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """A backup in a repository as the nodes of its instance last saw it.

    Kept up to date while the instance backs up to the repository, and kept
    as it was once the instance is deleted or backs up elsewhere.
    """

    __tablename__ = "postgres_backups"

    repository = relationships.relationship(
        PGBackupRepository, required=True, read_only=True
    )
    # None once the instance is deleted
    instance = relationships.relationship(PGInstance, default=None)
    stanza = properties.property(types.String(max_length=64), required=True)
    label = properties.property(types.String(max_length=64), required=True)
    type = properties.property(
        types.Enum([t.value for t in PGBackupType]), required=True
    )
    # The copy of the data kept before the rollback with this revision, None
    # for a scheduled backup
    before_revision = properties.property(
        types.AllowNone(types.Integer(min_value=0)), default=None
    )
    started_at = properties.property(types.UTCDateTimeZ(), required=True)
    finished_at = properties.property(types.UTCDateTimeZ(), required=True)
    # Of the data backed up and of what the repository keeps, bytes
    size = properties.property(types.Integer(min_value=0), default=0)
    stored_size = properties.property(types.Integer(min_value=0), default=0)
    # Whether a recovery can start from it: a backup on a timeline abandoned
    # by a rollback can't
    restorable = properties.property(types.Boolean(), default=True)
    # pgBackRest found errors, e.g. page checksums, in the backed up files
    error = properties.property(types.Boolean(), default=False)


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
