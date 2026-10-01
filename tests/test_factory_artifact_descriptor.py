"""Adversarial tests for the immutable, digest-bound artifact descriptor.

The chain under test is the one ``factory/registry/README.md`` §9.1 and
ADR-0018 §3.1, §4, §5 make normative:

.. code-block:: text

    component declaration
      → canonical artifact representation
      → deterministic SHA-256 digest
      → immutable digest-bound descriptor
      → fail-closed verification
      → publication-ready registry metadata

Every negative case below attacks one link of that chain and requires a
deterministic rejection that names the violated invariant. No case relies on
mocks: each one builds a real sealed artifact on disk and asks the real
builders and the real verifier for an answer.

Nothing here declares a component deployable or publishable — the tests assert
the opposite, that artifact metadata states only what it can prove.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from factory_artifact import (
    ArtifactDescriptor,
    ArtifactError,
    ArtifactMetadataError,
    ArtifactNotPublishedError,
    ArtifactVerificationError,
    artifact_violations,
    canonical_manifest_reference,
    describe_container_image,
    describe_source_package,
    descriptor_from_registry_metadata,
    registry_metadata,
    registry_metadata_violations,
    verify_artifact,
)
from factory_artifact.container_image import build_container_image_canonical
from factory_artifact.source_package import (
    SourcePackageError,
    build_source_package_canonical,
)

IMAGE_CONFIG = {"entrypoint": [], "cmd": [], "working_dir": ""}


# ---------------------------------------------------------------------------
# Fixtures — real sealed artifacts, no mocks
# ---------------------------------------------------------------------------


def sealed_source_package(root: Path) -> Path:
    """Create a sealed ``source_package/v1`` artifact and return its root."""
    (root / "nested").mkdir(parents=True)
    (root / "app.txt").write_bytes(b"app")
    (root / "nested" / "data.txt").write_bytes(b"data")
    return root


def source_declaration() -> dict[str, dict[str, bool]]:
    return {
        "app.txt": {"owned": True, "executable": False},
        "nested": {"owned": False, "executable": False},
        "nested/data.txt": {"owned": False, "executable": False},
    }


def sealed_container_image(root: Path) -> Path:
    """Create a sealed ``container_image/v1`` artifact and return its root."""
    base = root / "layers" / "0"
    component = root / "layers" / "1"
    base.mkdir(parents=True)
    component.mkdir(parents=True)
    (base / "runtime.txt").write_bytes(b"runtime")
    (component / "app.txt").write_bytes(b"app")
    return root


def image_declaration() -> dict[str, Any]:
    return {
        "layers": [
            {
                "ownership": "base",
                "entries": {"runtime.txt": {"owned": False, "executable": False}},
            },
            {
                "ownership": "component",
                "entries": {"app.txt": {"owned": True, "executable": False}},
            },
        ],
        "config": dict(IMAGE_CONFIG),
    }


@pytest.fixture
def package_root(tmp_path: Path) -> Path:
    return sealed_source_package(tmp_path / "package")


@pytest.fixture
def package_declaration() -> dict[str, dict[str, bool]]:
    return source_declaration()


@pytest.fixture
def package(package_root: Path, package_declaration: dict[str, dict[str, bool]]):
    return describe_source_package(package_root, package_declaration)


@pytest.fixture
def image_root(tmp_path: Path) -> Path:
    return sealed_container_image(tmp_path / "image")


@pytest.fixture
def image(image_root: Path):
    return describe_container_image(image_root, image_declaration())


def violations_for(
    root: Path,
    descriptor: ArtifactDescriptor,
    declaration: Mapping[str, Any],
    **kwargs: Any,
) -> list[str]:
    return artifact_violations(root, descriptor, declaration, **kwargs)


def rejects(
    root: Path,
    descriptor: ArtifactDescriptor,
    declaration: Mapping[str, Any],
    fragment: str,
    **kwargs: Any,
) -> list[str]:
    """Assert verification fails and names ``fragment`` as the reason."""
    violations = violations_for(root, descriptor, declaration, **kwargs)
    assert violations, "expected verification to fail, but the artifact passed"
    assert any(
        fragment in item for item in violations
    ), f"expected a violation containing {fragment!r}, got:\n" + "\n".join(violations)
    with pytest.raises(ArtifactVerificationError) as error:
        verify_artifact(root, descriptor, declaration, **kwargs)
    assert tuple(error.value.violations) == tuple(violations)
    return violations


# ---------------------------------------------------------------------------
# Positive: canonical representation and deterministic digest
# ---------------------------------------------------------------------------


def test_the_digest_is_the_sha256_of_the_canonical_representation(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    _, canonical_bytes, artifact_digest = build_source_package_canonical(
        package_root, package_declaration
    )

    assert artifact_digest == "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()
    assert package.digest == artifact_digest
    assert package.digest.startswith("sha256:")
    assert len(package.digest) == len("sha256:") + 64


def test_the_same_content_and_declaration_produce_the_same_identity(
    tmp_path: Path, package
) -> None:
    other_root = sealed_source_package(tmp_path / "elsewhere")
    other = describe_source_package(other_root, source_declaration())

    assert other.digest == package.digest
    assert other.canonical_manifest == package.canonical_manifest
    assert other == package


def test_filesystem_creation_order_does_not_change_the_identity(
    tmp_path: Path, package
) -> None:
    """Traversal order is not part of the canonical representation."""
    root = tmp_path / "reversed"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "data.txt").write_bytes(b"data")
    (root / "app.txt").write_bytes(b"app")

    assert describe_source_package(root, source_declaration()) == package


def test_declaration_insertion_order_does_not_change_the_identity(
    package_root: Path, package
) -> None:
    """Dictionary insertion order is not part of the canonical representation."""
    shuffled = {
        "nested/data.txt": {"owned": False, "executable": False},
        "app.txt": {"owned": True, "executable": False},
        "nested": {"owned": False, "executable": False},
    }

    assert describe_source_package(package_root, shuffled) == package


def test_the_artifact_digest_changes_when_relevant_content_changes(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    (package_root / "app.txt").write_bytes(b"tampered")

    changed = describe_source_package(package_root, package_declaration)

    assert changed.digest != package.digest
    assert changed.canonical_manifest != package.canonical_manifest


def test_layer_and_entry_order_are_canonical_for_a_container_image(
    tmp_path: Path, image
) -> None:
    """Physical enumeration order does not leak into the image identity."""
    root = tmp_path / "reordered"
    component = root / "layers" / "1"
    base = root / "layers" / "0"
    component.mkdir(parents=True)
    base.mkdir(parents=True)
    (component / "app.txt").write_bytes(b"app")
    (base / "runtime.txt").write_bytes(b"runtime")

    rebuilt = describe_container_image(root, image_declaration())

    assert rebuilt.digest == image.digest
    assert rebuilt.layer_digests == image.layer_digests
    assert len(image.layer_digests) == 2


def test_swapping_layer_contents_changes_the_image_identity(
    image_root: Path, image
) -> None:
    (image_root / "layers" / "1" / "app.txt").write_bytes(b"different")

    assert describe_container_image(image_root, image_declaration()).digest != (
        image.digest
    )


# ---------------------------------------------------------------------------
# Positive: the descriptor is immutable and digest-bound
# ---------------------------------------------------------------------------


def test_the_descriptor_is_immutable(package) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        package.digest = "sha256:" + "0" * 64  # type: ignore[misc]


def test_the_descriptor_is_hashable_and_comparable(package) -> None:
    assert hash(package) == hash(dataclasses.replace(package))
    assert package == dataclasses.replace(package)


def test_the_canonical_manifest_reference_is_derived_from_the_digest(package) -> None:
    assert package.canonical_manifest == canonical_manifest_reference(package.digest)
    assert package.canonical_manifest == (
        f"factory/artifacts/{package.digest}/canonical.json"
    )


def test_a_descriptor_rejects_a_reference_that_is_not_its_own_digest() -> None:
    """A reference bound to some other artifact is a mutable reference."""
    foreign = "factory/artifacts/sha256:" + "f" * 64 + "/canonical.json"

    with pytest.raises(ArtifactMetadataError, match="not bound to the declared digest"):
        ArtifactDescriptor(
            artifact_type="source_package",
            canonical_form="source_package/v1",
            digest="sha256:" + "a" * 64,
            canonical_manifest=foreign,
        )


def test_a_descriptor_rejects_a_reference_without_the_digest_prefix() -> None:
    """``factory/artifacts/<hex>/canonical.json`` is a forbidden spelling."""
    bare = "factory/artifacts/" + "a" * 64 + "/canonical.json"

    with pytest.raises(ArtifactMetadataError, match="content-addressed reference"):
        ArtifactDescriptor(
            artifact_type="source_package",
            canonical_form="source_package/v1",
            digest="sha256:" + "a" * 64,
            canonical_manifest=bare,
        )


@pytest.mark.parametrize("selector", ["latest", "current", "default", "stable", "edge"])
def test_a_descriptor_rejects_a_floating_selector(selector: str) -> None:
    with pytest.raises(ArtifactMetadataError, match="never by a floating selector"):
        ArtifactDescriptor(
            artifact_type="source_package",
            canonical_form="source_package/v1",
            digest=selector,
            canonical_manifest=canonical_manifest_reference("sha256:" + "a" * 64),
        )


def test_a_descriptor_rejects_a_mutable_layer_digest_sequence() -> None:
    with pytest.raises(ArtifactMetadataError, match="immutable tuple"):
        ArtifactDescriptor(
            artifact_type="container_image",
            canonical_form="container_image/v1",
            digest="sha256:" + "a" * 64,
            canonical_manifest=canonical_manifest_reference("sha256:" + "a" * 64),
            layer_digests=["sha256:" + "b" * 64],  # type: ignore[arg-type]
        )


def test_a_descriptor_rejects_an_inconsistent_canonical_form() -> None:
    with pytest.raises(ArtifactMetadataError, match="does not match artifact_type"):
        ArtifactDescriptor(
            artifact_type="source_package",
            canonical_form="container_image/v1",
            digest="sha256:" + "a" * 64,
            canonical_manifest=canonical_manifest_reference("sha256:" + "a" * 64),
        )


# ---------------------------------------------------------------------------
# Positive: verification of an untouched artifact
# ---------------------------------------------------------------------------


def test_an_untouched_artifact_verifies(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    assert verify_artifact(package_root, package, package_declaration) is package
    assert violations_for(package_root, package, package_declaration) == []


def test_an_untouched_image_verifies(image_root: Path, image) -> None:
    assert verify_artifact(image_root, image, image_declaration()) is image


def test_the_stored_canonical_manifest_is_accepted_when_it_matches(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    _, canonical_bytes, _ = build_source_package_canonical(
        package_root, package_declaration
    )

    assert (
        violations_for(
            package_root,
            package,
            package_declaration,
            canonical_manifest_bytes=canonical_bytes,
        )
        == []
    )


# ---------------------------------------------------------------------------
# Negative: missing artifact
# ---------------------------------------------------------------------------


def test_a_missing_artifact_is_rejected(
    tmp_path: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    rejects(
        tmp_path / "absent",
        package,
        package_declaration,
        "the sealed artifact is missing",
    )


def test_a_file_in_place_of_the_artifact_root_is_rejected(
    tmp_path: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    placeholder = tmp_path / "not-a-directory"
    placeholder.write_bytes(b"not a sealed artifact")

    rejects(
        placeholder,
        package,
        package_declaration,
        "the sealed artifact is missing",
    )


def test_an_artifact_type_that_publishes_nothing_has_no_descriptor() -> None:
    """``artifact_type: none`` is an explicit statement, not a missing value."""
    with pytest.raises(ArtifactNotPublishedError, match="publishes no artifact"):
        descriptor_from_registry_metadata(
            {
                "artifact_type": "none",
                "digest": None,
                "pinned": False,
                "canonical_form": None,
                "canonical_manifest": None,
            }
        )


# ---------------------------------------------------------------------------
# Negative: digest mismatch and content mutation
# ---------------------------------------------------------------------------


def test_content_mutation_after_the_digest_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    (package_root / "app.txt").write_bytes(b"mutated after digest")

    violations = rejects(
        package_root,
        package,
        package_declaration,
        "does not match the recomputed digest",
    )
    assert any("changed after the digest was calculated" in item for item in violations)


def test_an_added_file_after_the_digest_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    (package_root / "extra.txt").write_bytes(b"smuggled in")

    rejects(
        package_root,
        package,
        package_declaration,
        "the canonical representation could not be derived",
    )


def test_a_removed_file_after_the_digest_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    (package_root / "nested" / "data.txt").unlink()

    rejects(
        package_root,
        package,
        package_declaration,
        "the canonical representation could not be derived",
    )


def test_a_renamed_path_after_the_digest_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    (package_root / "app.txt").rename(package_root / "renamed.txt")

    rejects(
        package_root,
        package,
        package_declaration,
        "the canonical representation could not be derived",
    )


def test_a_mismatched_digest_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    forged = dataclasses.replace(
        package,
        digest="sha256:" + "9" * 64,
        canonical_manifest=canonical_manifest_reference("sha256:" + "9" * 64),
    )

    rejects(
        package_root,
        forged,
        package_declaration,
        "does not match the recomputed digest",
    )


def test_a_mismatched_layer_digest_is_rejected(image_root: Path, image) -> None:
    forged = dataclasses.replace(
        image, layer_digests=("sha256:" + "9" * 64, *image.layer_digests[1:])
    )

    rejects(
        image_root,
        forged,
        image_declaration(),
        "do not match the declared",
    )


# ---------------------------------------------------------------------------
# Negative: canonical representation mismatch
# ---------------------------------------------------------------------------


def test_a_stored_canonical_manifest_that_does_not_hash_to_the_digest_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    rejects(
        package_root,
        package,
        package_declaration,
        "is not the SHA-256 of the stored canonical manifest",
        canonical_manifest_bytes=b'{"entries":[],"form":"source_package/v1"}',
    )


def test_a_stored_canonical_manifest_that_disagrees_with_the_artifact_is_rejected(
    package_root: Path,
    package_declaration: dict[str, dict[str, bool]],
    package,
    tmp_path: Path,
) -> None:
    """Same digest input, different artifact: the manifest must still agree."""
    other_root = tmp_path / "other"
    (other_root / "app.txt").mkdir(parents=True)
    other_declaration = {"app.txt": {"owned": True, "executable": False}}
    _, other_bytes, _ = build_source_package_canonical(other_root, other_declaration)

    forged = dataclasses.replace(
        package,
        digest="sha256:" + hashlib.sha256(other_bytes).hexdigest(),
        canonical_manifest=canonical_manifest_reference(
            "sha256:" + hashlib.sha256(other_bytes).hexdigest()
        ),
    )

    violations = rejects(
        package_root,
        forged,
        package_declaration,
        "canonical representation mismatch",
        canonical_manifest_bytes=other_bytes,
    )
    assert any("does not match the recomputed digest" in item for item in violations)


def test_a_canonical_manifest_supplied_as_text_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], package
) -> None:
    rejects(
        package_root,
        package,
        package_declaration,
        "must be supplied as bytes",
        canonical_manifest_bytes="{}",  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Negative: canonicalization failure
# ---------------------------------------------------------------------------


def test_a_declaration_that_hides_a_physical_entry_is_rejected(
    package_root: Path, package
) -> None:
    """The declaration is the Factory's source of truth, so a gap is fatal."""
    incomplete = source_declaration()
    del incomplete["app.txt"]

    rejects(
        package_root,
        package,
        incomplete,
        "the canonical representation could not be derived",
    )


def test_a_declaration_for_an_absent_entry_is_rejected(
    package_root: Path, package
) -> None:
    invented = source_declaration()
    invented["ghost.txt"] = {"owned": True, "executable": False}

    rejects(
        package_root,
        package,
        invented,
        "the canonical representation could not be derived",
    )


def test_a_non_executable_declaration_over_executable_content_is_rejected(
    tmp_path: Path,
) -> None:
    """Hiding executable content behind ``executable=false`` fails closed."""
    root = tmp_path / "hiding"
    root.mkdir()
    script = root / "run.txt"
    script.write_bytes(b"#!/bin/sh\n")

    declaration = {"run.txt": {"owned": True, "executable": False}}

    with pytest.raises(SourcePackageError, match="executable"):
        describe_source_package(root, declaration)


def test_a_reserved_path_inside_the_sealed_artifact_is_rejected(tmp_path: Path) -> None:
    """``canonical.json`` is derived metadata and may not be an entry."""
    root = tmp_path / "circular"
    root.mkdir()
    (root / "canonical.json").write_bytes(b"{}")

    declaration = {"canonical.json": {"owned": True, "executable": False}}

    with pytest.raises(SourcePackageError, match="canonical.json"):
        describe_source_package(root, declaration)


def test_a_non_nfc_path_is_rejected(tmp_path: Path) -> None:
    """Canonicalization must reject, never silently normalize."""
    root = tmp_path / "nfc"
    root.mkdir()
    decomposed = "caf\u0065\u0301.txt"
    (root / decomposed).write_bytes(b"x")

    declaration = {decomposed: {"owned": True, "executable": False}}

    with pytest.raises(SourcePackageError, match="NFC"):
        describe_source_package(root, declaration)


def test_an_unknown_artifact_type_is_rejected(
    package_root: Path, package_declaration: dict[str, dict[str, bool]]
) -> None:
    from factory_artifact import describe_artifact

    with pytest.raises(ArtifactError, match="not a published artifact type"):
        describe_artifact("tarball", package_root, package_declaration)


# ---------------------------------------------------------------------------
# Negative: mutable / non-immutable artifact references
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        {"digest": "sha256:" + "a" * 64},
        ["sha256:" + "a" * 64],
        "factory/artifacts/sha256:" + "a" * 64 + "/canonical.json",
        "latest",
        None,
    ],
)
def test_a_mutable_or_bare_reference_is_not_an_artifact_identity(
    package_root: Path, package_declaration: dict[str, dict[str, bool]], reference
) -> None:
    rejects(
        package_root,
        reference,  # type: ignore[arg-type]
        package_declaration,
        "a mutable artifact reference is not an artifact identity",
    )


# ---------------------------------------------------------------------------
# Publication-ready registry metadata
# ---------------------------------------------------------------------------


def test_registry_metadata_states_only_what_the_artifact_proves(package) -> None:
    metadata = registry_metadata(package)

    assert metadata == {
        "artifact_type": "source_package",
        "digest": package.digest,
        "pinned": True,
        "canonical_form": "source_package/v1",
        "canonical_manifest": package.canonical_manifest,
    }


def test_registry_metadata_makes_no_deployability_or_publishability_claim(
    package,
) -> None:
    """Those are lifecycle claims; artifact metadata must not smuggle them in."""
    metadata = registry_metadata(package)

    assert "deployable" not in metadata
    assert "publishable" not in metadata
    assert "publishability_blockers" not in metadata


def test_registry_metadata_round_trips_through_the_descriptor(package) -> None:
    assert descriptor_from_registry_metadata(registry_metadata(package)) == package


def test_registry_metadata_requires_an_immutable_descriptor() -> None:
    with pytest.raises(ArtifactError, match="immutable ArtifactDescriptor"):
        registry_metadata({"digest": "sha256:" + "a" * 64})  # type: ignore[arg-type]


def test_a_missing_digest_is_rejected_in_registry_metadata() -> None:
    violations = registry_metadata_violations(
        {
            "artifact_type": "container_image",
            "digest": None,
            "pinned": True,
            "canonical_form": "container_image/v1",
            "canonical_manifest": None,
        }
    )

    assert any("requires an immutable digest" in item for item in violations)
    with pytest.raises(ArtifactMetadataError, match="requires an immutable digest"):
        descriptor_from_registry_metadata(
            {
                "artifact_type": "container_image",
                "digest": None,
                "pinned": True,
                "canonical_form": "container_image/v1",
                "canonical_manifest": None,
            }
        )


def test_an_unpinned_artifact_is_rejected_in_registry_metadata() -> None:
    digest = "sha256:" + "c" * 64
    violations = registry_metadata_violations(
        {
            "artifact_type": "container_image",
            "digest": digest,
            "pinned": False,
            "canonical_form": "container_image/v1",
            "canonical_manifest": canonical_manifest_reference(digest),
        }
    )

    assert any("is not pinned" in item for item in violations)


def test_inconsistent_registry_metadata_is_rejected_field_by_field() -> None:
    violations = registry_metadata_violations(
        {
            "artifact_type": "container_image",
            "digest": "latest",
            "pinned": False,
            "canonical_form": "source_package/v1",
            "canonical_manifest": "factory/artifacts/latest/canonical.json",
        }
    )

    assert any("never by a floating selector" in item for item in violations)
    assert any("is not pinned" in item for item in violations)
    assert any("does not match artifact_type" in item for item in violations)
    assert any("content-addressed reference" in item for item in violations)


def test_registry_metadata_validation_is_deterministic() -> None:
    block = {
        "artifact_type": "source_package",
        "digest": "sha256:" + "e" * 64,
        "pinned": True,
        "canonical_form": "container_image/v1",
        "canonical_manifest": "factory/artifacts/sha256:"
        + "0" * 64
        + "/canonical.json",
    }

    first = registry_metadata_violations(block)
    second = registry_metadata_violations(dict(block))

    assert first == second
    assert first


def test_a_missing_artifact_block_is_rejected() -> None:
    assert registry_metadata_violations(None) == [
        "artifact: explicit artifact identity metadata is required"
    ]


def test_the_canonical_manifest_reference_rejects_a_floating_digest() -> None:
    with pytest.raises(ArtifactError, match="never by a floating selector"):
        canonical_manifest_reference("latest")


def test_a_container_image_descriptor_carries_its_layer_digests(
    image_root: Path, image
) -> None:
    _, _, _, layer_digests = build_container_image_canonical(
        image_root, image_declaration()
    )

    assert image.layer_digests == tuple(layer_digests)
    assert len(image.layer_digests) == 2
    assert all(item.startswith("sha256:") for item in image.layer_digests)
