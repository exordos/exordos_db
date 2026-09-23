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

from gcl_iam import controllers as iam_controllers
from restalchemy.api import constants
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import field_permissions as field_p
from restalchemy.api import resources as ra_resources

from exordos_db.user_api.api import versions
from exordos_db.user_api.dm import models


class ApiEndpointController(ra_controllers.RoutesListController):
    """Controller for /v1/ endpoint"""

    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/"


class TypeController(ra_controllers.Controller):
    def filter(self, filters, order_by):
        return ["postgres"]


class PGController(ra_controllers.RoutesListController):
    """Controller for /v1/types/postgres/ endpoint"""

    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/types/postgres/"


class PGVersionController(
    iam_controllers.PolicyBasedWithoutProjectController,
    ra_controllers.BaseResourceControllerPaginated,
):
    __policy_service_name__ = "exordos_db"
    __policy_name__ = "pg_version"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.PGVersion,
        convert_underscore=False,
        process_filters=True,
    )


class PGInstanceController(
    iam_controllers.PolicyBasedController,
    ra_controllers.BaseResourceControllerPaginated,
):
    __policy_service_name__ = "exordos_db"
    __policy_name__ = "pg_instance"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.PGInstance,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "status": {constants.ALL: field_p.Permissions.RO},
                "ipsv4": {constants.ALL: field_p.Permissions.RO},
                "roles_imported": {constants.ALL: field_p.Permissions.HIDDEN},
                "restore_status": {constants.ALL: field_p.Permissions.RO},
            },
        ),
    )


class PGDatabaseController(
    iam_controllers.NestedPolicyBasedController,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __policy_service_name__ = "exordos_db"
    __policy_name__ = "database"
    __pr_name__ = "instance"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.PGDatabase,
        convert_underscore=False,
        process_filters=True,
    )


class PGUserController(
    iam_controllers.NestedPolicyBasedController,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __policy_service_name__ = "exordos_db"
    __policy_name__ = "user"
    __pr_name__ = "instance"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.PGUser,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "password_hash": {constants.ALL: field_p.Permissions.HIDDEN},
            },
        ),
    )
