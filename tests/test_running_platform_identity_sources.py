from __future__ import annotations

from dataclasses import dataclass

import pytest

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    EvidenceCorrelation,
    EvidenceFreshness,
    EvidenceProvenance,
    IdentityField,
    PlatformIdentityBinding,
)
from running_platform.identity_sources import (
    ActualGoldenBundle,
    ActualManifest,
    ActualMembership,
)

BINDING = PlatformIdentityBinding("running-platform")


def evidence(value: object) -> ActualEvidence:
    return ActualEvidence(
        value=value,
        provenance=EvidenceProvenance.MEASURED,
        correlation=EvidenceCorrelation("observation-1"),
        freshness=EvidenceFreshness(True),
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


def test_membership_contract_preserves_multiplicity() -> None:
    membership = ActualMembership(
        established=True,
        components=(component("alpha"), component("alpha")),
    )

    assert len(membership.components) == 2
    assert membership.components[0] == membership.components[1]


def test_membership_contract_does_not_silently_claim_established_membership() -> None:
    membership = ActualMembership(established=False, components=())

    assert membership.established is False


def test_manifest_contract_keeps_actual_state_separate() -> None:
    manifest = ActualManifest(
        manifest={
            "manifest_id": "actual-manifest",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:" + "1" * 64,
        },
        manifest_state="published",
    )

    assert manifest.manifest["manifest_id"] == "actual-manifest"
    assert manifest.manifest_state == "published"


def test_golden_bundle_contract_preserves_inventory_establishment() -> None:
    bundle = ActualGoldenBundle(
        identity=IdentityField.present({"digest": "sha256:" + "2" * 64}),
        inventory_established=False,
    )

    assert bundle.identity.state == "PRESENT"
    assert bundle.inventory_established is False


@dataclass
class MembershipImplementation:
    def observe_membership(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        assert binding == BINDING
        return evidence(
            ActualMembership(established=True, components=(component("alpha"),))
        )


@dataclass
class ManifestImplementation:
    def observe_manifest(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        assert binding == BINDING
        return evidence(
            ActualManifest(
                manifest={
                    "manifest_id": "actual-manifest",
                    "manifest_version": "1.0.0",
                    "manifest_digest": "sha256:" + "1" * 64,
                },
                manifest_state="published",
            )
        )


@dataclass
class ConfigurationImplementation:
    def observe_configuration(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        assert binding == BINDING
        return evidence(IdentityField.absent())


@dataclass
class GoldenBundleImplementation:
    def observe_golden_bundle(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        assert binding == BINDING
        return evidence(
            ActualGoldenBundle(
                identity=IdentityField.absent(),
                inventory_established=True,
            )
        )


@dataclass
class ExtensionImplementation:
    def observe_extensions(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        assert binding == BINDING
        return evidence(IdentityField.absent())


@dataclass
class BrandingImplementation:
    def observe_branding(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        assert binding == BINDING
        return evidence(IdentityField.absent())


@pytest.mark.parametrize(
    "implementation, method_name",
    [
        (MembershipImplementation(), "observe_membership"),
        (ManifestImplementation(), "observe_manifest"),
        (ConfigurationImplementation(), "observe_configuration"),
        (GoldenBundleImplementation(), "observe_golden_bundle"),
        (ExtensionImplementation(), "observe_extensions"),
        (BrandingImplementation(), "observe_branding"),
    ],
)
def test_each_source_contract_accepts_only_the_opaque_binding(
    implementation: object,
    method_name: str,
) -> None:
    method = getattr(implementation, method_name)
    observed = method(BINDING)

    assert isinstance(observed, ActualEvidence)
    assert observed.correlation.token == "observation-1"
    assert observed.freshness.current is True
    assert observed.provenance == EvidenceProvenance.MEASURED


def test_sources_do_not_receive_expected_instance_digest() -> None:
    for implementation, method_name in (
        (MembershipImplementation(), "observe_membership"),
        (ManifestImplementation(), "observe_manifest"),
        (ConfigurationImplementation(), "observe_configuration"),
        (GoldenBundleImplementation(), "observe_golden_bundle"),
        (ExtensionImplementation(), "observe_extensions"),
        (BrandingImplementation(), "observe_branding"),
    ):
        method = getattr(implementation, method_name)
        with pytest.raises(AssertionError):
            method(PlatformIdentityBinding("sha256:" + "0" * 64))


def test_owner_source_module_does_not_import_expected_deployment_state() -> None:
    import running_platform.identity_sources as module

    names = set(module.__dict__)
    forbidden = {
        "InstanceVerification",
        "DeploymentRecord",
        "DeploymentEnvironment",
        "PlatformInstance",
    }

    assert names.isdisjoint(forbidden)
}
