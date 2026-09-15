"""Loading, reading and discovering the canonical Component Catalog.

This module is the single programmatic door to the catalog: it loads the
canonical document together with the canonical registry it is derived from,
validates the derivation and exposes machine-readable discovery and filtering
over the registered inventory.

Filtering selects *for composition decisions* (ADR-0015 §5): component class,
data scope, lifecycle state, owner and SCS identity. The filtered view is the
catalog entry; the authoritative full metadata always remains the registry
entry, reachable through :meth:`Catalog.registry_entry`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from component_catalog.derive import CATALOG_PATH, Entry
from component_catalog.errors import CatalogNotFoundError, CatalogValidationError
from component_catalog.validation import validate_document
from component_registry import Registry, load_registry
from component_registry.validation import (
    COMPONENT_CLASSES,
    DATA_SCOPES,
    LIFECYCLE_STATES,
    is_floating_selector,
)

Document = Mapping[str, object]

#: The five discovery dimensions of the catalog view (Issue #64).
FILTER_DIMENSIONS = (
    "component_class",
    "data_scope",
    "lifecycle_state",
    "owner",
    "scs_id",
)


def load_catalog_document(path: Path) -> Document:
    """Read and JSON-decode a catalog document."""
    if not path.is_file():
        message = f"component catalog not found: {path}"
        raise CatalogNotFoundError(message)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = (
            f"component catalog is unreadable: {path} ({error.__class__.__name__})"
        )
        raise CatalogNotFoundError(message) from error
    if not isinstance(document, Mapping):
        message = f"component catalog is not a JSON object: {path}"
        raise CatalogNotFoundError(message)
    return document


def _require_vocabulary(name: str, value: object, allowed: frozenset[str]) -> None:
    """Reject a closed-vocabulary filter value that cannot match anything."""
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(sorted(allowed))
        message = f"{name}={value!r} is not a valid {name}; expected one of: {options}"
        raise ValueError(message)


def _require_text(name: str, value: object) -> None:
    """Reject an open-value filter argument that selects nothing."""
    if not isinstance(value, str) or not value.strip():
        message = f"{name} filter must be a non-empty string"
        raise ValueError(message)


@dataclass(frozen=True)
class Catalog:
    """A loaded Component Catalog document with its canonical registry."""

    document: Document
    registry: Registry
    root: Path
    path: Path

    # -- inventory -------------------------------------------------------
    @property
    def entries(self) -> tuple[Entry, ...]:
        """Every catalog entry, in canonical registry order."""
        components = self.document.get("components", [])
        return tuple(entry for entry in components if isinstance(entry, Mapping))

    @property
    def component_ids(self) -> tuple[str, ...]:
        """Catalogued component identities, in canonical registry order."""
        return tuple(
            entry["component_id"]
            for entry in self.entries
            if isinstance(entry.get("component_id"), str)
        )

    def entry(self, component_id: str) -> Entry:
        """Return the catalog view of ``component_id``."""
        for entry in self.entries:
            if entry.get("component_id") == component_id:
                return entry
        message = f"component is not catalogued: {component_id}"
        raise KeyError(message)

    def registry_entry(self, component_id: str) -> Entry:
        """Return the authoritative registry entry of ``component_id``.

        The catalog view mirrors only the discovery dimensions; the registry
        remains the single source of the full metadata (dependencies,
        compatibility, artifact identity, datasets).
        """
        return self.registry.entry(component_id)

    def version(self, component_id: str) -> str:
        """Return the explicit public version catalogued for ``component_id``."""
        version = self.entry(component_id).get("component_version")
        if not isinstance(version, str):
            message = f"component has no explicit version: {component_id}"
            raise CatalogValidationError([message])
        return version

    # -- discovery -------------------------------------------------------
    def filter(
        self,
        *,
        component_class: str | None = None,
        data_scope: str | None = None,
        lifecycle_state: str | None = None,
        owner: str | None = None,
        scs_id: str | None = None,
    ) -> tuple[Entry, ...]:
        """Return the catalog entries matching every given dimension.

        Closed-vocabulary dimensions (``component_class``, ``data_scope``,
        ``lifecycle_state``) reject invalid values instead of silently
        matching nothing. Open-value dimensions (``owner``, ``scs_id``) may
        legitimately return an empty result. A floating selector is never
        accepted as a filter value: discovery never selects "whatever happens
        to be newest".
        """
        if component_class is not None:
            _require_vocabulary("component_class", component_class, COMPONENT_CLASSES)
        if data_scope is not None:
            _require_vocabulary("data_scope", data_scope, DATA_SCOPES)
        if lifecycle_state is not None:
            _require_vocabulary("lifecycle_state", lifecycle_state, LIFECYCLE_STATES)
        if owner is not None:
            _require_text("owner", owner)
        if scs_id is not None:
            _require_text("scs_id", scs_id)

        for name, value in (
            ("component_class", component_class),
            ("data_scope", data_scope),
            ("lifecycle_state", lifecycle_state),
            ("owner", owner),
            ("scs_id", scs_id),
        ):
            if is_floating_selector(value):
                message = (
                    f"{name}={value!r} is a floating selector; "
                    "discovery filters never select implicitly"
                )
                raise ValueError(message)

        selected: list[Entry] = []
        for entry in self.entries:
            if component_class is not None and entry.get("class") != component_class:
                continue
            scopes = entry.get("data_scopes")
            if data_scope is not None and (
                not isinstance(scopes, list) or data_scope not in scopes
            ):
                continue
            lifecycle = entry.get("lifecycle")
            state = (
                lifecycle.get("registry_state")
                if isinstance(lifecycle, Mapping)
                else None
            )
            if lifecycle_state is not None and state != lifecycle_state:
                continue
            if owner is not None and entry.get("owner") != owner:
                continue
            if scs_id is not None and entry.get("scs_id") != scs_id:
                continue
            selected.append(entry)
        return tuple(selected)

    # -- verification ----------------------------------------------------
    def validate(self) -> list[str]:
        """Return every validation violation of this catalog (empty when valid)."""
        return validate_document(self.document, root=self.root)

    def require_valid(self) -> list[str]:
        """Raise :class:`CatalogValidationError` when the catalog is invalid."""
        errors = self.validate()
        if errors:
            raise CatalogValidationError(errors)
        return errors


def load_catalog(root: Path | None = None, *, strict: bool = True) -> Catalog:
    """Load the canonical Component Catalog with its canonical registry.

    The registry is loaded strictly first: a catalog cannot be derived from
    invalid canonical sources. With ``strict=True`` (the default) an invalid
    catalog raises :class:`CatalogValidationError` instead of being handed
    out silently.
    """
    from component_registry.registry import discover_root

    base = root if root is not None else discover_root()
    registry = load_registry(base)
    path = base / CATALOG_PATH
    document = load_catalog_document(path)
    catalog = Catalog(document=document, registry=registry, root=base, path=path)
    if strict:
        catalog.require_valid()
    return catalog
