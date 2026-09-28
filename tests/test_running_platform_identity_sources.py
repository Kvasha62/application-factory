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
    RuntimeMembershipSource,
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
    received_binding: PlatformIdentityBinding | None = None

    def observe_membership(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        self.received_binding = binding
        return evidence(
            ActualMembership(established=True, components=(component("alpha"),))
        )


@dataclass
class ManifestImplementation:
    received_binding: PlatformIdentityBinding | None = None

    def observe_manifest(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        self.received_binding = binding
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
    received_binding: PlatformIdentityBinding | None = None

    def observe_configuration(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        self.received_binding = binding
        return evidence(IdentityField.absent())


@dataclass
class GoldenBundleImplementation:
    received_binding: PlatformIdentityBinding | None = None

    def observe_golden_bundle(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        self.received_binding = binding
        return evidence(
            ActualGoldenBundle(
                identity=IdentityField.absent(),
                inventory_established=True,
            )
        )


@dataclass
class ExtensionImplementation:
    received_binding: PlatformIdentityBinding | None = None

    def observe_extensions(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        self.received_binding = binding
        return evidence(IdentityField.absent())


@dataclass
class BrandingImplementation:
    received_binding: PlatformIdentityBinding | None = None

    def observe_branding(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        self.received_binding = binding
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


def test_sources_receive_only_the_opaque_binding_object() -> None:
    opaque_binding = PlatformIdentityBinding("sha256:" + "0" * 64)
    for implementation, method_name in (
        (MembershipImplementation(), "observe_membership"),
        (ManifestImplementation(), "observe_manifest"),
        (ConfigurationImplementation(), "observe_configuration"),
        (GoldenBundleImplementation(), "observe_golden_bundle"),
        (ExtensionImplementation(), "observe_extensions"),
        (BrandingImplementation(), "observe_branding"),
    ):
        method = getattr(implementation, method_name)
        method(opaque_binding)

        assert implementation.received_binding is opaque_binding
        assert implementation.received_binding.token == opaque_binding.token


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


@dataclass
class RuntimeObserver:
    observations: dict[object, ActualEvidence]

    def observe_runtime_handle(self, handle: object) -> ActualEvidence:
        return self.observations[handle]


def test_runtime_membership_observes_the_complete_owner_runtime_set() -> None:
    handles = ("alpha-runtime", "beta-runtime")
    observer = RuntimeObserver(
        {
            handles[0]: evidence(component("alpha")),
            handles[1]: evidence(component("beta")),
        }
    )
    source = RuntimeMembershipSource(
        list_handles=lambda binding: handles,
        observer=observer,
    )

    observed = source.observe_membership(BINDING)

    membership = observed.value
    assert isinstance(membership, ActualMembership)
    assert membership.established is True
    assert [item.component_id for item in membership.components] == ["alpha", "beta"]
    assert observed.correlation.token == "observation-1"


def test_runtime_membership_does_not_derive_membership_from_the_binding() -> None:
    observed_handles = ("alpha-runtime", "beta-runtime")
    observer = RuntimeObserver(
        {
            observed_handles[0]: evidence(component("alpha")),
            observed_handles[1]: evidence(component("beta")),
        }
    )
    source = RuntimeMembershipSource(
        list_handles=lambda binding: observed_handles,
        observer=observer,
    )

    observed = source.observe_membership(
        PlatformIdentityBinding({"expected_components": ["alpha"]})
    )

    membership = observed.value
    assert isinstance(membership, ActualMembership)
    assert [item.component_id for item in membership.components] == ["alpha", "beta"]


def test_runtime_membership_refuses_duplicate_actual_components() -> None:
    handles = ("alpha-1", "alpha-2")
    observer = RuntimeObserver(
        {
            handles[0]: evidence(component("alpha")),
            handles[1]: evidence(component("alpha")),
        }
    )
    source = RuntimeMembershipSource(
        list_handles=lambda binding: handles,
        observer=observer,
    )

    with pytest.raises(ValueError, match="duplicate component"):
        source.observe_membership(BINDING)


def test_runtime_membership_refuses_an_empty_runtime_set() -> None:
    source = RuntimeMembershipSource(
        list_handles=lambda binding: (),
        observer=RuntimeObserver({}),
    )

    with pytest.raises(ValueError, match="membership is empty"):
        source.observe_membership(BINDING)


def test_runtime_membership_refuses_stale_component_evidence() -> None:
    stale = ActualEvidence(
        value=component("alpha"),
        provenance=EvidenceProvenance.MEASURED,
        correlation=EvidenceCorrelation("observation-1"),
        freshness=EvidenceFreshness(False),
    )
    source = RuntimeMembershipSource(
        list_handles=lambda binding: ("alpha-runtime",),
        observer=RuntimeObserver({"alpha-runtime": stale}),
    )

    with pytest.raises(ValueError, match="stale"):
        source.observe_membership(BINDING)


def test_runtime_membership_refuses_mixed_correlations() -> None:
    second = ActualEvidence(
        value=component("beta"),
        provenance=EvidenceProvenance.MEASURED,
        correlation=EvidenceCorrelation("observation-2"),
        freshness=EvidenceFreshness(True),
    )
    source = RuntimeMembershipSource(
        list_handles=lambda binding: ("alpha-runtime", "beta-runtime"),
        observer=RuntimeObserver(
            {
                "alpha-runtime": evidence(component("alpha")),
                "beta-runtime": second,
            }
        ),
    )

    with pytest.raises(ValueError, match="mixed correlation"):
        source.observe_membership(BINDING)
