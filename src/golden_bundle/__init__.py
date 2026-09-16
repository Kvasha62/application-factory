"""Golden Bundle — Slice D of Level 2 Component Factory (ADR-0015 §15).

A Golden Bundle is a validated, reproducible set of compatible component
versions intended to be used as a known-good composition (ARCHITECTURE.md
§14, ADR-0015 §7). It pins concrete component versions and records its
validated compatibility relationships explicitly; it never resolves,
substitutes or assembles versions, never becomes a second source of truth for
component metadata and never touches a component's internals.

Public surface:

* :func:`load_bundle` — load and validate a bundle document;
* :class:`GoldenBundle` — read access and deterministic digest;
* :func:`build_bundle_document` — deterministic construction;
* :func:`validate_document` — deterministic validation of any bundle document;
* :func:`check_immutability` — delivered-bundle immutability check;
* :func:`validate_transition` — lifecycle transition validation.
"""

from __future__ import annotations

from golden_bundle.bundle import (
    BUNDLE_DIR,
    BUNDLE_SCHEMA_PATH,
    GoldenBundle,
    build_bundle_document,
    bundle_content_for_digest,
    canonical_json,
    compute_bundle_digest,
    discover_root,
    load_bundle,
    load_bundle_document,
    render_bundle_document,
)
from golden_bundle.errors import (
    GoldenBundleError,
    GoldenBundleNotFoundError,
    GoldenBundleValidationError,
)
from golden_bundle.lifecycle import (
    BUNDLE_STATES,
    LIFECYCLE_ORDER,
    is_allowed_transition,
    is_forward_transition,
    is_valid_state,
    requires_certification,
)
from golden_bundle.schema import SCHEMA_PATH, load_schema
from golden_bundle.validation import (
    FLOATING_SELECTOR_TOKENS,
    check_immutability,
    is_floating_selector,
    parse_semver,
    validate_document,
    validate_transition,
)

__all__ = [
    "BUNDLE_DIR",
    "BUNDLE_SCHEMA_PATH",
    "BUNDLE_STATES",
    "FLOATING_SELECTOR_TOKENS",
    "LIFECYCLE_ORDER",
    "SCHEMA_PATH",
    "GoldenBundle",
    "GoldenBundleError",
    "GoldenBundleNotFoundError",
    "GoldenBundleValidationError",
    "build_bundle_document",
    "bundle_content_for_digest",
    "canonical_json",
    "check_immutability",
    "compute_bundle_digest",
    "discover_root",
    "is_allowed_transition",
    "is_floating_selector",
    "is_forward_transition",
    "is_valid_state",
    "load_bundle",
    "load_bundle_document",
    "load_schema",
    "parse_semver",
    "render_bundle_document",
    "requires_certification",
    "validate_document",
    "validate_transition",
]
