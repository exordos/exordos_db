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
"""Atomic reads and writes of the state files a node keeps."""

from __future__ import annotations

import json
import os
import shutil
import typing as tp


def read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def write(path: str, content: str, mode: int = 0o644, group: str | None = None) -> None:
    """Replace the file at once. `group` makes it root's with that group."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    # The mode of an existing file isn't changed by open
    os.chmod(tmp, mode)
    if group is not None:
        shutil.chown(tmp, user="root", group=group)
    os.replace(tmp, path)


def remove(path: str) -> bool:
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True


def read_json(path: str) -> tp.Any:
    content = read(path)
    return None if content is None else json.loads(content)


def write_json(path: str, data: tp.Any, **kwargs: tp.Any) -> None:
    write(path, json.dumps(data, sort_keys=True), **kwargs)
