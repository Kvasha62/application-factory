"""Focused tests for Slice C — Platform Manifest (Issue #66, ADR-0015 §15).

These tests verify architecture rules, not merely successful object
construction (ADR-0015 §16): normative schema, concrete versions, artifact
identity, manifest identity/version, lifecycle, predecessor, immutability,
authoritative Registry/Catalog consistency, reproducibility/determinism,
and the deterministic rejection of forbidden selectors and invalid
compositions.

The ``latest`` tests follow the Slice A/B doctrine: ``latest`` is not
forbidden as a string, it is forbidden as a *selector* — as a way of
choosing a version, a release, an artifact, a dependency or a production
target.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from component_registry import load_registry
from platform_manifest import (
    LIFECYCLE_ORDER,
    LIFECYCLE_STATES,
    ManifestNotFoundError,
    ManifestValidationError,
    build_manifest_document,
    canonical_json,
    check_immutability,
    compute_manifest_digest,
    discover_root,
    is_floating_selector,
    load_manifest,
    load_manifest_document,
    parse_semver,
    render_manifest_document,
    validate_document,
    validate_transition,
)
from platform_manifest.lifecycle import (
    is_allowed_transition,
    is_forward_transition,
    requires_approval,
    requires_publication,
)
from platform_manifest.schema import SCHEMA_PATH, load_schema

EXPECTED_INVENTORY = (
    "authorization",
    "identity",
    "tenant_authority",
    "records",
    "learning",
    "saga",
    "idempotency",
    "commerce",
    "booking",
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def registry(root: Path):
    return load_registry(root)


def _valid_components_from_registry(registry) -> list[dict]:
    comps = []
    for entry in registry.entries:
        comps.append(
            {
                "component_id": entry["component_id"],
                "component_version": entry["component_version"],
                "artifact": dict(entry["artifact"]),
            }
        )
    return sorted(comps, key=lambda e: e["component_id"])


@pytest.fixture(scope="module")
def valid_components(registry):
    return _valid_components_from_registry(registry)


@pytest.fixture(scope="module")
def minimal_manifest_doc(valid_components, root: Path) -> Mapping:
    # Minimal valid manifest: draft, 1 component
    doc = build_manifest_document(
        manifest_id="test-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=[valid_components[0]],
    )
    return doc


@pytest.fixture(scope="module")
def full_manifest_doc(valid_components, root: Path) -> Mapping:
    doc = build_manifest_document(
        manifest_id="education-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=valid_components,
    )
    return doc


def errors_for(document: object, root: Path) -> list[str]:
    return validate_document(document, root=root)


def rejects(document: object, root: Path, fragment: str) -> None:
    errors = errors_for(document, root)
    assert errors, "expected validation to fail, but document was accepted"
    assert any(
        fragment in err for err in errors
    ), f"expected error containing {fragment!r}, got:\n" + "\n".join(errors)


def with_manifest_field(document: Mapping, **fields) -> Mapping:
    clone = copy.deepcopy(document)
    clone.update(fields)
    # Recompute digest if not explicitly overridden
    if "manifest_digest" not in fields:
        # Compute digest over content excluding digest and $schema
        clone["manifest_digest"] = compute_manifest_digest(clone)
    return clone


def with_lifecycle(document: Mapping, state: str, **extra) -> Mapping:
    clone = copy.deepcopy(document)
    lifecycle = dict(clone.get("lifecycle", {}))
    lifecycle["state"] = state
    lifecycle.update(extra)
    clone["lifecycle"] = lifecycle
    # Adjust approval/publication to satisfy state requirements unless overridden
    if state in ("draft", "validated"):
        clone["approval"] = None
        clone["publication"] = None
    elif state == "approved":
        if clone.get("approval") is None:
            clone["approval"] = {
                "approved_by": "owner@example.com",
                "approved_at": "2026-09-15T00:00:00Z",
                "approval_id": "appr-001",
            }
        clone["publication"] = None
    elif state in ("published", "deployed", "superseded", "retired"):
        if clone.get("approval") is None:
            clone["approval"] = {
                "approved_by": "owner@example.com",
                "approved_at": "2026-09-15T00:00:00Z",
                "approval_id": "appr-001",
            }
        if clone.get("publication") is None:
            clone["publication"] = {
                "published_by": "publisher@example.com",
                "published_at": "2026-09-15T01:00:00Z",
                "publication_id": "pub-001",
            }
    # Recompute digest
    clone["manifest_digest"] = compute_manifest_digest(clone)
    return clone


def with_component_field(document: Mapping, target_id: str, **fields) -> Mapping:
    clone = copy.deepcopy(document)
    for comp in clone["components"]:
        if comp.get("component_id") == target_id:
            comp.update(fields)
            break
    else:
        raise AssertionError(f"no such component {target_id}")
    clone["manifest_digest"] = compute_manifest_digest(clone)
    return clone


def with_artifact_field(document: Mapping, target_id: str, **fields) -> Mapping:
    clone = copy.deepcopy(document)
    for comp in clone["components"]:
        if comp.get("component_id") == target_id:
            artifact = dict(comp.get("artifact", {}))
            artifact.update(fields)
            comp["artifact"] = artifact
            break
    else:
        raise AssertionError(f"no such component {target_id}")
    clone["manifest_digest"] = compute_manifest_digest(clone)
    return clone


def without_top_field(document: Mapping, key: str) -> Mapping:
    clone = copy.deepcopy(document)
    clone.pop(key, None)
    # Do not recompute digest here — we want to test missing field
    return clone


# ---------------------------------------------------------------------------
# Positive: schema and canonical manifest
# ---------------------------------------------------------------------------


def test_schema_exists(root: Path) -> None:
    assert (root / SCHEMA_PATH).is_file()
    schema = load_schema(root)
    assert isinstance(schema, Mapping)


def test_minimal_manifest_is_valid(minimal_manifest_doc, root: Path) -> None:
    assert errors_for(minimal_manifest_doc, root) == []


def test_full_manifest_is_valid(full_manifest_doc, root: Path) -> None:
    assert errors_for(full_manifest_doc, root) == []


def test_manifest_with_one_component_is_valid(root: Path, valid_components) -> None:
    doc = build_manifest_document(
        manifest_id="single-component-platform",
        manifest_version="0.1.0",
        lifecycle_state="draft",
        components=[valid_components[0]],
    )
    assert errors_for(doc, root) == []


def test_manifest_with_multiple_components_is_valid(
    root: Path, valid_components
) -> None:
    doc = build_manifest_document(
        manifest_id="multi-component-platform",
        manifest_version="0.1.0",
        lifecycle_state="draft",
        components=valid_components,
    )
    assert errors_for(doc, root) == []


def test_build_is_deterministic(valid_components, root: Path) -> None:
    doc1 = build_manifest_document(
        manifest_id="deterministic-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=valid_components,
    )
    doc2 = build_manifest_document(
        manifest_id="deterministic-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=valid_components,
    )
    assert doc1 == doc2
    assert compute_manifest_digest(doc1) == compute_manifest_digest(doc2)
    assert render_manifest_document(doc1) == render_manifest_document(doc2)


def test_validation_is_deterministic(full_manifest_doc, root: Path) -> None:
    first = errors_for(full_manifest_doc, root)
    second = errors_for(full_manifest_doc, root)
    third = errors_for(copy.deepcopy(full_manifest_doc), root)
    assert first == second == third == []


def test_digest_is_stable_and_matches_computed(full_manifest_doc) -> None:
    computed = compute_manifest_digest(full_manifest_doc)
    assert full_manifest_doc["manifest_digest"] == computed


def test_components_sorted_for_reproducibility(valid_components, root: Path) -> None:
    # Provide unsorted, builder should sort
    unsorted = list(reversed(valid_components))
    doc = build_manifest_document(
        manifest_id="sorted-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=unsorted,
    )
    ids = [c["component_id"] for c in doc["components"]]
    assert ids == sorted(ids)
    assert errors_for(doc, root) == []


def test_manifest_readme_exists(root: Path) -> None:
    assert (root / "factory" / "platform_manifest" / "README.md").is_file()


# ---------------------------------------------------------------------------
# Positive: identity and version
# ---------------------------------------------------------------------------


def test_manifest_identities_are_stable(full_manifest_doc) -> None:
    mid = full_manifest_doc["manifest_id"]
    assert mid == mid.strip().lower()
    assert is_floating_selector(mid) is False
    assert parse_semver(mid) is None


def test_manifest_versions_are_explicit_semver(full_manifest_doc) -> None:
    version = full_manifest_doc["manifest_version"]
    assert parse_semver(version) is not None
    assert is_floating_selector(version) is False


def test_manifest_digest_is_sha256(full_manifest_doc) -> None:
    digest = full_manifest_doc["manifest_digest"]
    assert digest.startswith("sha256:")
    assert len(digest) == 7 + 64


def test_lifecycle_states_are_valid() -> None:
    assert "draft" in LIFECYCLE_STATES
    assert "published" in LIFECYCLE_STATES
    assert len(LIFECYCLE_STATES) == 7
    assert LIFECYCLE_ORDER == (
        "draft",
        "validated",
        "approved",
        "published",
        "deployed",
        "superseded",
        "retired",
    )


# ---------------------------------------------------------------------------
# Positive: lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", LIFECYCLE_ORDER)
def test_all_lifecycle_states_are_accepted_when_metadata_satisfied(
    state: str, full_manifest_doc, root: Path
):
    doc = with_lifecycle(full_manifest_doc, state)
    assert (
        errors_for(doc, root) == []
    ), f"state {state} should be valid with proper metadata"


def test_lifecycle_forward_transition_is_allowed() -> None:
    assert is_forward_transition("draft", "validated") is True
    assert is_forward_transition("draft", "published") is True
    assert is_forward_transition("published", "draft") is False


def test_lifecycle_immediate_transition_is_allowed() -> None:
    assert is_allowed_transition("draft", "validated") is True
    assert is_allowed_transition("validated", "approved") is True
    assert is_allowed_transition("approved", "published") is True
    assert is_allowed_transition("published", "deployed") is True
    assert is_allowed_transition("deployed", "superseded") is True
    assert is_allowed_transition("superseded", "retired") is True


def test_lifecycle_same_state_is_allowed() -> None:
    assert is_allowed_transition("draft", "draft") is True
    assert is_allowed_transition("published", "published") is True


def test_lifecycle_skipping_is_not_allowed() -> None:
    assert is_allowed_transition("draft", "approved") is False
    assert is_allowed_transition("draft", "published") is False
    assert is_allowed_transition("validated", "published") is False


def test_validate_transition_reports_errors() -> None:
    assert validate_transition("draft", "validated") == []
    assert validate_transition("draft", "approved") != []
    assert "not allowed" in validate_transition("draft", "approved")[0]


def test_requires_approval_and_publication() -> None:
    assert requires_approval("draft") is False
    assert requires_approval("validated") is False
    assert requires_approval("approved") is True
    assert requires_approval("published") is True
    assert requires_publication("draft") is False
    assert requires_publication("approved") is False
    assert requires_publication("published") is True
    assert requires_publication("deployed") is True


# ---------------------------------------------------------------------------
# Positive: artifact identity
# ---------------------------------------------------------------------------


def test_artifact_identity_explicit_for_every_component(full_manifest_doc) -> None:
    for comp in full_manifest_doc["components"]:
        artifact = comp["artifact"]
        assert "artifact_type" in artifact
        assert "digest" in artifact
        assert "pinned" in artifact


def test_artifact_none_is_valid(full_manifest_doc, root: Path) -> None:
    # All components from registry have artifact_type none
    for comp in full_manifest_doc["components"]:
        assert comp["artifact"]["artifact_type"] == "none"
        assert comp["artifact"]["digest"] is None
        assert comp["artifact"]["pinned"] is False
    assert errors_for(full_manifest_doc, root) == []


def test_artifact_with_digest_is_valid_when_pinned(
    full_manifest_doc, root: Path
) -> None:
    digest = "sha256:" + "a" * 64
    doc = with_artifact_field(
        full_manifest_doc,
        full_manifest_doc["components"][0]["component_id"],
        artifact_type="container_image",
        digest=digest,
        pinned=True,
    )
    assert errors_for(doc, root) == []


# ---------------------------------------------------------------------------
# Positive: registry consistency
# ---------------------------------------------------------------------------


def test_components_exist_in_registry(full_manifest_doc, registry) -> None:
    for comp in full_manifest_doc["components"]:
        assert comp["component_id"] in registry.component_ids


def test_component_versions_match_registry(full_manifest_doc, registry) -> None:
    for comp in full_manifest_doc["components"]:
        assert comp["component_version"] == registry.version(comp["component_id"])


def test_manifest_with_golden_bundle_reference_is_valid(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["golden_bundle"] = {
        "bundle_id": "golden-bundle-v1",
        "bundle_version": "1.0.0",
        "bundle_digest": "sha256:" + "b" * 64,
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


def test_manifest_with_configuration_is_valid(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["configuration"] = {
        "learning": {"max_courses": 100},
        "commerce": {"currency": "USD"},
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


def test_manifest_with_extensions_is_valid(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["extensions"] = [{"extension_id": "custom_theme", "extension_version": "1.0.0"}]
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


def test_manifest_with_branding_is_valid(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["branding"] = {
        "logo_url": "https://example.com/logo.png",
        "primary_color": "#000000",
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


def test_manifest_with_validation_attestation_is_valid(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["validation_attestation"] = {
        "validated_by": "validator@example.com",
        "validated_at": "2026-09-15T00:00:00Z",
        "checks": ["schema", "registry"],
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


def test_manifest_with_predecessor_is_valid(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    # Change version to 1.0.1 and set predecessor to 1.0.0
    doc["manifest_version"] = "1.0.1"
    doc["predecessor"] = {
        "manifest_id": doc["manifest_id"],
        "manifest_version": "1.0.0",
        "manifest_digest": "sha256:" + "c" * 64,
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


# ---------------------------------------------------------------------------
# Negative: malformed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "not-a-manifest",
        42,
        {},
        {"manifest_id": "m", "manifest_version": "1.0.0"},
        {
            "manifest_id": "m",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:" + "a" * 64,
            "lifecycle": {"state": "draft"},
        },
    ],
)
def test_malformed_manifests_are_rejected(document: object, root: Path) -> None:
    assert errors_for(document, root)


def test_unknown_top_level_property_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["unexpected"] = "value"
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "unexpected property")


def test_missing_required_fields_are_rejected(full_manifest_doc, root: Path) -> None:
    for field in (
        "manifest_id",
        "manifest_version",
        "manifest_digest",
        "lifecycle",
        "components",
    ):
        doc = without_top_field(full_manifest_doc, field)
        # For digest field missing, we don't recompute, so error should be about missing
        errors = errors_for(doc, root)
        assert errors, f"should reject missing {field}"


# ---------------------------------------------------------------------------
# Negative: identity
# ---------------------------------------------------------------------------


def test_duplicate_component_identity_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["components"].append(copy.deepcopy(doc["components"][0]))
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "duplicate component identity")


def test_invalid_manifest_id_is_rejected(full_manifest_doc, root: Path) -> None:
    for bad_id in ["", "UPPER", "has space", "123start", "has-dash-"]:
        doc = with_manifest_field(full_manifest_doc, manifest_id=bad_id)
        assert errors_for(doc, root), f"should reject manifest_id {bad_id!r}"


def test_missing_manifest_id_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = without_top_field(full_manifest_doc, "manifest_id")
    rejects(doc, root, "manifest_id")


# ---------------------------------------------------------------------------
# Negative: version — including mandatory latest rejection
# ---------------------------------------------------------------------------


def test_latest_is_rejected_as_manifest_version(full_manifest_doc, root: Path) -> None:
    doc = with_manifest_field(full_manifest_doc, manifest_version="latest")
    rejects(doc, root, "floating selector")


@pytest.mark.parametrize(
    "version", ["latest", "LATEST", " current ", "default", "stable"]
)
def test_floating_selectors_are_rejected_as_manifest_versions(
    version: str, full_manifest_doc, root: Path
) -> None:
    doc = with_manifest_field(full_manifest_doc, manifest_version=version)
    rejects(doc, root, "floating selector")


@pytest.mark.parametrize(
    "version", ["1.0", "v1.0.0", "01.0.0", "1.0.0-beta", "", "1..0"]
)
def test_malformed_versions_are_rejected(
    version: str, full_manifest_doc, root: Path
) -> None:
    doc = with_manifest_field(full_manifest_doc, manifest_version=version)
    assert errors_for(doc, root)


def test_latest_is_rejected_as_component_version(full_manifest_doc, root: Path) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_component_field(full_manifest_doc, target, component_version="latest")
    rejects(doc, root, "floating selector")


@pytest.mark.parametrize(
    "version", ["latest", "current", "default", "stable", "*", "0.1.*"]
)
def test_floating_and_malformed_component_versions_are_rejected(
    version: str, full_manifest_doc, root: Path
) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_component_field(full_manifest_doc, target, component_version=version)
    assert errors_for(doc, root)


def test_component_version_must_match_registry(full_manifest_doc, root: Path) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_component_field(full_manifest_doc, target, component_version="9.9.9")
    rejects(doc, root, "canonical registry declares")


# ---------------------------------------------------------------------------
# Negative: artifact
# ---------------------------------------------------------------------------


def test_latest_is_rejected_as_artifact_digest(full_manifest_doc, root: Path) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_artifact_field(
        full_manifest_doc,
        target,
        artifact_type="container_image",
        digest="latest",
        pinned=True,
    )
    rejects(doc, root, "floating selector")


def test_unpinned_published_artifact_is_rejected(full_manifest_doc, root: Path) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_artifact_field(
        full_manifest_doc,
        target,
        artifact_type="container_image",
        digest="sha256:" + "a" * 64,
        pinned=False,
    )
    rejects(doc, root, "must be pinned by digest")


def test_published_artifact_without_digest_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_artifact_field(
        full_manifest_doc,
        target,
        artifact_type="container_image",
        digest=None,
        pinned=True,
    )
    rejects(doc, root, "requires an immutable digest")


def test_digest_on_none_artifact_is_rejected(full_manifest_doc, root: Path) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_artifact_field(
        full_manifest_doc,
        target,
        artifact_type="none",
        digest="sha256:" + "b" * 64,
        pinned=True,
    )
    rejects(doc, root, "must not declare a digest")


def test_invalid_artifact_type_is_rejected(full_manifest_doc, root: Path) -> None:
    target = full_manifest_doc["components"][0]["component_id"]
    doc = with_artifact_field(full_manifest_doc, target, artifact_type="tarball")
    assert errors_for(doc, root)


# ---------------------------------------------------------------------------
# Negative: lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["certified", "deployable", "active", "latest", ""])
def test_invalid_lifecycle_states_are_rejected(
    state: str, full_manifest_doc, root: Path
) -> None:
    doc = with_lifecycle(full_manifest_doc, state)
    rejects(doc, root, "lifecycle.state")


def test_latest_is_rejected_as_lifecycle_state(full_manifest_doc, root: Path) -> None:
    doc = with_lifecycle(full_manifest_doc, "latest")
    rejects(doc, root, "floating selector")


def test_approval_required_for_approved_state(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["lifecycle"] = {"state": "approved"}
    doc["approval"] = None
    doc["publication"] = None
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "approval metadata is required")


def test_publication_required_for_published_state(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["lifecycle"] = {"state": "published"}
    doc["approval"] = {
        "approved_by": "owner@example.com",
        "approved_at": "2026-09-15T00:00:00Z",
        "approval_id": "appr-001",
    }
    doc["publication"] = None
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "publication metadata is required")


def test_approval_must_be_null_for_draft(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["lifecycle"] = {"state": "draft"}
    doc["approval"] = {
        "approved_by": "owner@example.com",
        "approved_at": "2026-09-15T00:00:00Z",
        "approval_id": "appr-001",
    }
    doc["publication"] = None
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "must be null when lifecycle is 'draft'")


def test_publication_must_be_null_for_approved(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["lifecycle"] = {"state": "approved"}
    doc["approval"] = {
        "approved_by": "owner@example.com",
        "approved_at": "2026-09-15T00:00:00Z",
        "approval_id": "appr-001",
    }
    doc["publication"] = {
        "published_by": "pub@example.com",
        "published_at": "2026-09-15T01:00:00Z",
        "publication_id": "pub-001",
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "must be null when lifecycle is approved")


# ---------------------------------------------------------------------------
# Negative: predecessor
# ---------------------------------------------------------------------------


def test_predecessor_self_reference_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["predecessor"] = {
        "manifest_id": doc["manifest_id"],
        "manifest_version": doc["manifest_version"],
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "must not be self")


def test_predecessor_version_must_be_less(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["manifest_version"] = "1.0.0"
    doc["predecessor"] = {
        "manifest_id": doc["manifest_id"],
        "manifest_version": "1.0.1",
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "must be less than current version")


def test_predecessor_with_floating_selector_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["predecessor"] = {"manifest_id": "test-platform", "manifest_version": "latest"}
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


def test_predecessor_with_invalid_digest_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["predecessor"] = {
        "manifest_id": "test-platform",
        "manifest_version": "0.9.0",
        "manifest_digest": "latest",
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


# ---------------------------------------------------------------------------
# Negative: registry consistency
# ---------------------------------------------------------------------------


def test_unknown_component_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["components"].append(
        {
            "component_id": "payments",
            "component_version": "1.0.0",
            "artifact": {"artifact_type": "none", "digest": None, "pinned": False},
        }
    )
    doc["components"] = sorted(doc["components"], key=lambda e: e["component_id"])
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "is not registered in the canonical Component Registry")


def test_empty_components_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["components"] = []
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "expected at least 1 item")


def test_unsorted_components_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    # Reverse order to make unsorted
    doc["components"] = list(reversed(doc["components"]))
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "must be sorted by component_id")


# ---------------------------------------------------------------------------
# Negative: digest mismatch
# ---------------------------------------------------------------------------


def test_digest_mismatch_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["manifest_digest"] = "sha256:" + "0" * 64
    rejects(doc, root, "does not match computed digest")


def test_tampered_content_with_same_digest_is_rejected_via_immutability_check(
    full_manifest_doc,
) -> None:
    # Build two manifests with same id/version but different content
    doc1 = copy.deepcopy(full_manifest_doc)
    doc2 = copy.deepcopy(full_manifest_doc)
    # Change component version in doc2 but keep digest of doc1 (simulate tampering)
    target = doc2["components"][0]["component_id"]
    for comp in doc2["components"]:
        if comp["component_id"] == target:
            comp["component_version"] = "9.9.9"
            break
    # Keep digest from doc1
    doc2["manifest_digest"] = doc1["manifest_digest"]

    # Simulate published store
    doc1_published = with_lifecycle(doc1, "published")
    errors = check_immutability([doc1_published], doc2)
    assert errors, "immutability check should fail when content differs"


# ---------------------------------------------------------------------------
# Negative: golden bundle
# ---------------------------------------------------------------------------


def test_golden_bundle_with_floating_version_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["golden_bundle"] = {
        "bundle_id": "golden-bundle-v1",
        "bundle_version": "latest",
        "bundle_digest": "sha256:" + "a" * 64,
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


def test_golden_bundle_with_invalid_digest_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["golden_bundle"] = {
        "bundle_id": "golden-bundle-v1",
        "bundle_version": "1.0.0",
        "bundle_digest": "latest",
    }
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


# ---------------------------------------------------------------------------
# Boundary: minimal and maximal
# ---------------------------------------------------------------------------


def test_minimal_manifest_one_component(root: Path, valid_components) -> None:
    doc = build_manifest_document(
        manifest_id="minimal-platform",
        manifest_version="0.1.0",
        lifecycle_state="draft",
        components=[valid_components[0]],
    )
    assert errors_for(doc, root) == []


def test_maximal_manifest_all_components_with_optional_fields(
    root: Path, valid_components
) -> None:
    doc = build_manifest_document(
        manifest_id="maximal-platform",
        manifest_version="1.2.3",
        lifecycle_state="published",
        components=valid_components,
        predecessor={
            "manifest_id": "maximal-platform",
            "manifest_version": "1.2.2",
            "manifest_digest": "sha256:" + "a" * 64,
        },
        golden_bundle={
            "bundle_id": "golden-bundle",
            "bundle_version": "1.0.0",
            "bundle_digest": "sha256:" + "b" * 64,
        },
        configuration={"key": "value"},
        extensions=[{"extension_id": "ext_one", "extension_version": "1.0.0"}],
        branding={"logo": "url"},
        validation_attestation={
            "validated_by": "validator",
            "validated_at": "2026-09-15T00:00:00Z",
            "checks": ["schema"],
        },
        approval={
            "approved_by": "approver",
            "approved_at": "2026-09-15T00:00:00Z",
            "approval_id": "appr-1",
        },
        publication={
            "published_by": "publisher",
            "published_at": "2026-09-15T01:00:00Z",
            "publication_id": "pub-1",
        },
    )
    assert errors_for(doc, root) == []


def test_empty_configuration_is_valid(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["configuration"] = {}
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


def test_empty_extensions_is_valid(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["extensions"] = []
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root) == []


def test_duplicate_extensions_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["extensions"] = [
        {"extension_id": "ext_one"},
        {"extension_id": "ext_one"},
    ]
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "duplicate extension identity")


# ---------------------------------------------------------------------------
# Adversarial: hidden floating selectors
# ---------------------------------------------------------------------------


def test_latest_inside_nested_configuration_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["configuration"] = {"component": {"version": "latest"}}
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


def test_latest_in_unexpected_field_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["configuration"] = {"some_key": "current"}
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


def test_hidden_floating_dependency_in_configuration_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["configuration"] = {"nested": {"deep": {"selector": "stable"}}}
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


def test_floating_selector_in_branding_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["branding"] = {"theme": "latest"}
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


def test_floating_selector_in_extension_version_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["extensions"] = [{"extension_id": "ext_one", "extension_version": "latest"}]
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "floating selector")


def test_duplicate_component_entries_are_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    # Duplicate first component
    dup = copy.deepcopy(doc["components"][0])
    doc["components"].append(dup)
    doc["components"] = sorted(doc["components"], key=lambda e: e["component_id"])
    # After sorting, duplicate will be adjacent, but still duplicate id
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "duplicate component identity")


def test_digest_substitution_with_same_other_fields_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    # Change digest but keep artifact_type none (should fail)
    target = doc["components"][0]["component_id"]
    for comp in doc["components"]:
        if comp["component_id"] == target:
            comp["artifact"] = {
                "artifact_type": "none",
                "digest": "sha256:" + "a" * 64,
                "pinned": False,
            }
            break
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "must not declare a digest")


def test_published_manifest_immutability_enforced() -> None:
    # Create a published manifest
    from platform_manifest import build_manifest_document

    root = discover_root()
    registry = load_registry(root)
    comps = _valid_components_from_registry(registry)

    doc_v1 = build_manifest_document(
        manifest_id="immutable-platform",
        manifest_version="1.0.0",
        lifecycle_state="published",
        components=comps,
        approval={
            "approved_by": "owner",
            "approved_at": "2026-09-15T00:00:00Z",
            "approval_id": "appr-1",
        },
        publication={
            "published_by": "pub",
            "published_at": "2026-09-15T01:00:00Z",
            "publication_id": "pub-1",
        },
    )

    # Try to publish same id/version with different components
    comps2 = [comps[0]]  # fewer components
    doc_v1_tampered = build_manifest_document(
        manifest_id="immutable-platform",
        manifest_version="1.0.0",
        lifecycle_state="published",
        components=comps2,
        approval={
            "approved_by": "owner",
            "approved_at": "2026-09-15T00:00:00Z",
            "approval_id": "appr-1",
        },
        publication={
            "published_by": "pub",
            "published_at": "2026-09-15T01:00:00Z",
            "publication_id": "pub-1",
        },
    )

    errors = check_immutability([doc_v1], doc_v1_tampered)
    assert errors, "should reject rewriting published manifest"
    assert "immutable" in errors[0]


def test_changing_published_manifest_without_changing_file_is_rejected() -> None:
    # Simulate that manifest file content changed but path same
    root = discover_root()
    registry = load_registry(root)
    comps = _valid_components_from_registry(registry)

    doc = build_manifest_document(
        manifest_id="change-detect-platform",
        manifest_version="1.0.0",
        lifecycle_state="published",
        components=comps,
        approval={
            "approved_by": "owner",
            "approved_at": "2026-09-15T00:00:00Z",
            "approval_id": "appr-1",
        },
        publication={
            "published_by": "pub",
            "published_at": "2026-09-15T01:00:00Z",
            "publication_id": "pub-1",
        },
    )

    # Tamper: change component version but keep same digest (simulate file edit without digest update)
    tampered = copy.deepcopy(doc)
    tampered["components"][0]["component_version"] = "9.9.9"
    # Keep old digest to simulate not updating digest
    # Validation should catch digest mismatch
    errors = validate_document(tampered, root=root)
    assert any("does not match computed digest" in e for e in errors)


def test_decoy_metadata_source_is_rejected(full_manifest_doc, root: Path) -> None:
    # Try to reference component that is not in canonical registry but looks valid
    doc = copy.deepcopy(full_manifest_doc)
    doc["components"].append(
        {
            "component_id": "decoy_component",
            "component_version": "1.0.0",
            "artifact": {"artifact_type": "none", "digest": None, "pinned": False},
        }
    )
    doc["components"] = sorted(doc["components"], key=lambda e: e["component_id"])
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "is not registered")


def test_additional_unknown_fields_in_component_are_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["components"][0]["unexpected"] = "field"
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "unexpected property")


def test_null_instead_of_object_is_rejected(full_manifest_doc, root: Path) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["components"][0]["artifact"] = None
    doc["manifest_digest"] = compute_manifest_digest(doc)
    rejects(doc, root, "expected type")


def test_empty_string_as_component_id_is_rejected(
    full_manifest_doc, root: Path
) -> None:
    doc = copy.deepcopy(full_manifest_doc)
    doc["components"][0]["component_id"] = ""
    doc["manifest_digest"] = compute_manifest_digest(doc)
    assert errors_for(doc, root)


# ---------------------------------------------------------------------------
# latest audit: ensure no production usage of latest
# ---------------------------------------------------------------------------


def test_latest_is_allowed_in_error_messages_but_not_as_selector(
    full_manifest_doc, root: Path
) -> None:
    # The word latest in prose should not be rejected as selector when it's not in selector field
    # But our recursive check will reject it even in config if it's a value.
    # That's intentional: config should not contain floating selectors.
    # For this test, we check that error messages themselves contain the word latest
    # (allowed) but selector fields reject it.

    doc = with_component_field(
        full_manifest_doc,
        full_manifest_doc["components"][0]["component_id"],
        component_version="latest",
    )
    errors = errors_for(doc, root)
    assert errors
    # Error message contains the word latest (allowed in prose)
    assert any("latest" in e for e in errors)
    # But the document is rejected because latest is used as selector
    assert any("floating selector" in e for e in errors)


def test_no_floating_selector_in_canonical_valid_manifests(
    root: Path, valid_components
) -> None:
    doc = build_manifest_document(
        manifest_id="audit-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=valid_components,
    )
    # Ensure no selector field contains floating token
    for comp in doc["components"]:
        assert is_floating_selector(comp["component_id"]) is False
        assert is_floating_selector(comp["component_version"]) is False
        assert is_floating_selector(comp["artifact"]["artifact_type"]) is False
        assert is_floating_selector(comp["artifact"]["digest"]) is False
    assert is_floating_selector(doc["manifest_id"]) is False
    assert is_floating_selector(doc["manifest_version"]) is False
    assert is_floating_selector(doc["manifest_digest"]) is False
    assert is_floating_selector(doc["lifecycle"]["state"]) is False


# ---------------------------------------------------------------------------
# Reproducibility and determinism
# ---------------------------------------------------------------------------


def test_same_manifest_same_registry_yields_same_validation(
    root: Path, valid_components
) -> None:
    doc = build_manifest_document(
        manifest_id="repro-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=valid_components,
    )
    errors1 = validate_document(doc, root=root)
    errors2 = validate_document(copy.deepcopy(doc), root=root)
    assert errors1 == errors2 == []


def test_different_component_order_yields_different_digest_but_sorted_enforced(
    root: Path, valid_components
) -> None:
    # Build with sorted (valid)
    doc_sorted = build_manifest_document(
        manifest_id="order-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=valid_components,
    )
    # Manually create unsorted doc with same components but different order
    unsorted_comps = list(reversed(valid_components))
    doc_unsorted = copy.deepcopy(doc_sorted)
    doc_unsorted["components"] = unsorted_comps
    doc_unsorted["manifest_digest"] = compute_manifest_digest(doc_unsorted)

    # Unsorted should be rejected (determinism enforcement)
    assert errors_for(doc_unsorted, root)

    # Sorted should be valid
    assert errors_for(doc_sorted, root) == []

    # Digests differ because order differs (before sorting enforcement)
    assert doc_sorted["manifest_digest"] != doc_unsorted["manifest_digest"]


def test_canonical_json_is_deterministic() -> None:
    obj = {"b": 2, "a": 1, "c": {"z": 3, "y": 2}}
    first = canonical_json(obj)
    second = canonical_json(obj)
    assert first == second
    # Ensure sorted keys
    assert first.index('"a"') < first.index('"b"')
    assert first.index('"b"') < first.index('"c"')


def test_digest_computation_is_deterministic(full_manifest_doc) -> None:
    d1 = compute_manifest_digest(full_manifest_doc)
    d2 = compute_manifest_digest(copy.deepcopy(full_manifest_doc))
    assert d1 == d2


# ---------------------------------------------------------------------------
# Boundary: loading failures
# ---------------------------------------------------------------------------


def test_missing_manifest_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ManifestNotFoundError):
        load_manifest_document(tmp_path / "absent.json")


def test_unreadable_manifest_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ManifestNotFoundError):
        load_manifest_document(p)


def test_non_object_manifest_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestNotFoundError):
        load_manifest_document(p)


def test_strict_loading_refuses_invalid_manifest(
    tmp_path: Path, full_manifest_doc
) -> None:
    p = tmp_path / "manifest.json"
    invalid = copy.deepcopy(full_manifest_doc)
    invalid["components"][0]["component_version"] = "latest"
    invalid["manifest_digest"] = compute_manifest_digest(invalid)
    p.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ManifestValidationError):
        load_manifest(p)


def test_non_strict_loading_reports_violations(
    tmp_path: Path, full_manifest_doc
) -> None:
    p = tmp_path / "manifest.json"
    invalid = copy.deepcopy(full_manifest_doc)
    invalid["components"][0]["component_version"] = "latest"
    invalid["manifest_digest"] = compute_manifest_digest(invalid)
    p.write_text(json.dumps(invalid), encoding="utf-8")
    loaded = load_manifest(p, strict=False)
    assert loaded.validate()


# ---------------------------------------------------------------------------
# Architecture boundaries: no DB, no internal imports
# ---------------------------------------------------------------------------


def test_manifest_implementation_imports_only_allowed_modules(root: Path) -> None:
    import ast

    package = root / "src" / "platform_manifest"
    sources = sorted(package.glob("*.py"))
    assert sources, "platform_manifest package must exist"

    allowed_roots = frozenset(
        {
            "__future__",
            "argparse",
            "collections",
            "dataclasses",
            "hashlib",
            "json",
            "pathlib",
            "re",
            "sys",
            "typing",
            "component_registry",
            "component_catalog",
            "platform_manifest",
        }
    )
    forbidden_roots = frozenset(
        {
            "authorization_service",
            "booking_service",
            "commerce_service",
            "identity_service",
            "learning_service",
            "records_service",
            "tenant_authority",
            "saga",
            "idempotency",
            "sqlite3",
            "psycopg",
            "subprocess",
            "os",
        }
    )

    for src in sources:
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                root_mod = name.split(".")[0]
                assert (
                    root_mod in allowed_roots
                ), f"{src.name} imports {name!r}, not allowed"
                assert root_mod not in forbidden_roots


def test_manifest_never_touches_component_database(root: Path) -> None:
    package = root / "src" / "platform_manifest"
    for src in package.glob("*.py"):
        text = src.read_text(encoding="utf-8").lower()
        for kw in ("sqlite", "psycopg", ".execute(", "cursor(", "create table"):
            assert kw not in text, (src.name, kw)
