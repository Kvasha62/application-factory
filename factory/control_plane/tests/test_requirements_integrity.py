"""Integrity tests for Slice 3: S3-FLOW codes, cross-reference exactness, ledger
state, S2 semantics preservation, and absence of forbidden side effects."""

from __future__ import annotations

from pathlib import Path

import pytest
from factory_control_plane import requirements_flow as rf
from factory_control_plane import requirements_intake as ri
from factory_control_plane.approvals import (
    ApprovalLedger,
    new_approval_record,
    new_approval_revocation,
)
from factory_control_plane.digests import with_digest
from factory_control_plane.findings import CODE_SEVERITIES, warning_ids
from factory_control_plane.proposals import new_proposal_version
from factory_control_plane.requirements_flow import S3_FLOW_CODES, S3FlowError
from s3_fixtures import PROJECT_REF, build_context

_SUBTREE = Path(__file__).resolve().parent.parent
_MODULES = [
    _SUBTREE / "factory_control_plane" / "requirements_intake.py",
    _SUBTREE / "factory_control_plane" / "requirements_flow.py",
]

_FORBIDDEN_TOKENS = (
    "from src",
    "import src",
    "import composer",
    "from composer",
    "requests.",
    "httpx",
    "urllib",
    "socket",
    "subprocess",
    "boto3",
    "aiohttp",
    "openai",
    "oci",
    "ghcr",
    "open(",
    "os.write",
)


def test_s3_flow_codes_are_distinct_from_s2_finding_registry():
    # The S3 codes are a separate, local set and must never leak into S2.
    assert S3_FLOW_CODES == frozenset(
        {"S3-FLOW-001", "S3-FLOW-002", "S3-FLOW-003", "S3-FLOW-004", "S3-FLOW-005"}
    )
    assert S3_FLOW_CODES.isdisjoint(CODE_SEVERITIES)


def test_exact_requirements_reference_mismatch_is_s3_flow_003():
    context = build_context()
    tampered = dict(context["requirements"])
    tampered["digest"] = "sha256:" + "a" * 64
    reasons = rf.verify_requirements_configuration_link(
        context["configuration"], tampered, project_ref=PROJECT_REF
    )
    assert any(code == "S3-FLOW-003" for code, _ in (r.split(":", 1) for r in reasons))


def test_exact_proposal_reference_mismatch_is_s3_flow_003():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    tampered = copy_proposal_with(
        context, proposal, requirements_digest="sha256:" + "b" * 64
    )
    reasons = rf.verify_proposal_document(
        tampered,
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    assert any(code == "S3-FLOW-003" for code, _ in (r.split(":", 1) for r in reasons))


def copy_proposal_with(
    context, proposal, *, requirements_digest=None, configuration_digest=None
):
    clone = dict(proposal)
    ref = dict(proposal["requirements_ref"])
    if requirements_digest is not None:
        ref["digest"] = requirements_digest
    clone["requirements_ref"] = ref
    if configuration_digest is not None:
        cfg = dict(proposal["configuration_ref"])
        cfg["digest"] = configuration_digest
        clone["configuration_ref"] = cfg
    return with_digest(clone)


def test_reused_record_for_a_different_pair_is_refused():
    # A previously granted approval cannot authorize a different configuration.
    context_a = build_context()
    proposal_a = new_proposal_version(
        project_ref=context_a["project_ref"],
        configuration=context_a["configuration"],
        requirements=context_a["requirements"],
        project=context_a["project"],
        root=context_a["root"],
    )
    approval_a = new_approval_record(
        project_ref=context_a["project_ref"],
        configuration=context_a["configuration"],
        requirements=context_a["requirements"],
        project=context_a["project"],
        proposal=proposal_a,
        decision="granted",
        approver=context_a["approver"],
        decided_at=context_a["decided_at"],
        reason=context_a["reason"],
        acknowledged_findings=warning_ids(proposal_a["findings"]),
        root=context_a["root"],
    )
    ledger_a = ApprovalLedger.empty(context_a["project_ref"]).append(approval_a)

    context_b = build_context(channel="api")
    proposal_b = new_proposal_version(
        project_ref=context_b["project_ref"],
        configuration=context_b["configuration"],
        requirements=context_b["requirements"],
        project=context_b["project"],
        root=context_b["root"],
    )
    # Replaying approval_a against context_b's configuration/proposal fails.
    reasons = rf.verify_approval_use(
        approval_a,
        configuration=context_b["configuration"],
        requirements=context_b["requirements"],
        project=context_b["project"],
        proposal=proposal_b,
        ledger=ledger_a,
        root=context_b["root"],
    )
    assert any(code == "S3-FLOW-005" for code, _ in (r.split(":", 1) for r in reasons))


def test_revoked_approval_is_ineffective():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    approval = new_approval_record(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        proposal=proposal,
        decision="granted",
        approver=context["approver"],
        decided_at=context["decided_at"],
        reason=context["reason"],
        acknowledged_findings=warning_ids(proposal["findings"]),
        root=context["root"],
    )
    ledger = ApprovalLedger.empty(context["project_ref"]).append(approval)
    revocation = new_approval_revocation(
        project_ref=context["project_ref"],
        revoked=approval,
        approver=context["approver"],
        decided_at=context["decided_at"],
        reason="revoked",
        predecessor=approval,
    )
    extended = ledger.append(revocation)
    reasons = rf.verify_approval_use(
        approval,
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        proposal=proposal,
        ledger=extended,
        root=context["root"],
    )
    assert any(code == "S3-FLOW-005" for code, _ in (r.split(":", 1) for r in reasons))


def test_missing_ledger_member_is_refused():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    approval = new_approval_record(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        proposal=proposal,
        decision="granted",
        approver=context["approver"],
        decided_at=context["decided_at"],
        reason=context["reason"],
        acknowledged_findings=warning_ids(proposal["findings"]),
        root=context["root"],
    )
    # An empty ledger: the approval is not a member -> 005.
    empty_ledger = ApprovalLedger.empty(context["project_ref"])
    reasons = rf.verify_approval_use(
        approval,
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        proposal=proposal,
        ledger=empty_ledger,
        root=context["root"],
    )
    assert any(code == "S3-FLOW-005" for code, _ in (r.split(":", 1) for r in reasons))


def test_s2_warning_semantics_preserved_in_derived_proposal():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    # The converged requirement stays open, so the proposal warns (not errors).
    # S3 surfaces the warning set and requires it to be acknowledged.
    assert warning_ids(proposal["findings"])
    assert proposal["state"] == "ready"
    # A grant that omits the warning acknowledgement is refused (S2 semantics).
    with pytest.raises(S3FlowError) as error:
        rf.build_composition_request(
            project_ref=context["project_ref"],
            channel=context["channel"],
            candidates=context["candidates"],
            project=context["project"],
            configuration=context["configuration"],
            approver=context["approver"],
            decided_at=context["decided_at"],
            reason=context["reason"],
            acknowledged_findings=[],
            root=context["root"],
        )
    assert error.value.code == "S3-FLOW-005"


def test_s3_modules_have_no_forbidden_dependencies_or_side_effects():
    for module_path in _MODULES:
        source = module_path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_TOKENS:
            assert token not in source, f"{module_path.name} must not contain {token!r}"
        # No reverse src/ application dependency: only S2 + stdlib.
        assert "from src." not in source


def test_intake_and_flow_are_importable_without_src_services():
    # The wrapper composes only existing in-process S2 builders and verifiers.
    assert hasattr(ri, "intake_requirements_version")
    assert hasattr(rf, "build_composition_request")
    assert hasattr(rf, "verify_proposal_document")
    assert hasattr(rf, "verify_approval_use")
