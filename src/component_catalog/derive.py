"""Deterministic derivation of the Component Catalog from canonical sources.

The catalog is a *derived view* (ADR-0015 §5): every value in the catalog
document is computed from the canonical Component Registry and the published
Component Contracts. Nothing is invented here, and nothing in this module is
a source of truth — regenerate the catalog when the canonical sources change:

```bash
python -m component_catalog build
```

The derivation is a pure function of the canonical sources: the same registry
and the same contracts always produce the same catalog document, byte for
byte. Validation (:mod:`component_catalog.validation`) proves that the stored
document still is that derived view.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from component_registry import REGISTRY_PATH, Registry
from component_registry.validation import CONTRACT_ROOT

#: Canonical catalog document, relative to the repository root.
CATALOG_PATH = "factory/catalog/component_catalog.json"

#: Canonical catalog schema, relative to the repository root.
CATALOG_SCHEMA_PATH = "factory/catalog/schema/component_catalog.schema.json"

#: Stable identity of this catalog.
CATALOG_ID = "application-factory-component-catalog"

#: Version of the catalog document format itself.
CATALOG_SCHEMA_VERSION = "1.0.0"

#: The published contract field the catalog ``summary`` mirrors verbatim.
#: ``null`` means the contract declares no task statement — an honest absence,
#: never an invented description.
SUMMARY_CONTRACT_FIELD = "task"

Document = Mapping[str, object]
Entry = Mapping[str, object]


def canonical_json(value: object) -> str:
    """Serialize ``value`` to the canonical JSON form used for digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_digest(entry: Entry) -> str:
    """Return the immutable content digest that pins a registry entry.

    The digest covers the **whole** canonical registry entry — including the
    fields the catalog view does not copy — so any change to a registry entry
    makes the catalog's recorded digest stale, and validation rejects the
    catalog as diverged.
    """
    encoded = canonical_json(entry).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _contract_reference(entry: Entry) -> str:
    """Return the canonical contract reference of ``entry``'s component."""
    contracts = entry.get("contracts")
    relative = (
        contracts.get("component_contract") if isinstance(contracts, Mapping) else None
    )
    if not isinstance(relative, str) or not relative:
        message = "registry entry declares no canonical contract reference"
        raise ValueError(message)
    return relative


def read_published_contract(entry: Entry, root: Path) -> Mapping[str, object]:
    """Read the canonical published contract of ``entry``'s component.

    The registry entry is Slice-A-validated before it reaches this module:
    ``contracts.component_contract`` is the canonical path of the contract of
    exactly this component, and it exists and declares the same identity and
    version. Only published contract descriptors are read — never a
    component's internals (ADR-0015 §5, §11).
    """
    relative = _contract_reference(entry)
    document = json.loads((root / relative).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        message = f"published contract is not a JSON object: {relative}"
        raise TypeError(message)
    return document


def contract_summary(entry: Entry, root: Path) -> str | None:
    """Return the discovery summary derived from the published contract."""
    task = read_published_contract(entry, root).get(SUMMARY_CONTRACT_FIELD)
    if isinstance(task, str) and task.strip():
        return task
    return None


def _data_scopes(entry: Entry) -> list[object]:
    """Copy the declared data scopes of ``entry`` from its registry source."""
    ownership = entry.get("data_ownership")
    if not isinstance(ownership, Mapping):
        return []
    scopes = ownership.get("data_scopes")
    return list(scopes) if isinstance(scopes, list) else []


def _verbatim(entry: Entry, section: str) -> dict[str, object]:
    """Copy a sub-object of ``entry`` verbatim, preserving its exact keys."""
    value = entry.get(section)
    return dict(value) if isinstance(value, Mapping) else {}


def build_catalog_document(registry: Registry) -> dict[str, object]:
    """Derive the catalog document from the canonical registry and contracts.

    Deterministic: entries follow canonical registry order, keys follow one
    fixed layout, and sub-objects (``lifecycle``, ``contracts``) are copied
    verbatim from the registry entry. The only contract-derived value is the
    ``summary``; the only computed value is the ``entry_digest``.
    """
    components: list[dict[str, object]] = []
    for entry in registry.entries:
        components.append(
            {
                "component_id": entry.get("component_id"),
                "component_version": entry.get("component_version"),
                "class": entry.get("class"),
                "scs_id": entry.get("scs_id"),
                "maturity_level": entry.get("maturity_level"),
                "owner": entry.get("owner"),
                "data_scopes": _data_scopes(entry),
                "lifecycle": _verbatim(entry, "lifecycle"),
                "contracts": _verbatim(entry, "contracts"),
                "summary": contract_summary(entry, registry.root),
                "entry_digest": entry_digest(entry),
            }
        )
    return {
        "$schema": CATALOG_SCHEMA_PATH,
        "catalog_id": CATALOG_ID,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "derived_from": {
            "canonical_registry": REGISTRY_PATH,
            "registry_id": registry.document.get("registry_id"),
            "component_contracts_root": CONTRACT_ROOT,
        },
        "components": components,
    }


def render_catalog_document(document: Document) -> str:
    """Serialize a catalog document deterministically (for the canonical file)."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write_catalog(registry: Registry, root: Path | None = None) -> Path:
    """Regenerate the canonical catalog document on disk and return its path."""
    base = root if root is not None else registry.root
    path = base / CATALOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_catalog_document(build_catalog_document(registry))
    path.write_text(rendered, encoding="utf-8")
    return path
