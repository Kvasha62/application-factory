"""Component Registry — Slice A of Level 2 Component Factory (ADR-0015 §15).

The registry is the canonical machine-readable inventory of the factory's
components. It holds factory-level metadata only: identity, class, owner,
public version, published contract references, data ownership and data scope,
declared dependencies, compatibility policy, artifact identity and
lifecycle/release state.

It never holds business data, never accesses a component's internal database
and never imports a component's internals.

Public surface:

* :func:`load_registry` — load and validate the canonical registry;
* :class:`Registry` — read access to the registered inventory;
* :func:`validate_document` — deterministic validation of any registry document;
* :mod:`component_registry.validation` — identity, version, selector and
  boundary vocabulary used by that validation.
"""

from __future__ import annotations

from component_registry.errors import (
    RegistryError,
    RegistryNotFoundError,
    RegistryValidationError,
)
from component_registry.registry import (
    REGISTRY_PATH,
    Registry,
    discover_root,
    load_registry,
    load_registry_document,
)
from component_registry.validation import (
    COMPATIBILITY_POINTER,
    FLOATING_SELECTOR_TOKENS,
    OWNERSHIP_POINTER,
    canonical_contract_path,
    canonical_openapi_path,
    is_floating_selector,
    parse_semver,
    parse_version_range,
    range_admits,
    validate_document,
)

__all__ = [
    "COMPATIBILITY_POINTER",
    "FLOATING_SELECTOR_TOKENS",
    "OWNERSHIP_POINTER",
    "REGISTRY_PATH",
    "Registry",
    "RegistryError",
    "RegistryNotFoundError",
    "RegistryValidationError",
    "canonical_contract_path",
    "canonical_openapi_path",
    "discover_root",
    "is_floating_selector",
    "load_registry",
    "load_registry_document",
    "parse_semver",
    "parse_version_range",
    "range_admits",
    "validate_document",
]
