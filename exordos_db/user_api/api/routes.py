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

import typing as tp

from restalchemy.api import routes

from exordos_db.user_api.api import controllers


class PGDatabaseRoute(routes.Route):
    __controller__ = controllers.PGDatabaseController


class PGUserRoute(routes.Route):
    __controller__ = controllers.PGUserController


class PGInstanceRoute(routes.Route):
    __controller__ = controllers.PGInstanceController

    # route to /v1/types/postgres/instances/<uuid>/database/[<uuid>]
    databases = routes.route(PGDatabaseRoute, resource_route=True)
    # route to /v1/types/postgres/instances/<uuid>/users/[<uuid>]
    users = routes.route(PGUserRoute, resource_route=True)


class PGVersionRoute(routes.Route):
    __controller__ = controllers.PGVersionController


class PGBackupRepositoryRoute(routes.Route):
    __controller__ = controllers.PGBackupRepositoryController


class PGBackupRoute(routes.Route):
    __controller__ = controllers.PGBackupController
    __allow_methods__: tp.ClassVar[list] = [routes.FILTER, routes.GET]


class PGRoute(routes.Route):
    __controller__ = controllers.PGController
    __allow_methods__: tp.ClassVar[list] = [routes.FILTER]

    # route to /v1/types/postgres/instances/[<uuid>]
    instances = routes.route(PGInstanceRoute)

    # route to /v1/types/postgres/versions/[<uuid>]
    versions = routes.route(PGVersionRoute)

    # route to /v1/types/postgres/backup_repositories/[<uuid>]
    backup_repositories = routes.route(PGBackupRepositoryRoute)

    # route to /v1/types/postgres/backups/[<uuid>]
    backups = routes.route(PGBackupRoute)


class TypeRoute(routes.Route):
    """Handler for /v1/types/ endpoint"""

    __controller__ = controllers.TypeController
    __allow_methods__: tp.ClassVar[list] = [routes.FILTER]

    # route to /v1/types/postgres/
    postgres = routes.route(PGRoute)


class ApiEndpointRoute(routes.Route):
    """Handler for /v1/ endpoint"""

    __controller__ = controllers.ApiEndpointController
    __allow_methods__: tp.ClassVar[list] = [routes.FILTER]

    types = routes.route(TypeRoute)
