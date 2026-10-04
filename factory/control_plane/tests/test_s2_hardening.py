"""Adversarial S2-hardening proofs for the Proposal/Approval trust boundary."""

from __future__ import annotations

import copy

import pytest
from factory_control_plane import proposals
from factory_control_plane.approvals import (
    ApprovalError,
    ApprovalLedger,
    approval_ineffectiveness_reasons,
    new_approval_record,
    new_approval_revocation,
    plain,
    verify_ledger,
)
from factory_control_plane.composition_requests import (
    CompositionRequestError,
    new_request_record,
    verify_request_record,
)
from factory_control_plane.digests import with_digest
from factory_control_plane.findings import (
    CODE_ACKNOWLEDGEMENT_MISMATCH,
    CODE_APPROVAL_NOT_EFFECTIVE,
    warning_ids,
)
from slice2_fixtures import (
    APPROVER,
    DECIDED_AT,
    REASON,
    REPOSITORY_ROOT,
    build_proposal,
    build_v2,
    load,
)


def _chain():
    requirements = load("requirements")
    project = load("project")
    configuration = build_v2()
    proposal = build_proposal(
        configuration=configuration,
        requirements=requirements,
        project=project,
    )
    approval = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        requirements=requirements,
        project=project,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=warning_ids(proposal["findings"]),
    )
    ledger = ApprovalLedger.empty("demo_shop").append(approval)
    return requirements, project, configuration, proposal, approval, ledger


def _redigest(document):
    payload = {key: value for key, value in document.items() if key != "digest"}
    return with_digest(payload)


def test_rehashed_fabricated_ready_proposal_cannot_grant_or_compose():
    requirements, project, configuration, proposal, approval, _ledger = _chain()
    fabricated_proposal = copy.deepcopy(proposal)
    fabricated_proposal["findings"] = []
    fabricated_proposal["state"] = "ready"
    fabricated_proposal = _redigest(fabricated_proposal)

    problems = proposals.verify_proposal_version(
        fabricated_proposal,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("findings" in item for item in problems)

    with pytest.raises(ApprovalError, match=CODE_APPROVAL_NOT_EFFECTIVE):
        new_approval_record(
            project_ref="demo_shop",
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=fabricated_proposal,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason=REASON,
            acknowledged_findings=[],
        )

    forged_approval = plain(approval)
    forged_approval["proposal_ref"] = {
        "proposal_id": fabricated_proposal["proposal_id"],
        "digest": fabricated_proposal["digest"],
    }
    forged_approval["acknowledged_findings"] = []
    forged_approval = _redigest(forged_approval)
    forged_ledger = ApprovalLedger.empty("demo_shop").append(forged_approval)
    with pytest.raises(CompositionRequestError, match=CODE_APPROVAL_NOT_EFFECTIVE):
        new_request_record(
            project_ref="demo_shop",
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=fabricated_proposal,
            approval=forged_approval,
            ledger=forged_ledger,
            root=REPOSITORY_ROOT,
        )


def test_requirements_and_configuration_substitution_fail_even_when_redigested():
    requirements, project, configuration, proposal, _approval, _ledger = _chain()

    substituted_requirements = with_digest(
        {
            "schema_version": requirements["schema_version"],
            "requirements_id": requirements["requirements_id"],
            "project_ref": requirements["project_ref"],
            "entries": [
                {
                    **requirements["entries"][0],
                    "statement": {"summary": "A substituted intent."},
                }
            ],
            "predecessor": requirements["predecessor"],
        }
    )
    proposal_for_substitution = build_proposal(
        configuration=configuration,
        requirements=substituted_requirements,
        project=project,
    )
    assert proposal_for_substitution["digest"] != proposal["digest"]
    problems = proposals.verify_proposal_version(
        proposal_for_substitution,
        configuration=configuration,
        requirements=substituted_requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("configuration requirements_ref" in item for item in problems)

    substituted_proposal_ref = copy.deepcopy(proposal)
    substituted_proposal_ref["requirements_ref"] = {
        "requirements_id": substituted_requirements["requirements_id"],
        "digest": substituted_requirements["digest"],
    }
    substituted_proposal_ref = _redigest(substituted_proposal_ref)
    problems = proposals.verify_proposal_version(
        substituted_proposal_ref,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("proposal requirements_ref" in item for item in problems)

    substituted_configuration = build_v2(manifest_version="1.0.1")
    assert (
        substituted_configuration["configuration_id"]
        == configuration["configuration_id"]
    )
    assert substituted_configuration["digest"] != configuration["digest"]
    problems = proposals.verify_proposal_version(
        proposal,
        configuration=substituted_configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("configuration_ref" in item for item in problems)

    substituted_project = copy.deepcopy(project)
    substituted_project["project_id"] = "other_shop"
    problems = proposals.verify_proposal_version(
        proposal,
        configuration=configuration,
        requirements=requirements,
        project=substituted_project,
        root=REPOSITORY_ROOT,
    )
    assert any("does not match the supplied project" in item for item in problems)

    tampered_configuration_ref = copy.deepcopy(proposal)
    tampered_configuration_ref["configuration_ref"]["digest"] = (
        substituted_configuration["digest"]
    )
    tampered_configuration_ref = _redigest(tampered_configuration_ref)
    problems = proposals.verify_proposal_version(
        tampered_configuration_ref,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("configuration_ref" in item for item in problems)

    tampered_project_ref = copy.deepcopy(proposal)
    tampered_project_ref["project_ref"] = "other_shop"
    tampered_project_ref["proposal_id"] = "other_shop/proposals/1"
    tampered_project_ref = _redigest(tampered_project_ref)
    problems = proposals.verify_proposal_version(
        tampered_project_ref,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("project_ref" in item for item in problems)

    tampered_input_fingerprint = copy.deepcopy(proposal)
    current_fingerprint = proposal["inputs"]["registry_fingerprint"]
    zero_fingerprint = "sha256:" + "0" * 64
    replacement_fingerprint = (
        zero_fingerprint
        if current_fingerprint != zero_fingerprint
        else "sha256:" + "1" * 64
    )
    tampered_input_fingerprint["inputs"][
        "registry_fingerprint"
    ] = replacement_fingerprint
    tampered_input_fingerprint = _redigest(tampered_input_fingerprint)
    problems = proposals.verify_proposal_version(
        tampered_input_fingerprint,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("inputs" in item for item in problems)

    reasons = approval_ineffectiveness_reasons(
        _approval,
        configuration=configuration,
        requirements=requirements,
        project=substituted_project,
        proposal=proposal,
        ledger=_ledger,
    )
    assert any("does not match the Approval project_ref" in item for item in reasons)


def test_approval_must_be_exact_ledger_member_and_changed_ack_fails():
    requirements, project, configuration, proposal, approval, ledger = _chain()

    assert any(
        "ApprovalLedger is required" in item
        for item in approval_ineffectiveness_reasons(
            approval,
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=proposal,
        )
    )

    same_id_different_content = plain(approval)
    same_id_different_content["reason"] = "caller-substituted approval"
    same_id_different_content = _redigest(same_id_different_content)
    reasons = approval_ineffectiveness_reasons(
        same_id_different_content,
        configuration=configuration,
        requirements=requirements,
        project=project,
        proposal=proposal,
        ledger=ledger,
    )
    assert any(
        "exact approval id/digest and record content" in item for item in reasons
    )

    for field, value in (
        (
            "requirements_ref",
            {
                "requirements_id": requirements["requirements_id"],
                "digest": "sha256:" + "1" * 64,
            },
        ),
        (
            "configuration_ref",
            {
                "configuration_id": configuration["configuration_id"],
                "digest": "sha256:" + "2" * 64,
            },
        ),
        (
            "proposal_ref",
            {
                "proposal_id": proposal["proposal_id"],
                "digest": "sha256:" + "3" * 64,
            },
        ),
    ):
        substituted = plain(approval)
        substituted[field] = value
        substituted = _redigest(substituted)
        substituted_ledger = ApprovalLedger.empty("demo_shop").append(substituted)
        reasons = approval_ineffectiveness_reasons(
            substituted,
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=proposal,
            ledger=substituted_ledger,
        )
        assert any(CODE_APPROVAL_NOT_EFFECTIVE in item for item in reasons)

    changed_ack = plain(approval)
    changed_ack["acknowledged_findings"] = []
    changed_ack = _redigest(changed_ack)
    changed_ack_ledger = ApprovalLedger.empty("demo_shop").append(changed_ack)
    reasons = approval_ineffectiveness_reasons(
        changed_ack,
        configuration=configuration,
        requirements=requirements,
        project=project,
        proposal=proposal,
        ledger=changed_ack_ledger,
    )
    assert any(CODE_ACKNOWLEDGEMENT_MISMATCH in item for item in reasons)

    with pytest.raises(CompositionRequestError):
        new_request_record(
            project_ref="demo_shop",
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=proposal,
            approval=same_id_different_content,
            ledger=ledger,
            root=REPOSITORY_ROOT,
        )


def test_revocation_requires_exact_prior_target_and_is_single_append_only():
    _requirements, _project, _configuration, _proposal, approval, ledger = _chain()
    unappended_revocation = new_approval_revocation(
        project_ref="demo_shop",
        revoked=approval,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="unappended target",
    )
    with pytest.raises(ApprovalError, match="exact earlier record"):
        ApprovalLedger.empty("demo_shop").append(unappended_revocation)

    future_target = plain(approval)
    future_target["approval_id"] = "demo_shop/approvals/3"
    future_target["predecessor"] = None
    future_target = _redigest(future_target)
    future_revocation = new_approval_revocation(
        project_ref="demo_shop",
        revoked=future_target,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="target appears later in the input sequence",
        predecessor=approval,
    )
    future_target_problems = verify_ledger(
        [approval, future_revocation, future_target], project_ref="demo_shop"
    )
    assert any(
        "records[1]" in item
        and "revocation target" in item
        and "not an exact earlier record" in item
        for item in future_target_problems
    )

    wrong_target = new_approval_revocation(
        project_ref="demo_shop",
        revoked=approval,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="wrong target digest",
        predecessor=approval,
    )
    wrong_target["revokes"]["digest"] = "sha256:" + "0" * 64
    wrong_target = _redigest(wrong_target)
    with pytest.raises(ApprovalError, match="exact earlier record"):
        ledger.append(wrong_target)

    revocation = new_approval_revocation(
        project_ref="demo_shop",
        revoked=approval,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="valid revocation",
        predecessor=approval,
    )
    extended = ledger.append(revocation)
    assert verify_ledger(extended.documents(), project_ref="demo_shop") == []

    duplicate = new_approval_revocation(
        project_ref="demo_shop",
        revoked=approval,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="duplicate revocation",
        predecessor=revocation,
    )
    with pytest.raises(ApprovalError, match="already revoked"):
        extended.append(duplicate)
    assert len(ledger.records) == 1
    assert ledger.records[0]["decision"] == "granted"


def test_proposal_predecessor_is_explicit_and_chain_mismatch_fails():
    requirements = load("requirements")
    project = load("project")
    configuration = build_v2()
    predecessor = build_proposal(
        configuration=configuration,
        requirements=requirements,
        project=project,
    )
    successor = build_proposal(
        configuration=configuration,
        requirements=requirements,
        project=project,
        predecessor=predecessor,
    )
    assert (
        proposals.verify_proposal_version(
            successor,
            configuration=configuration,
            requirements=requirements,
            project=project,
            predecessor=predecessor,
            root=REPOSITORY_ROOT,
        )
        == []
    )
    assert proposals.verify_proposal_version(
        successor,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )

    wrong_predecessor = copy.deepcopy(predecessor)
    wrong_predecessor["inputs"]["validator_set"] = "control-plane/validators/v999"
    wrong_predecessor = _redigest(wrong_predecessor)
    assert proposals.verify_proposal_version(
        successor,
        configuration=configuration,
        requirements=requirements,
        project=project,
        predecessor=wrong_predecessor,
        root=REPOSITORY_ROOT,
    )


def test_ledger_sequence_and_partial_request_verification_fail_closed():
    requirements, project, configuration, proposal, approval, ledger = _chain()

    skipped = plain(approval)
    skipped["approval_id"] = "demo_shop/approvals/3"
    skipped["predecessor"] = {
        "approval_id": approval["approval_id"],
        "digest": approval["digest"],
    }
    skipped = _redigest(skipped)
    assert any("sequence" in item for item in verify_ledger([skipped]))
    with pytest.raises(ApprovalError, match="sequence"):
        ledger.append(skipped)

    with pytest.raises(CompositionRequestError, match="ApprovalLedger is required"):
        new_request_record(
            project_ref="demo_shop",
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=proposal,
            approval=approval,
            ledger=None,
            root=REPOSITORY_ROOT,
        )

    request = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        requirements=requirements,
        project=project,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
    )
    partial = verify_request_record(
        request,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("complete verification requires proposal" in item for item in partial)
    assert any("complete verification requires approval" in item for item in partial)
    assert any("complete verification requires ledger" in item for item in partial)

    assert (
        verify_request_record(
            request,
            configuration=configuration,
            requirements=requirements,
            project=project,
            proposal=proposal,
            approval=approval,
            ledger=ledger,
            root=REPOSITORY_ROOT,
        )
        == []
    )
