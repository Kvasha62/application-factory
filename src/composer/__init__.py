"""Composer — Slice E of Level 2 Component Factory (ADR-0015 §15).

The Composer implements ARCHITECTURE.md §17: it resolves dependencies, verifies
compatibility, selects versions, applies configuration, checks extensions,
produces a reproducible Platform Manifest and hands it to validation.

It composes platforms deterministically from published factory metadata only —
the authoritative Component Registry, published component contracts and Golden
Bundle definitions — and rejects an incompatible or contract-invalid assembly
with a complete, deterministic explanation (ADR-0015 §8).

It never reads business data, never accesses a component database, never
imports component internals, never invents a version or an availability source
of its own, and never approves, publishes or deploys anything: its output is a
``draft`` Platform Manifest (ADR-0015 §8, §11, §13).

Public surface:

* :func:`compose` — compose a platform from a validated composition request;
* :class:`Composition` — the produced draft manifest, its deterministic
  selection decisions and the composition report;
* :func:`compose_diagnostics` — every violation of a request/composition;
* :func:`validate_request_document` — deterministic request validation;
* :mod:`composer.request` — the composition request document;
* :mod:`composer.resolution` — dependency closure and version selection;
* :mod:`composer.verification` — compatibility, contract, configuration,
  extension and bundle-evidence verification.
"""

from __future__ import annotations

from composer.composer import (
    COMPOSER_DIR,
    EXAMPLE_REQUEST_PATH,
    PRODUCED_LIFECYCLE_STATE,
    REQUEST_SCHEMA_PATH,
    Composition,
    build_request_document,
    compose,
    compose_diagnostics,
    compose_request_document,
    compose_request_path,
    discover_root,
    load_request,
    load_request_document,
    render_request_document,
    validate_request_document,
)
from composer.errors import (
    ComposerError,
    ComposerNotFoundError,
    ComposerValidationError,
    CompositionRejectedError,
)
from composer.request import (
    COMPONENT_ID_PATTERN,
    EXTENSION_MECHANISMS,
    FORBIDDEN_DEPENDENCY_KINDS,
    MANIFEST_ID_PATTERN,
    CompositionRequest,
)
from composer.resolution import (
    SELECTED_AS_DEPENDENCY,
    SELECTED_AS_EXPLICIT_RANGE,
    SELECTED_AS_EXPLICIT_VERSION,
    Resolution,
    SelectedComponent,
    resolve_closure,
    selected_versions,
)
from composer.schema import SCHEMA_PATH, load_schema
from composer.verification import (
    COMPOSABLE_BUNDLE_STATES,
    SUPPORTED_CONFIGURATION_KEYWORDS,
    VerificationResult,
    contract_path,
    read_published_contract,
    verify_composition,
)

__all__ = [
    "COMPONENT_ID_PATTERN",
    "COMPOSABLE_BUNDLE_STATES",
    "COMPOSER_DIR",
    "EXAMPLE_REQUEST_PATH",
    "EXTENSION_MECHANISMS",
    "FORBIDDEN_DEPENDENCY_KINDS",
    "MANIFEST_ID_PATTERN",
    "PRODUCED_LIFECYCLE_STATE",
    "REQUEST_SCHEMA_PATH",
    "SCHEMA_PATH",
    "SELECTED_AS_DEPENDENCY",
    "SELECTED_AS_EXPLICIT_RANGE",
    "SELECTED_AS_EXPLICIT_VERSION",
    "SUPPORTED_CONFIGURATION_KEYWORDS",
    "ComposerError",
    "ComposerNotFoundError",
    "ComposerValidationError",
    "Composition",
    "CompositionRejectedError",
    "CompositionRequest",
    "Resolution",
    "SelectedComponent",
    "VerificationResult",
    "build_request_document",
    "compose",
    "compose_diagnostics",
    "compose_request_document",
    "compose_request_path",
    "contract_path",
    "discover_root",
    "load_request",
    "load_request_document",
    "load_schema",
    "read_published_contract",
    "render_request_document",
    "resolve_closure",
    "selected_versions",
    "validate_request_document",
    "verify_composition",
]
