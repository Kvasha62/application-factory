from __future__ import annotations

import pytest

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    ActualIdentityUnavailable,
    EvidenceCorrelation,
    EvidenceFreshness,
    EvidenceProvenance,
    IdentityField,
    PlatformIdentitySurface,
)
from running_platform.owner import RunningPlatformOwner, RunningPlatformOwnerError


def test_owner_lifecycle_is_independent_of_expected_platform_instance() -> None:
    owner = RunningPlatformOwner()

    owner.start()

    assert owner.started is True


def test_owner_has_no_identity_until_owner_side_observation_exists() -> None:
    owner = RunningPlatformOwner()
    owner.start()

    with pytest.raises(ActualIdentityUnavailable, match="observation is unavailable"):
        owner.observe_identity()


def test_owner_withdraws_observation_when_stopped() -> None:
    owner = RunningPlatformOwner()
    owner.start()
    owner.stop()

    assert owner.started is False
    with pytest.raises(ActualIdentityUnavailable, match="owner is not established"):
        owner.observe_identity()


def test_identity_cannot_be_published_before_owner_starts() -> None:
    owner = RunningPlatformOwner()

    with pytest.raises(RunningPlatformOwnerError, match="owner starts"):
        owner.publish_identity_surface(object())  # type: ignore[arg-type]


def test_owner_retains_existing_observation_correlation() -> None:
    correlation = EvidenceCorrelation("observation-1")
    surface = PlatformIdentitySurface(
        platform_id="example-platform",
        manifest={
            "manifest_id": "example-manifest",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:" + "1" * 64,
        },
        manifest_state="validated",
        components=(
            ActualEvidence(
                value=ActualComponentIdentity(
                    component_id="example-component",
                    component_version="1.0.0",
                    artifact_identity={
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                        "canonical_form": None,
                    },
                ),
                provenance=EvidenceProvenance.MEASURED,
                correlation=correlation,
                freshness=EvidenceFreshness(current=True),
            ),
        ),
        membership_established=True,
        configuration=IdentityField.absent(),
        golden_bundle=IdentityField.absent(),
        golden_bundle_inventory_established=True,
        extensions=IdentityField.absent(),
        branding=IdentityField.absent(),
    )
    owner = RunningPlatformOwner()
    owner.start()
    owner.publish_identity_surface(surface)

    assert owner.observation_correlation is correlation
    assert owner.observe_identity() is surface
