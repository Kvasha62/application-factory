"""Loading and reading the canonical Component Registry.

The canonical machine-readable registry is
``factory/registry/component_registry.json``. This module is the single
programmatic door to it: it locates the repository root, loads the document,
validates it and exposes read access to the registered inventory.

Nothing here reads business data. The registry is factory-level metadata and
this module touches only the registry document and published contract
descriptors (ADR-0015 §4, §11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from component_registry.errors import RegistryNotFoundError, RegistryValidationError
from component_registry.validation import validate_document

#: Canonical registry document, relative to the repository root.
REGISTRY_PATH = "factory/registry/component_registry.json"

Document = Mapping[str, object]
Entry = Mapping[str, object]


def discover_root(start: Path | None = None) -> Path:
    """Return the repository root, found by walking up from ``start``."""
    origin = Path(start if start is not None else __file__).resolve()
    candidates = [origin, *origin.parents]
    for candidate in candidates:
        base = candidate if candidate.is_dir() else candidate.parent
        if (base / "pyproject.toml").is_file() and (base / "components").is_dir():
            return base
    message = f"repository root not found above {origin}"
    raise RegistryNotFoundError(message)


def load_registry_document(path: Path) -> Document:
    """Read and JSON-decode a registry document."""
    if not path.is_file():
        message = f"component registry not found: {path}"
        raise RegistryNotFoundError(message)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = (
            f"component registry is unreadable: {path} ({error.__class__.__name__})"
        )
        raise RegistryNotFoundError(message) from error
    if not isinstance(document, Mapping):
        message = f"component registry is not a JSON object: {path}"
        raise RegistryNotFoundError(message)
    return document


@dataclass(frozen=True)
class Registry:
    """A loaded Component Registry document."""

    document: Document
    root: Path
    path: Path

    # -- inventory ------------------------------------------------------
    @property
    def entries(self) -> tuple[Entry, ...]:
        """Every registered component entry, in canonical registry order."""
        components = self.document.get("components", [])
        return tuple(entry for entry in components if isinstance(entry, Mapping))

    @property
    def component_ids(self) -> tuple[str, ...]:
        """Registered component identities, in canonical registry order."""
        return tuple(
            entry["component_id"]
            for entry in self.entries
            if isinstance(entry.get("component_id"), str)
        )

    def entry(self, component_id: str) -> Entry:
        """Return the entry for ``component_id``."""
        for entry in self.entries:
            if entry.get("component_id") == component_id:
                return entry
        message = f"component is not registered: {component_id}"
        raise KeyError(message)

    def version(self, component_id: str) -> str:
        """Return the explicit public version registered for ``component_id``."""
        version = self.entry(component_id).get("component_version")
        if not isinstance(version, str):
            message = f"component has no explicit version: {component_id}"
            raise RegistryValidationError([message])
        return version

    def dependencies(self, component_id: str) -> tuple[Entry, ...]:
        """Return the declared dependencies of ``component_id``."""
        dependencies = self.entry(component_id).get("dependencies", [])
        if not isinstance(dependencies, list):
            return ()
        return tuple(item for item in dependencies if isinstance(item, Mapping))

    # -- verification ---------------------------------------------------
    def validate(self, *, check_inventory: bool = True) -> list[str]:
        """Return every validation violation of this registry (empty when valid)."""
        return validate_document(
            self.document, root=self.root, check_inventory=check_inventory
        )

    def require_valid(self, *, check_inventory: bool = True) -> list[str]:
        """Raise :class:`RegistryValidationError` when the registry is invalid."""
        errors = self.validate(check_inventory=check_inventory)
        if errors:
            raise RegistryValidationError(errors)
        return errors


def load_registry(
    root: Path | None = None,
    *,
    check_inventory: bool = True,
    strict: bool = True,
) -> Registry:
    """Load and validate the canonical Component Registry.

    With ``strict=True`` (the default) an invalid registry raises
    :class:`RegistryValidationError` instead of being handed out silently.
    """
    base = root if root is not None else discover_root()
    path = base / REGISTRY_PATH
    document = load_registry_document(path)
    registry = Registry(document=document, root=base, path=path)
    if strict:
        registry.require_valid(check_inventory=check_inventory)
    return registry
