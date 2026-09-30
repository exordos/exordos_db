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

"""Build the OpenAPI documents this project publishes.

A document describes the code that is loaded and not the data a service
holds, so building one needs neither a database nor a service listening on a
port: walking the route tree of an application object produces the document
that service would answer ``/specifications/{version}`` with. The documents
are generated when the site or the element is built instead of being kept
in the tree, where they went stale whenever the API moved.

Two things reach for request state while the tree is walked, and each is
answered here with what the published document is meant to describe:

  - IAM controllers read the caller's introspection when they are
    constructed;
  - field permissions ask the enforcer whether a field is visible, so a
    document is only as complete as the rights of whoever asked for it. The
    published one is the complete one, so every rule is granted.
"""

import contextlib
import dataclasses
import importlib
import json
import typing as tp

from restalchemy.api import applications
from restalchemy.api import constants as api_constants
from restalchemy.api import contexts as api_contexts
from restalchemy.common import contexts as common_contexts
import webob

OPENAPI_VERSION = "3.0.3"

# What the published documents carry as the API version. The real one moves
# with every release while the API it describes does not.
PUBLISHED_VERSION = "latest"


@dataclasses.dataclass(frozen=True)
class Api:
    """One of the APIs this project serves."""

    name: str
    module: str
    # The default bind port of the matching `exordos-db-*-api` command
    port: int

    @property
    def filename(self) -> str:
        return f"openapi_{self.name}.yaml"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


USER_API = Api("user", "exordos_db.user_api.api.app", 8080)
ORCH_API = Api("orch", "exordos_db.orch_api.api.app", 11011)
STATUS_API = Api("status", "exordos_db.status_api.api.app", 11012)

APIS = (USER_API, ORCH_API, STATUS_API)


class _GrantEveryRule:
    """An enforcer that answers yes, the API is described in full."""

    def enforce(self, rule, do_raise=False, exc=None):
        return True

    def enforce_raw(self, rule, do_raise=False, exc=None):
        return True


class _SpecIamContext:
    """The IAM half of a context, holding what building a document reads."""

    def __init__(self):
        self.enforcer = _GrantEveryRule()

    def introspection_info(self) -> dict:
        # No project: a document is not scoped to anything
        return {}


@contextlib.contextmanager
def _spec_build_environment() -> tp.Iterator[None]:
    """Stand in for what a served request would have provided."""
    context = common_contexts.ContextWithStorage()
    context.iam_context = _SpecIamContext()
    with context.context_manager():
        yield


def _build_request(application: tp.Any) -> webob.Request:
    """A request asking to read, which is what serving the document is.

    Field visibility is decided per method, so the request the document is
    built from has to name one.
    """
    request = webob.Request.blank("/")
    request.application = application
    request.api_context = api_contexts.RequestContext(request)
    request.api_context.set_active_method(api_constants.GET)
    return request


def build(api: Api, version: str = OPENAPI_VERSION) -> dict:
    """Build the OpenAPI document of a single API."""
    module = importlib.import_module(api.module)

    with _spec_build_environment():
        application = applications.OpenApiApplication(
            route_class=module.get_api_application(),
            openapi_engine=module.get_openapi_engine(),
        )
        specification = application.openapi_engine.build_openapi_specification(
            version=version,
            request=_build_request(application),
        )

    # The servers block is built from the request being answered, and there
    # is no request here worth describing
    specification["servers"][0]["url"] = api.url
    specification["info"]["version"] = PUBLISHED_VERSION

    # A served document is JSON by the time anyone reads it, and the builder
    # leaves types behind that only survive in this process
    return json.loads(json.dumps(specification))


def build_all(version: str = OPENAPI_VERSION) -> dict[Api, dict]:
    """Build the OpenAPI document of every API this project serves."""
    return {api: build(api, version=version) for api in APIS}
