# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

from unittest import mock
import uuid

import pytest

from exordos_db.common import audit
from exordos_db.common import version_ref
from exordos_db.infra.dm import models as infra_models
from exordos_db.user_api.dm import models


@pytest.mark.parametrize("image", ["urn:images:1234", "https://host:8443/pg.raw"])
def test_reference_round_trip(image):
    assert (
        version_ref.parse_disk_image(version_ref.build_version_ref("id", image))
        == image
    )


@pytest.mark.parametrize(
    "ref", ["", "urn:exordos:postgres::image", "urn:exordos:s3:id:image"]
)
def test_invalid_reference(ref):
    with pytest.raises(ValueError):
        version_ref.parse_disk_image(ref)


def test_version_generates_reference_on_insert_and_update():
    version = models.PGVersion(name="18", image="urn:images:old")
    with (
        mock.patch.object(audit.AuditLogSQLStorableMixin, "insert"),
        mock.patch.object(audit.AuditLogSQLStorableMixin, "update"),
    ):
        version.insert()
        assert version.version_ref == version_ref.build_version_ref(
            str(version.uuid), "urn:images:old"
        )
        version.image = "urn:images:new"
        version.update()
        assert version.version_ref == version_ref.build_version_ref(
            str(version.uuid), "urn:images:new"
        )


def test_instance_uses_reference_for_root_and_keeps_data_disk():
    instance = infra_models.PGInstance(
        name="pg",
        project_id=uuid.uuid4(),
        cpu=1,
        ram=1024,
        disk_size=10,
        nodes_number=1,
        version=models.PGVersion(name="18", image="urn:images:old"),
        version_ref=version_ref.build_version_ref(str(uuid.uuid4()), "urn:images:new"),
    )
    nodeset = instance.get_infra(instance.project_id)[0]
    disks = nodeset.disk_spec.disks
    assert disks[0]["image"] == "urn:images:new"
    assert disks[1]["label"] == "data"
    assert disks[1]["size"] == 10
    assert "version_ref" in instance.get_resource_target_fields()


def _instance(**kwargs):
    defaults = {
        "name": "pg",
        "project_id": uuid.uuid4(),
        "cpu": 1,
        "ram": 1024,
        "disk_size": 10,
        "nodes_number": 1,
        "version": models.PGVersion(name="18", image="urn:images:old"),
    }
    defaults.update(kwargs)
    return models.PGInstance(**defaults)


class TestInstanceVersionReference:
    @pytest.mark.parametrize("operation", ["insert", "update"])
    @pytest.mark.parametrize("force", [False, True])
    @pytest.mark.parametrize("invalid", ["malformed", "other-version", "other-image"])
    def test_rejects_untrusted_reference(self, operation, force, invalid):
        instance = _instance()
        if invalid == "malformed":
            ref = "not-a-version-reference"
        elif invalid == "other-version":
            ref = version_ref.build_version_ref(
                str(uuid.uuid4()), instance.version.image
            )
        else:
            ref = version_ref.build_version_ref(
                str(instance.version.uuid), "http://untrusted"
            )
        instance.version_ref = ref
        with mock.patch.object(audit.AuditLogSQLStorableMixin, operation) as persist:
            with pytest.raises(
                models.PGValidationError, match="version_ref must match"
            ):
                getattr(instance, operation)(
                    **({"force": force} if operation == "update" else {})
                )
            persist.assert_not_called()

    @pytest.mark.parametrize("operation", ["insert", "update"])
    @pytest.mark.parametrize(
        "image", ["urn:images:trusted", "https://host:8443/image.raw"]
    )
    @pytest.mark.parametrize("empty", [False, True])
    def test_accepts_selected_version_or_legacy_fallback(self, operation, image, empty):
        instance = _instance(version_ref="old-ref" if operation == "update" else "")
        instance.version.image = image
        # Validate against the image, even if the stored version_ref is stale.
        instance.version.version_ref = "stale-ref"
        instance.version_ref = (
            ""
            if empty
            else version_ref.build_version_ref(str(instance.version.uuid), image)
        )
        with mock.patch.object(audit.AuditLogSQLStorableMixin, operation) as persist:
            getattr(instance, operation)()
            persist.assert_called_once()

    def test_unrelated_update_after_version_image_changes(self):
        version = models.PGVersion(name="18", image="urn:images:old")
        instance = _instance(
            version=version,
            version_ref=version_ref.build_version_ref(str(version.uuid), version.image),
        )
        version.image = "urn:images:new"
        instance.status = models.PGStatus.ACTIVE.value
        with mock.patch.object(audit.AuditLogSQLStorableMixin, "update") as persist:
            instance.update()
            persist.assert_called_once()
