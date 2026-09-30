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

"""The published documents are built here, so building them has to work."""

import configparser
import importlib
import os

import pytest
from restalchemy.common import contexts as ra_contexts

from exordos_db.common import openapi
from exordos_db.common import utils


@pytest.fixture(scope="module", params=openapi.APIS, ids=lambda api: api.name)
def specification(request):
    return request.param, openapi.build(request.param)


def _refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def test_describes_the_api(specification):
    api, spec = specification

    assert spec["openapi"] == openapi.OPENAPI_VERSION
    assert spec["paths"]
    assert spec["servers"][0]["url"] == api.url
    assert spec["info"]["version"] == openapi.PUBLISHED_VERSION


def _resolves(spec, ref):
    node = spec
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def test_every_reference_resolves(specification):
    _, spec = specification

    assert not {ref for ref in _refs(spec) if not _resolves(spec, ref)}


def test_backup_policies_are_described():
    paths = openapi.build(openapi.USER_API)["paths"]

    assert "/v1/types/postgres/instances/{PGInstanceUuid}/backup_policies/" in paths


def test_leaves_no_context_behind():
    openapi.build(openapi.STATUS_API)

    with pytest.raises(ra_contexts.ContextIsNotExistsInStorage):
        ra_contexts.get_context()


def test_core_agent_serves_every_resource_of_the_user_api():
    """A resource type the agent doesn't declare stays NEW in an element.

    The element manager hands a resource of a manifest to an agent declaring
    its kind, `em_dbaas_` and the path of its collection.
    """
    config = configparser.ConfigParser()
    config.read(
        os.path.join(utils.PROJECT_PATH, "etc", "exordos_db", "core_agent.conf.j2")
    )
    kinds = set()
    for path in openapi.build(openapi.USER_API)["paths"]:
        parts = [p for p in path.strip("/").split("/")[1:] if not p.startswith("{")]
        if path.endswith("/") and parts[:1] == ["types"] and len(parts) > 2:
            kinds.add("em_dbaas_" + "_".join(parts))

    assert kinds <= set(config["models"])
    assert kinds <= set(config["filters"])
    for kind in kinds:
        module, name = config["models"][kind].split(":")
        assert getattr(importlib.import_module(module), name)
