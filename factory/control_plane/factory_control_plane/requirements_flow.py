"""Slice 3 strict cross-link / re-derivation / approval-use wrapper (S3).

Consumes the intake adapters and drives the hardened Slice 2 proof chain end
to end:

    intake -> RequirementsVersion -> exact Requirements<->Configuration<->Project link
    -> derive (or strictly verify) Proposal -> create (or verify) Approval through
    the hardened S2 ledger -> delegate to the S2 Composition Request builder.

Every cross-reference is a typed ``(id, digest)`` pair; the Proposal is
re-derived from the exact inputs and compared to any supplied Proposal; the
Approval must be an exact member of a verified append-only ledger. The wrapper
introduces S3-local failure codes (``S3-FLOW-001`` ... ``S3-FLOW-005``) that
are distinct from the S2 finding registry, are never persisted, and are never
added to it. The wrapper performs no persistence, network access or
external-model call; it only composes existing in-process S2 builders and
verifiers, and delegates composition to the S2 request builder (which the
Composer owns).

Only after all S3 verification succeeds does the wrapper delegate to S2's
existing exact-v2 request builder / Composer path. No Manifest, Instance,
Artifact, Deployment, D&O, OCI or GHCR work happens here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from factory_control_plane import requirements_intake
from factory_control_plane.approvals import (
    ApprovalLedger,
    approval_ineffectiveness_reasons,
    new_approval_record,
)
from factory_control_plane.composition_requests import (
    CompositionRequestError,
    new_request_record,
    verify_request_record,
)
from factory_control_plane.configuration_v2 import is_v2, structural_errors
from factory_control_plane.digests import verify_digest
from factory_control_plane.documents import SCHEMA_REQUIREMENTS
from factory_control_plane.proposals import (
    SCHEMA_PROPOSAL,
    new_proposal_version,
    verify_proposal_version,
)
from factory_control_plane.validation import SCHEMA_APPROVAL, ControlPlaneError

#: S3-local deterministic precondition refusal codes. These are NOT Proposal
#: findings and are never added to the S2 finding registry.
S3_FLOW_CODES = frozenset(
    {
        "S3-FLOW-001",  # malformed/ambiguous/contradictory/unsupported/unrepresentable intake
        "S3-FLOW-002",  # invalid Requirements/Configuration/Proposal/Approval doc, schema id or digest
        "S3-FLOW-003",  # project or exact id+digest cross-reference mismatch
        "S3-FLOW-004",  # Proposal re-derivation / canonical rebuild differs
        "S3-FLOW-005",  # Approval absent/non-grant/stale/changed/revoked/replayed; downstream refused
    }
)


class S3FlowError(Exception):
    """An S3 deterministic precondition refusal (fail closed)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _code_and_message(text: str) -> tuple[str, str]:
    code, separator, message = text.partition(":")
    if not separator:
        return "", text
    return code.strip(), message.strip()


def verify_requirements_document(requirements: Any) -> list[str]:
    """Return S3-FLOW-002 reasons the Requirements document is invalid."""
    errors: list[str] = []
    if not isinstance(requirements, Mapping):
        return ["S3-FLOW-002: requirements version must be a JSON object"]
    if requirements.get("schema_version") != SCHEMA_REQUIREMENTS:
        errors.append(
            f"S3-FLOW-002: requirements schema_version must be {SCHEMA_REQUIREMENTS!r}"
        )
    if not verify_digest(requirements):
        errors.append(
            "S3-FLOW-002: requirements version digest does not match its payload"
        )
    return errors


def verify_configuration_document(configuration: Any) -> list[str]:
    """Return S3-FLOW-002 reasons the Configuration document is invalid."""
    errors: list[str] = []
    if not isinstance(configuration, Mapping):
        return ["S3-FLOW-002: configuration version must be a JSON object"]
    if not is_v2(configuration):
        errors.append(
            "S3-FLOW-002: only control-plane/configuration/v2 is projectable; "
            "a v1 version is valid but not projectable and is never converted"
        )
    errors.extend(
        f"S3-FLOW-002: {problem}" for problem in structural_errors(configuration)
    )
    if not verify_digest(configuration):
        errors.append(
            "S3-FLOW-002: configuration version digest does not match its payload"
        )
    return errors


def verify_requirements_configuration_link(
    configuration: Any, requirements: Any, *, project_ref: str
) -> list[str]:
    """Return S3-FLOW-003 reasons the exact Requirements<->Configuration link fails."""
    errors: list[str] = []
    if not isinstance(configuration, Mapping) or not isinstance(requirements, Mapping):
        return ["S3-FLOW-003: configuration and requirements must be JSON objects"]
    req_project = requirements.get("project_ref")
    cfg_project = configuration.get("project_ref")
    if req_project != project_ref or cfg_project != project_ref:
        errors.append(
            "S3-FLOW-003: project_ref mismatch between requirements, "
            "configuration and the supplied project"
        )
    expected_ref = {
        "requirements_id": requirements.get("requirements_id"),
        "digest": requirements.get("digest"),
    }
    if configuration.get("requirements_ref") != expected_ref:
        errors.append(
            "S3-FLOW-003: configuration.requirements_ref does not equal the "
            "exact supplied RequirementsVersion (id/digest mismatch)"
        )
    return errors


def verify_proposal_document(
    proposal: Any,
    *,
    configuration: Any,
    requirements: Any,
    project: Any,
    predecessor: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> list[str]:
    """Return S3-FLOW-002/003/004 reasons a Proposal fails strict verification."""
    errors: list[str] = []
    if not isinstance(proposal, Mapping):
        return ["S3-FLOW-002: proposal version must be a JSON object"]
    if proposal.get("schema_version") != SCHEMA_PROPOSAL:
        errors.append(
            f"S3-FLOW-002: proposal schema_version must be {SCHEMA_PROPOSAL!r}"
        )
    if not verify_digest(proposal):
        errors.append("S3-FLOW-002: proposal version digest does not match its payload")

    project_id = project.get("project_id") if isinstance(project, Mapping) else None
    if project_id is not None:
        for label, document in (
            ("requirements", requirements),
            ("configuration", configuration),
            ("proposal", proposal),
        ):
            if (
                isinstance(document, Mapping)
                and document.get("project_ref") != project_id
            ):
                errors.append(
                    f"S3-FLOW-003: {label} project_ref does not match the supplied project"
                )

    if (
        isinstance(requirements, Mapping)
        and isinstance(configuration, Mapping)
        and isinstance(proposal, Mapping)
    ):
        expected_requirements_ref = {
            "requirements_id": requirements.get("requirements_id"),
            "digest": requirements.get("digest"),
        }
        expected_configuration_ref = {
            "configuration_id": configuration.get("configuration_id"),
            "digest": configuration.get("digest"),
        }
        if proposal.get("requirements_ref") != expected_requirements_ref:
            errors.append(
                "S3-FLOW-003: proposal requirements_ref does not match the exact "
                "supplied RequirementsVersion"
            )
        if proposal.get("configuration_ref") != expected_configuration_ref:
            errors.append(
                "S3-FLOW-003: proposal configuration_ref does not match the exact "
                "supplied ConfigurationVersion"
            )

    # Re-derive / canonical rebuild only once the references are internally sane.
    if not errors:
        derivation = verify_proposal_version(
            proposal,
            configuration=configuration,
            requirements=requirements,
            project=project,
            predecessor=predecessor,
            root=root,
        )
        if derivation:
            errors.append(
                "S3-FLOW-004: proposal is not reproduced by its exact inputs: "
                + "; ".join(derivation)
            )
    return errors


def verify_approval_use(
    approval: Any,
    *,
    configuration: Any,
    requirements: Any,
    project: Any,
    proposal: Any,
    proposal_predecessor: Mapping[str, Any] | None = None,
    ledger: ApprovalLedger | None = None,
    root: Path | None = None,
) -> list[str]:
    """Return S3-FLOW-002/005 reasons an Approval cannot be used downstream."""
    errors: list[str] = []
    if not isinstance(approval, Mapping):
        return ["S3-FLOW-002: approval record must be a JSON object"]
    if approval.get("schema_version") != SCHEMA_APPROVAL:
        errors.append(
            f"S3-FLOW-002: approval schema_version must be {SCHEMA_APPROVAL!r}"
        )
    if not verify_digest(approval):
        errors.append("S3-FLOW-002: approval record digest does not match its payload")

    reasons = approval_ineffectiveness_reasons(
        approval,
        configuration=configuration,
        requirements=requirements,
        project=project,
        proposal=proposal,
        proposal_predecessor=proposal_predecessor,
        ledger=ledger,
        root=root,
    )
    if reasons:
        errors.append(
            "S3-FLOW-005: approval is not effective for the exact referenced "
            "documents: " + "; ".join(reasons)
        )
    return errors


def verify_composition_request(
    record: Any,
    *,
    configuration: Any,
    requirements: Any,
    project: Any,
    proposal: Any,
    approval: Any,
    ledger: ApprovalLedger,
    proposal_predecessor: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> list[str]:
    """Re-derive the full authorization chain for an existing record (fail closed)."""
    errors: list[str] = []
    reasons = verify_request_record(
        record,
        configuration=configuration,
        requirements=requirements,
        project=project,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        proposal_predecessor=proposal_predecessor,
        root=root,
    )
    if reasons:
        errors.append(
            "S3-FLOW-005: composition request authorization chain failed: "
            + "; ".join(reasons)
        )
    return errors


def build_composition_request(
    *,
    project_ref: str,
    channel: str,
    candidates: Any,
    project: Mapping[str, Any],
    configuration: Mapping[str, Any],
    decision: str = "granted",
    approver: Mapping[str, Any],
    decided_at: str,
    reason: str,
    acknowledged_findings: list[str] = (),
    requirements_predecessor: Mapping[str, Any] | None = None,
    proposal: Mapping[str, Any] | None = None,
    proposal_predecessor: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
    ledger: ApprovalLedger | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run the full S3 flow and return a verified Composition Request record.

    Intake builds the RequirementsVersion; the exact Requirements <-> Configuration
    <-> Project link is verified; the Proposal is derived from the exact inputs
    (or strictly verified when supplied); the Approval is created from the
    verified Proposal (or verified against the supplied ledger); and only then is
    the S2 Composition Request builder delegated to. Any failure raises
    ``S3FlowError`` with the matching S3-FLOW code. No persistence, network
    access or external model is involved. The function is pure: the supplied
    documents are never mutated.
    """
    # 1. Intake -> RequirementsVersion.
    try:
        requirements = requirements_intake.intake_requirements_version(
            project_ref=project_ref,
            channel=channel,
            candidates=candidates,
            predecessor=requirements_predecessor,
        )
    except requirements_intake.RequirementsIntakeError as error:
        raise S3FlowError(error.code, error.message) from error

    # 2. Configuration document + exact Requirements<->Configuration link.
    document_errors = verify_configuration_document(configuration)
    document_errors.extend(
        verify_requirements_configuration_link(
            configuration, requirements, project_ref=project_ref
        )
    )
    if document_errors:
        code, message = _code_and_message(document_errors[0])
        raise S3FlowError(code or "S3-FLOW-002", message)

    # 3. Proposal: derive fresh, or strictly verify a supplied one.
    if proposal is None:
        try:
            proposal = new_proposal_version(
                project_ref=project_ref,
                configuration=configuration,
                requirements=requirements,
                project=project,
                predecessor=proposal_predecessor,
                root=root,
            )
        except ControlPlaneError as error:
            raise S3FlowError(
                "S3-FLOW-004", f"proposal could not be derived: {error}"
            ) from error
    else:
        proposal_errors = verify_proposal_document(
            proposal,
            configuration=configuration,
            requirements=requirements,
            project=project,
            predecessor=proposal_predecessor,
            root=root,
        )
        if proposal_errors:
            code, message = _code_and_message(proposal_errors[0])
            raise S3FlowError(code or "S3-FLOW-004", message)

    # 4. Approval: verify a supplied one against its ledger, or create one.
    if approval is not None and ledger is not None:
        approval_errors = verify_approval_use(
            approval,
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=proposal,
            proposal_predecessor=proposal_predecessor,
            ledger=ledger,
            root=root,
        )
        if approval_errors:
            code, message = _code_and_message(approval_errors[0])
            raise S3FlowError(code or "S3-FLOW-005", message)
    else:
        if approval is not None and ledger is None:
            raise S3FlowError(
                "S3-FLOW-005", "a supplied approval requires its verified ledger"
            )
        try:
            approval = new_approval_record(
                project_ref=project_ref,
                configuration=configuration,
                requirements=requirements,
                project=project,
                proposal=proposal,
                decision=decision,
                approver=dict(approver),
                decided_at=decided_at,
                reason=reason,
                acknowledged_findings=list(acknowledged_findings),
                predecessor=None,
                proposal_predecessor=proposal_predecessor,
                root=root,
            )
        except ControlPlaneError as error:
            raise S3FlowError(
                "S3-FLOW-005", f"approval could not be created: {error}"
            ) from error
        ledger = ApprovalLedger.empty(project_ref).append(approval)

    # 5. Delegate to the S2 Composition Request builder only after verification.
    try:
        record = new_request_record(
            project_ref=project_ref,
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=proposal,
            approval=approval,
            ledger=ledger,
            predecessor=None,
            proposal_predecessor=proposal_predecessor,
            root=root,
        )
    except (ControlPlaneError, CompositionRequestError) as error:
        raise S3FlowError(
            "S3-FLOW-005", f"composition request not authorized: {error}"
        ) from error
    return record
