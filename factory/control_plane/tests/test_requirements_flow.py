"""Adversarial tests for the Slice 3 flow wrapper (requirements_flow)."""

from __future__ import annotations

import copy

import pytest
from factory_control_plane import requirements_flow as rf
from factory_control_plane.approvals import (
    ApprovalLedger,
    new_approval_record,
)
from factory_control_plane.digests import with_digest
from factory_control_plane.findings import warning_ids
from factory_control_plane.proposals import new_proposal_version
from factory_control_plane.requirements_flow import S3_FLOW_CODES, S3FlowError
from s3_fixtures import PROJECT_REF, build_context, warning_ids_for
from slice2_fixtures import load

FULL_CHAIN = ("S3-FLOW-001", "S3-FLOW-002", "S3-FLOW-003", "S3-FLOW-004", "S3-FLOW-005")


def test_s3_flow_codes_are_defined_and_distinct():
    assert S3_FLOW_CODES == frozenset(FULL_CHAIN)


def _granted_approval(context, proposal):
    return new_approval_record(
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


def test_happy_path_build_returns_composition_request():
    context = build_context()
    record = rf.build_composition_request(
        project_ref=context["project_ref"],
        channel=context["channel"],
        candidates=context["candidates"],
        project=context["project"],
        configuration=context["configuration"],
        approver=context["approver"],
        decided_at=context["decided_at"],
        reason=context["reason"],
        acknowledged_findings=warning_ids_for(context),
        root=context["root"],
    )
    assert record["schema_version"] == "control-plane/composition-request/v1"
    assert record["request_id"].startswith(f"{PROJECT_REF}/requests/")
    # Delegation only happens after verification: the nested payload is the
    # exact Composer projection of the configuration.
    assert "$schema" in record["request"]
    assert (
        record["configuration_ref"]["configuration_id"]
        == context["configuration"]["configuration_id"]
    )


def test_build_refuses_unacknowledged_warnings():
    context = build_context()
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
            acknowledged_findings=[],  # wrong ack set
            root=context["root"],
        )
    # S2 acknowledgement semantics are preserved (CP-APR-001) and surfaced as 005.
    assert error.value.code == "S3-FLOW-005"


def test_fabricated_proposal_refused():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    fabricated = copy.deepcopy(proposal)
    fabricated["findings"] = []
    fabricated["state"] = "ready"
    fabricated = with_digest(fabricated)

    reasons = rf.verify_proposal_document(
        fabricated,
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    assert any(code == "S3-FLOW-004" for code, _ in (r.split(":", 1) for r in reasons))

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
            acknowledged_findings=warning_ids_for(context),
            proposal=fabricated,
            root=context["root"],
        )
    assert error.value.code == "S3-FLOW-004"


def test_approval_bypass_refused():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    approval = _granted_approval(context, proposal)

    # Forge an approval whose configuration_ref no longer matches the config.
    forged = copy.deepcopy(approval)
    forged["configuration_ref"] = {
        "configuration_id": context["configuration"]["configuration_id"],
        "digest": "sha256:" + "0" * 64,
    }
    forged = with_digest(forged)
    forged_ledger = ApprovalLedger.empty(context["project_ref"]).append(forged)

    reasons = rf.verify_approval_use(
        forged,
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        proposal=proposal,
        ledger=forged_ledger,
        root=context["root"],
    )
    assert any(code == "S3-FLOW-005" for code, _ in (r.split(":", 1) for r in reasons))

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
            acknowledged_findings=warning_ids(proposal["findings"]),
            proposal=proposal,
            approval=forged,
            ledger=forged_ledger,
            root=context["root"],
        )
    assert error.value.code == "S3-FLOW-005"


def test_supplied_approval_requires_its_ledger():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    approval = _granted_approval(context, proposal)
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
            acknowledged_findings=warning_ids(proposal["findings"]),
            proposal=proposal,
            approval=approval,  # no ledger -> incomplete proof
            root=context["root"],
        )
    assert error.value.code == "S3-FLOW-005"


def test_v1_configuration_not_projectable():
    context = build_context()
    v1 = load("v1_version")
    reasons = rf.verify_configuration_document(v1)
    assert any(code == "S3-FLOW-002" for code, _ in (r.split(":", 1) for r in reasons))

    with pytest.raises(S3FlowError) as error:
        rf.build_composition_request(
            project_ref=context["project_ref"],
            channel=context["channel"],
            candidates=context["candidates"],
            project=context["project"],
            configuration=v1,
            approver=context["approver"],
            decided_at=context["decided_at"],
            reason=context["reason"],
            acknowledged_findings=warning_ids_for(context),
            root=context["root"],
        )
    assert error.value.code == "S3-FLOW-002"


def test_requirements_configuration_link_mismatch():
    # Point the configuration at a tampered Requirements reference.
    tampered = {
        "requirements_id": "demo_shop/requirements/1",
        "digest": "sha256:" + "f" * 64,
    }
    context = build_context(configuration_overrides={"requirements_ref": tampered})
    reasons = rf.verify_requirements_configuration_link(
        context["configuration"], context["requirements"], project_ref=PROJECT_REF
    )
    assert any(code == "S3-FLOW-003" for code, _ in (r.split(":", 1) for r in reasons))

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
            acknowledged_findings=warning_ids_for(context),
            root=context["root"],
        )
    assert error.value.code == "S3-FLOW-003"


def test_incomplete_composition_request_verification():
    context = build_context()
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    approval = _granted_approval(context, proposal)
    ledger = ApprovalLedger.empty(context["project_ref"]).append(approval)
    record = rf.build_composition_request(
        project_ref=context["project_ref"],
        channel=context["channel"],
        candidates=context["candidates"],
        project=context["project"],
        configuration=context["configuration"],
        approver=context["approver"],
        decided_at=context["decided_at"],
        reason=context["reason"],
        acknowledged_findings=warning_ids(proposal["findings"]),
        root=context["root"],
    )
    # Re-verify the existing record with an incomplete chain -> 005.
    reasons = rf.verify_composition_request(
        record,
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        proposal=proposal,
        approval=None,  # missing input
        ledger=ledger,
        root=context["root"],
    )
    assert any(code == "S3-FLOW-005" for code, _ in (r.split(":", 1) for r in reasons))


def test_flow_is_pure_and_does_not_mutate_inputs():
    context = build_context()
    snapshot = copy.deepcopy(context["configuration"])
    rf.build_composition_request(
        project_ref=context["project_ref"],
        channel=context["channel"],
        candidates=context["candidates"],
        project=context["project"],
        configuration=context["configuration"],
        approver=context["approver"],
        decided_at=context["decided_at"],
        reason=context["reason"],
        acknowledged_findings=warning_ids_for(context),
        root=context["root"],
    )
    assert context["configuration"] == snapshot
