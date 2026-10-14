#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from typing import Any

from gcl_sdk.audit.dm import models as audit_models


_SECRET_FIELDS = frozenset(
    {
        "password",
        "password_hash",
        "patroni_password",
        "access_key",
        "secret_key",
        "encryption_key",
    }
)


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_secrets(item)
            for key, item in value.items()
            if key not in _SECRET_FIELDS
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


class AuditLogSQLStorableMixin(audit_models.AuditLogSQLStorableMixin):
    """Record stored DBaaS properties without provisioning credentials."""

    __audit_service_name__ = "dbaas"

    def dump_to_audit_snapshot(self) -> dict[str, Any]:
        return _without_secrets(
            {
                name: prop.get_property_type().to_simple_type(getattr(self, name))
                for name, prop in self.properties.properties.items()
            }
        )
