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
"""Backups and point-in-time restores on a live cluster.

The tests are one scenario on one source instance and run in order; a test
depending on an earlier one is skipped when that one failed.
"""

import datetime
import time
import uuid as sys_uuid

import pytest

from exordos_db.tests.functional import conftest as fc

pytestmark = pytest.mark.skipif(
    not (fc.S3_ENDPOINT and fc.S3_ACCESS_KEY and fc.S3_SECRET_KEY),
    reason="PITR_S3_ENDPOINT, PITR_S3_ACCESS_KEY and PITR_S3_SECRET_KEY are not set",
)

APP_PASSWORD = "app-user-pass-functional"
APP_NEW_PASSWORD = "app-user-new-pass-functional"
OLD_PASSWORD = "old-user-pass-functional"


@pytest.fixture(scope="module")
def scenario():
    # Every run gets its own place and key, the bucket may be shared
    run = sys_uuid.uuid4().hex[:6]
    storage = fc.s3_storage(
        path=f"/functional-{run}", encryption_key=f"functional-{sys_uuid.uuid4()}"
    )
    return {"storage": storage}


def _require(scenario, *keys):
    missing = [k for k in keys if k not in scenario]
    if missing:
        pytest.skip(f"an earlier step didn't provide {missing}")


def _source(scenario, target, stanza=None):
    return {
        **scenario["storage"],
        "stanza": stanza or scenario["source"].uuid,
        "target": target,
    }


def _at(moment):
    return {"kind": "time", "time": moment}


def _append(cluster, note, count):
    cluster.sql(
        "set role app_user; create table if not exists orders "
        "(id serial primary key, note text); "
        f"insert into orders (note) select '{note}' from generate_series(1, {count})",
        "appdb",
    )


def _backup_status(cluster, status):
    """Wait for the primary to report the storage usable or not."""

    def reported():
        policy = cluster.backup_policy()
        return policy is not None and policy["status"] == status and policy

    return fc.wait_for(reported, f"backup policy of {cluster.uuid} to be {status}")


def test_backups_are_taken(scenario, instances):
    # No backup covers this moment: the timer takes a full as soon as the
    # stanza is ready, which can be before the instance is ACTIVE
    before = fc.utc_now() - datetime.timedelta(minutes=1)
    scenario["before_backups"] = before.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    source = instances("pitr-src", nodes=2)
    source.create_backup_policy(
        scenario["storage"], incr_interval_hours=1, retention_full=2
    )
    app = source.create_user("app_user", APP_PASSWORD)
    source.create_database("appdb", app)
    old = source.create_user("old_user", OLD_PASSWORD)
    source.create_database("olddb", old)

    source.wait_status("ACTIVE")
    source.wait_ready(["appdb", "olddb"])

    source.backup_now()

    backups = source.backups()
    assert [b["type"] for b in backups if not b.get("error")] == ["full"]
    assert source.sql("select failed_count from pg_stat_archiver") == "0"
    # The API leaves out null fields
    assert _backup_status(source, "ACTIVE").get("error") is None
    scenario["source"] = source


def test_one_backup_policy_per_instance(scenario):
    _require(scenario, "source")
    source = scenario["source"]
    body = {
        "name": "second",
        "project_id": fc.PROJECT_ID,
        "instance": source.path,
        "storage": scenario["storage"],
    }

    response = source.api.call("POST", f"{source.path}/backup_policies/", body)

    assert response.status_code == 409, response.text


def test_points_in_time(scenario):
    _require(scenario, "source")
    source = scenario["source"]

    _append(source, "first", 10)
    time.sleep(2)
    scenario["t0"] = source.now()
    time.sleep(2)
    _append(source, "second", 90)
    time.sleep(2)
    scenario["t1"] = source.now()
    time.sleep(2)

    # Everything after T1 is to be left out of a restore to it
    source.sql(
        "set role app_user; delete from orders; create table junk (x int)", "appdb"
    )
    _append(source, "late", 5)
    users = source.users()
    olddb = source.databases()["olddb"][0]
    source.api.call("DELETE", f"{source.path}/databases/{olddb}", expect=204)
    source.api.call("DELETE", f"{source.path}/users/{users['old_user']}", expect=204)
    late = source.create_user("late_user", "late-user-pass-functional")
    source.create_database("latedb", late)
    source.api.call(
        "PUT",
        f"{source.path}/users/{users['app_user']}",
        {"password": APP_NEW_PASSWORD},
        expect=200,
    )
    fc.wait_for(
        lambda: (
            source.can_login("app_user", APP_NEW_PASSWORD, "appdb")
            and "latedb" in source.sql("select datname from pg_database").splitlines()
            and "olddb"
            not in source.sql("select datname from pg_database").splitlines()
        ),
        "the role changes on the cluster",
    )
    source.archive_now()
    scenario["points"] = True


def test_clone_at_a_target_time(scenario, clones):
    _require(scenario, "points")
    clone = clones("pitr-clone", restore_from=_source(scenario, _at(scenario["t1"])))

    clone.wait_status("ACTIVE")

    assert clone.rows() == {"first": 10, "second": 90}
    assert not clone.table_exists("junk")
    assert set(clone.users()) == {"app_user", "old_user"}
    assert {n: o for n, (_, o) in clone.databases().items()} == {
        "appdb": "app_user",
        "olddb": "old_user",
    }
    # The roles keep their password hashes of the target time
    assert clone.can_login("app_user", APP_PASSWORD, "appdb")
    assert clone.can_login("old_user", OLD_PASSWORD, "olddb")
    assert clone.instance().get("restore_status") is None
    # A restored instance doesn't back up unless told to
    assert clone.backup_policy() is None


def test_clone_to_the_end_of_the_archive(scenario, clones):
    _require(scenario, "points")
    clone = clones("pitr-latest", restore_from=_source(scenario, {"kind": "latest"}))

    clone.wait_status("ACTIVE")

    assert clone.rows() == {"late": 5}
    assert clone.table_exists("junk")
    assert set(clone.users()) == {"app_user", "late_user"}
    # Imported users have no password, their hash is kept
    assert clone.can_login("app_user", APP_NEW_PASSWORD, "appdb")
    assert clone.instance().get("restore_status") is None


def test_target_in_the_future_is_rejected(scenario, api, pg_version):
    _require(scenario, "source")
    future = fc.utc_now() + datetime.timedelta(days=1)
    body = {
        "name": f"pitr-future-{sys_uuid.uuid4().hex[:6]}",
        "project_id": fc.PROJECT_ID,
        "cpu": 1,
        "ram": 2048,
        "disk_size": 15,
        "nodes_number": 1,
        "sync_replica_number": 0,
        "version": f"/v1/types/postgres/versions/{pg_version}",
        "restore_from": _source(scenario, _at(future.strftime("%Y-%m-%dT%H:%M:%SZ"))),
    }

    response = api.call("POST", fc.INSTANCES, body)

    assert response.status_code == 400, response.text


def test_clone_without_a_backup_fails(scenario, clones):
    # Not known to the control plane: the data plane finds out
    _require(scenario, "points")
    clone = clones(
        "pitr-nobackup",
        restore_from=_source(scenario, _at(scenario["before_backups"])),
    )

    clone.wait_status("ERROR")

    status = clone.instance()["restore_status"]
    assert status["phase"] == "failed", status
    assert status["error"], status


def test_storage_errors_dont_hold_up_the_roles(scenario):
    _require(scenario, "points")
    source = scenario["source"]
    broken = {
        **scenario["storage"],
        "secret_key": "wrong-secret-key",
        "path": "/functional-broken",
    }

    policy = f"{source.path}/backup_policies/{source.backup_policy()['uuid']}"
    source.api.call("PUT", policy, {"storage": broken}, expect=200)
    user = source.create_user("during_error", "during-error-pass-functional")

    fc.wait_for(
        lambda: source.can_login(
            "during_error", "during-error-pass-functional", "postgres"
        ),
        "the user to be created while the storage fails",
    )
    assert _backup_status(source, "ERROR")["error"]
    source.api.call("DELETE", f"{source.path}/users/{user}", expect=204)
    scenario["broken"] = True


def test_backups_are_turned_off(scenario):
    _require(scenario, "broken")
    source = scenario["source"]

    policy = f"{source.path}/backup_policies/{source.backup_policy()['uuid']}"
    source.api.call("DELETE", policy, expect=204)

    fc.wait_for(
        lambda: (
            "pgbackrest"
            not in source.dcs()["postgresql"]["parameters"].get("archive_command", "")
        ),
        "archiving to stop",
    )
    scenario["off"] = True


def test_clone_of_a_deleted_instance(scenario, api, clones):
    # The backups are found by the storage and the stanza
    _require(scenario, "off")
    source = scenario["source"]
    # The policy goes with the instance
    source.create_backup_policy(scenario["storage"])
    api.call("DELETE", source.path, expect=204)
    fc.wait_for(
        lambda: api.call("GET", source.path).status_code == 404,
        f"{source.uuid} to be deleted",
    )

    clone = clones(
        "pitr-orphan",
        restore_from=_source(scenario, _at(scenario["t0"]), stanza=source.uuid),
    )

    clone.wait_status("ACTIVE")
    assert clone.rows() == {"first": 10}
