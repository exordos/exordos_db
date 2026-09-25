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
"""Where a repository endpoint may point.

The repository is reached from the nodes of an instance, and errors of
reaching it are reported through the API, so an endpoint on a node itself or
on the metadata service would turn the backups into a probe of the
installation. Private addresses are allowed: the storage is often in the same
network as the nodes.

The control plane checks an endpoint when it is saved, and the node pins the
name to a checked address when it renders the config, so the address can't
change between the check and the request.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse

Address = ipaddress.IPv4Address | ipaddress.IPv6Address

DEFAULT_PORTS = {"http": 80, "https": 443}


def literal_address(host: str) -> Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # The nodes also take the short and numeric forms, e.g. 127.1 or
    # 2130706433, for an IPv4 address
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def check_address(host: str, address: Address) -> None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        raise ValueError(f"endpoint address {host} is not allowed")


def host_of(endpoint: str) -> str:
    return urllib.parse.urlsplit(endpoint).hostname or ""


def check_host(endpoint: str) -> None:
    """Reject an endpoint whose host is a rejected address or `localhost`."""
    host = host_of(endpoint)
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"endpoint host {host} is not allowed")
    address = literal_address(host)
    if address is not None:
        check_address(host, address)


def resolved_addresses(host: str) -> list[Address] | None:
    """The addresses of a name, None when it can't be resolved."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    # Without the scope of a link-local IPv6 address
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


def pinned_address(endpoint: str) -> tuple[str, int] | None:
    """The checked address and port of an endpoint's name to connect to.

    None for an endpoint that is already an address: there is nothing left to
    resolve, so nothing can change under the check.
    """
    check_host(endpoint)
    host = host_of(endpoint)
    if literal_address(host) is not None:
        return None

    addresses = resolved_addresses(host)
    if not addresses:
        raise ValueError(f"endpoint host {host} can't be resolved")
    for address in addresses:
        check_address(host, address)

    parts = urllib.parse.urlsplit(endpoint)
    port = parts.port or DEFAULT_PORTS[parts.scheme]
    return str(addresses[0]), port
