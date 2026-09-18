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

import types

import pytest

from exordos_db.user_api.api import controllers
from exordos_db.user_api.dm import models


class Rows:
    def __init__(self):
        self.done = []

    def create(self, parent_resource, **kwargs):
        self.done.append("create")

    def update(self, parent_resource, uuid, **kwargs):
        self.done.append("update")

    def delete(self, parent_resource, uuid):
        self.done.append("delete")


class Controller(controllers.RolesLockedMixin, Rows):
    pass


def _instance(roles_managed):
    return types.SimpleNamespace(uuid="i", roles_managed=lambda: roles_managed)


@pytest.mark.parametrize(
    "call",
    [
        lambda c, i: c.create(i, name="late"),
        lambda c, i: c.update(i, "u", password="new-password"),
        lambda c, i: c.delete(i, "u"),
    ],
)
def test_rows_are_locked_while_the_roles_arent_matched(call):
    # A row created during a rollback has no role on the rolled back cluster
    # and would be deleted when the roles are matched
    controller = Controller()

    with pytest.raises(models.RolesLockedError) as e:
        call(controller, _instance(False))

    assert e.value.get_code() == 409
    assert controller.done == []


def test_rows_change_once_the_roles_are_matched():
    controller = Controller()
    instance = _instance(True)

    controller.create(instance, name="app")
    controller.update(instance, "u", password="new-password")
    controller.delete(instance, "u")

    assert controller.done == ["create", "update", "delete"]
