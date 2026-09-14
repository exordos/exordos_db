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

from exordos_db.paas.services import roles


def _found(users, databases):
    return {
        "users": {name: {"pw_hash": f"hash-{name}"} for name in users},
        "databases": {name: {"owner": owner} for name, owner in databases.items()},
    }


def test_restored_cluster_without_rows():
    plan = roles.plan_roles(
        set(), {}, _found(["app", "report"], {"appdb": "app", "stats": "report"})
    )

    assert plan.create_users == {"app": "hash-app", "report": "hash-report"}
    assert plan.create_databases == {"appdb": "app", "stats": "report"}
    assert plan.delete_users == plan.delete_databases == []
    assert plan.change_owners == {}


def test_rollback_keeps_matching_rows():
    # Rows that existed at the target time keep their uuid and password:
    # nothing to do with them
    plan = roles.plan_roles(
        {"app"}, {"appdb": "app"}, _found(["app"], {"appdb": "app"})
    )

    assert plan == roles.RolesPlan()


def test_rollback_drops_what_was_created_later():
    plan = roles.plan_roles(
        {"app", "late_user"},
        {"appdb": "app", "late_db": "late_user"},
        _found(["app"], {"appdb": "app"}),
    )

    assert plan.delete_databases == ["late_db"]
    assert plan.delete_users == ["late_user"]
    assert plan.create_users == plan.create_databases == {}


def test_rollback_brings_back_what_was_dropped_later():
    plan = roles.plan_roles(
        {"app"}, {}, _found(["app", "old_user"], {"appdb": "app", "old_db": "old_user"})
    )

    assert plan.create_users == {"old_user": "hash-old_user"}
    assert plan.create_databases == {"appdb": "app", "old_db": "old_user"}


def test_owner_goes_back():
    plan = roles.plan_roles(
        {"app", "new_owner"},
        {"appdb": "new_owner"},
        _found(["app", "new_owner"], {"appdb": "app"}),
    )

    assert plan.change_owners == {"appdb": "app"}
    assert plan.delete_databases == []


def test_owner_created_later_is_replaced_before_it_is_deleted():
    # The database existed at the target time with another owner, the owner
    # it has now didn't exist then
    plan = roles.plan_roles(
        {"app", "late_owner"},
        {"appdb": "late_owner"},
        _found(["app"], {"appdb": "app"}),
    )

    assert plan.change_owners == {"appdb": "app"}
    assert plan.delete_users == ["late_owner"]


def test_unmanageable_roles():
    found = {
        "users": {"app": {"pw_hash": "h"}, "nopass": {"pw_hash": None}},
        "databases": {
            "appdb": {"owner": "app"},
            "sysdb": {"owner": "postgres"},
            "nopassdb": {"owner": "nopass"},
        },
    }

    plan = roles.plan_roles({"app"}, {"sysdb": "app"}, found)

    assert plan.unmanageable_users == ["nopass"]
    assert plan.unmanageable_databases == ["nopassdb", "sysdb"]
    # A row whose database is owned by an unmanaged role can't stay
    assert plan.delete_databases == ["sysdb"]
    assert plan.create_databases == {"appdb": "app"}
