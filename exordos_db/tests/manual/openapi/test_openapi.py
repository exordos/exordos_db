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

import os

import requests
import ruamel.yaml

from exordos_db.common.utils import PROJECT_PATH

SPECIFICATIONS_PATH = "specifications/3.0.3"
yaml = ruamel.yaml.YAML()
yaml.indent(sequence=4, offset=2)


class TestGetOpenApiSpecs:
    def test_user_openapi_base(self):
        # User API
        user_base_url = "http://10.20.0.20:8080"
        url = f"{user_base_url}/{SPECIFICATIONS_PATH}"
        response = requests.get(url)
        assert response.status_code == 200

        path = os.path.join(PROJECT_PATH, "docs", "openapi", "openapi_user.yaml")
        spec = response.json()
        spec["info"]["version"] = "latest"
        with open(path, "w") as f:
            yaml.dump(spec, f)

        # Orch API
        orch_base_url = "http://10.20.0.20:11011"
        url = f"{orch_base_url}/{SPECIFICATIONS_PATH}"
        response = requests.get(url)
        assert response.status_code == 200

        path = os.path.join(PROJECT_PATH, "docs", "openapi", "openapi_orch.yaml")
        spec = response.json()
        spec["info"]["version"] = "latest"
        with open(path, "w") as f:
            yaml.dump(spec, f)

        # Status API
        status_base_url = "http://10.20.0.20:11012"
        url = f"{status_base_url}/{SPECIFICATIONS_PATH}"
        response = requests.get(url)
        assert response.status_code == 200

        path = os.path.join(PROJECT_PATH, "docs", "openapi", "openapi_status.yaml")
        spec = response.json()
        spec["info"]["version"] = "latest"
        with open(path, "w") as f:
            yaml.dump(spec, f)
