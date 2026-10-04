"""Immutable ProposalVersion — the validation report for one exact configuration.

An acknowledgement has to be auditable: someone must be able to see *which*
findings an approval accepted and *why* the configuration was accepted at all.
This module builds that immutable report. It binds one exact
ConfigurationVersion (by digest) and one RequirementsVersion (by digest) to the
deterministic findings that justify accepting, acknowledging or rejecting it,
and derives the proposal state from those findings (``blocked`` exactly when a
blocking ``error`` is present).

Scope note. This slice implements findings, state and references. Proposal
alternatives and conflict objects (Slice 0 §3.6, decision package §6) are **not**
implemented in S2-1/S2-2/S2-3 and remain a separate authorization; nothing here
pretends to offer them.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from factory_control_plane.configuration_v2 import (
    composer_errors,
    is_v2,
    not_projectable_errors,
    structural_errors,
)
from factory_control_plane.digests import verify_digest
from factory_control_plane.findings import (
    collect_findings,
    has_blocking_findings,
    registry_fingerprint,
    warning_ids,
)
from factory_control_plane.support import settle, successor_sequence
from factory_control_plane.validation import (
    SCHEMA_PROPOSAL,
    ControlPlaneError,
    proposal_version_errors,
)

#: Identifier of the rule set that produced a proposal. Changing any rule must
#: bump this identifier: it participates in the proposal payload (and therefore
#: in the digest), so an approval can never silently inherit "the same
#: findings" under different rules.
VALIDATOR_SET = "control-plane/validators/v1"


def new_proposal_version(
    *,
    project_ref: str,
    configuration: Mapping[str, Any],
    requirements: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
    predecessor: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Create the next immutable ProposalVersion for ``configuration``.

    The configuration must be structurally valid for its declared generation
    (a malformed document has no report to give — its validator speaks). A
    structurally valid **v1** configuration is accepted and reported as
    ``blocked`` with the fail-closed projection refusal, which is exactly the
    S2-1 guarantee: v1 stays valid, and composition is refused deterministically.
    """
    problems = structural_errors(configuration)
    if problems:
        joined = "; ".join(problems)
        message = f"cannot analyse an invalid configuration version: {joined}"
        raise ControlPlaneError(message)
    if configuration.get("project_ref") != project_ref:
        raise ControlPlaneError(
            f"configuration version belongs to project "
            f"{configuration.get('project_ref')!r}, not {project_ref!r}"
        )

    refusal: tuple[str, ...] = tuple(not_projectable_errors(configuration))
    delegated: tuple[str, ...] = ()
    if not refusal and is_v2(configuration):
        delegated = tuple(composer_errors(configuration, root=root))

    findings = collect_findings(
        configuration,
        requirements=requirements,
        project=project,
        composer_errors=delegated,
        not_projectable=refusal,
    )
    previous_id = None
    previous_digest = None
    if predecessor is not None:
        previous_id = predecessor["proposal_id"]
        previous_digest = predecessor["digest"]
    reference = configuration["requirements_ref"]
    document: dict[str, Any] = {
        "schema_version": SCHEMA_PROPOSAL,
        "proposal_id": f"{project_ref}/proposals/{successor_sequence(previous_id)}",
        "project_ref": project_ref,
        "requirements_ref": {
            "requirements_id": reference["requirements_id"],
            "digest": reference["digest"],
        },
        "configuration_ref": {
            "configuration_id": configuration["configuration_id"],
            "digest": configuration["digest"],
        },
        "inputs": {
            "validator_set": VALIDATOR_SET,
            "registry_fingerprint": registry_fingerprint(),
        },
        "findings": findings,
        "state": "blocked" if has_blocking_findings(findings) else "ready",
        "predecessor": (
            None
            if previous_id is None
            else {"proposal_id": previous_id, "digest": previous_digest}
        ),
    }
    return settle(document, proposal_version_errors, subject="proposal version")


def verify_proposal_version(
    document: Mapping[str, Any],
    *,
    configuration: Mapping[str, Any] | None = None,
    requirements: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> list[str]:
    """Re-derive a proposal from its inputs and report every disagreement.

    Checking more than the digest is the point: a tampered *finding* would break
    the digest, but a proposal that lied about its inputs consistently would
    still have to reproduce the same findings and state here.
    """
    if not isinstance(document, Mapping):
        return ["proposal version must be a JSON object"]
    errors: list[str] = []
    if not verify_digest(document):
        errors.append("proposal version digest does not match its payload")
    if configuration is None:
        return sorted(errors)
    expected_ref = {
        "configuration_id": configuration.get("configuration_id"),
        "digest": configuration.get("digest"),
    }
    if document.get("configuration_ref") != expected_ref:
        errors.append(
            "proposal version configuration_ref does not reference the "
            "analysed configuration version"
        )
    refusal: tuple[str, ...] = tuple(not_projectable_errors(configuration))
    delegated: tuple[str, ...] = ()
    if not refusal and is_v2(configuration):
        delegated = tuple(composer_errors(configuration, root=root))
    expected_findings = collect_findings(
        configuration,
        requirements=requirements,
        project=project,
        composer_errors=delegated,
        not_projectable=refusal,
    )
    if list(document.get("findings") or []) != expected_findings:
        errors.append("proposal version findings are not the ones its inputs justify")
    expected_state = "blocked" if has_blocking_findings(expected_findings) else "ready"
    if document.get("state") != expected_state:
        errors.append(
            f"proposal version state must be {expected_state!r} for its inputs"
        )
    return sorted(errors)


def proposal_warning_ids(document: Mapping[str, Any]) -> list[str]:
    """Return the sorted finding ids of every warning in the proposal."""
    return warning_ids(document.get("findings") or [])


def proposal_state(document: Mapping[str, Any]) -> str:
    """Return the declared proposal state (``ready`` or ``blocked``)."""
    return str(document.get("state"))


def proposal_is_ready(document: Mapping[str, Any]) -> bool:
    """True when the proposal carries no blocking finding."""
    return proposal_state(document) == "ready"
