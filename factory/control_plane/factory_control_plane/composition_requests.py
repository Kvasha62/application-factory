"""Immutable Composition Request records — the approved composition path.

The record is the only object Slice 2 hands toward composition. It wraps the
**exact** Composition Request payload the Composer consumes and carries the
provenance (configuration digest, approval digest, generator identity) in the
wrapper, never inside the payload — the request schema forbids unknown
properties, and a control-plane field inside it would be a second authority
speaking in the Composer's own document.

The proof chain is re-derivable and re-checked here, fail closed:

    effective Approval → exact Configuration digest → Composition Request →
    existing Composer → draft Manifest → existing Manifest/Artifact gates

Nothing in this module publishes, approves a manifest, writes a manifest or
touches artifact identity: the produced payload yields a ``draft`` manifest and
the Platform Manifest lifecycle keeps its sole publication authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from factory_control_plane.approvals import (
    ApprovalLedger,
    approval_ineffectiveness_reasons,
    approval_reference,
    plain,
)
from factory_control_plane.configuration_v2 import (
    ConfigurationProjectionError,
    generate_request_payload,
)
from factory_control_plane.digests import digest_of, verify_digest
from factory_control_plane.findings import CODE_APPROVAL_NOT_EFFECTIVE
from factory_control_plane.support import settle, successor_sequence
from factory_control_plane.validation import (
    SCHEMA_COMPOSITION_REQUEST,
    ControlPlaneError,
    composition_request_record_errors,
)

#: Identity of the generator that produced the request payload; participates in
#: the record digest, so a change in generation behaviour is visible.
GENERATOR_VERSION = "control-plane/composition-requests/v1"


class CompositionRequestError(ControlPlaneError):
    """A composition request record cannot be created or verified (fail closed)."""


def request_fingerprint(document: Mapping[str, Any]) -> str:
    """Return the idempotency identity of a record.

    Derived from the approved pair ``(configuration_ref, approval_ref)`` and the
    generator identity only — never from the sequence number — so a repeated
    request for the same approved configuration is recognisably the same request
    and a store can return the existing record instead of appending a duplicate.
    """
    identity = {
        "configuration_ref": plain(document["configuration_ref"]),
        "approval_ref": plain(document["approval_ref"]),
        "generator_version": document.get("generator_version"),
    }
    return digest_of(identity)


def same_request(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when two records describe the same approved composition."""
    return request_fingerprint(left) == request_fingerprint(right)


def new_request_record(
    *,
    project_ref: str,
    configuration: Mapping[str, Any],
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    ledger: ApprovalLedger | None = None,
    predecessor: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Create the immutable record for an approved configuration (fail closed).

    There is no parameter for a request payload: the payload can only come from
    :func:`~factory_control_plane.configuration_v2.generate_request_payload` on
    the exact approved configuration, so no caller can hand in a composition the
    approval does not cover.
    """
    reasons = approval_ineffectiveness_reasons(
        approval, configuration=configuration, proposal=proposal, ledger=ledger
    )
    if reasons:
        joined = "; ".join(reasons)
        message = (
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the approval is not effective for "
            f"this configuration: {joined}"
        )
        raise CompositionRequestError(message)
    if configuration.get("project_ref") != project_ref:
        message = "the configuration version does not belong to this project"
        raise CompositionRequestError(message)
    try:
        payload = generate_request_payload(configuration, root=root)
    except ConfigurationProjectionError as error:
        raise CompositionRequestError(str(error)) from error
    request_id = (
        f"{project_ref}/requests/"
        f"{successor_sequence(predecessor['request_id'] if predecessor else None)}"
    )
    document: dict[str, Any] = {
        "schema_version": SCHEMA_COMPOSITION_REQUEST,
        "request_id": request_id,
        "project_ref": project_ref,
        "configuration_ref": {
            "configuration_id": configuration["configuration_id"],
            "digest": configuration["digest"],
        },
        "approval_ref": approval_reference(approval),
        "generator_version": GENERATOR_VERSION,
        "request": payload,
    }
    return settle(
        document,
        composition_request_record_errors,
        subject="composition request record",
    )


def verify_request_record(
    record: Mapping[str, Any],
    *,
    configuration: Mapping[str, Any] | None = None,
    proposal: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
    ledger: ApprovalLedger | None = None,
    root: Path | None = None,
) -> list[str]:
    """Re-derive the whole chain behind a record and report every disagreement.

    This is the detection half of the boundary: given the record and the
    documents it references, the payload must be the exact projection of the
    approved configuration, the approval must still be effective, and every
    digest must verify — offline, deterministically, with no writes.
    """
    if not isinstance(record, Mapping):
        return ["composition request record must be a JSON object"]
    document = plain(record)
    errors: list[str] = list(composition_request_record_errors(document))
    if not verify_digest(document):
        errors.append("composition request record digest does not match its payload")
    if configuration is None:
        return sorted(set(errors))
    expected_configuration = {
        "configuration_id": configuration.get("configuration_id"),
        "digest": configuration.get("digest"),
    }
    if document.get("configuration_ref") != expected_configuration:
        errors.append(
            "configuration_ref does not reference the supplied configuration version"
        )
    try:
        expected_payload = generate_request_payload(configuration, root=root)
        if document.get("request") != expected_payload:
            errors.append(
                "the nested request is not the exact projection of the referenced "
                "configuration version"
            )
    except ConfigurationProjectionError as error:
        errors.append(f"the referenced configuration cannot be projected: {error}")
    if approval is not None:
        if document.get("approval_ref") != approval_reference(approval):
            errors.append(
                "approval_ref does not reference the supplied approval record"
            )
        reasons = approval_ineffectiveness_reasons(
            approval, configuration=configuration, proposal=proposal, ledger=ledger
        )
        if reasons:
            errors.append(
                "the referenced approval is not effective: " + "; ".join(reasons)
            )
    return sorted(set(errors))
