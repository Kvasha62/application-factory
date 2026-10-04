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
from factory_control_plane.documents import SCHEMA_PROJECT, SCHEMA_REQUIREMENTS
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
    project_errors,
    proposal_version_errors,
    requirements_errors,
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
    configuration: Mapping[str, Any],
    requirements: Mapping[str, Any],
    project: Mapping[str, Any],
    predecessor: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> list[str]:
    """Strictly re-derive a Proposal from its exact typed inputs.

    A valid digest is necessary but not sufficient: all four document shapes,
    digests and typed references are checked, then the complete Proposal is
    rebuilt and compared.  A non-null predecessor in ``document`` must be
    supplied separately so a self-asserted predecessor cannot satisfy the
    chain check.  This function is the proof boundary used before Approval and
    Composition Request authorization; incomplete inputs are never a successful
    partial verification.
    """
    if not isinstance(document, Mapping):
        return ["proposal version must be a JSON object"]
    if not isinstance(configuration, Mapping):
        return ["configuration version must be a JSON object"]
    if not isinstance(requirements, Mapping):
        return ["requirements version must be a JSON object"]
    if not isinstance(project, Mapping):
        return ["project must be a JSON object"]

    proposal_doc = dict(document)
    configuration_doc = dict(configuration)
    requirements_doc = dict(requirements)
    project_doc = dict(project)
    errors: list[str] = []

    errors.extend(proposal_version_errors(proposal_doc))
    if not verify_digest(proposal_doc):
        errors.append("proposal version digest does not match its payload")

    errors.extend(requirements_errors(requirements_doc))
    if requirements_doc.get("schema_version") != SCHEMA_REQUIREMENTS:
        errors.append(
            f"requirements version schema_version must be {SCHEMA_REQUIREMENTS!r}"
        )
    if not verify_digest(requirements_doc):
        errors.append("requirements version digest does not match its payload")

    errors.extend(structural_errors(configuration_doc))
    if not verify_digest(configuration_doc):
        errors.append("configuration version digest does not match its payload")

    errors.extend(project_errors(project_doc))
    if project_doc.get("schema_version") != SCHEMA_PROJECT:
        errors.append(f"project schema_version must be {SCHEMA_PROJECT!r}")

    project_id = project_doc.get("project_id")
    if project_id != configuration_doc.get("project_ref"):
        errors.append("configuration project_ref does not match the supplied project")
    if project_id != requirements_doc.get("project_ref"):
        errors.append("requirements project_ref does not match the supplied project")
    if project_id != proposal_doc.get("project_ref"):
        errors.append("proposal project_ref does not match the supplied project")

    requirements_ref = {
        "requirements_id": requirements_doc.get("requirements_id"),
        "digest": requirements_doc.get("digest"),
    }
    configuration_requirements_ref = configuration_doc.get("requirements_ref")
    if configuration_requirements_ref != requirements_ref:
        errors.append(
            "configuration requirements_ref does not reference the exact "
            "supplied RequirementsVersion (id/digest mismatch)"
        )
    if proposal_doc.get("requirements_ref") != requirements_ref:
        errors.append(
            "proposal requirements_ref does not reference the exact "
            "supplied RequirementsVersion (id/digest mismatch)"
        )

    configuration_ref = {
        "configuration_id": configuration_doc.get("configuration_id"),
        "digest": configuration_doc.get("digest"),
    }
    if proposal_doc.get("configuration_ref") != configuration_ref:
        errors.append(
            "proposal configuration_ref does not reference the exact "
            "supplied ConfigurationVersion (id/digest mismatch)"
        )

    expected_predecessor_ref = None
    predecessor_doc: dict[str, Any] | None = None
    if predecessor is not None:
        if not isinstance(predecessor, Mapping):
            errors.append("proposal predecessor must be a JSON object")
        else:
            predecessor_doc = dict(predecessor)
            errors.extend(
                f"proposal predecessor: {problem}"
                for problem in proposal_version_errors(predecessor_doc)
            )
            if not verify_digest(predecessor_doc):
                errors.append("proposal predecessor digest does not match its payload")
            if predecessor_doc.get("project_ref") != project_id:
                errors.append("proposal predecessor belongs to a different project")
            if isinstance(predecessor_doc.get("proposal_id"), str) and isinstance(
                predecessor_doc.get("digest"), str
            ):
                expected_predecessor_ref = {
                    "proposal_id": predecessor_doc["proposal_id"],
                    "digest": predecessor_doc["digest"],
                }
    if proposal_doc.get("predecessor") != expected_predecessor_ref:
        errors.append(
            "proposal predecessor link does not match the separately supplied "
            "expected predecessor"
        )

    # Do not invoke the builder on invalid inputs; its report-generation path is
    # intentionally useful for blocked candidates, whereas authorization must
    # have well-formed, digest-valid documents and exact cross-references.
    if errors:
        return sorted(set(errors))

    try:
        expected = new_proposal_version(
            project_ref=str(project_id),
            configuration=configuration_doc,
            requirements=requirements_doc,
            project=project_doc,
            predecessor=predecessor_doc,
            root=root,
        )
    except (ControlPlaneError, KeyError, TypeError, ValueError) as error:
        return [f"proposal could not be re-derived from its exact inputs: {error}"]

    if proposal_doc != expected:
        fields = (
            "proposal_id",
            "project_ref",
            "requirements_ref",
            "configuration_ref",
            "inputs",
            "findings",
            "state",
            "predecessor",
            "digest",
        )
        for field in fields:
            if proposal_doc.get(field) != expected.get(field):
                errors.append(
                    f"proposal version {field} is not reproduced by its inputs"
                )
        if set(proposal_doc) != set(expected):
            errors.append("proposal version has a different canonical field set")
    return sorted(set(errors))


def proposal_warning_ids(document: Mapping[str, Any]) -> list[str]:
    """Return the sorted finding ids of every warning in the proposal."""
    return warning_ids(document.get("findings") or [])


def proposal_state(document: Mapping[str, Any]) -> str:
    """Return the declared proposal state (``ready`` or ``blocked``)."""
    return str(document.get("state"))


def proposal_is_ready(document: Mapping[str, Any]) -> bool:
    """True when the proposal carries no blocking finding."""
    return proposal_state(document) == "ready"
