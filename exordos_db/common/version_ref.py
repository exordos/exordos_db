# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""PostgreSQL version references, compatible with observability URNs."""


def build_version_ref(uuid: str, image: str) -> str:
    return f"urn:exordos:postgres:{uuid}:{image.removeprefix('urn:images:')}"


def parse_disk_image(version_ref: str) -> str:
    parts = version_ref.split(":", 4)
    if (
        len(parts) != 5
        or parts[:3] != ["urn", "exordos", "postgres"]
        or not all(parts[3:])
    ):
        raise ValueError(f"Invalid PostgreSQL version reference: {version_ref!r}")
    image = parts[4]
    return image if image.startswith(("http://", "https://")) else f"urn:images:{image}"
