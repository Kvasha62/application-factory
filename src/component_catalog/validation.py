"""Deterministic validation of the Component Catalog document.

The catalog is a derived view, so its validity is defined by its canonical
sources (ADR-0015 §5; Issue #64):

* the canonical Component Registry must itself load and validate;
* the catalog must describe exactly the registered inventory, in canonical
  registry order, with no invented and no silently missing component;
* every mirrored field must equal its registry source;
* every ``entry_digest`` must pin the current canonical registry entry;
* every ``summary`` must mirror the published contract of its component;
* the whole document must be the deterministic derived view
  (:func:`component_catalog.derive.build_catalog_document`).

Any divergence — a hand edit, a stale snapshot after a registry or contract
change, a reordered or duplicated entry — is rejected with a complete,
deterministic report. The catalog is therefore never a second source of
truth: it is either the derived view of the canonical metadata, or invalid.

The catalog validator reads only the canonical registry, the published
contract descriptors and the catalog document itself. It never imports a
component's internals and never opens a component database (ADR-0015 §5, §11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from component_catalog.derive import (
    CATALOG_SCHEMA_PATH,
    build_catalog_document,
    contract_summary,
    entry_digest,
)
from component_registry import REGISTRY_PATH, Registry, load_registry
from component_registry.errors import RegistryError
from component_registry.schema import validate_structure
from component_registry.validation import CONTRACT_ROOT, is_floating_selector

#: Fields of a catalog entry that mirror one scalar registry-entry field.
_MIRRORED_FIELDS = (
    ("component_version", "component_version"),
    ("class", "class"),
    ("scs_id", "scs_id"),
    ("maturity_level", "maturity_level"),
    ("owner", "owner"),
)

#: Fields of the verbatim-copied ``lifecycle`` view.
_LIFECYCLE_FIELDS = (
    "registry_state",
    "deployable",
    "publishable",
    "publishability_blockers",
)


def load_catalog_schema(root: Path) -> Mapping[str, object]:
    """Return the normative catalog schema read from ``root``."""
    path = root / CATALOG_SCHEMA_PATH
    if not path.is_file():
        message = f"catalog schema not found: {CATALOG_SCHEMA_PATH}"
        raise FileNotFoundError(message)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        message = f"catalog schema is not a JSON object: {CATALOG_SCHEMA_PATH}"
        raise TypeError(message)
    return document


def _load_canonical_registry(root: Path) -> Registry | None:
    """Load the canonical registry strictly; None when it cannot serve as source."""
    try:
        return load_registry(root)
    except RegistryError:
        return None


def _linkage_errors(document: Mapping, registry: Registry) -> list[str]:
    """The ``derived_from`` block must link to the canonical registry."""
    errors: list[str] = []
    derived_from = document.get("derived_from")
    if not isinstance(derived_from, Mapping):
        return ["$.derived_from: an explicit derivation link is required"]

    registry_path = derived_from.get("canonical_registry")
    if registry_path != REGISTRY_PATH:
        errors.append(
            "$.derived_from.canonical_registry: "
            f"{registry_path!r} is not the canonical registry {REGISTRY_PATH!r}"
        )

    registry_id = derived_from.get("registry_id")
    canonical_id = registry.document.get("registry_id")
    if registry_id != canonical_id:
        errors.append(
            "$.derived_from.registry_id: "
            f"{registry_id!r} does not link to the canonical registry identity {canonical_id!r}"
        )

    contracts_root = derived_from.get("component_contracts_root")
    if contracts_root != CONTRACT_ROOT:
        errors.append(
            "$.derived_from.component_contracts_root: "
            f"{contracts_root!r} is not the canonical component contracts root {CONTRACT_ROOT!r}"
        )
    return errors


def _inventory_errors(document: Mapping, registry: Registry) -> list[str]:
    """Inventory parity: exactly the registered components, in registry order."""
    errors: list[str] = []
    components = document.get("components")
    if not isinstance(components, list):
        errors.append("$.components: expected a list of catalog entries")
        return errors

    seen: set[str] = set()
    catalogued: list[str] = []
    for index, entry in enumerate(components):
        path = f"$.components[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{path}: catalog entry must be an object")
            continue
        component_id = entry.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            errors.append(
                f"{path}.component_id: a stable component identity is required"
            )
            continue
        if component_id in seen:
            errors.append(
                f"{path}.component_id: duplicate component identity {component_id!r}"
            )
            continue
        seen.add(component_id)
        catalogued.append(component_id)

    registered = list(registry.component_ids)
    for component_id in sorted(seen - set(registered)):
        errors.append(
            f"$.components: catalog describes {component_id!r}, "
            "which the canonical registry does not register"
        )
    for component_id in sorted(set(registered) - seen):
        errors.append(
            f"$.components: registered component {component_id!r} is missing from the catalog"
        )

    known = [item for item in catalogued if item in set(registered)]
    if not errors and known != registered:
        errors.append(
            "$.components: catalog entries do not follow the canonical registry order"
        )
    return errors


def _selector_errors(entry: Mapping, path: str) -> list[str]:
    """A catalog entry never selects anything by a floating selector."""
    errors: list[str] = []
    component_id = entry.get("component_id")
    if is_floating_selector(component_id):
        errors.append(
            f"{path}.component_id: {component_id!r} is a floating selector, "
            "not a stable component identity"
        )
    version = entry.get("component_version")
    if is_floating_selector(version):
        errors.append(
            f"{path}.component_version: {version!r} is a floating selector; "
            "an explicit SemVer version is required"
        )
    return errors


def _mirror_errors(
    entry: Mapping, path: str, registry_entry: Mapping, root: Path
) -> list[str]:
    """Every mirrored field must equal its canonical registry source."""
    errors: list[str] = []
    component_id = registry_entry.get("component_id")

    for field, registry_field in _MIRRORED_FIELDS:
        declared = entry.get(field)
        expected = registry_entry.get(registry_field)
        if declared != expected:
            errors.append(
                f"{path}.{field}: catalog declares {declared!r}, "
                f"the canonical registry declares {expected!r} for {component_id!r}"
            )

    ownership = registry_entry.get("data_ownership")
    expected_scopes = (
        ownership.get("data_scopes") if isinstance(ownership, Mapping) else None
    )
    if entry.get("data_scopes") != expected_scopes:
        errors.append(
            f"{path}.data_scopes: catalog declares {entry.get('data_scopes')!r}, "
            f"the canonical registry declares {expected_scopes!r} for {component_id!r}"
        )

    registry_lifecycle = registry_entry.get("lifecycle")
    lifecycle = entry.get("lifecycle")
    for field in _LIFECYCLE_FIELDS:
        expected = (
            registry_lifecycle.get(field)
            if isinstance(registry_lifecycle, Mapping)
            else None
        )
        declared = lifecycle.get(field) if isinstance(lifecycle, Mapping) else None
        if declared != expected:
            errors.append(
                f"{path}.lifecycle.{field}: catalog declares {declared!r}, "
                f"the canonical registry declares {expected!r} for {component_id!r}"
            )

    registry_contracts = registry_entry.get("contracts")
    contracts = entry.get("contracts")
    for field in ("component_contract", "openapi"):
        expected = (
            registry_contracts.get(field)
            if isinstance(registry_contracts, Mapping)
            else None
        )
        declared = contracts.get(field) if isinstance(contracts, Mapping) else None
        if declared != expected:
            errors.append(
                f"{path}.contracts.{field}: catalog declares {declared!r}, "
                f"the canonical registry declares {expected!r} for {component_id!r}"
            )

    declared_digest = entry.get("entry_digest")
    expected_digest = entry_digest(registry_entry)
    if declared_digest != expected_digest:
        errors.append(
            f"{path}.entry_digest: {declared_digest!r} does not pin the canonical "
            f"registry entry of {component_id!r} (expected {expected_digest!r})"
        )

    declared_summary = entry.get("summary")
    expected_summary = contract_summary(registry_entry, root)
    if declared_summary != expected_summary:
        contract = (
            registry_contracts.get("component_contract")
            if isinstance(registry_contracts, Mapping)
            else None
        )
        errors.append(
            f"{path}.summary: does not mirror the task statement "
            f"of the published contract {contract!r} of {component_id!r}"
        )
    return errors


def validate_document(document: object, *, root: Path | None = None) -> list[str]:
    """Validate a catalog document and return every violation, sorted.

    The same document always yields the same list. Validation is total: a
    catalog is valid only when it is exactly the derived view of the current
    canonical registry and the published contracts — anything else is
    divergence and is reported.
    """
    from component_registry.registry import discover_root

    base = root if root is not None else discover_root()
    errors: list[str] = list(validate_structure(document, load_catalog_schema(base)))

    if not isinstance(document, Mapping):
        return sorted(set(errors))

    registry = _load_canonical_registry(base)
    if registry is None:
        errors.append(
            "$.derived_from: the canonical registry is not loadable and valid; "
            "the catalog cannot be derived from invalid canonical sources"
        )
        return sorted(set(errors))

    errors.extend(_linkage_errors(document, registry))
    errors.extend(_inventory_errors(document, registry))

    registered = set(registry.component_ids)
    components = document.get("components")
    if isinstance(components, list):
        for index, entry in enumerate(components):
            path = f"$.components[{index}]"
            if not isinstance(entry, Mapping):
                continue
            errors.extend(_selector_errors(entry, path))
            component_id = entry.get("component_id")
            if isinstance(component_id, str) and component_id in registered:
                errors.extend(
                    _mirror_errors(entry, path, registry.entry(component_id), base)
                )

    if document != build_catalog_document(registry):
        errors.append(
            "catalog document: not the deterministic derived view of the canonical "
            "registry and the published contracts; "
            "regenerate it (python -m component_catalog build)"
        )

    return sorted(set(errors))
