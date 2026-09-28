from __future__ import annotations

from dataclasses import dataclass

import pytest

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualIdentityUnavailable,
    EvidenceProvenance,
    IdentityField,
    PlatformIdentityBinding,
    project_actual_identity,
)
from deployment_operations.platform_identity_source import (
    ActualPlatformSnapshot,
    OwnerSuppliedPlatformIdentityProvider,
)


def component(component_id: str) -> ActualComponentIdentity:
    return ActualComponentIdentity(
        component_id=component_id,
        component_version="1.0.0",
        artifact_identity={
            "artifact_type": "none",
            "digest": None,
            "pinned": False,
            "canonical_form": None,
        },
    )


def snapshot(**overrides: object) -> ActualPlatformSnapshot:
    values: dict[str, object] = {
        "platform_id": "example-platform",
        "manifest": {
            "manifest_id": "example-manifest",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:" + "1" * 64,
        },
        "manifest_state": "published",
        "components": (component("alpha"),),
        "membership_established": True,
        "configuration": IdentityField.absent(),
        "golden_bundle": IdentityField.absent(),
        "golden_bundle_inventory_established": True,
        "extensions": IdentityField.absent(),
        "branding": IdentityField.absent(),
        "provenance": EvidenceProvenance.MEASURED,
        "correlation_token": object(),
        "freshness_current": True,
    }
    values.update(overrides)
    return ActualPlatformSnapshot(**values)


@dataclass
class Source:
    value: ActualPlatformSnapshot

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        assert binding.token == "running-platform"
        return self.value


def provider(value: ActualPlatformSnapshot):
    return OwnerSuppliedPlatformIdentityProvider(Source(value))


def test_provider_preserves_owner_supplied_actual_identity():
    evidence = provider(snapshot()).observe_identity(
        PlatformIdentityBinding("running-platform")
    )
    assert evidence.value.platform_id == "example-platform"
    assert evidence.value.components[0].value.component_id == "alpha"


def test_incomplete_membership_is_unavailable():
    with pytest.raises(ActualIdentityUnavailable):
        provider(snapshot(membership_established=False)).observe_identity(
            PlatformIdentityBinding("running-platform")
        )


def test_duplicate_actual_members_are_unavailable():
    member = component("alpha")
    with pytest.raises(ActualIdentityUnavailable):
        provider(snapshot(components=(member, member))).observe_identity(
            PlatformIdentityBinding("running-platform")
        )


def test_stale_snapshot_is_unavailable():
    with pytest.raises(ActualIdentityUnavailable):
        provider(snapshot(freshness_current=False)).observe_identity(
            PlatformIdentityBinding("running-platform")
        )


def test_expected_digest_is_not_part_of_snapshot_contract():
    actual = snapshot()
    assert not hasattr(actual, "instance_digest")
    assert not hasattr(actual, "expected_instance")


def test_unknown_identity_field_is_unavailable():
    evidence = provider(
        snapshot(configuration=IdentityField.unknown())
    ).observe_identity(PlatformIdentityBinding("running-platform"))

    with pytest.raises(ActualIdentityUnavailable, match="UNKNOWN"):
        project_actual_identity(evidence)
