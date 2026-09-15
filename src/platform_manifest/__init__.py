"""Platform Manifest — Slice C of Level 2 Component Factory (ADR-0015 §15).

The Platform Manifest is the explicit, reproducible description of a Platform
Instance composition: concrete component versions, artifact identities, manifest
identity/version, lifecycle, and optional references to Golden Bundle,
configuration, extensions and branding.

It is validated against authoritative Component Registry/Catalog metadata and
never uses floating selectors. Published manifests are immutable.

Public surface:

* :func:`load_manifest` — load and validate a manifest document;
* :class:`Manifest` — read access and deterministic digest;
* :func:`build_manifest_document` — deterministic construction;
* :func:`validate_document` — deterministic validation of any manifest document;
* :func:`check_immutability` — published immutability check;
* :func:`validate_transition` — lifecycle transition validation.
"""

from __future__ import annotations

from platform_manifest.errors import (
    ManifestError,
    ManifestNotFoundError,
    ManifestValidationError,
)
from platform_manifest.lifecycle import (
    LIFECYCLE_ORDER,
    LIFECYCLE_STATES,
    is_allowed_transition,
    is_forward_transition,
    is_valid_state,
    requires_approval,
    requires_publication,
)
from platform_manifest.manifest import (
    MANIFEST_DIR,
    MANIFEST_SCHEMA_PATH,
    Manifest,
    build_manifest_document,
    canonical_json,
    compute_manifest_digest,
    discover_root,
    load_manifest,
    load_manifest_document,
    manifest_content_for_digest,
    render_manifest_document,
)
from platform_manifest.schema import SCHEMA_PATH, load_schema
from platform_manifest.validation import (
    FLOATING_SELECTOR_TOKENS,
    check_immutability,
    is_floating_selector,
    parse_semver,
    validate_document,
    validate_transition,
)

__all__ = [
    "FLOATING_SELECTOR_TOKENS",
    "LIFECYCLE_ORDER",
    "LIFECYCLE_STATES",
    "MANIFEST_DIR",
    "MANIFEST_SCHEMA_PATH",
    "SCHEMA_PATH",
    "Manifest",
    "ManifestError",
    "ManifestNotFoundError",
    "ManifestValidationError",
    "build_manifest_document",
    "canonical_json",
    "check_immutability",
    "compute_manifest_digest",
    "discover_root",
    "is_allowed_transition",
    "is_floating_selector",
    "is_forward_transition",
    "is_valid_state",
    "load_manifest",
    "load_manifest_document",
    "load_schema",
    "manifest_content_for_digest",
    "parse_semver",
    "render_manifest_document",
    "requires_approval",
    "requires_publication",
    "validate_document",
    "validate_transition",
]
