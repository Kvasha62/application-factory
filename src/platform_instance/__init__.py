"""Platform Instance — Slice F of Level 2 Component Factory (ADR-0015 §15).

The Platform Instance assembly representation is the deterministic result of
assembling one concrete platform from one accepted/validated Platform
Manifest (ARCHITECTURE.md §2.2, §31; ADR-0015 §15):

    Composer → Platform Manifest → Validated Manifest
        → Slice F — Platform Instance Assembly
        → Concrete Platform Instance

An instance document binds a Platform Instance identity (``platform_id``) to
exactly one manifest identity/version/digest and preserves the manifest's
composition content verbatim: concrete component identities/versions,
artifact identities, Golden Bundle reference (or the explicit uncertified
status), configuration, extensions and branding — with environment-specific
settings entering through the existing component-configuration contract path
only. The result carries a deterministic assembly identity
(``instance_digest``, sha256 over canonical JSON) with no dependence on
time, randomness, machine state or network.

This slice is a representation step, not a deployment step: it deploys,
provisions, rolls out, approves and publishes nothing, introduces no new
lifecycle vocabulary, creates no second source of truth and never mutates
authoritative Registry/Catalog/Manifest/Golden Bundle data.

Public surface:

* :func:`assemble` — assemble an instance from a validated manifest;
* :func:`assemble_document` — assemble from a manifest document;
* :func:`assemble_manifest_path` — load, validate and assemble;
* :func:`assembly_diagnostics` — every violation, deterministically, without
  raising;
* :func:`build_instance_document` — deterministic construction;
* :func:`validate_instance_document` — deterministic validation of an
  instance against its bound manifest and the applicable factory contracts;
* :class:`Instance` — read access and deterministic digest;
* :func:`load_instance_document` / :func:`render_instance_document`.
"""

from __future__ import annotations

from platform_instance.assembly import (
    PLATFORM_ID_PATTERN,
    assemble,
    assemble_document,
    assemble_manifest_path,
    assembly_diagnostics,
    build_instance_document,
)
from platform_instance.errors import (
    AssemblyRejectedError,
    InstanceNotFoundError,
    InstanceValidationError,
    PlatformInstanceError,
)
from platform_instance.instance import (
    EXAMPLE_INSTANCE_PATH,
    INSTANCE_DIR,
    Instance,
    canonical_json,
    compute_instance_digest,
    discover_root,
    instance_content_for_digest,
    load_instance_document,
    render_instance_document,
)
from platform_instance.schema import SCHEMA_PATH, load_schema
from platform_instance.validation import (
    ASSEMBLABLE_MANIFEST_STATES,
    PLATFORM_IDENTITY_CONFIG_KEYS,
    validate_instance_document,
)

__all__ = [
    "ASSEMBLABLE_MANIFEST_STATES",
    "EXAMPLE_INSTANCE_PATH",
    "INSTANCE_DIR",
    "PLATFORM_IDENTITY_CONFIG_KEYS",
    "PLATFORM_ID_PATTERN",
    "SCHEMA_PATH",
    "AssemblyRejectedError",
    "Instance",
    "InstanceNotFoundError",
    "InstanceValidationError",
    "PlatformInstanceError",
    "assemble",
    "assemble_document",
    "assemble_manifest_path",
    "assembly_diagnostics",
    "build_instance_document",
    "canonical_json",
    "compute_instance_digest",
    "discover_root",
    "instance_content_for_digest",
    "load_instance_document",
    "load_schema",
    "render_instance_document",
    "validate_instance_document",
]
