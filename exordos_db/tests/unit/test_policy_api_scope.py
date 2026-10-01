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

"""Project checks run through the real user API routes and controllers."""

from unittest import mock
import uuid

import pytest
from restalchemy.api import applications
from restalchemy.common import contexts
from restalchemy.storage import exceptions as storage_exc
import webob

from exordos_db.common.api.middlewares import errors
from exordos_db.user_api.api import app
from exordos_db.user_api.dm import backups
from exordos_db.user_api.dm import models

PROJECT = uuid.uuid4()
FOREIGN = uuid.uuid4()
PARENT = uuid.uuid4()
FOREIGN_PARENT = uuid.uuid4()


@pytest.fixture
def api(monkeypatch):
    ctx = mock.Mock()
    ctx.iam_context.introspection_info.return_value = {"project_id": str(PROJECT)}
    ctx.iam_context.enforcer.enforce.return_value = True
    contexts.ContextWithStorage._store_context_session(ctx)
    manager = mock.Mock()

    def get_parent(**kwargs):
        resource_project = PROJECT if str(kwargs["uuid"]) == str(PARENT) else FOREIGN
        if str(kwargs["uuid"]) not in {str(PARENT), str(FOREIGN_PARENT)}:
            raise storage_exc.RecordNotFound(model=models.PGInstance, filters=kwargs)
        if "project_id" in kwargs and str(kwargs["project_id"]) != str(
            resource_project
        ):
            raise storage_exc.RecordNotFound(model=models.PGInstance, filters=kwargs)
        return models.PGInstance(
            uuid=uuid.UUID(str(kwargs["uuid"])),
            project_id=resource_project,
            name="own",
            cpu=1,
            ram=1024,
            disk_size=8,
            nodes_number=1,
            version=models.PGVersion(name="18", image="pg.raw"),
        )

    # The collection is a fresh object on each access; replace the class entry.
    manager.get_one.side_effect = lambda **kwargs: get_parent(
        **{
            name: getattr(clause, "value", clause)
            for name, clause in kwargs["filters"].items()
        }
    )
    monkeypatch.setattr(models.PGInstance, "objects", manager)
    application = errors.ErrorsHandlerMiddleware(
        applications.Application(app.UserApiApp)
    )
    try:
        yield application
    finally:
        contexts.ContextWithStorage._clear_context()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_foreign_parent_is_inaccessible(api, method):
    path = f"/v1/types/postgres/instances/{FOREIGN_PARENT}/backup_policies/"
    if method in {"PUT", "DELETE"}:
        path += str(uuid.uuid4())
    request = webob.Request.blank(path, method=method)
    if method in {"POST", "PUT"}:
        request.json_body = {}
    response = request.get_response(api)
    assert response.status_int == 404, response.text


def test_create_cannot_override_project(api):
    request = webob.Request.blank(
        f"/v1/types/postgres/instances/{PARENT}/backup_policies/",
        method="POST",
    )
    request.json_body = {
        "project_id": str(FOREIGN),
        "storage": {
            "kind": "s3",
            "endpoint": "https://s3.example.com",
            "bucket": "backups",
            "access_key": "key",
            "secret_key": "secret",
        },
    }
    response = request.get_response(api)
    assert response.status_int == 403, response.text


def test_create_cannot_select_foreign_instance_in_body(api):
    request = webob.Request.blank(
        f"/v1/types/postgres/instances/{PARENT}/backup_policies/",
        method="POST",
    )
    request.json_body = {
        "project_id": str(PROJECT),
        "instance": f"/v1/types/postgres/instances/{FOREIGN_PARENT}",
        "storage": {
            "kind": "s3",
            "endpoint": "https://s3.example.com",
            "bucket": "backups",
            "access_key": "key",
            "secret_key": "secret",
        },
    }
    response = request.get_response(api)
    assert response.status_int == 404, response.text


def test_update_cannot_override_project(api, monkeypatch):
    parent = models.PGInstance(
        uuid=PARENT,
        project_id=PROJECT,
        name="own",
        cpu=1,
        ram=1024,
        disk_size=8,
        nodes_number=1,
        version=models.PGVersion(name="18", image="pg.raw"),
    )
    policy = models.PGBackupPolicy(
        instance=parent,
        project_id=PROJECT,
        storage=backups.STORAGE_TYPE.from_simple_type(
            {
                "kind": "s3",
                "endpoint": "https://s3.example.com",
                "bucket": "backups",
                "access_key": "key",
                "secret_key": "secret",
            }
        ),
    )
    monkeypatch.setattr(
        models.PGBackupPolicy,
        "objects",
        mock.Mock(get_one=mock.Mock(return_value=policy)),
    )
    update = mock.Mock()
    monkeypatch.setattr(policy, "update", update)
    request = webob.Request.blank(
        f"/v1/types/postgres/instances/{PARENT}/backup_policies/{policy.uuid}",
        method="PUT",
    )
    request.json_body = {"project_id": str(FOREIGN)}
    response = request.get_response(api)
    assert response.status_int == 400, response.text
    assert policy.project_id == PROJECT
    update.assert_not_called()
