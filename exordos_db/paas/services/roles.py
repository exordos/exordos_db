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
"""Bring users and databases of the control plane to a restored cluster.

The roles are part of the data a backup restores: the ones that existed at the
target time come back, the ones created later are gone. The control plane
matches its rows to the roles found on the data plane by name, so a row that
survives keeps its uuid (manifests refer to it) and its password: a password
is a credential clients and secrets already use, not data to roll back.
"""

from __future__ import annotations

import dataclasses
import typing as tp


@dataclasses.dataclass
class RolesPlan:
    # In the order to apply: a database refers to its owner
    delete_databases: list[str] = dataclasses.field(default_factory=list)
    # name -> password hash found on the data plane
    create_users: dict[str, str] = dataclasses.field(default_factory=dict)
    # name -> owner
    create_databases: dict[str, str] = dataclasses.field(default_factory=dict)
    change_owners: dict[str, str] = dataclasses.field(default_factory=dict)
    delete_users: list[str] = dataclasses.field(default_factory=list)
    # Found, but can't be managed: dropped by the agent afterwards
    unmanageable_users: list[str] = dataclasses.field(default_factory=list)
    unmanageable_databases: list[str] = dataclasses.field(default_factory=list)


def plan_roles(
    users: tp.Collection[str],
    databases: dict[str, str],
    found_roles: dict[str, tp.Any],
) -> RolesPlan:
    """Plan the changes of the rows to match the found roles.

    `users` are the names of the user rows, `databases` maps database rows to
    their owners, `found_roles` is what the agent reported.
    """
    found_users = found_roles["users"]
    found_databases = found_roles["databases"]
    plan = RolesPlan()

    managed = set()
    for name, user in found_users.items():
        if name in users:
            managed.add(name)
        elif user.get("pw_hash"):
            plan.create_users[name] = user["pw_hash"]
            managed.add(name)
        else:
            # A row can't be created without a password
            plan.unmanageable_users.append(name)

    for name, database in found_databases.items():
        owner = database["owner"]
        if owner not in managed:
            # E.g. owned by postgres
            plan.unmanageable_databases.append(name)
            if name in databases:
                plan.delete_databases.append(name)
        elif name not in databases:
            plan.create_databases[name] = owner
        elif databases[name] != owner:
            plan.change_owners[name] = owner

    plan.delete_databases += [n for n in databases if n not in found_databases]
    plan.delete_users = [n for n in users if n not in found_users]

    for field in dataclasses.fields(plan):
        value = getattr(plan, field.name)
        if isinstance(value, list):
            value.sort()
    return plan
