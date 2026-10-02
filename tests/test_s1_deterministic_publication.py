"""S1: derived deterministic GHCR identity verification tests (T6/T7).

These tests pin the contract of ``--mode derived``: expected remote manifest
digests come from the repository's own deterministic build, the full existing
byte-level battery is preserved, and the fetched manifest must bind its
canonical-manifest layer to the source-package lock. The legacy pinned set is
governed by ``scripts/ghcr_legacy_envelopes.json`` and is non-default.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.build_component_artifacts import build_all
from scripts.verify_ghcr_immutability import (
    ACCESS_BLOCKED,
    CANONICAL_LAYER_MEDIA_TYPE,
    LEGACY_EXPECTED_MANIFEST_DIGESTS,
    PASS,
    ContentUnavailableError,
    _load_legacy_expected,
    derive_expected_manifest_digests,
    main,
    repository_for,
    verify_all,
    verify_component,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_FILE = REPO_ROOT / "scripts" / "ghcr_legacy_envelopes.json"


@pytest.fixture(scope="module")
def deterministic_build(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, list[dict[str, Any]]]:
    out = tmp_path_factory.mktemp("s1_derived_build")
    return out, build_all(REPO_ROOT, out)


def _layout_reader(artifacts_root: Path, results: list[dict[str, Any]]):
    """Read-only reader serving the exact bytes of the built OCI layouts."""
    blobs: dict[tuple[str, str], bytes] = {}
    for item in results:
        component = str(item["component_id"])
        repo = repository_for(component)
        blob_dir = artifacts_root / "oci" / component / "blobs" / "sha256"
        for blob in blob_dir.iterdir():
            blobs[(repo, "sha256:" + blob.name)] = blob.read_bytes()

    class _LayoutReader:
        def fetch_manifest(self, repository: str, digest: str) -> bytes:
            key = (repository, digest)
            if key not in blobs:
                raise ContentUnavailableError(f"manifest {digest} not in layout")
            return blobs[key]

        def fetch_blob(self, repository: str, digest: str) -> bytes:
            key = (repository, digest)
            if key not in blobs:
                raise ContentUnavailableError(f"blob {digest} not in layout")
            return blobs[key]

    return _LayoutReader()


def test_derived_expectations_match_deterministic_build(
    deterministic_build: tuple[Path, list[dict[str, Any]]],
) -> None:
    _, results = deterministic_build
    expected, locks = derive_expected_manifest_digests(REPO_ROOT)
    assert sorted(expected) == sorted(locks)
    by_id = {str(item["component_id"]): item for item in results}
    assert set(expected) == set(by_id)
    for component, digest in expected.items():
        assert digest == by_id[component]["oci"]["oci_manifest_digest"]
        assert locks[component] == by_id[component]["artifact"]["digest"]
        # A deterministic envelope digest never equals the historical oras-era
        # digest nor the source-package lock: three distinct identity spaces.
        assert digest != LEGACY_EXPECTED_MANIFEST_DIGESTS[component]
        assert digest != locks[component]


def test_derived_mode_passes_over_the_layout_reader(
    deterministic_build: tuple[Path, list[dict[str, Any]]],
) -> None:
    artifacts_root, results = deterministic_build
    reader = _layout_reader(artifacts_root, results)
    expected, locks = derive_expected_manifest_digests(REPO_ROOT)
    verified = verify_all(reader, expected, canonical_locks=locks)
    assert len(verified) == 9
    assert {r.classification for r in verified} == {PASS}


def test_derived_mode_rejects_foreign_canonical_layer(
    deterministic_build: tuple[Path, list[dict[str, Any]]],
) -> None:
    """Same blobs, foreign canonical layer digest => the binding check fails."""
    artifacts_root, results = deterministic_build
    target = next(item for item in results if item["component_id"] == "saga")
    repo = repository_for("saga")
    manifest_digest = str(target["oci"]["oci_manifest_digest"])
    blob_dir = artifacts_root / "oci" / "saga" / "blobs" / "sha256"
    manifest_bytes = (blob_dir / manifest_digest.removeprefix("sha256:")).read_bytes()
    document = json.loads(manifest_bytes)
    tampered_digest = "sha256:" + hashlib.sha256(b"tampered").hexdigest()
    for layer in document["layers"]:
        if layer["mediaType"] == CANONICAL_LAYER_MEDIA_TYPE:
            layer["digest"] = tampered_digest
    tampered = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    class _TamperedReader:
        def fetch_manifest(self, repository: str, digest: str) -> bytes:
            if (repository, digest) == (repo, manifest_digest):
                return tampered
            raise AssertionError("unexpected manifest fetch")

        def fetch_blob(self, repository: str, digest: str) -> bytes:
            blob = blob_dir / digest.removeprefix("sha256:")
            if blob.is_file():
                return blob.read_bytes()
            if digest == tampered_digest:
                return b"tampered"
            raise AssertionError("unexpected blob fetch")

    result = verify_component(
        _TamperedReader(),
        "saga",
        manifest_digest,
        expected_canonical_lock=str(target["artifact"]["digest"]),
    )
    # The tampered envelope no longer hashes to the expected digest (FAIL on
    # digest mismatch) and its canonical layer is foreign; either way the
    # derived gate must never classify this as PASS.
    assert result.classification != PASS
    assert any(
        "canonical layer" in reason or "digest mismatch" in reason
        for reason in result.reasons
    )


def test_missing_remote_content_stays_access_blocked_not_pass(
    deterministic_build: tuple[Path, list[dict[str, Any]]],
) -> None:
    expected, locks = derive_expected_manifest_digests(REPO_ROOT)
    empty_reader = _layout_reader(Path("/nonexistent-s1"), [])
    verified = verify_all(empty_reader, expected, canonical_locks=locks)
    assert {r.classification for r in verified} == {ACCESS_BLOCKED}


def test_legacy_envelope_file_matches_pinned_constants() -> None:
    document = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
    assert document["schema"] == "ghcr-legacy-envelopes/1"
    assert dict(document["components"]) == dict(LEGACY_EXPECTED_MANIFEST_DIGESTS)
    assert _load_legacy_expected() == dict(LEGACY_EXPECTED_MANIFEST_DIGESTS)


def test_default_main_mode_is_derived(tmp_path: Path) -> None:
    """The default gate must derive expectations, never consult legacy silently."""
    expected_derived, _locks = derive_expected_manifest_digests(REPO_ROOT)
    empty_reader = _layout_reader(Path("/nonexistent-s1"), [])
    code = main(["--output", str(tmp_path / "out")], reader=empty_reader)
    assert code == 1  # ACCESS BLOCKED for all derived references
    document = json.loads((tmp_path / "out" / "verification-matrix.json").read_text())
    assert document["mode"] == "derived"
    assert document["context"]["expected_digests_mode"] == "derived"
    assert {c["expected_manifest_digest"] for c in document["components"]} == set(
        expected_derived.values()
    )
