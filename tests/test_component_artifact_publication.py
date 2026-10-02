"""Positive and adversarial tests for the nine-component artifact publication
pipeline, OCI packaging, GHCR publication seam, dependency closure validation,
and fail-closed registry reconciliation (Issue #127).
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlparse

import pytest

from component_registry import (
    REGISTRY_PATH,
    load_registry,
    load_registry_document,
    validate_document,
)
from factory_artifact import canonical_manifest_reference
from scripts.build_component_artifacts import (
    APPROVED_COMPONENT_IDS,
    CANONICAL_MANIFEST_MEDIA_TYPE,
    COMPONENT_ROOTS,
    COMPONENT_VERSION_DIGESTS,
    WORKFLOW_PATH,
    DependencyClosureError,
    GHCRHTTPTransport,
    InMemoryOCIRegistry,
    PublicationBlockedError,
    PublicationRecord,
    PublicationVerificationError,
    _RefuseRedirects,
    build_all,
    declaration_for,
    dependency_closure_violations,
    extract_workflow_trigger_paths,
    load_publication_layout,
    main,
    publication_violations,
    publish_artifacts_to_oci,
    reconcile_registry,
    redact_secrets,
    validate_dependency_closure,
    validate_workflow_trigger_coverage,
    verify_immutable_version_digest,
)

EXPECTED_COMPONENTS = {
    "authorization",
    "identity",
    "tenant_authority",
    "records",
    "learning",
    "saga",
    "idempotency",
    "commerce",
    "booking",
}

FORGED_DIGEST = "sha256:" + "f" * 64


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path.cwd()


@pytest.fixture(scope="module")
def registry_doc(repo_root: Path) -> dict[str, Any]:
    return dict(load_registry_document(repo_root / REGISTRY_PATH))


@pytest.fixture(scope="module")
def built_suite(
    tmp_path_factory: pytest.TempPathFactory, repo_root: Path
) -> tuple[Path, list[dict[str, Any]], list[PublicationRecord]]:
    """Build and publish all 9 artifacts into an in-memory OCI registry once."""
    artifacts_dir = tmp_path_factory.mktemp("published_artifacts")
    results = build_all(repo_root, artifacts_dir)
    oci_registry = InMemoryOCIRegistry()
    records = publish_artifacts_to_oci(
        results, artifacts_root=artifacts_dir, transport=oci_registry
    )
    return artifacts_dir, results, records


# ---------------------------------------------------------------------------
# Positive: Approved 9-component scope, deterministic canonical + OCI build
# ---------------------------------------------------------------------------


def test_component_set_is_exactly_the_approved_nine() -> None:
    assert set(COMPONENT_ROOTS) == EXPECTED_COMPONENTS
    assert APPROVED_COMPONENT_IDS == EXPECTED_COMPONENTS


@pytest.mark.parametrize("component_id", sorted(EXPECTED_COMPONENTS))
def test_component_source_root_exists(component_id: str) -> None:
    root = COMPONENT_ROOTS[component_id]
    assert root.is_dir(), root


@pytest.mark.parametrize("component_id", sorted(EXPECTED_COMPONENTS))
def test_component_declaration_covers_every_physical_entry(
    component_id: str,
) -> None:
    root = COMPONENT_ROOTS[component_id]
    declaration = declaration_for(root)

    physical = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    assert set(declaration) == physical


def test_build_all_creates_content_addressed_manifests(tmp_path: Path) -> None:
    repository_root = Path.cwd()
    output = tmp_path / "artifacts"

    results = build_all(repository_root, output)

    assert {item["component_id"] for item in results} == EXPECTED_COMPONENTS
    for item in results:
        cid = item["component_id"]
        version = item["component_version"]
        digest = item["artifact"]["digest"]
        assert digest.startswith("sha256:")
        assert digest == COMPONENT_VERSION_DIGESTS[(cid, version)]
        manifest = output / digest / "canonical.json"
        assert manifest.is_file()
        assert manifest.read_bytes()
        assert item["artifact"]["pinned"] is True
        assert item["artifact"]["canonical_form"] == "source_package/v1"
        assert item["artifact"]["canonical_manifest"] == (
            f"factory/artifacts/{digest}/canonical.json"
        )


def test_build_all_is_deterministic(tmp_path: Path) -> None:
    repository_root = Path.cwd()
    first = tmp_path / "first"
    second = tmp_path / "second"

    build_all(repository_root, first)
    build_all(repository_root, second)

    def snapshot(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert snapshot(first) == snapshot(second)


def test_publication_manifest_contains_no_lifecycle_claims(tmp_path: Path) -> None:
    results = build_all(Path.cwd(), tmp_path / "artifacts")
    for item in results:
        assert "deployable" not in item["artifact"]
        assert "publishable" not in item["artifact"]
        assert "publishability_blockers" not in item["artifact"]


def test_unknown_component_set_is_not_silently_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(COMPONENT_ROOTS, "unexpected", Path("src"))
    with pytest.raises(ValueError, match="component set mismatch"):
        build_all(Path.cwd(), tmp_path / "artifacts")


def test_build_all_excludes_bytecode_caches_from_canonical_entries(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    _, results, _ = built_suite
    for item in results:
        paths = [entry["path"] for entry in item["canonical"]["entries"]]
        assert not any("__pycache__" in path or path.endswith(".pyc") for path in paths)


def test_oci_layout_and_references_are_digest_pinned_without_mutable_tags(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, records = built_suite
    assert len(records) == 9

    for item, record in zip(results, records, strict=True):
        cid = item["component_id"]
        digest = item["artifact"]["digest"]
        oci = item["oci"]
        repo = f"ghcr.io/kvasha62/application-factory/{cid}"

        assert oci["registry_repository"] == repo
        assert oci["digest_reference"] == f"{repo}@{digest}"
        assert oci["oci_digest_reference"] == f"{repo}@{oci['oci_manifest_digest']}"
        assert record.authority_tag is None
        assert record.published is True
        assert record.verified is True

        layout_root = artifacts_dir / oci["layout_path"]
        assert (layout_root / "oci-layout").is_file()
        assert (layout_root / "index.json").is_file()

        blobs_dir = layout_root / "blobs" / "sha256"
        for blob_digest in (
            digest,
            oci["oci_manifest_digest"],
            oci["config_digest"],
            oci["package_layer_digest"],
        ):
            blob_file = blobs_dir / blob_digest.removeprefix("sha256:")
            assert blob_file.is_file()


def test_build_all_supports_container_image_canonical_machinery(
    tmp_path: Path, repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    output = tmp_path / "container_artifacts"
    results = build_all(repo_root, output, artifact_type="container_image")
    assert {item["component_id"] for item in results} == EXPECTED_COMPONENTS

    oci_registry = InMemoryOCIRegistry()
    records = publish_artifacts_to_oci(
        results, artifacts_root=output, transport=oci_registry
    )
    reconciled = reconcile_registry(
        registry_doc,
        records,
        repository_root=repo_root,
        artifacts_root=output,
    )
    assert validate_document(reconciled, root=repo_root) == []
    for entry in reconciled["components"]:
        assert entry["artifact"]["artifact_type"] == "container_image"
        assert entry["artifact"]["canonical_form"] == "container_image/v1"
        assert entry["artifact"]["pinned"] is True
        assert entry["lifecycle"]["deployable"] is True
        assert entry["lifecycle"]["publishable"] is False
        assert entry["lifecycle"]["publishability_blockers"] == ["migrations_absent"]


# ---------------------------------------------------------------------------
# Positive: Dependency closure & truthful registry reconciliation
# ---------------------------------------------------------------------------


def test_dependency_closure_of_approved_nine_is_valid(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    assert dependency_closure_violations(registry_doc, repository_root=repo_root) == []
    validate_dependency_closure(registry_doc, repository_root=repo_root)


def test_reconcile_registry_updates_artifact_and_lifecycle_truthfully(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, records = built_suite
    expected_digests = {
        item["component_id"]: item["artifact"]["digest"] for item in results
    }

    reconciled = reconcile_registry(
        registry_doc,
        records,
        repository_root=repo_root,
        artifacts_root=artifacts_dir,
    )

    assert validate_document(reconciled, root=repo_root) == []
    for entry in reconciled["components"]:
        cid = entry["component_id"]
        digest = expected_digests[cid]
        assert entry["artifact"] == {
            "artifact_type": "source_package",
            "digest": digest,
            "pinned": True,
            "canonical_form": "source_package/v1",
            "canonical_manifest": f"factory/artifacts/{digest}/canonical.json",
        }
        assert entry["lifecycle"] == {
            "registry_state": "registered",
            "deployable": True,
            "publishable": False,
            "publishability_blockers": ["migrations_absent"],
        }


def test_canonical_registry_on_disk_remains_false_without_external_publication(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    """When GHCR credentials/environment are unavailable, registry claims stay false."""
    assert validate_document(registry_doc, root=repo_root) == []
    for entry in registry_doc["components"]:
        assert entry["artifact"]["artifact_type"] == "none"
        assert entry["artifact"]["digest"] is None
        assert entry["artifact"]["pinned"] is False
        assert entry["lifecycle"]["deployable"] is False
        assert entry["lifecycle"]["publishable"] is False
        assert entry["lifecycle"]["publishability_blockers"] == [
            "deployment_artifact_absent",
            "migrations_absent",
        ]


# ---------------------------------------------------------------------------
# Adversarial: Dependency existence, version-range, and closure validation
# ---------------------------------------------------------------------------


def test_unknown_dependency_target_is_rejected(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    broken = copy.deepcopy(registry_doc)
    broken["components"][0]["dependencies"].append(
        {
            "component_id": "unknown_service",
            "version_range": ">=0.1.0,<0.2.0",
            "kind": "api",
            "contract": "components/unknown_service/contract/component_contract.json",
        }
    )
    with pytest.raises(DependencyClosureError, match="unknown dependency target"):
        validate_dependency_closure(broken, repository_root=repo_root)


def test_incompatible_dependency_version_range_is_rejected(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    broken = copy.deepcopy(registry_doc)
    # authorization depends on identity (registered at 0.3.0); require >=0.9.0,<1.0.0
    broken["components"][0]["dependencies"][0]["version_range"] = ">=0.9.0,<1.0.0"
    with pytest.raises(DependencyClosureError, match="incompatible dependency version"):
        validate_dependency_closure(broken, repository_root=repo_root)


def test_malformed_dependency_version_range_is_rejected(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    broken = copy.deepcopy(registry_doc)
    broken["components"][0]["dependencies"][0]["version_range"] = "not-a-range"
    with pytest.raises(
        DependencyClosureError, match="not a valid explicit version range"
    ):
        validate_dependency_closure(broken, repository_root=repo_root)


@pytest.mark.parametrize("selector", ["latest", "current", "default", "stable", "edge"])
def test_floating_selector_in_dependency_is_rejected(
    selector: str, repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    broken = copy.deepcopy(registry_doc)
    broken["components"][0]["dependencies"][0]["version_range"] = selector
    with pytest.raises(DependencyClosureError, match="floating selector"):
        validate_dependency_closure(broken, repository_root=repo_root)


def test_self_dependency_and_duplicate_dependency_are_rejected(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    broken = copy.deepcopy(registry_doc)
    broken["components"][0]["dependencies"].append(
        {
            "component_id": "authorization",
            "version_range": ">=0.1.0,<0.2.0",
            "kind": "api",
        }
    )
    broken["components"][0]["dependencies"].append(
        copy.deepcopy(broken["components"][0]["dependencies"][0])
    )
    with pytest.raises(DependencyClosureError) as exc_info:
        validate_dependency_closure(broken, repository_root=repo_root)
    joined = "\n".join(exc_info.value.violations)
    assert "cannot depend on itself" in joined
    assert "duplicate dependency" in joined


def test_unclosed_component_selection_is_rejected(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    # Omit tenant_authority, which identity and authorization depend on
    subset = EXPECTED_COMPONENTS - {"tenant_authority"}
    with pytest.raises(DependencyClosureError) as exc_info:
        validate_dependency_closure(
            registry_doc,
            repository_root=repo_root,
            selected_components=subset,
        )
    joined = "\n".join(exc_info.value.violations)
    assert "dependency closure violation" in joined
    assert "component set mismatch" in joined


def test_contract_version_divergence_is_rejected(
    repo_root: Path, registry_doc: dict[str, Any]
) -> None:
    broken = copy.deepcopy(registry_doc)
    broken["components"][0]["component_version"] = "9.9.9"
    with pytest.raises(DependencyClosureError, match="contract component_version"):
        validate_dependency_closure(broken, repository_root=repo_root)


# ---------------------------------------------------------------------------
# Adversarial: Missing publication & Partial publication
# ---------------------------------------------------------------------------


def test_missing_publication_records_are_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, _ = built_suite
    with pytest.raises(
        PublicationVerificationError,
        match="missing publication records for all 9 components",
    ):
        reconcile_registry(
            registry_doc,
            [],
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


def test_partial_publication_missing_one_component_is_rejected_atomically(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    partial = [rec for rec in records if rec.component_id != "booking"]
    snapshot_before = copy.deepcopy(registry_doc)

    with pytest.raises(
        PublicationVerificationError,
        match="partial or missing publication; missing components: \\['booking'\\]",
    ):
        reconcile_registry(
            registry_doc,
            partial,
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )
    assert registry_doc == snapshot_before


def test_unpublished_or_unverified_record_is_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    unpublished = [
        (
            dataclasses.replace(rec, published=False)
            if rec.component_id == "authorization"
            else rec
        )
        for rec in records
    ]
    with pytest.raises(
        PublicationVerificationError,
        match="has not been published",
    ):
        reconcile_registry(
            registry_doc,
            unpublished,
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )

    unverified = [
        (
            dataclasses.replace(rec, verified=False)
            if rec.component_id == "learning"
            else rec
        )
        for rec in records
    ]
    with pytest.raises(
        PublicationVerificationError,
        match="has not been digest-verified",
    ):
        reconcile_registry(
            registry_doc,
            unverified,
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


def test_missing_stored_canonical_manifest_is_rejected(
    tmp_path: Path,
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    copied_artifacts = tmp_path / "artifacts_missing_canonical"
    shutil.copytree(artifacts_dir, copied_artifacts)

    target_digest = records[0].artifact["digest"]
    (copied_artifacts / target_digest / "canonical.json").unlink()

    with pytest.raises(
        PublicationVerificationError,
        match="stored canonical manifest is missing",
    ):
        reconcile_registry(
            registry_doc,
            records,
            repository_root=repo_root,
            artifacts_root=copied_artifacts,
        )


# ---------------------------------------------------------------------------
# Adversarial: Wrong digest, tampering, and mismatch
# ---------------------------------------------------------------------------


def test_wrong_artifact_digest_is_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    forged_artifact = dict(records[0].artifact)
    forged_artifact["digest"] = FORGED_DIGEST
    forged_artifact["canonical_manifest"] = canonical_manifest_reference(FORGED_DIGEST)
    forged_record = dataclasses.replace(
        records[0],
        artifact=forged_artifact,
        digest_reference=f"{records[0].registry_repository}@{FORGED_DIGEST}",
    )
    tampered = [forged_record, *records[1:]]

    with pytest.raises(PublicationVerificationError) as exc_info:
        reconcile_registry(
            registry_doc,
            tampered,
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )
    joined = "\n".join(exc_info.value.violations)
    assert "does not match the recomputed digest" in joined


def test_tampered_canonical_manifest_bytes_on_disk_are_rejected(
    tmp_path: Path,
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    copied_artifacts = tmp_path / "artifacts_tampered_canonical"
    shutil.copytree(artifacts_dir, copied_artifacts)

    target_digest = records[0].artifact["digest"]
    (copied_artifacts / target_digest / "canonical.json").write_bytes(
        b'{"form":"source_package/v1","entries":[]}'
    )

    with pytest.raises(
        PublicationVerificationError,
        match="is not the SHA-256 of the stored canonical manifest",
    ):
        reconcile_registry(
            registry_doc,
            records,
            repository_root=repo_root,
            artifacts_root=copied_artifacts,
        )


def test_physical_source_mutation_after_build_is_rejected(
    tmp_path: Path,
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    mutated_Saga = tmp_path / "mutated_saga"
    shutil.copytree(repo_root / COMPONENT_ROOTS["saga"], mutated_Saga)
    (mutated_Saga / "injected.py").write_text("x = 1\n", encoding="utf-8")

    custom_roots = dict(COMPONENT_ROOTS)
    custom_roots["saga"] = mutated_Saga.relative_to(tmp_path)
    # Symlink components/ into tmp_path so contract lookup succeeds
    (tmp_path / "components").symlink_to(repo_root / "components")
    for cid, rel in COMPONENT_ROOTS.items():
        if cid != "saga":
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(repo_root / rel)

    with pytest.raises(PublicationVerificationError) as exc_info:
        reconcile_registry(
            registry_doc,
            records,
            repository_root=tmp_path,
            artifacts_root=artifacts_dir,
            component_roots=custom_roots,
        )
    joined = "\n".join(exc_info.value.violations)
    assert "publication[saga]" in joined
    assert "does not match the recomputed digest" in joined


def test_canonical_manifest_reference_bound_to_foreign_digest_is_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    bad_artifact = dict(records[0].artifact)
    bad_artifact["canonical_manifest"] = canonical_manifest_reference(FORGED_DIGEST)
    bad_record = dataclasses.replace(records[0], artifact=bad_artifact)

    with pytest.raises(
        PublicationVerificationError,
        match="is not bound to the declared digest",
    ):
        reconcile_registry(
            registry_doc,
            [bad_record, *records[1:]],
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


def test_artifact_type_and_canonical_form_mismatch_is_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    bad_artifact = dict(records[0].artifact)
    bad_artifact["canonical_form"] = "container_image/v1"
    bad_record = dataclasses.replace(records[0], artifact=bad_artifact)

    with pytest.raises(
        PublicationVerificationError,
        match="does not match artifact_type",
    ):
        reconcile_registry(
            registry_doc,
            [bad_record, *records[1:]],
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


def test_unpinned_artifact_is_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    unpinned_artifact = dict(records[0].artifact)
    unpinned_artifact["pinned"] = False
    unpinned_record = dataclasses.replace(records[0], artifact=unpinned_artifact)

    with pytest.raises(
        PublicationVerificationError,
        match="is not pinned",
    ):
        reconcile_registry(
            registry_doc,
            [unpinned_record, *records[1:]],
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


def test_component_version_mismatch_in_publication_record_is_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    bad_record = dataclasses.replace(records[0], component_version="9.9.9")

    with pytest.raises(
        PublicationVerificationError,
        match="record declares '9.9.9', registry declares",
    ):
        reconcile_registry(
            registry_doc,
            [bad_record, *records[1:]],
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


def test_tampered_oci_manifest_blob_is_rejected(
    tmp_path: Path,
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    copied_artifacts = tmp_path / "artifacts_tampered_oci"
    shutil.copytree(artifacts_dir, copied_artifacts)

    rec = records[0]
    blob_path = (
        copied_artifacts
        / "oci"
        / rec.component_id
        / "blobs"
        / "sha256"
        / rec.oci_manifest_digest.removeprefix("sha256:")
    )
    blob_path.write_bytes(b'{"tampered":true}')

    with pytest.raises(
        PublicationVerificationError,
        match="stored OCI manifest hashes to",
    ):
        reconcile_registry(
            registry_doc,
            records,
            repository_root=repo_root,
            artifacts_root=copied_artifacts,
        )


# ---------------------------------------------------------------------------
# Adversarial: Floating references & Mutable tags as authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "floating_ref",
    [
        "latest",
        "stable",
        "current",
        "default",
        "edge",
        "ghcr.io/kvasha62/application-factory/authorization:latest",
        "ghcr.io/kvasha62/application-factory/authorization:0.1.0",
        "ghcr.io/kvasha62/application-factory/authorization@latest",
    ],
)
def test_floating_or_tag_reference_is_rejected(
    floating_ref: str,
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    bad_record = dataclasses.replace(records[0], digest_reference=floating_ref)

    with pytest.raises(PublicationVerificationError):
        reconcile_registry(
            registry_doc,
            [bad_record, *records[1:]],
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


@pytest.mark.parametrize("tag", ["0.1.0", "latest", "stable", "v1"])
def test_authority_tag_in_publication_record_is_rejected(
    tag: str,
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    bad_record = dataclasses.replace(records[0], authority_tag=tag)

    with pytest.raises(
        PublicationVerificationError,
        match="mutable tag .* cannot be used as publication authority",
    ):
        reconcile_registry(
            registry_doc,
            [bad_record, *records[1:]],
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


# ---------------------------------------------------------------------------
# Adversarial: Remote OCI registry verification failures
# ---------------------------------------------------------------------------


def test_remote_oci_registry_corrupting_manifest_digest_fails_closed(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite

    class CorruptingManifestRegistry(InMemoryOCIRegistry):
        def fetch_manifest(self, repository: str, digest: str) -> bytes:
            return b'{"corrupted":true}'

    with pytest.raises(
        PublicationVerificationError,
        match="remote OCI manifest digest .* != expected",
    ):
        publish_artifacts_to_oci(
            results,
            artifacts_root=artifacts_dir,
            transport=CorruptingManifestRegistry(),
        )


def test_remote_oci_registry_corrupting_canonical_blob_fails_closed(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite

    class CorruptingBlobRegistry(InMemoryOCIRegistry):
        def fetch_blob(self, repository: str, digest: str) -> bytes:
            return b"corrupted-canonical-manifest"

    with pytest.raises(
        PublicationVerificationError,
        match="remote canonical manifest digest .* != expected",
    ):
        publish_artifacts_to_oci(
            results,
            artifacts_root=artifacts_dir,
            transport=CorruptingBlobRegistry(),
        )


# ---------------------------------------------------------------------------
# Adversarial: Migration fabrication & Secret / credential leakage
# ---------------------------------------------------------------------------


def test_fabricating_migrations_or_suppressing_migrations_absent_is_rejected(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite

    with pytest.raises(
        PublicationVerificationError,
        match="fabricating migration contracts or suppressing 'migrations_absent' is forbidden",
    ):
        reconcile_registry(
            registry_doc,
            records,
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
            allow_fabricated_migrations=True,
        )

    # Also test a registry document that falsely omits migrations_absent
    fabricated_doc = copy.deepcopy(registry_doc)
    fabricated_doc["components"][0]["lifecycle"]["publishability_blockers"] = [
        "deployment_artifact_absent"
    ]
    with pytest.raises(
        PublicationVerificationError,
        match="'migrations_absent' cannot be removed when no component migration contract exists",
    ):
        reconcile_registry(
            fabricated_doc,
            records,
            repository_root=repo_root,
            artifacts_root=artifacts_dir,
        )


def test_missing_ghcr_credentials_fail_closed_and_report_blocker_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(
        PublicationBlockedError,
        match="ghcr_credentials_unavailable",
    ):
        publish_artifacts_to_oci(results, artifacts_root=artifacts_dir)


def test_secret_credentials_are_redacted_and_never_leaked(
    monkeypatch: pytest.MonkeyPatch,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite
    secret_token = "ghp_SuperSecretTokenValue1234567890ABCD"
    monkeypatch.setenv("GHCR_TOKEN", secret_token)

    def failing_opener(request: Any, timeout: int = 30) -> Any:
        auth = request.get_header("Authorization")
        raise urllib.error.URLError(
            f"TLS handshake failed with Authorization={auth} and token={secret_token}"
        )

    transport = GHCRHTTPTransport(token=secret_token, opener=failing_opener)
    with pytest.raises(PublicationBlockedError) as exc_info:
        publish_artifacts_to_oci(
            results,
            artifacts_root=artifacts_dir,
            transport=transport,
        )

    error_text = str(exc_info.value)
    assert secret_token not in error_text
    assert "<REDACTED>" in error_text

    # Direct redaction checks for basic, bearer, URL credentials, and env tokens
    raw = (
        f"Bearer {secret_token} Basic dXNlcjpwYXNz "
        "https://user:my_secret_pw@ghcr.io/v2/ password=topsecret"
    )
    scrubbed = redact_secrets(raw, [secret_token, "my_secret_pw", "topsecret"])
    assert secret_token not in scrubbed
    assert "my_secret_pw" not in scrubbed
    assert "topsecret" not in scrubbed
    assert "dXNlcjpwYXNz" not in scrubbed
    assert "<REDACTED>" in scrubbed


def test_cli_main_builds_and_blocks_honestly_without_ghcr_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    output = tmp_path / "cli_artifacts"
    assert main(["--root", str(repo_root), "--output", str(output)]) == 0
    assert (output / "publication-manifest.json").is_file()

    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(PublicationBlockedError, match="ghcr_credentials_unavailable"):
        main(
            [
                "--root",
                str(repo_root),
                "--output",
                str(output),
                "--publish-ghcr",
                "--reconcile-registry",
            ]
        )


def test_publication_violations_reporting_is_deterministic(
    repo_root: Path,
    registry_doc: dict[str, Any],
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, records = built_suite
    broken_record = dataclasses.replace(
        records[0],
        published=False,
        verified=False,
        authority_tag="latest",
        digest_reference="ghcr.io/kvasha62/application-factory/authorization:latest",
    )
    first = publication_violations(
        registry_doc,
        [broken_record, *records[1:-1]],
        repository_root=repo_root,
        artifacts_root=artifacts_dir,
    )
    second = publication_violations(
        registry_doc,
        [broken_record, *records[1:-1]],
        repository_root=repo_root,
        artifacts_root=artifacts_dir,
    )
    assert first
    assert first == second
    assert first == sorted(set(first))


# ---------------------------------------------------------------------------
# Workflow trigger coverage & immutable version->digest lock (recovered slice)
# ---------------------------------------------------------------------------


def _write_workflow_with_triggers(
    path: Path, pr_paths: list[str], push_paths: list[str]
) -> Path:
    pr_lines = "\n".join(f'      - "{p}"' for p in pr_paths)
    push_lines = "\n".join(f'      - "{p}"' for p in push_paths)
    path.write_text(
        "name: Publish component artifacts\n\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        f"    paths:\n{pr_lines}\n"
        "  push:\n"
        "    branches: [main]\n"
        f"    paths:\n{push_lines}\n",
        encoding="utf-8",
    )
    return path


def test_workflow_trigger_coverage_is_complete_for_all_nine_components(
    tmp_path: Path,
) -> None:
    validate_workflow_trigger_coverage(
        WORKFLOW_PATH, COMPONENT_ROOTS, events=("pull_request",)
    )

    triggers = extract_workflow_trigger_paths(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert "src/tenant_authority/**" in triggers["pull_request"]
    assert "src/saga/**" in triggers["pull_request"]
    assert "src/idempotency/**" in triggers["pull_request"]
    assert "src/*_service/**" in triggers["pull_request"]

    complete_workflow = _write_workflow_with_triggers(
        tmp_path / "publish-component-artifacts.yml",
        triggers["pull_request"],
        triggers["pull_request"],
    )
    validate_workflow_trigger_coverage(complete_workflow, COMPONENT_ROOTS)

    if "src/tenant_authority/**" in triggers.get(
        "push", []
    ) and "src/saga/**" in triggers.get("push", []):
        validate_workflow_trigger_coverage(WORKFLOW_PATH, COMPONENT_ROOTS)
    else:
        with pytest.raises(
            ValueError,
            match=r"on\.push\.paths does not cover component 'tenant_authority'",
        ):
            validate_workflow_trigger_coverage(WORKFLOW_PATH, COMPONENT_ROOTS)


@pytest.mark.parametrize(
    ("event", "omitted_pattern", "expected_component"),
    [
        ("push", "src/tenant_authority/**", "tenant_authority"),
        ("push", "src/saga/**", "saga"),
        ("push", "src/idempotency/**", "idempotency"),
        ("push", "src/*_service/**", "authorization"),
        ("pull_request", "src/tenant_authority/**", "tenant_authority"),
        ("pull_request", "src/saga/**", "saga"),
        ("pull_request", "src/idempotency/**", "idempotency"),
        ("pull_request", "src/*_service/**", "booking"),
    ],
)
def test_workflow_trigger_validation_fails_closed_when_source_root_omitted(
    tmp_path: Path,
    event: str,
    omitted_pattern: str,
    expected_component: str,
) -> None:
    base_triggers = extract_workflow_trigger_paths(
        WORKFLOW_PATH.read_text(encoding="utf-8")
    )
    full_paths = list(base_triggers["pull_request"])
    triggers = {
        "pull_request": list(full_paths),
        "push": list(full_paths),
    }
    triggers[event] = [p for p in triggers[event] if p != omitted_pattern]

    workflow_copy = _write_workflow_with_triggers(
        tmp_path / "publish-component-artifacts.yml",
        triggers["pull_request"],
        triggers["push"],
    )

    with pytest.raises(
        ValueError,
        match=rf"on\.{event}\.paths does not cover component '{expected_component}'",
    ):
        validate_workflow_trigger_coverage(workflow_copy, COMPONENT_ROOTS)


def test_workflow_trigger_validation_fails_closed_on_new_or_changed_component_root() -> (
    None
):
    drifted_roots = dict(COMPONENT_ROOTS)
    drifted_roots["tenant_authority"] = Path("src/tenant_authority_v2")

    with pytest.raises(
        ValueError,
        match=r"on\.pull_request\.paths does not cover component 'tenant_authority'",
    ):
        validate_workflow_trigger_coverage(WORKFLOW_PATH, drifted_roots)


def test_workflow_trigger_validation_rejects_non_recursive_or_overbroad_globs(
    tmp_path: Path,
) -> None:
    workflow_copy = _write_workflow_with_triggers(
        tmp_path / "publish-component-artifacts.yml",
        ["src/**"],
        ["src/tenant_authority/*"],
    )

    with pytest.raises(ValueError, match="workflow trigger coverage violation"):
        validate_workflow_trigger_coverage(workflow_copy, COMPONENT_ROOTS)


def test_locked_version_digests_cover_exact_registered_inventory(
    repo_root: Path,
) -> None:
    registry = load_registry(root=repo_root)
    expected_keys = {(cid, registry.version(cid)) for cid in registry.component_ids}
    assert set(COMPONENT_VERSION_DIGESTS) == expected_keys


def test_same_component_and_version_cannot_silently_point_to_different_content(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    repo_copy = tmp_path / "repo"
    repo_copy.mkdir()
    shutil.copytree(repo_root / "factory", repo_copy / "factory")
    shutil.copytree(repo_root / "components", repo_copy / "components")
    for root in COMPONENT_ROOTS.values():
        shutil.copytree(repo_root / root, repo_copy / root)

    target_file = repo_copy / "src" / "tenant_authority" / "__init__.py"
    target_file.write_text(
        target_file.read_text(encoding="utf-8") + "\n# mutated content\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=(
            r"immutable publication violation: tenant_authority@0\.1\.0 "
            r"resolved to digest 'sha256:[0-9a-f]{64}', expected locked digest"
        ),
    ):
        build_all(repo_copy, tmp_path / "out")


def test_unpinned_component_version_is_rejected_fail_closed() -> None:
    with pytest.raises(
        ValueError,
        match=(
            r"immutable publication violation: tenant_authority@9\.9\.9 "
            r"has no locked canonical digest"
        ),
    ):
        verify_immutable_version_digest(
            "tenant_authority",
            "9.9.9",
            "sha256:" + "a" * 64,
        )


def test_existing_publication_manifest_conflict_fails_closed(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    output = tmp_path / "artifacts"
    results = build_all(repo_root, output)

    tampered = list(results)
    tampered[0] = dict(tampered[0])
    tampered[0]["artifact"] = dict(tampered[0]["artifact"])
    tampered[0]["artifact"]["digest"] = "sha256:" + "0" * 64
    (output / "publication-manifest.json").write_text(
        json.dumps(tampered), encoding="utf-8"
    )

    with pytest.raises(
        ValueError,
        match=r"immutable publication violation: existing manifest binds",
    ):
        build_all(repo_root, output)


def test_bytecode_cache_in_source_root_does_not_change_canonical_digest(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    repo_copy = tmp_path / "repo"
    repo_copy.mkdir()
    shutil.copytree(repo_root / "factory", repo_copy / "factory")
    shutil.copytree(repo_root / "components", repo_copy / "components")
    for root in COMPONENT_ROOTS.values():
        shutil.copytree(repo_root / root, repo_copy / root)

    pycache = repo_copy / "src" / "tenant_authority" / "__pycache__"
    pycache.mkdir(parents=True, exist_ok=True)
    (pycache / "__init__.cpython-313.pyc").write_bytes(b"\x00\x01\x02\x03")

    results = build_all(repo_copy, tmp_path / "out")
    by_id = {item["component_id"]: item for item in results}
    assert (
        by_id["tenant_authority"]["artifact"]["digest"]
        == COMPONENT_VERSION_DIGESTS[("tenant_authority", "0.1.0")]
    )


# ---------------------------------------------------------------------------
# S1: deterministic digest-addressed publication (GHCRHTTPTransport, records)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal urllib response double for transport tests."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _http_error(
    url: str, code: int, headers: dict[str, str] | None = None, body: bytes = b""
) -> urllib.error.HTTPError:
    msg = Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(
        url, code, f"HTTP Error {code}", msg, io.BytesIO(body)
    )


def test_transport_follows_401_challenge_with_scoped_token_exchange() -> None:
    """OCI token authentication: 401 challenge -> one scoped exchange -> retry."""
    calls: list[str] = []
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'

    def opener(req, timeout=None):
        url = req.full_url
        calls.append(url)
        auth = req.get_header("Authorization")
        if "ghcr.io/token" in url:
            assert auth is not None and auth.startswith("Basic ")
            username, _, token = (
                base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8").partition(":")
            )
            assert username == "publisher"
            assert token == "raw-workflow-token"
            query = parse_qs(urlparse(url).query)
            assert query["service"] == ["ghcr.io"]
            assert query["scope"] == [
                "repository:kvasha62/application-factory/authorization:pull,push"
            ]
            return _FakeResponse(
                200, {}, json.dumps({"token": "scoped-registry-token"}).encode()
            )
        if auth == "Bearer raw-workflow-token":
            raise _http_error(url, 401, {"WWW-Authenticate": challenge})
        assert auth == "Bearer scoped-registry-token"
        return _FakeResponse(200, {}, b"remote-manifest-bytes")

    transport = GHCRHTTPTransport(
        token="raw-workflow-token",
        username="publisher",
        opener=opener,
    )
    digest = "sha256:" + "0" * 64
    body = transport.fetch_manifest(
        "ghcr.io/kvasha62/application-factory/authorization", digest
    )
    assert body == b"remote-manifest-bytes"
    assert calls[0].endswith(f"/manifests/{digest}")
    assert "ghcr.io/token" in calls[1]
    assert calls[2].endswith(f"/manifests/{digest}")


def test_transport_never_retries_without_challenge_or_after_failed_exchange() -> None:
    """401 without a usable challenge must block; no silent retry loops."""
    digest = "sha256:" + "1" * 64

    def bare_401_opener(req, timeout=None):
        raise _http_error(req.full_url, 401, {})

    transport = GHCRHTTPTransport(
        token="raw-workflow-token", username="u", opener=bare_401_opener
    )
    with pytest.raises(PublicationBlockedError, match=r"failed \(401\): unauthorized"):
        transport.fetch_blob("ghcr.io/kvasha62/application-factory/saga", digest)

    def challenge_without_realm(req, timeout=None):
        if "ghcr.io/token" in req.full_url:
            raise _http_error(req.full_url, 401, {}, b"nope")
        raise _http_error(
            req.full_url,
            401,
            {"WWW-Authenticate": 'Bearer service="ghcr.io"'},
        )

    transport2 = GHCRHTTPTransport(
        token="raw-workflow-token", username="u", opener=challenge_without_realm
    )
    with pytest.raises(PublicationBlockedError, match="carries no realm"):
        transport2.fetch_blob("ghcr.io/kvasha62/application-factory/saga", digest)


def test_exchange_failure_redacts_credentials(tmp_path: Path) -> None:
    secret = "ghp_LEAKTESTVALUE0123456789abcdefABCDEF"

    def opener(req, timeout=None):
        raise _http_error(
            req.full_url,
            500,
            {},
            f"upstream saw Authorization: Bearer {secret}".encode(),
        )

    transport = GHCRHTTPTransport(token=secret, username="u", opener=opener)
    with pytest.raises(PublicationBlockedError) as excinfo:
        transport.fetch_blob(
            "ghcr.io/kvasha62/application-factory/saga", "sha256:" + "2" * 64
        )
    assert secret not in str(excinfo.value)


def test_remote_manifest_bytes_hash_to_recorded_identity_fields(
    repo_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """InMemory parity: sha256(pushed manifest bytes) == oci_manifest_digest and
    the canonical layer digest == artifact digest; every descriptor size matches."""
    import hashlib

    artifacts_dir = tmp_path_factory.mktemp("s1_registry")
    results = build_all(repo_root, artifacts_dir)
    oci = InMemoryOCIRegistry()
    records = publish_artifacts_to_oci(
        results, artifacts_root=artifacts_dir, transport=oci
    )
    assert len(records) == 9
    for record in records:
        manifest_bytes = oci.manifests[
            (record.registry_repository, record.oci_manifest_digest)
        ]
        assert "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() == (
            record.oci_manifest_digest
        )
        document = json.loads(manifest_bytes)
        canonical_layers = [
            layer
            for layer in document["layers"]
            if layer["mediaType"] == CANONICAL_MANIFEST_MEDIA_TYPE
        ]
        assert len(canonical_layers) == 1
        assert canonical_layers[0]["digest"] == record.artifact["digest"]
        for descriptor in [document["config"], *document["layers"]]:
            blob = oci.blobs[(record.registry_repository, descriptor["digest"])]
            assert descriptor["size"] == len(blob)
        expected = f"{record.registry_repository}@{record.artifact['digest']}"
        assert record.digest_reference == expected
        assert record.oci_digest_reference == (
            f"{record.registry_repository}@{record.oci_manifest_digest}"
        )
        assert record.oci_digest_reference != record.digest_reference


def test_swapped_digest_and_oci_references_are_rejected(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
    registry_doc: dict[str, Any],
    repo_root: Path,
) -> None:
    """digest_reference and oci_digest_reference are distinct identities."""
    artifacts_dir, _, records = built_suite
    broken = [dataclasses.asdict(record) for record in records]
    broken[0]["digest_reference"], broken[0]["oci_digest_reference"] = (
        broken[0]["oci_digest_reference"],
        broken[0]["digest_reference"],
    )
    violations = publication_violations(
        registry_doc,
        broken,
        repository_root=repo_root,
        artifacts_root=artifacts_dir,
    )
    assert any("digest_reference" in violation for violation in violations)
    assert any("oci_digest_reference" in violation for violation in violations)


def test_build_all_oci_layout_is_reproducible_across_runs(
    repo_root: Path, tmp_path: Path
) -> None:
    """S1 relies on identical manifest digests from independent builds."""
    first = build_all(repo_root, tmp_path / "a")
    second = build_all(repo_root, tmp_path / "b")
    identity = lambda item: (
        item["component_id"],
        item["component_version"],
        item["artifact"],
        item["oci"],
    )
    assert [identity(item) for item in first] == [identity(item) for item in second]
    for item in first:
        layout_a = tmp_path / "a" / item["oci"]["layout_path"] / "blobs" / "sha256"
        layout_b = tmp_path / "b" / item["oci"]["layout_path"] / "blobs" / "sha256"
        assert sorted(p.name for p in layout_a.iterdir()) == sorted(
            p.name for p in layout_b.iterdir()
        )
        for blob in layout_a.iterdir():
            assert blob.read_bytes() == (layout_b / blob.name).read_bytes()


def test_write_records_round_trip_is_accepted_by_publication_violations(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
    registry_doc: dict[str, Any],
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """publication-records.json is canonical JSON and reconcilable as mappings."""
    artifacts_dir, _, records = built_suite
    payload = [dataclasses.asdict(record) for record in records]
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    (tmp_path / "publication-records.json").write_text(text, encoding="utf-8")
    loaded = json.loads((tmp_path / "publication-records.json").read_text())
    assert len(loaded) == 9
    assert all(entry["authority_tag"] is None for entry in loaded)
    assert all(
        entry["published"] is True and entry["verified"] is True for entry in loaded
    )
    violations = publication_violations(
        registry_doc,
        loaded,
        repository_root=repo_root,
        artifacts_root=artifacts_dir,
    )
    assert violations == []


def test_publish_workflow_uses_deterministic_digest_addressed_path() -> None:
    """T1 workflow lint: Path B only. No ORAS client, no tags, no tag publish."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "--publish-ghcr" in text
    assert "--write-records factory/artifacts/publication-records.json" in text
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in text
    assert "packages: write" in text
    assert "packages: read" not in text  # write scope is on the publish leg only
    # The publish step itself speaks only the deterministic digest path.
    publish_step = text.split(
        "- name: Publish deterministic OCI artifacts to GHCR by digest", 1
    )[1]
    publish_step = publish_step.split("- name: Archive generated artifacts", 1)[0]
    low = publish_step.lower()
    for forbidden in (
        "oras",
        "publish_item_to_ghcr",
        "login",
        "docker",
        "tag",
        "latest",
        "reconcile",
    ):
        assert forbidden not in low, f"publish step must not reference {forbidden!r}"


def test_publish_workflow_splits_write_scope_into_dedicated_job() -> None:
    """T1b permission split: only the publication job may hold packages: write."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow_permissions, jobs = text.split("jobs:", 1)
    # Workflow default: read-only; no registry scope anywhere near it.
    assert "permissions:\n  contents: read" in workflow_permissions
    assert "packages" not in workflow_permissions.split("permissions:", 1)[1]
    build_job, publish_job = jobs.split("\n  publish-by-digest:", 1)
    # The build/verify job (also the PR leg) is strictly read-only: it must
    # never hold a packages scope nor see the registry token nor publish.
    assert "packages: write" not in build_job
    assert (
        "packages:" not in build_job.split("permissions:", 1)[1].split("steps:", 1)[0]
    )
    assert "GHCR_TOKEN" not in build_job
    assert "--publish-ghcr" not in build_job
    # The publication job must consume the transferred exact bytes: the
    # --from-layout seam (no rebuild, no overwrite) is on its publish command
    # and on that command only.
    assert "--from-layout" in publish_job
    assert "from-layout" not in build_job
    # The publication job: explicit minimal scope, gated to main, and chained
    # behind the verified build through the workflow artifact transfer only.
    assert "needs: build-and-verify" in publish_job
    assert "contents: read" in publish_job
    assert "packages: write" in publish_job
    assert (
        "if: github.event_name == 'push' && github.ref == 'refs/heads/main'"
        in publish_job
    )
    assert "actions/upload-artifact@v4" in build_job
    assert "actions/download-artifact@v4" in publish_job
    assert build_job.count("name: deterministic-oci-layout") == 1
    assert publish_job.count("name: deterministic-oci-layout") == 1
    # Evidence archive naming/retention preserved on both legs; exactly one
    # evidence upload per event type (pull request: build job; push: publish).
    evidence = "component-artifacts-${{ github.sha }}"
    assert build_job.count(evidence) == 1 and publish_job.count(evidence) == 1
    assert "retention-days: 90" in build_job and "retention-days: 90" in publish_job
    assert "retention-days: 1" in build_job  # transfer artifact is ephemeral


def test_cli_write_records_requires_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit):
        main(["--output", "/tmp/s1-unused", "--write-records", "/tmp/s1-rec.json"])


def test_cli_write_records_not_created_when_blocked(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> None:
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    records_path = tmp_path / "publication-records.json"
    with pytest.raises(PublicationBlockedError, match="ghcr_credentials_unavailable"):
        main(
            [
                "--root",
                str(repo_root),
                "--output",
                str(tmp_path / "artifacts"),
                "--publish-ghcr",
                "--write-records",
                str(records_path),
            ]
        )
    assert not records_path.exists()


# ---------------------------------------------------------------------------
# Follow-up mutation 5949538008: exact-bytes layout seam (T8), auth trust
# boundary (T9), and full-token redaction on generic error paths (T10).
# ---------------------------------------------------------------------------


def _dir_fingerprint(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_from_layout_loader_reproduces_publish_inputs(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    """T8a: loaded entries == build_all data; publishing them replays records."""
    artifacts_dir, results, records = built_suite
    loaded = load_publication_layout(artifacts_dir)
    assert len(loaded) == 9
    by_id = {item["component_id"]: item for item in results}
    for entry in loaded:
        original = by_id[str(entry["component_id"])]
        assert entry["component_version"] == original["component_version"]
        assert entry["artifact"] == original["artifact"]
        assert entry["oci"] == original["oci"]
    replayed = publish_artifacts_to_oci(
        loaded, artifacts_root=artifacts_dir, transport=InMemoryOCIRegistry()
    )
    assert [dataclasses.asdict(rec) for rec in replayed] == [
        dataclasses.asdict(rec) for rec in records
    ]


def test_from_layout_loader_is_read_only(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, _ = built_suite
    before = _dir_fingerprint(artifacts_dir)
    load_publication_layout(artifacts_dir)
    assert _dir_fingerprint(artifacts_dir) == before


def test_from_layout_cli_publishes_bytes_without_touching_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    """T8b: a blocked --from-layout run writes nothing at all (no rebuild)."""
    artifacts_dir, _, _ = built_suite
    downloaded = tmp_path / "downloaded"
    shutil.copytree(artifacts_dir, downloaded)
    before = _dir_fingerprint(downloaded)
    records_path = tmp_path / "records.json"
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(PublicationBlockedError, match="ghcr_credentials_unavailable"):
        main(
            [
                "--output",
                str(downloaded),
                "--from-layout",
                "--publish-ghcr",
                "--write-records",
                str(records_path),
            ]
        )
    assert _dir_fingerprint(downloaded) == before
    assert not records_path.exists()


def test_from_layout_cli_still_requires_publication(
    tmp_path: Path,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, _, _ = built_suite
    with pytest.raises(SystemExit):
        main(["--output", str(artifacts_dir), "--from-layout"])


def test_from_layout_loader_fails_closed_on_bad_transfers(
    tmp_path: Path,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    """Missing manifest, non-list, missing blobs, or wrong component set block."""
    artifacts_dir, results, _ = built_suite
    with pytest.raises(PublicationBlockedError, match="publication-manifest.json"):
        load_publication_layout(tmp_path)

    (tmp_path / "publication-manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PublicationBlockedError, match="not a list"):
        load_publication_layout(tmp_path)

    partial = tmp_path / "partial"
    partial.mkdir()
    subset = [item for item in results if item["component_id"] != "saga"]
    (partial / "publication-manifest.json").write_text(
        json.dumps(subset), encoding="utf-8"
    )
    with pytest.raises(PublicationBlockedError, match="layout blobs missing"):
        load_publication_layout(partial)

    complete_no_saga = tmp_path / "complete-no-saga"
    shutil.copytree(artifacts_dir, complete_no_saga)
    document = json.loads(
        (complete_no_saga / "publication-manifest.json").read_text(encoding="utf-8")
    )
    (complete_no_saga / "publication-manifest.json").write_text(
        json.dumps([e for e in document if e["component_id"] != "saga"]),
        encoding="utf-8",
    )
    with pytest.raises(PublicationBlockedError, match="not the required nine"):
        load_publication_layout(complete_no_saga)

    removed = tmp_path / "removed-blob"
    shutil.copytree(artifacts_dir, removed)
    victim = next(item for item in results if item["component_id"] == "records")
    blob = (
        removed
        / victim["oci"]["layout_path"]
        / "blobs"
        / "sha256"
        / str(victim["artifact"]["digest"]).removeprefix("sha256:")
    )
    blob.unlink()
    with pytest.raises(PublicationBlockedError, match="layout blobs missing"):
        load_publication_layout(removed)


def test_from_layout_publish_refuses_tampered_bytes_fail_closed(
    tmp_path: Path,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    """T8c: mutated transferred bytes fail the local integrity seam, not the
    registry: publication raises before a single write is attempted."""
    artifacts_dir, results, _ = built_suite
    copied = tmp_path / "tampered"
    shutil.copytree(artifacts_dir, copied)
    victim = next(item for item in results if item["component_id"] == "saga")
    blob = (
        copied
        / victim["oci"]["layout_path"]
        / "blobs"
        / "sha256"
        / str(victim["artifact"]["digest"]).removeprefix("sha256:")
    )
    blob.write_bytes(b"tampered mid-transfer")
    loaded = load_publication_layout(copied)
    transport = _RecordingTransport()
    entry = next(item for item in loaded if item["component_id"] == "saga")
    with pytest.raises(PublicationVerificationError, match="integrity mismatch"):
        publish_artifacts_to_oci([entry], artifacts_root=copied, transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize(
    ("challenge", "match"),
    [
        ('Basic realm="https://ghcr.io/token"', "not Bearer"),
        ('Bearer realm="http://ghcr.io/token"', "untrusted realm"),
        ('Bearer realm="https://token.attacker.example/token"', "untrusted realm"),
        ('Bearer realm="https://ghcr.io:8443/token"', "untrusted realm"),
        ('Bearer realm="https://ghcr.io@attacker.example/token"', "untrusted realm"),
        ('Bearer realm="/token"', "untrusted realm"),
    ],
)
def test_transport_refuses_untrusted_realms_without_network(
    challenge: str, match: str
) -> None:
    """T9: attacker-controlled challenges block publication pre-network."""
    attempted: list[str] = []

    def opener(req, timeout=None):
        if req.full_url.startswith("https://ghcr.io/v2/"):
            raise _http_error(req.full_url, 401, {"WWW-Authenticate": challenge})
        attempted.append(req.full_url)
        raise AssertionError(f"credentials sent to untrusted realm: {req.full_url}")

    transport = GHCRHTTPTransport(token="raw-workflow-token", opener=opener)
    with pytest.raises(PublicationBlockedError, match=match) as excinfo:
        transport.fetch_manifest(
            "ghcr.io/kvasha62/application-factory/saga", "sha256:" + "1" * 64
        )
    assert attempted == []
    assert "raw-workflow-token" not in str(excinfo.value)


def test_transport_trusted_host_override_is_explicit_only() -> None:
    """T9b: allowlist is per-transport; defaults refuse the override host."""
    exchanged = "scoped-token-for-override"

    def opener(req, timeout=None):
        if "token.test/token" in req.full_url:
            return _FakeResponse(200, {}, json.dumps({"token": exchanged}).encode())
        assert req.full_url.startswith("https://token.test/v2/")
        if req.get_header("Authorization") == f"Bearer {exchanged}":
            return _FakeResponse(200, {}, b"manifest-after-overridden-trust")
        raise _http_error(
            req.full_url,
            401,
            {"WWW-Authenticate": 'Bearer realm="https://token.test/token"'},
        )

    trusted = GHCRHTTPTransport(
        token="raw-workflow-token",
        base_url="https://token.test",
        opener=opener,
        trusted_token_hosts=("token.test",),
    )
    body = trusted.fetch_manifest("token.test/o/r", "sha256:" + "2" * 64)
    assert body == b"manifest-after-overridden-trust"

    refused = GHCRHTTPTransport(
        token="raw-workflow-token",
        base_url="https://token.test",
        opener=opener,
    )
    with pytest.raises(PublicationBlockedError, match="untrusted realm"):
        refused.fetch_manifest("token.test/o/r", "sha256:" + "2" * 64)


def test_unreachable_send_error_redacts_both_workflow_and_exchanged_tokens() -> None:
    """T10a: generic transport errors scrub the workflow token AND the bearer."""
    workflow_token = "wf-" + "s" * 24
    exchanged = "ex-" + "t" * 24
    state = {"calls": 0}

    def opener(req, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise _http_error(
                req.full_url,
                401,
                {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token"'},
            )
        if state["calls"] == 2:
            return _FakeResponse(200, {}, json.dumps({"token": exchanged}).encode())
        auth = req.get_header("Authorization")
        raise urllib.error.URLError(
            f"connection reset (echo auth={auth} raw={workflow_token})"
        )

    transport = GHCRHTTPTransport(token=workflow_token, opener=opener)
    with pytest.raises(PublicationBlockedError) as excinfo:
        transport.fetch_manifest("ghcr.io/o/r", "sha256:" + "3" * 64)
    message = str(excinfo.value)
    assert workflow_token not in message
    assert exchanged not in message
    assert "<REDACTED>" in message
    assert "ghcr_unreachable" in message


def test_http_error_after_exchange_redacts_exchanged_token() -> None:
    """T10b: a registry 5xx echoing the bearer header must not leak it."""
    workflow_token = "wf2-" + "u" * 24
    exchanged = "ex2-" + "v" * 24
    state = {"calls": 0}

    def opener(req, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise _http_error(
                req.full_url,
                401,
                {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token"'},
            )
        if state["calls"] == 2:
            return _FakeResponse(200, {}, json.dumps({"token": exchanged}).encode())
        raise _http_error(
            f"{req.full_url}; bearer={exchanged}",
            500,
            {},
            f"server saw Bearer {exchanged}".encode(),
        )

    transport = GHCRHTTPTransport(token=workflow_token, opener=opener)
    with pytest.raises(PublicationBlockedError) as excinfo:
        transport.push_blob("ghcr.io/o/r", "sha256:" + "4" * 64, b"payload")
    message = str(excinfo.value)
    assert exchanged not in message
    assert workflow_token not in message


def test_unreadable_exchange_body_is_never_echoed() -> None:
    """T10c: malformed token responses are replaced by a static refusal."""
    workflow_token = "ghp_" + "w" * 30
    leaked = "gho_" + "b" * 30

    def opener(req, timeout=None):
        if req.full_url.startswith("https://ghcr.io/v2/"):
            raise _http_error(
                req.full_url,
                401,
                {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token"'},
            )
        body = (
            b'{"broken ' + workflow_token.encode() + b" and " + leaked.encode() + b"}"
        )
        return _FakeResponse(200, {}, body)

    transport = GHCRHTTPTransport(token=workflow_token, opener=opener)
    with pytest.raises(PublicationBlockedError) as excinfo:
        transport.fetch_blob("ghcr.io/o/r", "sha256:" + "5" * 64)
    message = str(excinfo.value)
    assert "unreadable" in message
    assert workflow_token not in message
    assert leaked not in message


# ---------------------------------------------------------------------------
# Follow-up mutation 5950433186: pre-write blob integrity (T11) and the
# upload-Location / redirect credential boundary (T12).
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """OCI transport double: records every attempted call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def push_blob(self, repository: str, digest: str, data: bytes) -> None:
        self.calls.append(("blob", digest))

    def push_manifest(
        self, repository: str, digest: str, media_type: str, data: bytes
    ) -> None:
        self.calls.append(("manifest", digest))

    def fetch_blob(self, repository: str, digest: str) -> bytes:
        self.calls.append(("fetch-blob", digest))
        return b""

    def fetch_manifest(self, repository: str, digest: str) -> bytes:
        self.calls.append(("fetch-manifest", digest))
        return b""


@pytest.mark.parametrize(
    "blob_key",
    ["manifest", "config", "package", "canonical"],
)
def test_integrity_mismatch_blocks_before_any_registry_write(
    tmp_path: Path,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
    blob_key: str,
) -> None:
    """T11: every transferred blob type is hash-verified before first write."""
    artifacts_dir, results, _ = built_suite
    copied = tmp_path / f"tamper-{blob_key}"
    shutil.copytree(artifacts_dir, copied)
    victim = next(item for item in results if item["component_id"] == "saga")
    oci = victim["oci"]
    digests = {
        "manifest": str(oci["oci_manifest_digest"]),
        "config": str(oci["config_digest"]),
        "package": str(oci["package_layer_digest"]),
        "canonical": str(victim["artifact"]["digest"]),
    }
    blob = (
        copied
        / oci["layout_path"]
        / "blobs"
        / "sha256"
        / digests[blob_key].removeprefix("sha256:")
    )
    blob.write_bytes(blob.read_bytes() + b"!")
    loaded = load_publication_layout(copied)
    entry = next(item for item in loaded if item["component_id"] == "saga")
    transport = _RecordingTransport()
    with pytest.raises(
        PublicationVerificationError, match="integrity mismatch before any"
    ):
        publish_artifacts_to_oci([entry], artifacts_root=copied, transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize(
    "location",
    [
        "http://ghcr.io/uploads/1",
        "https://evil.example/uploads/1",
        "https://ghcr.io:8443/uploads/1",
        "https://user:pw@ghcr.io/uploads/1",
    ],
)
def test_push_blob_refuses_credentials_to_untrusted_upload_location(
    location: str,
) -> None:
    """T12a: a hostile registry-provided Location must not receive any PUT."""
    attempts: list[tuple[str, str]] = []

    def opener(req, timeout=None):
        attempts.append((req.get_method(), req.full_url))
        if req.get_method() == "POST":
            return _FakeResponse(202, {"Location": location})
        raise AssertionError(
            f"follow-up request to hostile upload target: {req.full_url}"
        )

    transport = GHCRHTTPTransport(token="secret-token-xyz", opener=opener)
    with pytest.raises(
        PublicationBlockedError, match="outside the trusted registry endpoint"
    ):
        transport.push_blob("ghcr.io/o/r", "sha256:" + "b" * 64, b"payload")
    # only the initial POST happened; no PUT to the hostile Location
    assert [m for m, _u in attempts] == ["POST"]


def test_push_blob_relative_location_stays_on_trusted_authority() -> None:
    """T12b: Location is resolved against the endpoint authority only."""
    attempts: list[str] = []

    def opener(req, timeout=None):
        url = req.full_url
        attempts.append(url)
        if req.get_method() == "POST":
            return _FakeResponse(202, {"Location": "//evil.example/uploads/9"})
        return _FakeResponse(200, {}, b"")

    transport = GHCRHTTPTransport(token="tok", opener=opener)
    transport.push_blob("ghcr.io/o/r", "sha256:" + "c" * 64, b"payload")
    assert len(attempts) == 2
    assert attempts[0].startswith("https://ghcr.io/v2/")
    # protocol-relative Location stays pinned to ghcr.io authority
    assert attempts[1].startswith("https://ghcr.io/")
    assert "evil.example" not in attempts[1].split("/")[2]


def test_transport_never_follows_redirects() -> None:
    """T12c: 3xx surfaces as a blocked transport error; no replayed request."""
    attempts: list[str] = []

    def opener(req, timeout=None):
        attempts.append(req.full_url)
        raise _http_error(
            req.full_url,
            302,
            {"Location": "https://evil.example/catch"},
        )

    transport = GHCRHTTPTransport(token="tok", opener=opener)
    with pytest.raises(PublicationBlockedError, match=r"failed \(302\)"):
        transport.fetch_manifest("ghcr.io/o/r", "sha256:" + "d" * 64)
    assert len(attempts) == 1

    # the production default opener actively refuses redirect following
    default = GHCRHTTPTransport(token="tok2")
    director = default._open.__self__
    assert any(isinstance(h, _RefuseRedirects) for h in director.handlers)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _RefuseRedirects().redirect_request(
            urllib.request.Request("https://ghcr.io/x"),
            io.BytesIO(b""),
            302,
            "Found",
            Message(),
            "https://evil.example/y",
        )
    assert excinfo.value.code == 302
    assert "redirects are never followed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Follow-up mutation 5951966281: workflow artifact transfer via deterministic
# tar archive (T13).
# ---------------------------------------------------------------------------


def test_workflow_artifact_transfer_avoids_colon_in_upload_path() -> None:
    """T13a: artifact upload must transfer an archive path to prevent colon-in-path failures."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    _workflow_permissions, jobs = text.split("jobs:", 1)
    build_job, publish_job = jobs.split("\n  publish-by-digest:", 1)

    assert "deterministic-oci-layout.tar" in build_job
    assert "deterministic-oci-layout.tar" in publish_job

    # The upload-artifact step for deterministic-oci-layout must upload a tar file,
    # never a directory path that contains colons (e.g. raw factory/artifacts).
    upload_step = build_job.split("name: deterministic-oci-layout", 1)[1].split(
        "\n\n", 1
    )[0]
    assert "path: factory/artifacts" not in upload_step
    assert "deterministic-oci-layout.tar" in upload_step

    # Build job packages factory/artifacts into the tar archive with deterministic flags
    assert "--sort=name" in build_job
    assert "--format=gnu" in build_job
    assert "--mtime=@0" in build_job
    assert "--owner=0" in build_job
    assert "--group=0" in build_job
    assert "--numeric-owner" in build_job
    assert 'deterministic-oci-layout.tar" factory/artifacts' in build_job

    # Publish job extracts the transferred tar archive before publication
    assert "deterministic-oci-layout.tar" in publish_job
    assert "tar -xf" in publish_job


def test_deterministic_layout_tar_roundtrip_preserves_exact_bytes_and_paths(
    tmp_path: Path,
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    """T13b: tar archive transfer preserves exact files, digests, and layout."""
    artifacts_dir, _original_results, original_records = built_suite
    # Ensure there are directories with colons (e.g. sha256:...)
    colon_dirs = [p for p in artifacts_dir.iterdir() if ":" in p.name]
    assert len(colon_dirs) == 9

    tar_path = tmp_path / "deterministic-oci-layout.tar"
    subprocess.run(
        [
            "tar",
            "--sort=name",
            "--format=gnu",
            "--mtime=@0",
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "--mode=a+rX,u+w,go-w",
            "-cf",
            str(tar_path),
            artifacts_dir.name,
        ],
        cwd=artifacts_dir.parent,
        check=True,
    )
    assert tar_path.is_file()

    extract_dest = tmp_path / "extracted"
    extract_dest.mkdir()
    subprocess.run(
        ["tar", "-xf", str(tar_path)],
        cwd=extract_dest,
        check=True,
    )

    extracted_artifacts = extract_dest / artifacts_dir.name
    assert _dir_fingerprint(artifacts_dir) == _dir_fingerprint(extracted_artifacts)

    # Loader succeeds from extracted layout and reproduces identical inputs
    loaded = load_publication_layout(extracted_artifacts)
    assert len(loaded) == 9
    replayed = publish_artifacts_to_oci(
        loaded, artifacts_root=extracted_artifacts, transport=InMemoryOCIRegistry()
    )
    assert [dataclasses.asdict(rec) for rec in replayed] == [
        dataclasses.asdict(rec) for rec in original_records
    ]
