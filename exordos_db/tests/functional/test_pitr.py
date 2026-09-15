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
"""Backups, point-in-time restores and in-place rollbacks on a live cluster.

The tests are one scenario on one source instance and run in order; a test
depending on an earlier one is skipped when that one failed.
"""

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
    return {}


def _require(scenario, *keys):
    missing = [k for k in keys if k not in scenario]
    if missing:
        pytest.skip(f"an earlier step didn't provide {missing}")


def _source(scenario, s3_storage, **fields):
    return {**s3_storage, "stanza": scenario["source"].uuid, **fields}


def _at(moment):
    return {"kind": "time", "time": moment}


def _before(revision):
    return {"kind": "before_revision", "revision": revision}


def _append(cluster, note, count):
    cluster.sql(
        "set role app_user; create table if not exists orders "
        "(id serial primary key, note text); "
        f"insert into orders (note) select '{note}' from generate_series(1, {count})",
        "appdb",
    )


def test_backups_are_taken(scenario, instances, s3_storage):
    source = instances(
        "pitr-src",
        nodes=2,
        backup={**s3_storage, "incr_interval_hours": 1},
    )
    app = source.create_user("app_user", APP_PASSWORD)
    scenario["appdb"] = source.create_database("appdb", app)
    old = source.create_user("old_user", OLD_PASSWORD)
    source.create_database("olddb", old)
    scenario["app_user"] = app

    source.wait_status("ACTIVE")
    source.wait_ready(["appdb", "olddb"])
    # No backup covers this moment
    scenario["before_backups"] = source.now()
    time.sleep(2)

    source.backup_now()

    backups = source.backups()
    assert [b["type"] for b in backups if not b.get("error")] == ["full"]
    assert source.sql("select failed_count from pg_stat_archiver") == "0"
    scenario["source"] = source


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

    # Everything after T1 is to be undone
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


def test_clone_at_a_target_time(scenario, instances, s3_storage):
    _require(scenario, "points")
    clone = instances(
        "pitr-clone",
        restore_from=_source(scenario, s3_storage, target=_at(scenario["t1"])),
    )

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


def test_clone_to_the_end_of_the_archive(scenario, instances, s3_storage):
    _require(scenario, "points")
    clone = instances(
        "pitr-latest",
        restore_from=_source(scenario, s3_storage, target={"kind": "latest"}),
    )

    clone.wait_status("ACTIVE")

    assert clone.rows() == {"late": 5}
    assert clone.table_exists("junk")
    assert set(clone.users()) == {"app_user", "late_user"}
    assert clone.instance().get("restore_status") is None


def test_clone_without_a_backup_fails(scenario, instances, s3_storage):
    _require(scenario, "points")
    clone = instances(
        "pitr-nobackup",
        restore_from=_source(
            scenario, s3_storage, target=_at(scenario["before_backups"])
        ),
    )

    clone.wait_status("ERROR")

    status = clone.instance().get("restore_status")
    assert status["revision"] is None
    assert "No backup" in status["error"], status


@pytest.mark.parametrize(
    "change",
    [
        # Another instance's backups have another system identifier
        {"stanza": str(sys_uuid.uuid4())},
        # The end of the archive is the state the instance already has
        {"target": {"kind": "latest"}},
        {"target": {"kind": "time", "time": "2999-01-01T00:00:00Z"}},
        # Backups go elsewhere: the latest WAL isn't there
        {"path": "/functional-elsewhere"},
    ],
)
def test_invalid_rollback_is_rejected(scenario, s3_storage, change):
    _require(scenario, "points")
    source = scenario["source"]
    restore_from = _source(scenario, s3_storage, target=_at(scenario["t1"]), revision=1)

    response = source.api.call(
        "PUT", source.path, {"restore_from": {**restore_from, **change}}
    )

    assert response.status_code == 400, response.text


def test_rollback_in_place(scenario, s3_storage):
    _require(scenario, "points")
    source = scenario["source"]
    timelines = {ip: source.timeline(ip) for ip in source.ips()}
    body = {
        "restore_from": _source(
            scenario, s3_storage, target=_at(scenario["t1"]), revision=1
        )
    }

    source.api.call("PUT", source.path, body, expect=200)
    # The rolled back cluster has no role for a user created meanwhile
    response = source.api.call(
        "POST",
        f"{source.path}/users/",
        {
            "name": "during_rollback",
            "password": "during-rollback-pass-functional",
            "project_id": fc.PROJECT_ID,
            "instance": source.path,
        },
    )
    assert response.status_code == 409, response.text
    source.wait_rollback(1)

    source.wait_rows({"first": 10, "second": 90})
    for ip in source.ips():
        assert source.timeline(ip) > timelines[ip]
    assert not source.table_exists("junk")
    # The rows that existed at T1 keep their uuids and the current password
    users = source.users()
    assert set(users) == {"app_user", "old_user"}
    assert users["app_user"] == scenario["app_user"]
    databases = source.databases()
    assert databases["appdb"][0] == scenario["appdb"]
    assert databases["olddb"][1] == "old_user"
    assert "latedb" not in databases
    assert fc.wait_for(
        lambda: source.can_login("app_user", APP_NEW_PASSWORD, "appdb"),
        "the current password to be set again",
    )
    assert source.can_login("old_user", OLD_PASSWORD, "olddb")

    # Older backups belong to another timeline
    source.backup_now()
    assert source.backups()[-1]["type"] == "full"
    scenario["rolled_back"] = body


def test_same_source_again_does_nothing(scenario):
    _require(scenario, "rolled_back")
    source = scenario["source"]
    timelines = {ip: source.timeline(ip) for ip in source.ips()}

    source.api.call("PUT", source.path, scenario["rolled_back"], expect=200)
    time.sleep(60)

    assert {ip: source.timeline(ip) for ip in source.ips()} == timelines
    changed = {
        "restore_from": {
            **scenario["rolled_back"]["restore_from"],
            "target": _at(scenario["t0"]),
        }
    }
    response = source.api.call("PUT", source.path, changed)
    assert response.status_code == 400, response.text


def test_added_node_does_not_repeat_the_rollback(scenario):
    _require(scenario, "rolled_back")
    source = scenario["source"]
    _append(source, "after rollback", 7)
    leader = source.leader()
    timeline = source.timeline(leader)
    old_ips = set(source.ips())

    source.api.call("PUT", source.path, {"nodes_number": 3}, expect=200)

    new_ip = fc.wait_for(
        lambda: next(iter(set(source.ips()) - old_ips), None),
        "the new node",
    )
    fc.wait_for(
        lambda: any(
            m["host"] == new_ip and m["state"] in ("streaming", "running")
            for m in source.members()
        ),
        "the new node to replicate",
    )
    fc.wait_for(
        lambda: source.applied_rollback(new_ip) == "1",
        "the new node to mark the rollback",
    )

    assert source.dcs().get("pause") is not True
    assert source.timeline(source.leader()) == timeline
    source.wait_rows({"first": 10, "second": 90, "after rollback": 7})


def test_second_rollback_to_an_earlier_point(scenario, s3_storage):
    _require(scenario, "rolled_back")
    source = scenario["source"]
    body = {
        "restore_from": _source(
            scenario, s3_storage, target=_at(scenario["t0"]), revision=2
        )
    }

    source.api.call("PUT", source.path, body, expect=200)
    source.wait_rollback(2)

    assert len(source.ips()) == 3
    source.wait_rows({"first": 10})
    assert set(source.users()) == {"app_user", "old_user"}
    scenario["second_rollback"] = True


def _rollback(scenario, s3_storage, target_time, revision):
    source = scenario["source"]
    body = {
        "restore_from": _source(
            scenario, s3_storage, target=_at(target_time), revision=revision
        )
    }
    source.api.call("PUT", source.path, body, expect=200)


def test_rollback_after_the_replicas_recycled_their_wal(scenario, s3_storage):
    _require(scenario, "second_rollback")
    source = scenario["source"]
    _append(source, "before recycling", 4)
    source.archive_now()
    # The rollbacks left the earlier full backups on abandoned timelines, and
    # the periodic one of this timeline may straddle the target: a backup
    # counts only if it finished before it
    source.backup_now()
    time.sleep(2)
    target = source.now()
    time.sleep(2)
    # Push the replicas' restartpoints past the target: pg_rewind can't find
    # the WAL back to the fork any more
    for _ in range(12):
        _append(source, "recycled", 1)
        source.sql("select pg_switch_wal()")
    source.sql("checkpoint")
    leader = source.leader()
    for ip in source.ips():
        if ip != leader:
            source.sql("checkpoint", ip=ip)

    _rollback(scenario, s3_storage, target, revision=3)
    source.wait_rollback(3)

    source.wait_rows({"first": 10, "before recycling": 4})
    members = source.members()
    timelines = {m["timeline"] for m in members}
    assert len(timelines) == 1, members
    assert {m["state"] for m in members if m["role"] != "leader"} == {"streaming"}
    scenario["after_recycling"] = True


def test_rollback_to_a_moment_not_archived_yet(scenario, s3_storage):
    _require(scenario, "after_recycling")
    source = scenario["source"]
    _append(source, "tail", 6)
    time.sleep(2)
    target = source.now()
    time.sleep(2)
    _append(source, "after tail", 2)

    # No archive_now(): the target is in WAL only the leader has
    _rollback(scenario, s3_storage, target, revision=4)
    source.wait_rollback(4)

    source.wait_rows({"first": 10, "before recycling": 4, "tail": 6})
    scenario["tail"] = True


def test_failed_rollback_is_recovered_by_a_higher_revision(scenario, s3_storage):
    _require(scenario, "tail")
    source = scenario["source"]
    leader = source.leader()
    # The rollbacks forced full backups, and the retention has expired the
    # ones covering the earlier points: the recovery target needs a fresh one
    source.backup_now()
    _append(source, "before failure", 3)
    source.archive_now()
    time.sleep(2)
    target = source.now()
    time.sleep(2)
    _append(source, "after failure target", 1)
    time.sleep(2)
    # Undone by the rollback that recovers the failed one
    scenario["undone"] = source.now()
    time.sleep(2)

    # No backup covers the target, the job fails before touching the data
    _rollback(scenario, s3_storage, scenario["before_backups"], revision=5)
    fc.wait_for(
        lambda: source.rollback_phase(leader) == "failed", "the rollback to fail"
    )
    source.wait_status("ERROR")
    status = source.instance().get("restore_status")
    assert status["revision"] == 5
    assert "No backup" in status["error"], status
    assert source.dcs().get("pause") is True
    # The data of the leader is intact: its PostgreSQL still runs
    assert source.sql("select count(*) from orders", "appdb", ip=leader) == "24"

    # The leader of the failed rollback may not be removed
    response = source.api.call("PUT", source.path, {"nodes_number": 2})
    assert response.status_code == 400, response.text

    _rollback(scenario, s3_storage, target, revision=6)
    source.wait_rollback(6)

    source.wait_rows(
        {"first": 10, "before recycling": 4, "tail": 6, "before failure": 3}
    )
    assert source.instance().get("restore_status") is None
    scenario["recovered"] = True


def test_rollback_into_an_undone_interval(scenario, s3_storage):
    _require(scenario, "recovered")
    source = scenario["source"]
    # The target lies between the point the last rollback went to and the
    # rollback itself: that WAL is on the abandoned timeline
    _append(source, "after recovery", 2)
    source.archive_now()

    _rollback(scenario, s3_storage, scenario["undone"], revision=7)
    source.wait_rollback(7)

    # The latest timeline is followed: its history leaves the abandoned one at
    # the point of the last rollback, and nothing on the new one is older
    # than the target
    source.wait_rows(
        {"first": 10, "before recycling": 4, "tail": 6, "before failure": 3}
    )
    scenario["into_undone"] = True


def _undo(scenario, s3_storage, before_revision, revision):
    source = scenario["source"]
    restore_from = _source(
        scenario, s3_storage, target=_before(before_revision), revision=revision
    )
    source.api.call("PUT", source.path, {"restore_from": restore_from}, expect=200)


def test_rollback_is_undone(scenario, s3_storage):
    _require(scenario, "into_undone")
    source = scenario["source"]
    timelines = {m["timeline"] for m in source.members()}
    _append(source, "unarchived", 1)

    # Kept by rollback 7, taken right after "after recovery"
    _undo(scenario, s3_storage, before_revision=7, revision=8)
    source.wait_rollback(8)

    source.wait_rows(
        {
            "first": 10,
            "before recycling": 4,
            "tail": 6,
            "before failure": 3,
            "after recovery": 2,
        }
    )
    # A timeline no rollback has used, and archiving goes on on it
    assert source.timeline(source.leader()) > max(timelines)
    source.archive_now()
    assert source.sql("select failed_count from pg_stat_archiver") == "0"
    scenario["undo"] = True


def test_undo_is_undone_in_turn(scenario, s3_storage):
    _require(scenario, "undo")
    source = scenario["source"]
    _append(source, "after undo", 5)

    # Back to the state rollback 8 replaced, with the row that wasn't archived
    _undo(scenario, s3_storage, before_revision=8, revision=9)
    source.wait_rollback(9)

    source.wait_rows(
        {
            "first": 10,
            "before recycling": 4,
            "tail": 6,
            "before failure": 3,
            "unarchived": 1,
        }
    )
    scenario["undo_twice"] = True


def test_state_before_a_rollback_is_cloned(scenario, instances, s3_storage):
    _require(scenario, "rolled_back")
    clone = instances(
        "pitr-before", restore_from=_source(scenario, s3_storage, target=_before(1))
    )

    clone.wait_status("ACTIVE")

    # What rollback 1 replaced: the late changes
    assert clone.rows() == {"late": 5}
    assert clone.table_exists("junk")
    assert set(clone.users()) == {"app_user", "late_user"}
    assert clone.can_login("app_user", APP_NEW_PASSWORD, "appdb")


@pytest.mark.parametrize(
    "change",
    [
        {"target": {"kind": "before_revision", "revision": 999}},
        # A target is of one kind only
        {
            "target": {
                "kind": "before_revision",
                "revision": 1,
                "time": "2026-09-14T10:30:00Z",
            }
        },
    ],
)
def test_invalid_undo_is_rejected(scenario, s3_storage, change):
    _require(scenario, "rolled_back")
    source = scenario["source"]
    restore_from = _source(scenario, s3_storage, revision=1000, **change)

    response = source.api.call("PUT", source.path, {"restore_from": restore_from})

    assert response.status_code == 400, response.text


def test_repository_errors_dont_hold_up_the_roles(scenario, s3_storage):
    _require(scenario, "recovered")
    source = scenario["source"]
    broken = {
        **s3_storage,
        "secret_key": "wrong-secret-key",
        "path": "/functional-broken",
    }

    source.api.call("PUT", source.path, {"backup": broken}, expect=200)
    user = source.create_user("during_error", "during-error-pass-functional")

    fc.wait_for(
        lambda: source.can_login(
            "during_error", "during-error-pass-functional", "postgres"
        ),
        "the user to be created while the repository fails",
    )
    source.api.call("DELETE", f"{source.path}/users/{user}", expect=204)
