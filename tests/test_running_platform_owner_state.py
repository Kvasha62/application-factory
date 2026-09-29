from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualIdentityUnavailable,
    EvidenceProvenance,
    IdentityField,
    PlatformIdentityBinding,
)
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from running_platform.identity_sources import (
    ActualGoldenBundle,
    ActualManifest,
    ActualMembership,
)
from running_platform.owner_state import (
    FileRunningPlatformOwnerStateReader,
    OwnerStateSnapshotSource,
    OwnerStateSource,
    RunningPlatformOwnerState,
)

BINDING = PlatformIdentityBinding("evaluation-1")


def component(component_id: str = "authorization") -> ActualComponentIdentity:
    return ActualComponentIdentity(
        component_id=component_id,
        component_version="0.1.0",
        artifact_identity={
            "artifact_type": "none",
            "digest": None,
            "pinned": False,
            "canonical_form": None,
        },
    )


def state(**overrides: object) -> RunningPlatformOwnerState:
    values: dict[str, object] = {
        "platform_id": "example-platform",
        "membership": ActualMembership(True, (component(),)),
        "manifest": ActualManifest(
            {
                "manifest_id": "example-manifest",
                "manifest_version": "1.0.0",
                "manifest_digest": "sha256:" + "1" * 64,
            },
            "published",
        ),
        "configuration": IdentityField.present(
            {"identity": {"platform_id": "example-platform"}}
        ),
        "golden_bundle": ActualGoldenBundle(IdentityField.absent(), True),
        "extensions": IdentityField.absent(),
        "branding": IdentityField.absent(),
        "provenance": EvidenceProvenance.MEASURED,
        "correlation_token": "evaluation-1",
        "freshness_current": True,
    }
    values.update(overrides)
    return RunningPlatformOwnerState(**values)


@dataclass
class Reader:
    value: RunningPlatformOwnerState

    def read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
        assert binding is BINDING
        return self.value


def test_owner_state_sources_receive_only_the_opaque_binding() -> None:
    reader = Reader(state())
    source = OwnerStateSource(reader)

    observed = source.observe_membership(BINDING)

    assert observed.value == state().membership
    assert reader.value.platform_id == "example-platform"


def test_snapshot_source_composes_the_existing_snapshot_contract() -> None:
    snapshot = OwnerStateSnapshotSource(Reader(state())).observe(BINDING)

    assert snapshot.platform_id == "example-platform"
    assert snapshot.manifest["manifest_id"] == "example-manifest"
    assert snapshot.components == (component(),)
    assert snapshot.membership_established is True
    assert snapshot.correlation_token == "evaluation-1"
    assert snapshot.freshness_current is True


def test_snapshot_source_preserves_duplicate_membership_for_validation() -> None:
    duplicate = component()
    owner_state = state(
        membership=ActualMembership(True, (duplicate, duplicate)),
    )

    provider = OwnerSuppliedPlatformIdentityProvider(
        OwnerStateSnapshotSource(Reader(owner_state))
    )

    with pytest.raises(ActualIdentityUnavailable, match="duplicate"):
        provider.observe_identity(BINDING)


def test_snapshot_source_rejects_stale_owner_state() -> None:
    provider = OwnerSuppliedPlatformIdentityProvider(
        OwnerStateSnapshotSource(Reader(state(freshness_current=False)))
    )

    with pytest.raises(ActualIdentityUnavailable, match="stale"):
        provider.observe_identity(BINDING)


def test_snapshot_source_rejects_unknown_configuration() -> None:
    provider = OwnerSuppliedPlatformIdentityProvider(
        OwnerStateSnapshotSource(Reader(state(configuration=IdentityField.unknown())))
    )

    with pytest.raises(ActualIdentityUnavailable):
        provider.observe_identity(BINDING)


def test_snapshot_source_contains_no_expected_identity() -> None:
    snapshot = OwnerStateSnapshotSource(Reader(state())).observe(BINDING)

    assert not hasattr(snapshot, "instance_digest")
    assert not hasattr(snapshot, "expected_instance")


def test_complete_owner_state_reaches_existing_provider() -> None:
    evidence = OwnerSuppliedPlatformIdentityProvider(
        OwnerStateSnapshotSource(Reader(state()))
    ).observe_identity(BINDING)

    assert evidence.value.platform_id == "example-platform"
    assert evidence.value.components[0].value.component_id == "authorization"


def _surface_document(token: object) -> dict[str, object]:
    return {
        "platform_id": "example-platform",
        "membership_established": True,
        "components": [
            {
                "component_id": "authorization",
                "component_version": "0.1.0",
                "artifact_identity": {
                    "artifact_type": "none",
                    "digest": None,
                    "pinned": False,
                    "canonical_form": None,
                },
            }
        ],
        "manifest": {
            "manifest_id": "example-manifest",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:" + "1" * 64,
        },
        "manifest_state": "published",
        "configuration": {
            "state": "PRESENT",
            "value": {"identity": {"platform_id": "example-platform"}},
        },
        "golden_bundle": {"state": "ABSENT"},
        "golden_bundle_inventory_established": True,
        "extensions": {"state": "ABSENT"},
        "branding": {"state": "ABSENT"},
        "provenance": "MEASURED",
        "correlation_token": token,
        "freshness_current": True,
    }


def test_file_reader_accepts_independent_actual_identity_surface(tmp_path) -> None:
    path = tmp_path / "running_platform_identity.json"
    path.write_text(
        json.dumps(_surface_document("evaluation-1")),
        encoding="utf-8",
    )

    observed = FileRunningPlatformOwnerStateReader(path).read(BINDING)

    assert observed.platform_id == "example-platform"
    assert observed.membership.components[0].component_id == "authorization"
    assert observed.correlation_token == "evaluation-1"


def test_file_reader_rejects_expected_identity_contamination(tmp_path) -> None:
    document = _surface_document("evaluation-1")
    document["instance_digest"] = "sha256:" + "a" * 64
    path = tmp_path / "running_platform_identity.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ActualIdentityUnavailable, match="expected identity"):
        FileRunningPlatformOwnerStateReader(path).read(BINDING)


def test_file_reader_rejects_foreign_correlation(tmp_path) -> None:
    path = tmp_path / "running_platform_identity.json"
    path.write_text(
        json.dumps(_surface_document("different-evaluation")),
        encoding="utf-8",
    )

    with pytest.raises(ActualIdentityUnavailable, match="stale or foreign"):
        FileRunningPlatformOwnerStateReader(path).read(BINDING)


def test_file_reader_rejects_missing_surface(tmp_path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(ActualIdentityUnavailable, match="unavailable"):
        FileRunningPlatformOwnerStateReader(path).read(BINDING)
