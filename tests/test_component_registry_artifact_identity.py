"""Adversarial tests for the registry's canonical manifest reference.

``factory/registry/README.md`` §9.1.6 (Variant B) makes the registry the place
that records artifact identity, and §9.1.4 fixes the *only* legal spelling of
the derived canonical manifest reference:

.. code-block:: text

    factory/artifacts/sha256:<64 lowercase hex>/canonical.json

The reference is content-addressed by ``artifact.digest``, so it is immutable
by construction: a published artifact has exactly one legal reference, and a
floating selector has none. These tests defend that against registry metadata
that is missing the reference, floating, malformed, or bound to a different
digest than the one the entry declares.

They also defend the honest current state: the canonical registry publishes no
artifact for any component, and no entry claims deployability or
publishability. Nothing in this suite selects a production component set or an
external registry.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path

import pytest

from component_registry import (
    REGISTRY_PATH,
    discover_root,
    is_floating_selector,
    load_registry_document,
    validate_document,
)
from factory_artifact import canonical_manifest_reference

#: A well-formed digest used by the synthetic published-artifact entries below.
DIGEST = "sha256:" + "a" * 64
REFERENCE = canonical_manifest_reference(DIGEST)

TARGET = "authorization"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def document(root: Path) -> Mapping:
    return load_registry_document(root / REGISTRY_PATH)


def with_artifact(document: Mapping, target: str, **fields: object) -> Mapping:
    """Return a deep copy whose ``artifact`` block of one entry is merged."""
    clone = copy.deepcopy(document)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry["artifact"].update(fields)
            return clone
    raise AssertionError(f"no such component: {target}")


def with_published_artifact(
    document: Mapping, target: str, **fields: object
) -> Mapping:
    """Return a deep copy whose entry publishes an artifact.

    The synthetic entry stays internally honest: once an artifact exists,
    ``deployment_artifact_absent`` is no longer a true blocker, so the
    remaining blocker is stated instead. Deployability and publishability are
    *not* claimed — the entry only proves that the metadata shape validates.
    """
    clone = with_artifact(document, target, **fields)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry["lifecycle"]["publishability_blockers"] = ["migrations_absent"]
    return clone


def errors_for(document: object, root: Path) -> list[str]:
    return validate_document(document, root=root)


def rejects(document: object, root: Path, fragment: str) -> None:
    """Assert validation rejects ``document`` naming ``fragment``."""
    errors = errors_for(document, root)
    assert errors, "expected validation to fail, but the document was accepted"
    assert any(
        fragment in error for error in errors
    ), f"expected an error containing {fragment!r}, got:\n" + "\n".join(errors)


def published(**overrides: object) -> dict[str, object]:
    """A consistent, pinned published-artifact block."""
    block: dict[str, object] = {
        "artifact_type": "container_image",
        "digest": DIGEST,
        "pinned": True,
        "canonical_form": "container_image/v1",
        "canonical_manifest": REFERENCE,
    }
    block.update(overrides)
    return block


# ---------------------------------------------------------------------------
# Positive: the canonical registry states truthfully that nothing is published
# ---------------------------------------------------------------------------


def test_the_canonical_registry_is_valid(root: Path, document: Mapping) -> None:
    assert errors_for(document, root) == []


def test_no_entry_publishes_an_artifact(document: Mapping) -> None:
    """Registration is not production readiness (README §10)."""
    for entry in document["components"]:
        artifact = entry["artifact"]
        assert artifact["artifact_type"] == "none"
        assert artifact["digest"] is None
        assert artifact["pinned"] is False
        assert artifact.get("canonical_form") is None
        assert artifact.get("canonical_manifest") is None


def test_no_entry_claims_deployability_or_publishability(document: Mapping) -> None:
    for entry in document["components"]:
        lifecycle = entry["lifecycle"]
        assert lifecycle["deployable"] is False
        assert lifecycle["publishable"] is False
        assert lifecycle[
            "publishability_blockers"
        ], f"{entry['component_id']!r} must state why it is not publishable"


# ---------------------------------------------------------------------------
# Positive: a consistent published-artifact entry is accepted
# ---------------------------------------------------------------------------


def test_a_published_artifact_with_its_derived_reference_is_accepted(
    root: Path, document: Mapping
) -> None:
    clone = with_published_artifact(document, TARGET, **published())

    assert errors_for(clone, root) == []


def test_a_source_package_entry_with_its_derived_reference_is_accepted(
    root: Path, document: Mapping
) -> None:
    clone = with_published_artifact(
        document,
        TARGET,
        **published(
            artifact_type="source_package",
            canonical_form="source_package/v1",
        ),
    )

    assert errors_for(clone, root) == []


def test_a_bare_hex_digest_is_rejected(root: Path, document: Mapping) -> None:
    """The registry spells a digest with its ``sha256:`` prefix.

    The content-addressed reference is derived from that spelling, so a digest
    declared without the prefix cannot have a valid reference either.
    """
    clone = with_published_artifact(
        document, TARGET, **published(digest=DIGEST.removeprefix("sha256:"))
    )

    assert errors_for(clone, root)


# ---------------------------------------------------------------------------
# Negative: missing, floating, malformed and mis-bound references
# ---------------------------------------------------------------------------


def test_a_published_artifact_without_a_canonical_manifest_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_published_artifact(
        document, TARGET, **published(canonical_manifest=None)
    )
    rejects(
        clone,
        root,
        "requires the immutable content-addressed canonical manifest reference",
    )


def test_an_omitted_canonical_manifest_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_published_artifact(document, TARGET, **published())
    for entry in clone["components"]:
        if entry.get("component_id") == TARGET:
            del entry["artifact"]["canonical_manifest"]

    rejects(
        clone,
        root,
        "requires the immutable content-addressed canonical manifest reference",
    )


def test_a_reference_bound_to_another_digest_is_rejected(
    root: Path, document: Mapping
) -> None:
    """A reference that points at a different artifact is a mutable reference."""
    foreign = canonical_manifest_reference("sha256:" + "f" * 64)
    clone = with_published_artifact(
        document, TARGET, **published(canonical_manifest=foreign)
    )

    rejects(clone, root, "is not bound to the declared digest")


@pytest.mark.parametrize("selector", ["latest", "current", "default", "stable"])
def test_a_floating_reference_is_rejected(
    selector: str, root: Path, document: Mapping
) -> None:
    assert is_floating_selector(selector)
    clone = with_published_artifact(
        document, TARGET, **published(canonical_manifest=selector)
    )

    rejects(clone, root, "is a floating selector")


@pytest.mark.parametrize("selector", ["latest", "stable"])
def test_a_floating_selector_inside_a_reference_path_is_rejected(
    selector: str, root: Path, document: Mapping
) -> None:
    """A floating token in a path is still not a content-addressed reference."""
    clone = with_published_artifact(
        document,
        TARGET,
        **published(canonical_manifest=f"factory/artifacts/{selector}"),
    )

    rejects(clone, root, "not an immutable content-addressed reference")


def test_a_reference_without_the_digest_prefix_is_rejected(
    root: Path, document: Mapping
) -> None:
    """``factory/artifacts/<hex>/canonical.json`` is a forbidden spelling."""
    bare = f"factory/artifacts/{DIGEST.removeprefix('sha256:')}/canonical.json"
    clone = with_published_artifact(
        document, TARGET, **published(canonical_manifest=bare)
    )

    rejects(clone, root, "not an immutable content-addressed reference")


def test_an_uppercase_reference_is_rejected(root: Path, document: Mapping) -> None:
    upper = f"factory/artifacts/{DIGEST.upper()}/canonical.json"
    clone = with_published_artifact(
        document, TARGET, **published(canonical_manifest=upper)
    )

    rejects(clone, root, "not an immutable content-addressed reference")


def test_a_reference_outside_the_content_addressed_store_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_published_artifact(
        document,
        TARGET,
        **published(canonical_manifest=f"elsewhere/{DIGEST}/canonical.json"),
    )

    rejects(clone, root, "not an immutable content-addressed reference")


def test_artifact_type_none_must_not_declare_a_canonical_manifest(
    root: Path, document: Mapping
) -> None:
    """An unpublished artifact has no canonical representation to reference."""
    clone = with_artifact(document, TARGET, canonical_manifest=REFERENCE)

    rejects(clone, root, "must not declare a canonical manifest reference")


# ---------------------------------------------------------------------------
# Determinism and single source of truth for the reference form
# ---------------------------------------------------------------------------


def test_the_registry_and_the_factory_agree_on_the_reference_form() -> None:
    """The Factory owns the form; the registry restates it and must not drift.

    Slice A keeps ``component_registry`` free of first-party imports, so the
    reference form is restated there. This test is what makes that safe: it
    compares the restated template, pattern and derivation against the
    normative Factory definition.
    """
    from component_registry.validation import (
        _CANONICAL_MANIFEST_RE,
        _CANONICAL_MANIFEST_TEMPLATE,
        _canonical_manifest_reference,
    )
    from factory_artifact import (
        CANONICAL_MANIFEST_PATTERN,
        CANONICAL_MANIFEST_TEMPLATE,
    )
    from factory_artifact.descriptor import canonical_manifest_reference

    assert _CANONICAL_MANIFEST_TEMPLATE == CANONICAL_MANIFEST_TEMPLATE
    assert _CANONICAL_MANIFEST_RE.pattern == CANONICAL_MANIFEST_PATTERN
    assert _canonical_manifest_reference(DIGEST) == canonical_manifest_reference(DIGEST)
    assert _canonical_manifest_reference(DIGEST.removeprefix("sha256:")) == (
        canonical_manifest_reference(DIGEST)
    )
    assert REFERENCE == f"factory/artifacts/{DIGEST}/canonical.json"


def test_adversarial_validation_is_deterministic(root: Path, document: Mapping) -> None:
    """The same broken document always yields exactly the same report."""
    broken = with_published_artifact(
        document, TARGET, **published(canonical_manifest="latest")
    )

    first = errors_for(broken, root)
    second = errors_for(copy.deepcopy(broken), root)

    assert first
    assert first == second
    assert first == sorted(set(first))
