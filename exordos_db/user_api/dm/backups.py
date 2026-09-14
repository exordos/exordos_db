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
    pattern = re.compile(r"^/[A-Za-z0-9_./-]{0,1023}$")


class S3Storage(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    """A pgBackRest repository in S3."""

    endpoint = properties.property(HttpEndpointType(), required=True)
    bucket = properties.property(BucketNameType(), required=True)
    region = properties.property(OptionValueType(), default="us-east-1")
    access_key = properties.property(OptionValueType(), required=True)
    secret_key = properties.property(OptionValueType(), required=True)
    uri_style = properties.property(types.Enum(("path", "host")), default="path")
    verify_tls = properties.property(types.Boolean(), default=True)
    path = properties.property(RepoPathType(), default="/exordos_db")
    # The repository is encrypted by pgBackRest when set. Losing the key
    # makes the backups unrecoverable.
    encryption_key = properties.property(
        types.AllowNone(OptionValueType()),
        default=None,
    )

    def storage_repo_options(self) -> dict[str, str]:
        """Return pgBackRest `repo1-*` options to reach the repository."""
        options = {
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
        if self.encryption_key is not None:
            options["repo1-cipher-type"] = "aes-256-cbc"
            options["repo1-cipher-pass"] = self.encryption_key
        return options


class S3Backup(S3Storage):
    KIND = "s3"

    full_interval_hours = properties.property(
        types.Integer(min_value=1, max_value=8784),
        default=168,
    )
    incr_interval_hours = properties.property(
        types.Integer(min_value=1, max_value=8784),
        default=24,
    )
    retention_full = properties.property(
        types.Integer(min_value=1, max_value=365),
        default=2,
    )

    def pgbackrest_repo_options(self) -> dict[str, str]:
        return {
            **self.storage_repo_options(),
            "repo1-retention-full": str(self.retention_full),
        }


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


RESTORE_TARGET_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(RestoreLatest),
    types_dynamic.KindModelType(RestoreTime),
)


class S3RestoreSource(S3Storage):
    KIND = "s3"

    # Stanza of the backed up instance, i.e. its uuid. The instance itself
    # may be gone already.
    stanza = properties.property(types.UUID(), required=True)
    # What the recovery stops at
    target = properties.property(RESTORE_TARGET_TYPE, default=RestoreLatest)

    def restore_spec(self) -> dict[str, tp.Any]:
        target_time = None
        if isinstance(self.target, RestoreTime):
            target_time = self.target.time.astimezone(datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S.%f+00"
            )
        return {
            "stanza": str(self.stanza),
            "options": self.storage_repo_options(),
            "target_time": target_time,
        }


BACKUP_TYPE = types.AllowNone(
    types_dynamic.KindModelSelectorType(
        types_dynamic.KindModelType(S3Backup),
    )
)

RESTORE_SOURCE_TYPE = types.AllowNone(
    types_dynamic.KindModelSelectorType(
        types_dynamic.KindModelType(S3RestoreSource),
    )
)
