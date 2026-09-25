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

import datetime
import re
import typing as tp

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from exordos_db.common import endpoints


class HttpEndpointType(types.BaseCompiledRegExpTypeFromAttr):
    # Scheme and authority only, pgBackRest takes no path in the endpoint
    pattern = re.compile(r"^https?://[^\s/]+/?$")


class OptionValueType(types.BaseCompiledRegExpTypeFromAttr):
    # Values become lines of pgbackrest.conf on the data plane, so no
    # whitespace (and no line breaks in particular) is allowed
    pattern = re.compile(r"^\S{1,1024}$")


class BucketNameType(types.BaseCompiledRegExpTypeFromAttr):
    pattern = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class RepoPathType(types.BaseCompiledRegExpTypeFromAttr):
    # No ".." segments
    pattern = re.compile(r"^(?!.*/\.\.(/|$))/[A-Za-z0-9_./-]{0,1023}$")


def check_endpoint_host(endpoint: str) -> None:
    """Reject endpoints on the nodes themselves or the metadata service.

    A host name is checked by `check_endpoint_resolved`, since this check
    runs on every read of the storage as well.
    """
    endpoints.check_host(endpoint)


def check_endpoint_resolved(endpoint: str) -> None:
    """Reject a host name resolving to an address `check_endpoint_host` rejects.

    The name is resolved on the control plane when the repository is saved.
    One the control plane can't resolve is left to the nodes, whose resolver
    may differ; the node checks the addresses it gets before it connects.
    """
    host = endpoints.host_of(endpoint)
    if endpoints.literal_address(host) is not None:
        return
    for address in endpoints.resolved_addresses(host) or []:
        endpoints.check_address(host, address)


class S3Storage(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """Where a pgBackRest repository is in S3 and how to reach it."""

    KIND = "s3"

    endpoint = properties.property(HttpEndpointType(), required=True)
    bucket = properties.property(BucketNameType(), required=True)
    region = properties.property(OptionValueType(), default="us-east-1")
    access_key = properties.property(OptionValueType(), required=True)
    secret_key = properties.property(OptionValueType(), required=True)
    uri_style = properties.property(types.Enum(("path", "host")), default="path")
    verify_tls = properties.property(types.Boolean(), default=True)
    path = properties.property(RepoPathType(), default="/exordos_db")

    def validate(self) -> None:
        check_endpoint_host(self.endpoint)

    def location(self) -> tuple[str, ...]:
        """Where the repository is, regardless of the credentials to it."""
        return (self.KIND, self.endpoint.rstrip("/"), self.bucket, self.path)

    def repo_options(self) -> dict[str, str]:
        """Return pgBackRest `repo1-*` options to reach the repository."""
        return {
            "repo1-type": "s3",
            "repo1-s3-endpoint": self.endpoint.rstrip("/"),
            "repo1-s3-bucket": self.bucket,
            "repo1-s3-region": self.region,
            "repo1-s3-key": self.access_key,
            "repo1-s3-key-secret": self.secret_key,
            "repo1-s3-uri-style": self.uri_style,
            "repo1-storage-verify-tls": "y" if self.verify_tls else "n",
            "repo1-path": self.path,
        }


STORAGE_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(S3Storage),
)


class RetentionDays(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """Keep what recovers to any moment of the last days."""

    KIND = "days"

    days = properties.property(types.Integer(min_value=1, max_value=3650), default=7)

    def repo_options(self) -> dict[str, str]:
        # pgBackRest keeps the newest full backup older than that too, so
        # the whole period stays recoverable
        return {
            "repo1-retention-full-type": "time",
            "repo1-retention-full": str(self.days),
        }


class RetentionFullBackups(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """Keep a number of full backups with their incremental ones."""

    KIND = "full_backups"

    count = properties.property(types.Integer(min_value=1, max_value=365), default=2)

    def repo_options(self) -> dict[str, str]:
        return {
            "repo1-retention-full-type": "count",
            "repo1-retention-full": str(self.count),
        }


RETENTION_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(RetentionDays),
    types_dynamic.KindModelType(RetentionFullBackups),
)


class RepositoryBackup(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """Backups of an instance to a repository."""

    KIND = "repository"

    repository = properties.property(types.UUID(), required=True)
    full_interval_hours = properties.property(
        types.Integer(min_value=1, max_value=8784),
        default=168,
    )
    incr_interval_hours = properties.property(
        types.Integer(min_value=1, max_value=8784),
        default=24,
    )
    retention = properties.property(RETENTION_TYPE, default=RetentionDays)


class RestoreLatest(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """Replay the whole archive, i.e. recover to the end of it."""

    KIND = "latest"


class RestoreTime(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """Replay the archive up to a moment."""

    KIND = "time"

    time = properties.property(types.UTCDateTimeZ(), required=True)

    def validate(self) -> None:
        # A moment that was in the past when it was saved stays in the past,
        # so this holds when the row is read back as well
        if self.time > datetime.datetime.now(datetime.timezone.utc):
            raise ValueError("target.time is in the future")


class RestoreBeforeRevision(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """The state kept before rollback N."""

    KIND = "before_revision"

    revision = properties.property(
        types.Integer(min_value=0, max_value=2**31 - 1),
        required=True,
    )


RESTORE_TARGET_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(RestoreLatest),
    types_dynamic.KindModelType(RestoreTime),
    types_dynamic.KindModelType(RestoreBeforeRevision),
)


class RepositoryRestoreSource(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """Backups of an instance, maybe a deleted one, in a repository."""

    KIND = "repository"

    repository = properties.property(types.UUID(), required=True)
    # Stanza of the backed up instance, i.e. its uuid. The instance itself
    # may be gone already.
    stanza = properties.property(types.UUID(), required=True)
    # What the recovery stops at
    target = properties.property(RESTORE_TARGET_TYPE, default=RestoreLatest)
    # Setting a source with a higher revision on an existing instance rolls
    # its data back in place. Nothing else about the source can change
    # without it, so a rollback is never the side effect of an edit.
    revision = properties.property(
        types.Integer(min_value=0, max_value=2**31 - 1),
        default=0,
    )

    def target_spec(self) -> dict[str, tp.Any]:
        """What to restore, without the repository options."""
        target_time = None
        before_revision = None
        if isinstance(self.target, RestoreTime):
            target_time = self.target.time.astimezone(datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S.%f+00"
            )
        elif isinstance(self.target, RestoreBeforeRevision):
            before_revision = self.target.revision
        return {
            "stanza": str(self.stanza),
            "target_time": target_time,
            "before_revision": before_revision,
        }

    def identity(self) -> tuple[str, str | None, int | None, int]:
        """What makes two sources restore the same data."""
        spec = self.target_spec()
        return (
            spec["stanza"],
            spec["target_time"],
            spec["before_revision"],
            self.revision,
        )


BACKUP_TYPE = types.AllowNone(
    types_dynamic.KindModelSelectorType(
        types_dynamic.KindModelType(RepositoryBackup),
    )
)

RESTORE_SOURCE_TYPE = types.AllowNone(
    types_dynamic.KindModelSelectorType(
        types_dynamic.KindModelType(RepositoryRestoreSource),
    )
)
