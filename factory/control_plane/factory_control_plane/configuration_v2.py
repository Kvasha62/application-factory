"""``control-plane/configuration/v2`` — the request-shaped configuration (S2-1).

Slice 2 adds a second ConfigurationVersion generation. The merged Slice 1 v1
payload cannot be projected into a Composition Request at all: the request
schema requires ``manifest.manifest_version`` and request-shaped
``golden_bundle``/``extensions``, while v1 carries only ``manifest_id`` and
free-form objects (proved by execution: four deterministic composer
violations). v2 is therefore **additive** — v1 documents, their digests and
their validators are untouched and every v1 document stays valid forever.

Fail-closed boundary: a v1 configuration is *valid* but **not projectable**.
Submitting it to composition fails closed with the deterministic message of
:data:`NOT_PROJECTABLE_TEMPLATE`; it is never silently upgraded, never
defaulted, and there is no compatibility path that fabricates a
``manifest_version``. Migration is a v2 successor: a new version with a new
digest, which requires a new approval like any other technical change.

Projection delegates: the payload is built by the Composer's own public
builder (``build_request_document``) and validated by the Composer's own
validators (``validate_request_document`` and ``compose_diagnostics``), so no
composition rule is re-implemented here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from composer import (
    build_request_document,
    compose_diagnostics,
    discover_root,
    validate_request_document,
)
from factory_control_plane.support import settle, successor_sequence
from factory_control_plane.validation import (
    SCHEMA_CONFIGURATION_V2,
    ControlPlaneError,
    configuration_version_errors,
    configuration_version_v2_errors,
)

#: The frozen Slice 1 generation, referenced only to explain the refusal.
SCHEMA_CONFIGURATION_V1 = "control-plane/configuration/v1"

#: Deterministic fail-closed message for every non-v2 configuration version.
NOT_PROJECTABLE_TEMPLATE = (
    "configuration version {schema} cannot be projected into a Composition "
    "Request: only control-plane/configuration/v2 carries the request-shaped "
    "manifest header (manifest_id, manifest_version, predecessor) and the "
    "Composition Request golden_bundle/extensions forms. The version stays "
    "valid and digest-verifiable; create a v2 successor (new version, new "
    "digest, new approval) to make it composable."
)

#: Attachment slots, exactly as in Slice 1 (approval is an immutable ledger
#: record in Slice 2, never a field of the configuration payload).
_ATTACHMENT_KEYS = ("proposal", "approval", "build", "release")


class ConfigurationProjectionError(ControlPlaneError):
    """A configuration version cannot be projected into a composition request."""


def _as_extensions(value: object) -> object:
    """Normalise the extension list without hiding a malformed shape.

    A sequence of objects is copied entry by entry; anything else is passed
    through untouched, so the validator — not this helper — decides the
    wording of the refusal.
    """
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [dict(entry) if isinstance(entry, Mapping) else entry for entry in value]
    return value


def is_v2(document: object) -> bool:
    """True when ``document`` declares the Slice 2 configuration generation."""
    return (
        isinstance(document, Mapping)
        and document.get("schema_version") == SCHEMA_CONFIGURATION_V2
    )


def not_projectable_errors(document: object) -> list[str]:
    """Return the fail-closed refusal for any non-v2 configuration version."""
    schema = document.get("schema_version") if isinstance(document, Mapping) else None
    if schema == SCHEMA_CONFIGURATION_V2:
        return []
    return [NOT_PROJECTABLE_TEMPLATE.format(schema=schema or "unset")]


def structural_errors(document: object) -> list[str]:
    """Control-plane structural errors for the document's declared generation."""
    if is_v2(document):
        return configuration_version_v2_errors(document)
    return configuration_version_errors(document)


def request_payload(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact Composition Request payload of a v2 configuration.

    The payload is built by the Composer's public builder: component order,
    optional-field omission and the ``$schema`` declaration are the Composer's
    decisions, not a second implementation of them.
    """
    manifest = document["manifest"]
    return build_request_document(
        manifest_id=manifest["manifest_id"],
        manifest_version=manifest["manifest_version"],
        predecessor=manifest.get("predecessor"),
        components=[dict(component) for component in document["components"]],
        configuration=dict(document["configuration"]),
        golden_bundle=document.get("golden_bundle"),
        extensions=document.get("extensions"),
        branding=document.get("branding"),
    )


def composer_errors(
    document: Mapping[str, Any], *, root: Path | None = None
) -> list[str]:
    """Return the Composer's own diagnostics for the projected request.

    Delegated verbatim: request validation and composition verification are
    the Composer's rules, so this function only asks and reports.
    """
    base = root if root is not None else discover_root()
    payload = request_payload(document)
    errors = list(validate_request_document(payload, root=base))
    errors.extend(compose_diagnostics(payload, root=base))
    return sorted(set(errors))


def projection_violations(document: object, *, root: Path | None = None) -> list[str]:
    """Return every reason the document cannot produce a composition request.

    Total and deterministic: a non-v2 generation yields the fail-closed
    refusal, a structurally invalid v2 yields its validator messages, and a
    valid v2 yields the Composer's diagnostics. An empty list is the only
    state in which :func:`generate_request_payload` returns a payload.
    """
    refusal = not_projectable_errors(document)
    if refusal:
        return refusal
    structural = structural_errors(document)
    if structural:
        return structural
    return composer_errors(document, root=root)


def generate_request_payload(
    document: object, *, root: Path | None = None
) -> dict[str, Any]:
    """Return the Composition Request payload of a valid v2 configuration.

    Fails closed (``ConfigurationProjectionError``) for a v1 configuration, for
    an invalid v2 document and for any payload the Composer itself would
    reject — nothing is defaulted or repaired to make a request exist.
    """
    violations = projection_violations(document, root=root)
    if violations:
        joined = "; ".join(violations)
        message = f"composition request cannot be generated: {joined}"
        raise ConfigurationProjectionError(message)
    return request_payload(document)


def new_configuration_version_v2(
    *,
    project_ref: str,
    requirements_ref: Mapping[str, Any],
    manifest_id: str,
    manifest_version: str,
    components: Sequence[Mapping[str, Any]],
    configuration: Mapping[str, Any],
    predecessor: Mapping[str, Any] | None = None,
    manifest_predecessor: Mapping[str, Any] | None = None,
    template_ref: str | None = None,
    variant_ref: str | None = None,
    golden_bundle: Mapping[str, Any] | None = None,
    extensions: Sequence[Mapping[str, Any]] | None = None,
    branding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the next immutable, selector-free ``configuration/v2`` version.

    Same guarantees as the Slice 1 builder — digest settled before any write,
    no update path, Registry cross-check fail-closed — plus the request-shaped
    header v2 needs. ``predecessor`` links the configuration chain;
    ``manifest_predecessor`` records the manifest lineage of the composed
    platform (optional; null for the first manifest of the lineage).
    """
    previous_id = None
    previous_digest = None
    if predecessor is not None:
        previous_id = predecessor["configuration_id"]
        previous_digest = predecessor["digest"]
    sequence = successor_sequence(previous_id)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_CONFIGURATION_V2,
        "configuration_id": f"{project_ref}/configurations/{sequence}",
        "project_ref": project_ref,
        "requirements_ref": {
            "requirements_id": requirements_ref["requirements_id"],
            "digest": requirements_ref["digest"],
        },
        "template_ref": template_ref,
        "variant_ref": variant_ref,
        "manifest": {
            "manifest_id": manifest_id,
            "manifest_version": manifest_version,
            "predecessor": (
                None if manifest_predecessor is None else dict(manifest_predecessor)
            ),
        },
        "components": [dict(component) for component in components],
        "configuration": dict(configuration),
        "golden_bundle": None if golden_bundle is None else dict(golden_bundle),
        "extensions": None if extensions is None else _as_extensions(extensions),
        "branding": None if branding is None else dict(branding),
        "attachments": dict.fromkeys(_ATTACHMENT_KEYS),
        "predecessor": (
            None
            if previous_id is None
            else {"configuration_id": previous_id, "digest": previous_digest}
        ),
    }
    return settle(
        document,
        configuration_version_v2_errors,
        subject="configuration version v2 document",
    )
