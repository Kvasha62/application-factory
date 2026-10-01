"""Positive and adversarial tests for the nine-component artifact publication
pipeline, OCI packaging, GHCR publication seam, dependency closure validation,
and fail-closed registry reconciliation (Issue #127).
"""

from __future__ import annotations

import copy
import dataclasses
import shutil
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from component_registry import (
    REGISTRY_PATH,
    load_registry_document,
    validate_document,
)
from factory_artifact import canonical_manifest_reference
from scripts.build_component_artifacts import (
    APPROVED_COMPONENT_IDS,
    COMPONENT_ROOTS,
    DependencyClosureError,
    GHCRHTTPTransport,
    InMemoryOCIRegistry,
    PublicationBlockedError,
    PublicationRecord,
    PublicationVerificationError,
    build_all,
    declaration_for,
    dependency_closure_violations,
    main,
    publication_violations,
    publish_artifacts_to_oci,
    reconcile_registry,
    redact_secrets,
    validate_dependency_closure,
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
        digest = item["artifact"]["digest"]
        assert digest.startswith("sha256:")
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
