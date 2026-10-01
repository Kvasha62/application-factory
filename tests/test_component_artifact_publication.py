"""Positive and adversarial tests for the nine-component artifact publication
pipeline, OCI packaging, GHCR publication seam, dependency closure validation,
and fail-closed registry reconciliation (Issue #127).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

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
    build_all,
    declaration_for,
    dependency_closure_violations,
    extract_workflow_trigger_paths,
    main,
    publication_violations,
    publish_artifacts_to_oci,
    publish_item_to_ghcr,
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


def test_publish_item_to_ghcr_pushes_when_tag_not_yet_present(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite
    item = results[0]
    calls: list[list[str]] = []

    def fake_runner(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cmd[:3] == ["oras", "manifest", "fetch"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Error: not found"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    outcome = publish_item_to_ghcr(
        item, artifacts_root=artifacts_dir, runner=fake_runner
    )
    assert outcome == "published"
    assert len(calls) == 2
    assert calls[0][:3] == ["oras", "manifest", "fetch"]
    assert calls[1][:2] == ["oras", "push"]


def test_publish_item_to_ghcr_is_idempotent_when_same_digest_already_published(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite
    item = results[0]
    digest = item["artifact"]["digest"]
    calls: list[list[str]] = []

    remote_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "layers": [
                {
                    "mediaType": CANONICAL_MANIFEST_MEDIA_TYPE,
                    "digest": digest,
                }
            ],
        }
    )

    def fake_runner(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=remote_manifest, stderr="")

    outcome = publish_item_to_ghcr(
        item, artifacts_root=artifacts_dir, runner=fake_runner
    )
    assert outcome == "already_published"
    assert len(calls) == 1
    assert calls[0][:3] == ["oras", "manifest", "fetch"]


def test_publish_item_to_ghcr_refuses_to_overwrite_existing_tag_with_different_digest(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite
    item = results[0]
    calls: list[list[str]] = []

    remote_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "layers": [
                {
                    "mediaType": CANONICAL_MANIFEST_MEDIA_TYPE,
                    "digest": "sha256:" + "f" * 64,
                }
            ],
        }
    )

    def fake_runner(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=remote_manifest, stderr="")

    with pytest.raises(
        ValueError,
        match=r"immutable publication violation .* refusing to overwrite",
    ):
        publish_item_to_ghcr(item, artifacts_root=artifacts_dir, runner=fake_runner)

    assert len(calls) == 1
    assert calls[0][:3] == ["oras", "manifest", "fetch"]


def test_publish_item_to_ghcr_fails_closed_on_registry_inspection_error(
    built_suite: tuple[Path, list[dict[str, Any]], list[PublicationRecord]],
) -> None:
    artifacts_dir, results, _ = built_suite
    item = results[0]

    def fake_runner(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="response status code 403: Forbidden"
        )

    with pytest.raises(RuntimeError, match="failed to inspect existing GHCR tag"):
        publish_item_to_ghcr(item, artifacts_root=artifacts_dir, runner=fake_runner)
