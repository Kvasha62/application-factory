"""Loading, building and digesting Platform Instance documents (Slice F).

A Platform Instance document is the deterministic assembly representation of
one concrete platform (ARCHITECTURE.md §2.2, §31; ADR-0015 §15):

    Platform Instance =
        Platform Manifest
      + Configuration
      + Extensions
      + Branding
      + Environment-specific settings

Slice F represents that value as factory-level metadata: an explicit binding
of a ``platform_id`` to exactly one validated Platform Manifest
(identity/version/digest) plus the verbatim, canonical copy of the manifest's
composition content — components with artifact identities, Golden Bundle
reference (or the explicit uncertified status ``null``), configuration,
extensions and branding. Environment-specific settings enter through the
existing contract path only: component-declared configuration keys carried by
the manifest's ``configuration`` (ARCHITECTURE.md §20); this slice invents no
separate environment-overlay contract.

The digest model is the factory's single canonical model (catalog
``entry_digest``, manifest ``manifest_digest``, bundle ``bundle_digest``):
``sha256:`` over canonical JSON (``sort_keys=True``, compact separators) of
the content excluding the digest field itself. No time, randomness, machine
state or network takes part in identity.

Nothing here reads business data, touches a component database or imports a
component's internals (ADR-0015 §11): this module touches only the instance
document, the bound manifest document and the authoritative factory loaders
behind Platform Manifest validation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_instance.errors import InstanceNotFoundError
from platform_manifest.manifest import discover_root  # single root discovery

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Directory holding the normative schema, rules and the deterministic example.
INSTANCE_DIR = "factory/platform_instance"

#: The deterministic example instance shipped with the slice.
EXAMPLE_INSTANCE_PATH = "factory/platform_instance/example_instance.json"


def canonical_json(value: object) -> str:
    """Serialize ``value`` to the canonical JSON form used for digests.

    Same form as catalog derivation, manifest and bundle digests:
    ``sort_keys=True``, compact separators, ``ensure_ascii=False``.
    Deterministic across runs and machines.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def instance_content_for_digest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the instance content covered by ``compute_instance_digest``.

    The digest covers the whole document except the ``instance_digest`` field
    itself (to avoid circularity) and the ``$schema`` field (a non-semantic
    pointer). Every other field — platform identity, manifest binding,
    components, configuration, extensions, branding — is covered.
    """
    content: dict[str, Any] = {}
    for key, value in document.items():
        if key in ("instance_digest", "$schema"):
            continue
        content[key] = value
    return content


def compute_instance_digest(document: Mapping[str, Any]) -> str:
    """Compute the immutable content digest of an instance document.

    ``sha256:`` + hex of the canonical JSON of the content excluding the
    digest field itself. This is the deterministic assembly identity: equal
    authoritative inputs always produce the same value.
    """
    content = instance_content_for_digest(document)
    encoded = canonical_json(content).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_instance_document(path: Path) -> Document:
    """Read and JSON-decode an instance document."""
    if not path.is_file():
        message = f"platform instance document not found: {path}"
        raise InstanceNotFoundError(message)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = (
            f"platform instance document is unreadable: {path} "
            f"({error.__class__.__name__})"
        )
        raise InstanceNotFoundError(message) from error
    if not isinstance(document, Mapping):
        message = f"platform instance document is not a JSON object: {path}"
        raise InstanceNotFoundError(message)
    return document


@dataclass(frozen=True)
class Instance:
    """An assembled Platform Instance document and its bound manifest."""

    document: Document
    root: Path
    path: Path | None = None
    manifest: Any = None

    @property
    def platform_id(self) -> str | None:
        value = self.document.get("platform_id")
        return value if isinstance(value, str) else None

    @property
    def manifest_binding(self) -> Entry | None:
        value = self.document.get("manifest")
        return value if isinstance(value, Mapping) else None

    @property
    def manifest_id(self) -> str | None:
        binding = self.manifest_binding
        if binding is None:
            return None
        value = binding.get("manifest_id")
        return value if isinstance(value, str) else None

    @property
    def manifest_version(self) -> str | None:
        binding = self.manifest_binding
        if binding is None:
            return None
        value = binding.get("manifest_version")
        return value if isinstance(value, str) else None

    @property
    def manifest_digest(self) -> str | None:
        binding = self.manifest_binding
        if binding is None:
            return None
        value = binding.get("manifest_digest")
        return value if isinstance(value, str) else None

    @property
    def manifest_state(self) -> str | None:
        value = self.document.get("manifest_state")
        return value if isinstance(value, str) else None

    @property
    def instance_digest(self) -> str | None:
        value = self.document.get("instance_digest")
        return value if isinstance(value, str) else None

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

    @property
    def certified(self) -> bool:
        """True when the instance inherits a Golden Bundle reference.

        ``golden_bundle: null`` is the explicit uncertified status
        (ARCHITECTURE.md §31) — never an implied certification.
        """
        return isinstance(self.document.get("golden_bundle"), Mapping)

    def compute_digest(self) -> str:
        """Return the computed content digest of this instance document."""
        return compute_instance_digest(self.document)

    def validate(self, *, manifest_document: Document | None = None) -> list[str]:
        """Return every validation violation (empty when valid).

        The bound manifest document is required: an instance is validated
        against its manifest, never in isolation. When ``manifest_document``
        is omitted, the manifest captured at assembly time is used.
        """
        from platform_instance.validation import validate_instance_document

        bound = (
            manifest_document
            if manifest_document is not None
            else getattr(self.manifest, "document", None)
        )
        return validate_instance_document(self.document, bound, root=self.root)

    def require_valid(self, *, manifest_document: Document | None = None) -> list[str]:
        """Raise :class:`InstanceValidationError` when the instance is invalid."""
        from platform_instance.errors import InstanceValidationError

        errors = self.validate(manifest_document=manifest_document)
        if errors:
            raise InstanceValidationError(errors)
        return errors

    def render(self) -> str:
        """Serialize the instance document deterministically for storage."""
        return render_instance_document(self.document)


def render_instance_document(document: Document) -> str:
    """Serialize an instance document deterministically."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


__all__ = [
    "EXAMPLE_INSTANCE_PATH",
    "INSTANCE_DIR",
    "Instance",
    "canonical_json",
    "compute_instance_digest",
    "discover_root",
    "instance_content_for_digest",
    "load_instance_document",
    "render_instance_document",
]
