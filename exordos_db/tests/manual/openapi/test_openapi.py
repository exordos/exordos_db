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

"""The generated documents are what the running services serve.

`exordos_db.common.openapi` builds them without a service, standing in for
the request state the services have. This asks running services for theirs
and compares, which is what keeps those stand-ins honest.

DBAAS_HOST points at a control plane node (default 10.20.0.20).
"""

import os

import pytest
import requests

from exordos_db.common import openapi

HOST = os.environ.get("DBAAS_HOST", "10.20.0.20")


@pytest.mark.parametrize("api", openapi.APIS, ids=lambda api: api.name)
def test_generated_document_is_the_served_one(api):
    url = f"http://{HOST}:{api.port}/specifications/{openapi.OPENAPI_VERSION}"
    response = requests.get(url, timeout=30)
    assert response.status_code == 200, response.text
    served = response.json()

    generated = openapi.build(api)

    # The only parts describing the request rather than the API
    served["servers"] = generated["servers"]
    served["info"]["version"] = generated["info"]["version"]
    assert served == generated
