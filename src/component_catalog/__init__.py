"""Component Catalog — Slice B of Level 2 Component Factory (ADR-0015 §15).

The catalog is the discoverable view of the components available to the
factory. It is a **derived view**, not a second source of truth: every value
is computed from the canonical Component Registry (Slice A) and the published
Component Contracts, and validation rejects any divergence from that derived
form (ADR-0015 §5).

The catalog holds factory-level discovery metadata only. It never holds
business data, never accesses a component's internal database, never imports
a component's internals and never grants permission to reach another
component's internals.

Public surface:

* :func:`load_catalog` — load the canonical catalog with its registry;
* :class:`Catalog` — machine-readable discovery and filtering;
* :func:`build_catalog_document` — the deterministic derivation itself;
* :func:`validate_document` — deterministic validation of any catalog document.
"""

from __future__ import annotations

from component_catalog.catalog import (
    CATALOG_PATH,
    FILTER_DIMENSIONS,
    Catalog,
    load_catalog,
    load_catalog_document,
)
from component_catalog.derive import (
    CATALOG_ID,
    CATALOG_SCHEMA_PATH,
    CATALOG_SCHEMA_VERSION,
    SUMMARY_CONTRACT_FIELD,
    build_catalog_document,
    canonical_json,
    entry_digest,
    render_catalog_document,
    write_catalog,
)
from component_catalog.errors import (
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from component_catalog.validation import load_catalog_schema, validate_document

__all__ = [
    "CATALOG_ID",
    "CATALOG_PATH",
    "CATALOG_SCHEMA_PATH",
    "CATALOG_SCHEMA_VERSION",
    "FILTER_DIMENSIONS",
    "SUMMARY_CONTRACT_FIELD",
    "Catalog",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogValidationError",
    "build_catalog_document",
    "canonical_json",
    "entry_digest",
    "load_catalog",
    "load_catalog_document",
    "load_catalog_schema",
    "render_catalog_document",
    "validate_document",
    "write_catalog",
]
