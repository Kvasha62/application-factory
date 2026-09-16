"""Loading, reading and digesting Golden Bundle documents (Slice D).

A Golden Bundle (ARCHITECTURE.md §14, ADR-0015 §7) is a validated,
reproducible set of compatible component versions intended to be used as a
known-good composition. This module is the single programmatic door to it:
it locates the repository root, loads a document, validates it structurally
and semantically, and exposes deterministic digest calculation.

Nothing here reads business data. The bundle is factory-level composition
metadata and this module touches only the bundle document itself and the
authoritative Component Registry for reference validation (ADR-0015 §11).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from golden_bundle.errors import GoldenBundleNotFoundError, GoldenBundleValidationError
from golden_bundle.schema import SCHEMA_PATH

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Canonical bundle schema path, relative to repository root.
BUNDLE_SCHEMA_PATH = SCHEMA_PATH

#: Canonical bundles directory. The bundle is a per-composition artifact; this
#: directory holds the examples produced by ``build-example`` and the schema.
BUNDLE_DIR = "factory/golden_bundle"


def discover_root(start: Path | None = None) -> Path:
    """Return the repository root, found by walking up from ``start``."""
    origin = Path(start if start is not None else __file__).resolve()
    candidates = [origin, *origin.parents]
    for candidate in candidates:
        base = candidate if candidate.is_dir() else candidate.parent
        if (base / "pyproject.toml").is_file() and (base / "components").is_dir():
            return base
    message = f"repository root not found above {origin}"
    raise GoldenBundleNotFoundError(message)


def canonical_json(value: object) -> str:
    """Serialize ``value`` to the canonical JSON form used for digests.

    Same form as catalog derivation and manifest digest: sort_keys=True,
    compact separators, ensure_ascii=False. Deterministic across runs
    (ARCHITECTURE.md §11; ADR-0015 §6 reproducibility rule).
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bundle_content_for_digest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bundle content that is covered by ``bundle_digest``.

    The digest covers the whole bundle except the ``bundle_digest`` field
    itself (to avoid circularity) and the ``$schema`` field (a non-semantic
    pointer). All other fields — lifecycle, pinned components, compatibility
    matrix and certification evidence — are covered, so the same
    ``bundle_id + bundle_version`` always digests to the same value for the
    same content and any content change changes the digest.
    """
    content: dict[str, Any] = {}
    for key, value in document.items():
        if key in ("bundle_digest", "$schema"):
            continue
        content[key] = value
    return content


def compute_bundle_digest(document: Mapping[str, Any]) -> str:
    """Compute the immutable content digest of a bundle document.

    The digest is ``sha256:`` + hex of the canonical JSON of the bundle
    content (excluding ``bundle_digest`` itself). This matches the digest
    model of the catalog ``entry_digest`` and the manifest ``manifest_digest``.
    """
    content = bundle_content_for_digest(document)
    encoded = canonical_json(content).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_bundle_document(path: Path) -> Document:
    """Read and JSON-decode a bundle document."""
    if not path.is_file():
        message = f"golden bundle not found: {path}"
        raise GoldenBundleNotFoundError(message)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = f"golden bundle is unreadable: {path} ({error.__class__.__name__})"
        raise GoldenBundleNotFoundError(message) from error
    if not isinstance(document, Mapping):
        message = f"golden bundle is not a JSON object: {path}"
        raise GoldenBundleNotFoundError(message)
    return document


@dataclass(frozen=True)
class GoldenBundle:
    """A loaded Golden Bundle document."""

    document: Document
    root: Path
    path: Path | None = None

    @property
    def bundle_id(self) -> str | None:
        value = self.document.get("bundle_id")
        return value if isinstance(value, str) else None

    @property
    def bundle_version(self) -> str | None:
        value = self.document.get("bundle_version")
        return value if isinstance(value, str) else None

    @property
    def bundle_digest(self) -> str | None:
        value = self.document.get("bundle_digest")
        return value if isinstance(value, str) else None

    @property
    def lifecycle_state(self) -> str | None:
        lifecycle = self.document.get("lifecycle")
        if not isinstance(lifecycle, Mapping):
            return None
        state = lifecycle.get("state")
        return state if isinstance(state, str) else None

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
        message = f"component is not pinned in bundle: {component_id}"
        raise KeyError(message)

    def compute_digest(self) -> str:
        """Return the computed digest of this bundle's content."""
        return compute_bundle_digest(self.document)

    def validate(self) -> list[str]:
        """Return every validation violation of this bundle (empty when valid)."""
        from golden_bundle.validation import validate_document

        return validate_document(self.document, root=self.root)

    def require_valid(self) -> list[str]:
        """Raise GoldenBundleValidationError when the bundle is invalid."""
        errors = self.validate()
        if errors:
            raise GoldenBundleValidationError(errors)
        return errors


def load_bundle(
    path: Path, root: Path | None = None, *, strict: bool = True
) -> GoldenBundle:
    """Load and optionally strictly validate a bundle document from ``path``."""
    base = root if root is not None else discover_root()
    document = load_bundle_document(path)
    bundle = GoldenBundle(document=document, root=base, path=path)
    if strict:
        bundle.require_valid()
    return bundle


def build_bundle_document(
    *,
    bundle_id: str,
    bundle_version: str,
    lifecycle_state: str,
    components: list[dict[str, Any]],
    compatibility_pairs: list[dict[str, Any]],
    certification: dict[str, Any] | None = None,
    schema_path: str = BUNDLE_SCHEMA_PATH,
) -> dict[str, Any]:
    """Build a bundle document deterministically and compute its digest.

    The caller provides the semantic content; this function computes the
    ``bundle_digest`` over the canonical form and returns the complete
    document. The digest is computed over content excluding itself.

    * components are sorted by ``component_id``;
    * compatibility pairs are sorted by ``(from, to)``;
    * ``certification`` is included only when provided (explicit lifecycle
      evidence, never invented by the builder).
    """
    sorted_components = sorted(
        components, key=lambda entry: entry.get("component_id", "")
    )
    sorted_pairs = sorted(
        compatibility_pairs,
        key=lambda pair: (
            pair.get("from", ""),
            pair.get("to", ""),
            pair.get("mechanism", ""),
        ),
    )

    base: dict[str, Any] = {
        "$schema": schema_path,
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "lifecycle": {"state": lifecycle_state},
        "components": sorted_components,
        "compatibility": {"pairs": sorted_pairs},
        "certification": certification,
    }

    digest = compute_bundle_digest(base)
    base["bundle_digest"] = digest
    return base


def render_bundle_document(document: Document) -> str:
    """Serialize a bundle document deterministically for storage."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
