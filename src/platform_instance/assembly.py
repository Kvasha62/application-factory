"""Deterministic Platform Instance assembly (Slice F).

ARCHITECTURE.md §2.2 defines the value this slice represents:

    Platform Instance =
        Platform Manifest
      + Configuration
      + Extensions
      + Branding
      + Environment-specific settings

and LAW-09 requires every Platform Instance to be reproducible from its
manifest. This module is the single programmatic door to that behaviour:

* :func:`assemble` — assemble an instance from a validated :class:`Manifest`;
* :func:`assemble_document` — assemble from a manifest document;
* :func:`assemble_manifest_path` — load, validate and assemble;
* :func:`assembly_diagnostics` — every violation of an assembly, sorted,
  without raising;
* :func:`build_instance_document` — deterministic construction of the
  canonical instance document.

Assembly is a **representation step, not a deployment step**. It creates no
runtime, provisions nothing, rolls nothing out, approves nothing and
publishes nothing (those belong to the Platform Manifest lifecycle and,
beyond it, to explicitly unauthorized future slices). It reads exactly two
authoritative inputs — the bound Platform Manifest document and, behind
Slice C validation, the authoritative Component Registry — plus the explicit
``platform_id`` the caller names. Every instance value is inherited verbatim
from the manifest: versions and artifacts are never re-resolved,
substituted or upgraded, configuration, extensions, branding and the
Golden Bundle reference (or the explicit uncertified ``null``) are preserved
exactly, and environment-specific settings enter only through the existing
contract path — component-declared configuration keys carried by the
manifest (ARCHITECTURE.md §20).

Determinism contract: the same manifest document and the same ``platform_id``
always produce the same canonical instance document and the same
``instance_digest``. No current time, randomness, machine-specific state,
network state or uncommitted environment takes part — identity is a pure
function of the inputs (sha256 over canonical JSON).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from platform_instance.errors import AssemblyRejectedError
from platform_instance.instance import (
    Instance,
    compute_instance_digest,
)
from platform_instance.schema import SCHEMA_PATH
from platform_instance.validation import (
    ASSEMBLABLE_MANIFEST_STATES,
    validate_instance_document,
)
from platform_manifest import Manifest, load_manifest_document
from platform_manifest.manifest import compute_manifest_digest, discover_root
from platform_manifest.validation import is_floating_selector

Document = Mapping[str, Any]

#: Instance identity pattern — the existing stable-identity vocabulary of the
#: factory (same shape as manifest/bundle/extension identities).
PLATFORM_ID_PATTERN = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"
_PLATFORM_ID_RE = re.compile(PLATFORM_ID_PATTERN)


def build_instance_document(
    *,
    platform_id: str,
    manifest_document: Document,
    schema_path: str = SCHEMA_PATH,
) -> dict[str, Any]:
    """Build an instance document deterministically and compute its digest.

    The caller provides the Platform Instance identity and the validated
    manifest document; this function inherits the manifest's composition
    content verbatim, computes the ``instance_digest`` over the canonical
    form and returns the complete document. Components are preserved in the
    manifest's normative (sorted) order; sections the manifest does not
    carry stay absent.
    """
    binding = {
        "manifest_id": manifest_document["manifest_id"],
        "manifest_version": manifest_document["manifest_version"],
        "manifest_digest": manifest_document["manifest_digest"],
    }

    lifecycle = manifest_document.get("lifecycle")
    manifest_state = lifecycle.get("state") if isinstance(lifecycle, Mapping) else None

    base: dict[str, Any] = {
        "$schema": schema_path,
        "platform_id": platform_id,
        "instance_digest": "",
        "manifest": binding,
        "manifest_state": manifest_state,
        # Always present: a bundle reference, or the explicit uncertified
        # status (ARCHITECTURE.md §31).
        "golden_bundle": copy.deepcopy(manifest_document.get("golden_bundle")),
        "components": copy.deepcopy(manifest_document.get("components", [])),
    }

    # Sections exist in the instance only where the manifest carries them.
    for field in ("configuration", "extensions", "branding"):
        if field in manifest_document:
            base[field] = copy.deepcopy(manifest_document[field])

    base["instance_digest"] = compute_instance_digest(base)
    return base


def _platform_id_errors(platform_id: object) -> list[str]:
    if not isinstance(platform_id, str) or not platform_id:
        return [
            (
                "$.platform_id: an explicit Platform Instance identity is "
                "required; it is never generated at assembly time"
            )
        ]
    errors: list[str] = []
    if is_floating_selector(platform_id):
        errors.append(
            f"$.platform_id: {platform_id!r} is a floating selector, not a "
            "stable Platform Instance identity"
        )
    if _PLATFORM_ID_RE.fullmatch(platform_id) is None:
        errors.append(
            f"$.platform_id: {platform_id!r} does not match pattern "
            f"{PLATFORM_ID_PATTERN!r}"
        )
    return errors


def _manifest_errors(manifest: Document) -> list[str]:
    """Validate the bound manifest by the normative Slice C surface."""
    from platform_manifest import validate_document as validate_manifest_document

    base = discover_root()
    return [
        f"$.manifest: the bound Platform Manifest is invalid — {error}"
        for error in validate_manifest_document(manifest, root=base)
    ]


def _state_errors(manifest: Document) -> list[str]:
    lifecycle = manifest.get("lifecycle")
    state = lifecycle.get("state") if isinstance(lifecycle, Mapping) else None
    if state not in ASSEMBLABLE_MANIFEST_STATES:
        return [
            (
                "$.manifest: a Platform Instance is assembled from an "
                "accepted/validated manifest; state "
                f"{state!r} is not assemblable "
                f"(assemblable: {sorted(ASSEMBLABLE_MANIFEST_STATES)!r})"
            )
        ]
    return []


def _digest_errors(manifest: Document) -> list[str]:
    declared = manifest.get("manifest_digest")
    computed = compute_manifest_digest(manifest)
    if declared == computed:
        return []
    return [
        (
            "$.manifest: manifest_digest "
            f"{declared!r} does not match the computed content digest "
            f"{computed!r}; a tampered or non-canonical manifest cannot be "
            "bound to an instance"
        )
    ]


def assembly_diagnostics(
    manifest_document: object,
    *,
    platform_id: object = None,
    root: Path | None = None,
) -> list[str]:
    """Return every violation of an assembly attempt, without raising.

    Requested platform identity, manifest validity, exact digest, lifecycle
    state and the fully assembled instance are checked together, so a caller
    sees the complete, deterministic list. The same inputs always produce the
    same report.
    """
    if not isinstance(manifest_document, Mapping):
        return ["$: platform manifest must be a JSON object"]

    errors = list(_platform_id_errors(platform_id))
    errors.extend(_manifest_errors(manifest_document))
    errors.extend(_digest_errors(manifest_document))
    errors.extend(_state_errors(manifest_document))
    if errors:
        return sorted(set(errors))

    assert isinstance(platform_id, str)  # narrowed by _platform_id_errors

    # The assembled representation must itself pass the full instance
    # validation against its manifest — a belt-and-braces handoff gate: an
    # instance that cannot be validated is never produced (ARCHITECTURE.md
    # §17 discipline, applied to assembly).
    document = build_instance_document(
        platform_id=platform_id, manifest_document=manifest_document
    )
    errors.extend(validate_instance_document(document, manifest_document, root=root))
    return sorted(set(errors))


def assemble(
    manifest: Manifest,
    *,
    platform_id: str,
    root: Path | None = None,
) -> Instance:
    """Assemble a Platform Instance from a validated Platform Manifest.

    Any violation — an invalid or tampered manifest, a non-assemblable
    lifecycle state, a floating or mismatched platform identity — rejects
    the assembly deterministically and produces no instance. The bound
    manifest is never mutated: assembly only reads it.
    """
    base = root if root is not None else manifest.root
    document = _assemble_checked(manifest.document, platform_id=platform_id, root=base)
    return Instance(
        document=document,
        root=base,
        path=manifest.path,
        manifest=manifest,
    )


def assemble_document(
    manifest_document: Document,
    *,
    platform_id: str,
    root: Path | None = None,
) -> Instance:
    """Validate a manifest document and assemble a Platform Instance from it."""
    base = root if root is not None else discover_root()
    document = _assemble_checked(manifest_document, platform_id=platform_id, root=base)
    return Instance(document=document, root=base, manifest=None)


def assemble_manifest_path(
    path: Path,
    *,
    platform_id: str,
    root: Path | None = None,
) -> Instance:
    """Load, validate and assemble a Platform Instance from a manifest file."""
    base = root if root is not None else discover_root()
    document = _assemble_checked(
        load_manifest_document(path), platform_id=platform_id, root=base
    )
    return Instance(document=document, root=base, manifest=None)


def _assemble_checked(
    manifest_document: Document,
    *,
    platform_id: str,
    root: Path | None,
) -> dict[str, Any]:
    errors = assembly_diagnostics(manifest_document, platform_id=platform_id, root=root)
    if errors:
        raise AssemblyRejectedError(errors)
    return build_instance_document(
        platform_id=platform_id, manifest_document=manifest_document
    )


__all__ = [
    "ASSEMBLABLE_MANIFEST_STATES",
    "PLATFORM_ID_PATTERN",
    "Instance",
    "assemble",
    "assemble_document",
    "assemble_manifest_path",
    "assembly_diagnostics",
    "build_instance_document",
]
