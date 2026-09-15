"""Loading, reading and digesting Platform Manifest documents.

The manifest is the explicit, reproducible description of a Platform Instance
composition (ARCHITECTURE.md §16, ADR-0015 §6). This module is the single
programmatic door to it: it locates the repository root, loads a document,
validates it structurally and semantically, and exposes deterministic digest
calculation.

Nothing here reads business data. The manifest is factory-level composition
metadata and this module touches only the manifest document itself and the
authoritative Component Registry/Catalog for reference validation
(ADR-0015 §11).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_manifest.errors import ManifestNotFoundError, ManifestValidationError
from platform_manifest.schema import SCHEMA_PATH

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Canonical manifest schema path, relative to repository root.
MANIFEST_SCHEMA_PATH = SCHEMA_PATH

#: Example or canonical manifest location (not required for Slice C, but
#: useful for documentation). The manifest itself is per-platform, not
#: single canonical.
MANIFEST_DIR = "factory/platform_manifest"


def discover_root(start: Path | None = None) -> Path:
    """Return the repository root, found by walking up from ``start``."""
    origin = Path(start if start is not None else __file__).resolve()
    candidates = [origin, *origin.parents]
    for candidate in candidates:
        base = candidate if candidate.is_dir() else candidate.parent
        if (base / "pyproject.toml").is_file() and (base / "components").is_dir():
            return base
    message = f"repository root not found above {origin}"
    raise ManifestNotFoundError(message)


def canonical_json(value: object) -> str:
    """Serialize ``value`` to the canonical JSON form used for digests.

    Same form as catalog derivation: sort_keys=True, compact separators,
    ensure_ascii=False. Deterministic across runs.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_content_for_digest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the manifest content that is covered by ``manifest_digest``.

    The digest covers the whole manifest except the ``manifest_digest`` field
    itself (to avoid circularity) and the ``$schema`` field (which is a
    non-semantic pointer). All other fields, including lifecycle, components,
    predecessor, approval, publication, etc., are covered.

    The returned dict is sorted deterministically by ``canonical_json`` later,
    but we exclude the digest field here explicitly.
    """
    # Copy everything except manifest_digest and $schema.
    content: dict[str, Any] = {}
    for key, value in document.items():
        if key in ("manifest_digest", "$schema"):
            continue
        content[key] = value
    return content


def compute_manifest_digest(document: Mapping[str, Any]) -> str:
    """Compute the immutable content digest of a manifest document.

    The digest is ``sha256:`` + hex of canonical JSON of the manifest content
    (excluding ``manifest_digest`` itself). This matches the digest pinning
    model of catalog entry_digest.
    """
    content = manifest_content_for_digest(document)
    encoded = canonical_json(content).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_manifest_document(path: Path) -> Document:
    """Read and JSON-decode a manifest document."""
    if not path.is_file():
        message = f"platform manifest not found: {path}"
        raise ManifestNotFoundError(message)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = (
            f"platform manifest is unreadable: {path} ({error.__class__.__name__})"
        )
        raise ManifestNotFoundError(message) from error
    if not isinstance(document, Mapping):
        message = f"platform manifest is not a JSON object: {path}"
        raise ManifestNotFoundError(message)
    return document


@dataclass(frozen=True)
class Manifest:
    """A loaded Platform Manifest document."""

    document: Document
    root: Path
    path: Path | None = None

    @property
    def manifest_id(self) -> str | None:
        value = self.document.get("manifest_id")
        return value if isinstance(value, str) else None

    @property
    def manifest_version(self) -> str | None:
        value = self.document.get("manifest_version")
        return value if isinstance(value, str) else None

    @property
    def manifest_digest(self) -> str | None:
        value = self.document.get("manifest_digest")
        return value if isinstance(value, str) else None

    @property
    def lifecycle_state(self) -> str | None:
        lifecycle = self.document.get("lifecycle")
        if not isinstance(lifecycle, Mapping):
            return None
        state = lifecycle.get("state")
        return state if isinstance(state, str) else None

    @property
    def predecessor(self) -> Mapping[str, Any] | None:
        value = self.document.get("predecessor")
        if isinstance(value, Mapping):
            return value
        return None

    @property
    def components(self) -> tuple[Entry, ...]:
        comps = self.document.get("components", [])
        if not isinstance(comps, list):
            return ()
        return tuple(item for item in comps if isinstance(item, Mapping))

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(
            entry["component_id"]
            for entry in self.components
            if isinstance(entry.get("component_id"), str)
        )

    def component(self, component_id: str) -> Entry:
        for entry in self.components:
            if entry.get("component_id") == component_id:
                return entry
        message = f"component is not in manifest: {component_id}"
        raise KeyError(message)

    def compute_digest(self) -> str:
        """Return the computed digest of this manifest's content."""
        return compute_manifest_digest(self.document)

    def validate(self) -> list[str]:
        """Return every validation violation of this manifest (empty when valid)."""
        from platform_manifest.validation import validate_document

        return validate_document(self.document, root=self.root)

    def require_valid(self) -> list[str]:
        """Raise ManifestValidationError when the manifest is invalid."""
        errors = self.validate()
        if errors:
            raise ManifestValidationError(errors)
        return errors


def load_manifest(
    path: Path, root: Path | None = None, *, strict: bool = True
) -> Manifest:
    """Load and optionally strictly validate a manifest document from ``path``."""
    base = root if root is not None else discover_root()
    document = load_manifest_document(path)
    manifest = Manifest(document=document, root=base, path=path)
    if strict:
        manifest.require_valid()
    return manifest


def build_manifest_document(
    *,
    manifest_id: str,
    manifest_version: str,
    lifecycle_state: str,
    components: list[dict[str, Any]],
    predecessor: dict[str, Any] | None = None,
    golden_bundle: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
    extensions: list[dict[str, Any]] | None = None,
    branding: dict[str, Any] | None = None,
    validation_attestation: dict[str, Any] | None = None,
    approval: dict[str, Any] | None = None,
    publication: dict[str, Any] | None = None,
    schema_path: str = MANIFEST_SCHEMA_PATH,
) -> dict[str, Any]:
    """Build a manifest document deterministically and compute its digest.

    The caller provides the semantic content; this function computes the
    ``manifest_digest`` over the canonical form and returns the complete
    document. The digest is computed over content excluding itself.

    Components are sorted by component_id to ensure deterministic order
    (reproducibility requirement). The caller may provide them unsorted; they
    will be sorted here.

    This function does NOT validate against registry — validation is a
    separate step. It only ensures deterministic construction.
    """
    # Sort components by component_id for reproducibility.
    sorted_components = sorted(
        components, key=lambda entry: entry.get("component_id", "")
    )

    # Base document without digest.
    base: dict[str, Any] = {
        "$schema": schema_path,
        "manifest_id": manifest_id,
        "manifest_version": manifest_version,
        "lifecycle": {"state": lifecycle_state},
        "components": sorted_components,
    }

    # Optional fields — include only when provided, to keep minimal manifest
    # possible. Predecessor is always included as null when not provided to
    # satisfy schema's expectation of explicit predecessor linkage.
    if predecessor is not None:
        base["predecessor"] = predecessor
    else:
        base["predecessor"] = None

    if golden_bundle is not None:
        base["golden_bundle"] = golden_bundle
    else:
        base["golden_bundle"] = None

    if configuration is not None:
        base["configuration"] = configuration

    if extensions is not None:
        base["extensions"] = extensions

    if branding is not None:
        base["branding"] = branding

    if validation_attestation is not None:
        base["validation_attestation"] = validation_attestation
    else:
        base["validation_attestation"] = None

    if approval is not None:
        base["approval"] = approval
    else:
        base["approval"] = None

    if publication is not None:
        base["publication"] = publication
    else:
        base["publication"] = None

    # Compute digest over content excluding digest and $schema.
    digest = compute_manifest_digest(base)
    base["manifest_digest"] = digest

    # Ensure deterministic key order for rendering: we will render with indent,
    # but the digest itself was computed from canonical_json which sorts keys.
    return base


def render_manifest_document(document: Document) -> str:
    """Serialize a manifest document deterministically for storage."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
