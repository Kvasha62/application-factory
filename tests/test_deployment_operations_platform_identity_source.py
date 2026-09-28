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
    ComposedRunningPlatformIdentitySource,
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
    value: object

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        assert binding.token == "running-platform"
        return self.value  # type: ignore[return-value]


def provider(value: ActualPlatformSnapshot):
    return OwnerSuppliedPlatformIdentityProvider(Source(value))


def test_provider_preserves_owner_supplied_actual_identity():
    evidence = provider(snapshot()).observe_identity(
        PlatformIdentityBinding("running-platform")
    )
    assert evidence.value.platform_id == "example-platform"
    assert evidence.value.components[0].value.component_id == "alpha"


def test_malformed_owner_snapshot_is_unavailable():
    class MalformedSource:
        def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
            return None  # type: ignore[return-value]

    with pytest.raises(ActualIdentityUnavailable, match="invalid snapshot"):
        OwnerSuppliedPlatformIdentityProvider(MalformedSource()).observe_identity(
            PlatformIdentityBinding("running-platform")
        )


def test_owner_source_failure_is_unavailable():
    class FailingSource:
        def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
            raise RuntimeError("source unavailable")

    with pytest.raises(ActualIdentityUnavailable, match="invalid actual evidence"):
        OwnerSuppliedPlatformIdentityProvider(FailingSource()).observe_identity(
            PlatformIdentityBinding("running-platform")
        )


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


@dataclass
class EvidenceSource:
    values: dict[str, object]

    def _evidence(self, key: str) -> object:
        from deployment_operations.platform_identity import (
            ActualEvidence,
            EvidenceCorrelation,
            EvidenceFreshness,
        )
        return ActualEvidence(
            value=self.values[key],
            provenance=EvidenceProvenance.MEASURED,
            correlation=EvidenceCorrelation("evaluation-1"),
            freshness=EvidenceFreshness(True),
        )

    def observe_membership(self, binding: PlatformIdentityBinding) -> object:
        return self._evidence("membership")

    def observe_manifest(self, binding: PlatformIdentityBinding) -> object:
        return self._evidence("manifest")

    def observe_configuration(self, binding: PlatformIdentityBinding) -> object:
        return self._evidence("configuration")

    def observe_golden_bundle(self, binding: PlatformIdentityBinding) -> object:
        return self._evidence("golden")

    def observe_extensions(self, binding: PlatformIdentityBinding) -> object:
        return self._evidence("extensions")

    def observe_branding(self, binding: PlatformIdentityBinding) -> object:
        return self._evidence("branding")


def test_composed_owner_sources_build_one_actual_snapshot() -> None:
    from running_platform.identity_sources import (
        ActualGoldenBundle,
        ActualManifest,
        ActualMembership,
    )

    source = EvidenceSource(
        {
            "membership": ActualMembership(True, (component("alpha"),)),
            "manifest": ActualManifest(
                {
                    "manifest_id": "example-manifest",
                    "manifest_version": "1.0.0",
                    "manifest_digest": "sha256:" + "1" * 64,
                },
                "published",
            ),
            "configuration": IdentityField.present(
                {"alpha": {"platform_id": "example-platform"}}
            ),
            "golden": ActualGoldenBundle(IdentityField.absent(), True),
            "extensions": IdentityField.absent(),
            "branding": IdentityField.absent(),
        }
    )

    composed = ComposedRunningPlatformIdentitySource(
        membership=source,
        manifest=source,
        configuration=source,
        golden_bundle=source,
        extensions=source,
        branding=source,
    )
    actual = composed.observe(PlatformIdentityBinding("running-platform"))

    assert actual.platform_id == "example-platform"
    assert actual.components == (component("alpha"),)
    assert actual.manifest_state == "published"
    assert actual.golden_bundle_inventory_established is True


def test_composed_owner_sources_refuse_mixed_correlation() -> None:
    from deployment_operations.platform_identity import (
        ActualEvidence,
        EvidenceCorrelation,
        EvidenceFreshness,
    )
    from running_platform.identity_sources import ActualManifest, ActualMembership

    class MixedManifestSource(EvidenceSource):
        def observe_manifest(self, binding: PlatformIdentityBinding) -> object:
            return ActualEvidence(
                value=ActualManifest(
                    {
                        "manifest_id": "example-manifest",
                        "manifest_version": "1.0.0",
                        "manifest_digest": "sha256:" + "1" * 64,
                    },
                    "published",
                ),
                provenance=EvidenceProvenance.MEASURED,
                correlation=EvidenceCorrelation("other-evaluation"),
                freshness=EvidenceFreshness(True),
            )

    values = {
        "membership": ActualMembership(True, (component("alpha"),)),
        "manifest": ActualManifest(
            {
                "manifest_id": "example-manifest",
                "manifest_version": "1.0.0",
                "manifest_digest": "sha256:" + "1" * 64,
            },
            "published",
        ),
        "configuration": IdentityField.present(
            {"alpha": {"platform_id": "example-platform"}}
        ),
        "golden": ActualGoldenBundle(IdentityField.absent(), True),
        "extensions": IdentityField.absent(),
        "branding": IdentityField.absent(),
    }
    base = EvidenceSource(values)
    source = MixedManifestSource(values)

    composed = ComposedRunningPlatformIdentitySource(
        membership=base,
        manifest=source,
        configuration=base,
        golden_bundle=base,
        extensions=base,
        branding=base,
    )

    with pytest.raises(ActualIdentityUnavailable, match="mixed correlation"):
        composed.observe(PlatformIdentityBinding("running-platform"))
