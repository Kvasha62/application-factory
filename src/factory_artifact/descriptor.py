"""Immutable, digest-bound Factory artifact descriptors.

This module is the executable form of the artifact contract in
``factory/registry/README.md`` §9.1 and ADR-0018 §3.1, §4, §5:

.. code-block:: text

    component declaration
      → canonical artifact representation    (§9.1.2 / §9.1.3 builders)
      → deterministic SHA-256 digest         (§9.1.4)
      → immutable digest-bound descriptor    (this module)
      → fail-closed verification             (§9.1.5)
      → publication-ready registry metadata  (§9.1.6, §9.1.12)

Canonicalization is deliberately *not* re-implemented here.
:mod:`factory_artifact.source_package` and
:mod:`factory_artifact.container_image` remain the only definitions of the
canonical forms; this module binds their output to an immutable identity and
refuses anything that cannot be proven to match it. Verification recomputes
the canonical representation through those same builders, so a physical
artifact, its digest and its canonical representation can only agree by
actually agreeing — never by construction.

Scope discipline: nothing here selects a component set, a deployment target or
an external registry, and nothing here declares a component deployable or
publishable. :func:`registry_metadata` states that a pinned, digest-addressed
artifact exists; ``lifecycle.deployable`` and ``lifecycle.publishable`` remain
separate claims that need evidence this module does not hold
(ARCHITECTURE.md §30; ``factory/registry/README.md`` §10).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory_artifact.container_image import (
    ContainerImageError,
    build_container_image_canonical,
)
from factory_artifact.source_package import (
    SourcePackageError,
    build_source_package_canonical,
)

__all__ = [
    "CANONICAL_FORMS",
    "CANONICAL_MANIFEST_PATTERN",
    "CANONICAL_MANIFEST_TEMPLATE",
    "DIGEST_PATTERN",
    "DIGEST_PREFIX",
    "ArtifactDescriptor",
    "ArtifactError",
    "ArtifactMetadataError",
    "ArtifactNotPublishedError",
    "ArtifactVerificationError",
    "artifact_violations",
    "canonical_manifest_reference",
    "describe_artifact",
    "describe_container_image",
    "describe_source_package",
    "descriptor_from_registry_metadata",
    "registry_metadata",
    "registry_metadata_violations",
    "verify_artifact",
]

#: Digest notation. The prefix is part of the normative reference form, so a
#: bare hex digest is not a valid artifact identity.
DIGEST_PREFIX = "sha256:"

#: An immutable artifact identity: ``sha256:`` plus 64 lowercase hex chars.
#: Anything else — ``latest``, ``stable``, an uppercase digest, a bare hex
#: string — is rejected (ARCHITECTURE.md §1.3, §11).
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

#: Content-addressed location of the derived canonical manifest
#: (``factory/registry/README.md`` §9.1.4). The form without the ``sha256:``
#: prefix is explicitly forbidden, so it is not expressible here.
CANONICAL_MANIFEST_TEMPLATE = "factory/artifacts/{digest}/canonical.json"
CANONICAL_MANIFEST_PATTERN = r"^factory/artifacts/sha256:[0-9a-f]{64}/canonical\.json$"

#: The only canonical forms the Factory defines, keyed by artifact type.
#: ``none`` is absent on purpose: it publishes no artifact and therefore has
#: no canonical form (``factory/registry/README.md`` §9.1.11).
CANONICAL_FORMS = {
    "source_package": "source_package/v1",
    "container_image": "container_image/v1",
}

_DIGEST_RE = re.compile(DIGEST_PATTERN)
_CANONICAL_MANIFEST_RE = re.compile(CANONICAL_MANIFEST_PATTERN)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ArtifactError(ValueError):
    """Raised when an artifact violates the Factory artifact contract."""


class ArtifactNotPublishedError(ArtifactError):
    """The component publishes no artifact, so no artifact identity exists."""


class _ViolationReport(ArtifactError):
    """An artifact failure that reports *every* violation at once.

    Collecting the whole report keeps a rejection deterministic: the same
    broken artifact always produces the same message.
    """

    def __init__(self, violations: Sequence[str], subject: str) -> None:
        self.violations: tuple[str, ...] = tuple(violations)
        joined = "\n".join(f"  - {item}" for item in self.violations)
        super().__init__(f"{subject} ({len(self.violations)} errors):\n{joined}")


class ArtifactMetadataError(_ViolationReport):
    """Artifact metadata is missing, unpinned, mutable or inconsistent."""

    def __init__(self, violations: Sequence[str]) -> None:
        super().__init__(violations, "invalid artifact metadata")


class ArtifactVerificationError(_ViolationReport):
    """A physical artifact could not be proven to match its descriptor."""

    def __init__(self, violations: Sequence[str]) -> None:
        super().__init__(violations, "artifact verification failed")


# --------------------------------------------------------------------------
# Canonical manifest reference
# --------------------------------------------------------------------------


def canonical_manifest_reference(digest: str) -> str:
    """Return the content-addressed canonical manifest reference for ``digest``.

    The reference is *derived* from the digest, never declared independently,
    which is what makes it immutable: there is exactly one spelling for a
    given artifact, and a floating selector has no spelling at all.
    """
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        message = (
            f"artifact digest {digest!r} is not an immutable digest: an "
            "artifact is addressed only by 'sha256:<64 lowercase hex>' and "
            "never by a floating selector"
        )
        raise ArtifactError(message)
    return CANONICAL_MANIFEST_TEMPLATE.format(digest=digest)


# --------------------------------------------------------------------------
# Shared identity rules
# --------------------------------------------------------------------------


def _identity_violations(
    *,
    artifact_type: object,
    canonical_form: object,
    digest: object,
    pinned: object,
    canonical_manifest: object,
    where: str,
) -> list[str]:
    """Return every violation of one published-artifact identity block.

    Every field is checked independently, so a block that is wrong in several
    ways reports all of them rather than only the first: an unsupported
    ``artifact_type`` does not excuse a missing digest, an unpinned artifact or
    a malformed canonical manifest reference.

    ``artifact_type`` is external input, so it is type-checked before it is
    used as a mapping key: an unhashable value such as ``[]`` or ``{}`` yields
    an ordinary identity violation instead of a ``TypeError``.
    """
    errors: list[str] = []

    if not isinstance(artifact_type, str) or artifact_type not in CANONICAL_FORMS:
        errors.append(
            f"{where}.artifact_type: {artifact_type!r} is not a published "
            "artifact type; 'none' publishes no artifact and has no descriptor"
        )

    # ``.get`` needs a hashable key, and an unhashable artifact_type has no
    # canonical form to look up.
    expected_form = (
        CANONICAL_FORMS.get(artifact_type) if isinstance(artifact_type, str) else None
    )

    if digest is None:
        errors.append(
            f"{where}.digest: a deployable artifact requires an immutable "
            "digest; a missing digest means no published artifact"
        )
    elif not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        errors.append(
            f"{where}.digest: {digest!r} is not an immutable digest; an "
            "artifact is addressed only by 'sha256:<64 lowercase hex>' and "
            "never by a floating selector"
        )

    if pinned is not True:
        errors.append(
            f"{where}.pinned: {pinned!r} is not pinned; an artifact is "
            "selectable only by its immutable digest"
        )

    if expected_form is None:
        errors.append(
            f"{where}.canonical_form: {canonical_form!r} cannot correspond to "
            f"artifact_type {artifact_type!r}, which is not a published "
            "artifact type"
        )
    elif canonical_form != expected_form:
        errors.append(
            f"{where}.canonical_form: {canonical_form!r} does not match "
            f"artifact_type {artifact_type!r}, which requires {expected_form!r}"
        )

    if canonical_manifest is None:
        errors.append(
            f"{where}.canonical_manifest: a published artifact requires the "
            "immutable content-addressed canonical manifest reference"
        )
    elif (
        not isinstance(canonical_manifest, str)
        or _CANONICAL_MANIFEST_RE.fullmatch(canonical_manifest) is None
    ):
        errors.append(
            f"{where}.canonical_manifest: {canonical_manifest!r} is not an "
            "immutable content-addressed reference of the form "
            "'factory/artifacts/sha256:<64 lowercase hex>/canonical.json'"
        )
    elif isinstance(digest, str) and _DIGEST_RE.fullmatch(digest) is not None:
        expected = canonical_manifest_reference(digest)
        if canonical_manifest != expected:
            errors.append(
                f"{where}.canonical_manifest: {canonical_manifest!r} is not "
                f"bound to the declared digest {digest!r}; expected "
                f"{expected!r}"
            )

    return errors


# --------------------------------------------------------------------------
# The immutable descriptor
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactDescriptor:
    """Immutable identity of one sealed Factory artifact.

    A descriptor holds no mutable state and admits no floating selector: the
    artifact it identifies is selected only by :attr:`digest`, and
    :attr:`canonical_manifest` is the content-addressed location of the
    canonical representation that digest was computed over. Construction
    validates every field, so an ``ArtifactDescriptor`` instance is always a
    well-formed, digest-bound identity.
    """

    artifact_type: str
    canonical_form: str
    digest: str
    canonical_manifest: str
    layer_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        violations = _identity_violations(
            artifact_type=self.artifact_type,
            canonical_form=self.canonical_form,
            digest=self.digest,
            pinned=True,
            canonical_manifest=self.canonical_manifest,
            where="artifact",
        )
        violations.extend(_layer_digest_violations(self))
        if violations:
            raise ArtifactMetadataError(violations)


def _layer_digest_violations(descriptor: ArtifactDescriptor) -> list[str]:
    """Return every violation of a descriptor's layer digest tuple."""
    if not isinstance(descriptor.layer_digests, tuple):
        return [
            (
                "artifact.layer_digests: layer digests must be an immutable "
                f"tuple, got {type(descriptor.layer_digests).__name__}"
            )
        ]

    errors: list[str] = []
    if descriptor.artifact_type != "container_image" and descriptor.layer_digests:
        errors.append(
            "artifact.layer_digests: only container_image/v1 declares layer " "digests"
        )
    for layer_digest in descriptor.layer_digests:
        if not isinstance(layer_digest, str) or (
            _DIGEST_RE.fullmatch(layer_digest) is None
        ):
            errors.append(
                f"artifact.layer_digests: {layer_digest!r} is not an immutable "
                "digest"
            )
    return errors


# --------------------------------------------------------------------------
# Declaration → canonical representation → digest → descriptor
# --------------------------------------------------------------------------


def _canonicalize(
    artifact_type: str,
    root: Path,
    factory_declaration: Mapping[str, Any],
) -> tuple[Mapping[str, Any], bytes, str, tuple[str, ...]]:
    """Return ``(canonical, canonical_bytes, digest, layer_digests)``."""
    if artifact_type == "source_package":
        canonical, canonical_bytes, digest = build_source_package_canonical(
            root, factory_declaration
        )
        return canonical, canonical_bytes, digest, ()
    if artifact_type == "container_image":
        canonical, canonical_bytes, digest, layer_digests = (
            build_container_image_canonical(root, factory_declaration)
        )
        return canonical, canonical_bytes, digest, tuple(layer_digests)
    message = (
        f"artifact_type {artifact_type!r} is not a published artifact type; "
        f"the Factory defines only {sorted(CANONICAL_FORMS)!r}"
    )
    raise ArtifactError(message)


def describe_artifact(
    artifact_type: str,
    root: Path,
    factory_declaration: Mapping[str, Any],
) -> ArtifactDescriptor:
    """Build the immutable descriptor of the sealed artifact at ``root``.

    The digest is SHA-256 over the canonical representation produced by the
    artifact type's own builder, so the same content and the same declaration
    always yield the same descriptor and any relevant content change yields a
    different one.
    """
    canonical_form = CANONICAL_FORMS.get(artifact_type)
    if canonical_form is None:
        message = (
            f"artifact_type {artifact_type!r} is not a published artifact "
            f"type; the Factory defines only {sorted(CANONICAL_FORMS)!r}"
        )
        raise ArtifactError(message)

    _, _, digest, layer_digests = _canonicalize(
        artifact_type, root, factory_declaration
    )
    return ArtifactDescriptor(
        artifact_type=artifact_type,
        canonical_form=canonical_form,
        digest=digest,
        canonical_manifest=canonical_manifest_reference(digest),
        layer_digests=layer_digests,
    )


def describe_source_package(
    root: Path,
    factory_declaration: Mapping[str, Mapping[str, bool]],
) -> ArtifactDescriptor:
    """Build the descriptor of a sealed ``source_package/v1`` artifact."""
    return describe_artifact("source_package", root, factory_declaration)


def describe_container_image(
    root: Path,
    factory_declaration: Mapping[str, Any],
) -> ArtifactDescriptor:
    """Build the descriptor of a sealed ``container_image/v1`` artifact."""
    return describe_artifact("container_image", root, factory_declaration)


# --------------------------------------------------------------------------
# Publication-ready registry metadata
# --------------------------------------------------------------------------


def registry_metadata(descriptor: ArtifactDescriptor) -> dict[str, Any]:
    """Return the Component Registry ``artifact`` block for ``descriptor``.

    This is truthful metadata and nothing more: it records the artifact type,
    the canonical form, the immutable digest and the content-addressed
    canonical manifest reference, and it sets ``pinned`` because the artifact
    *is* selected by an immutable digest. It makes no deployability or
    publishability claim.
    """
    if not isinstance(descriptor, ArtifactDescriptor):
        message = (
            "registry metadata requires an immutable ArtifactDescriptor; a "
            "mutable artifact reference is not an artifact identity"
        )
        raise ArtifactError(message)
    return {
        "artifact_type": descriptor.artifact_type,
        "digest": descriptor.digest,
        "pinned": True,
        "canonical_form": descriptor.canonical_form,
        "canonical_manifest": descriptor.canonical_manifest,
    }


def registry_metadata_violations(artifact: object) -> list[str]:
    """Return every violation of a registry ``artifact`` block.

    The block is read as the metadata of a *published* artifact: an
    ``artifact_type`` of ``none`` publishes nothing and is reported as such,
    because there is then no identity to check.
    """
    if not isinstance(artifact, Mapping):
        return ["artifact: explicit artifact identity metadata is required"]

    if artifact.get("artifact_type") == "none":
        return [
            (
                "artifact.artifact_type: 'none' publishes no artifact, so "
                "there is no canonical representation, no digest and no "
                "immutable reference"
            )
        ]

    return _identity_violations(
        artifact_type=artifact.get("artifact_type"),
        canonical_form=artifact.get("canonical_form"),
        digest=artifact.get("digest"),
        pinned=artifact.get("pinned"),
        canonical_manifest=artifact.get("canonical_manifest"),
        where="artifact",
    )


def descriptor_from_registry_metadata(artifact: object) -> ArtifactDescriptor:
    """Rebuild the immutable descriptor stored in a registry ``artifact`` block.

    Fail-closed: metadata that is missing a digest, unpinned, floating,
    inconsistent, or whose canonical manifest reference is not bound to the
    declared digest is rejected rather than repaired.
    """
    if isinstance(artifact, Mapping) and artifact.get("artifact_type") == "none":
        message = (
            "artifact_type 'none' publishes no artifact: there is no canonical "
            "representation, no digest and no descriptor to verify "
            "(factory/registry/README.md §9.1.11)"
        )
        raise ArtifactNotPublishedError(message)

    violations = registry_metadata_violations(artifact)
    if violations:
        raise ArtifactMetadataError(violations)

    # An empty violation report means every identity field is a well-formed
    # string; the descriptor constructor re-checks them rather than trusting
    # this call site.
    block: Mapping[str, Any] = artifact if isinstance(artifact, Mapping) else {}
    return ArtifactDescriptor(
        artifact_type=block.get("artifact_type"),
        canonical_form=block.get("canonical_form"),
        digest=block.get("digest"),
        canonical_manifest=block.get("canonical_manifest"),
    )


# --------------------------------------------------------------------------
# Fail-closed verification
# --------------------------------------------------------------------------


def artifact_violations(
    root: Path,
    descriptor: ArtifactDescriptor,
    factory_declaration: Mapping[str, Any],
    *,
    canonical_manifest_bytes: bytes | None = None,
) -> list[str]:
    """Return every reason ``root`` cannot be proven to match ``descriptor``.

    An empty list is the only proof of correspondence. Verification is
    fail-closed: a missing artifact, an unreadable artifact, a declaration
    that does not match the physical content, a canonical representation that
    cannot be derived, or a digest that does not match the recomputed one all
    produce violations instead of a silent pass.

    ``canonical_manifest_bytes`` is the stored ``canonical.json`` content, when
    the caller has it. Supplying it additionally proves that the declared
    digest really is the SHA-256 of that canonical representation, and that
    the physical artifact still canonicalizes to exactly those bytes.
    """
    if not isinstance(descriptor, ArtifactDescriptor):
        return [
            (
                "artifact: an artifact is identified only by an immutable "
                f"ArtifactDescriptor, got {type(descriptor).__name__}; a "
                "mutable artifact reference is not an artifact identity"
            )
        ]

    errors = _identity_violations(
        artifact_type=descriptor.artifact_type,
        canonical_form=descriptor.canonical_form,
        digest=descriptor.digest,
        pinned=True,
        canonical_manifest=descriptor.canonical_manifest,
        where="artifact",
    )

    recomputed_bytes: bytes | None = None
    path = Path(root)
    if not path.is_dir():
        errors.append(f"artifact: the sealed artifact is missing at {root}")
    else:
        try:
            _, recomputed_bytes, recomputed_digest, layer_digests = _canonicalize(
                descriptor.artifact_type, path, factory_declaration
            )
        except (ArtifactError, ContainerImageError, SourcePackageError) as error:
            errors.append(
                "artifact: the canonical representation could not be derived: "
                f"{error}"
            )
        except (TypeError, KeyError, AttributeError) as error:
            # ``build_source_package_canonical`` accepts only a mapping
            # declaration and has no structural guard of its own, so malformed
            # external input (``None``, a list, a bare object) surfaces here as
            # an ordinary structural error. The verifier is a fail-closed trust
            # boundary: it converts that into a violation instead of letting an
            # unexpected exception decide the outcome. ``build_container_image_canonical``
            # guards its own declaration and never reaches this branch.
            errors.append(
                "artifact: the Factory declaration is malformed, so the "
                f"canonical representation could not be derived: "
                f"{type(error).__name__}: {error}"
            )
        except OSError as error:
            errors.append(f"artifact: the sealed artifact is unreadable: {error}")
        else:
            if recomputed_digest != descriptor.digest:
                errors.append(
                    f"artifact.digest: declared {descriptor.digest!r} does not "
                    f"match the recomputed digest {recomputed_digest!r} of the "
                    "canonical representation; the artifact content changed "
                    "after the digest was calculated"
                )
            if (
                descriptor.layer_digests
                and tuple(layer_digests) != descriptor.layer_digests
            ):
                errors.append(
                    "artifact.layer_digests: the recomputed layer digests "
                    f"{tuple(layer_digests)!r} do not match the declared "
                    f"{descriptor.layer_digests!r}"
                )

    if canonical_manifest_bytes is not None:
        if not isinstance(canonical_manifest_bytes, bytes):
            errors.append(
                "artifact: the canonical manifest must be supplied as bytes, "
                f"got {type(canonical_manifest_bytes).__name__}"
            )
        else:
            stored = (
                DIGEST_PREFIX + hashlib.sha256(canonical_manifest_bytes).hexdigest()
            )
            if stored != descriptor.digest:
                errors.append(
                    f"artifact.digest: declared {descriptor.digest!r} is not "
                    f"the SHA-256 of the stored canonical manifest ({stored!r})"
                )
            if (
                recomputed_bytes is not None
                and canonical_manifest_bytes != recomputed_bytes
            ):
                errors.append(
                    "artifact: canonical representation mismatch; the stored "
                    "canonical manifest differs from the canonical "
                    "representation of the physical artifact"
                )

    return sorted(set(errors))


def verify_artifact(
    root: Path,
    descriptor: ArtifactDescriptor,
    factory_declaration: Mapping[str, Any],
    *,
    canonical_manifest_bytes: bytes | None = None,
) -> ArtifactDescriptor:
    """Prove that ``root`` is the artifact ``descriptor`` identifies.

    Returns the descriptor unchanged on success and raises
    :class:`ArtifactVerificationError` listing every violated invariant
    otherwise. Absence of proof is a rejection, never a pass.
    """
    violations = artifact_violations(
        root,
        descriptor,
        factory_declaration,
        canonical_manifest_bytes=canonical_manifest_bytes,
    )
    if violations:
        raise ArtifactVerificationError(violations)
    return descriptor
