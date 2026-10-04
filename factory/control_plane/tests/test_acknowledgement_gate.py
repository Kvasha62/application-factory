"""S2-3 proofs: acknowledgement semantics for error / warning / info findings.

Proves: an ``error`` cannot be acknowledged and cannot be bypassed; a
``warning`` cannot proceed unacknowledged (the approval is refused without an
exact acknowledgement set, and an approval that no longer matches is not
effective, so no request record can exist); an ``info`` needs nothing; and
acknowledgements are explicit, recorded and auditable.
"""

from __future__ import annotations

import copy

import pytest
from factory_control_plane import documents, proposals
from factory_control_plane.approvals import (
    ApprovalError,
    ApprovalLedger,
    approval_effective,
    approval_ineffectiveness_reasons,
    new_approval_record,
    plain,
)
from factory_control_plane.composition_requests import (
    CompositionRequestError,
    new_request_record,
)
from factory_control_plane.digests import with_digest
from factory_control_plane.findings import (
    CODE_ACKNOWLEDGEMENT_MISMATCH,
    CODE_ERROR_ACKNOWLEDGED,
    CODE_REQUIREMENTS_UNKNOWN,
    finding_id,
    warnings_of,
)
from slice2_fixtures import APPROVER, DECIDED_AT, REASON, build_proposal, build_v2, load

WARNING_CODE = "CP-WARN-OPEN-REQUIREMENTS"
INFO_CODE = "CP-DEP-001"


def _warning_ids(proposal):
    return [item["finding_id"] for item in warnings_of(proposal["findings"])]


def _resolver(configuration, proposal, requirements=None, project=None):
    documents = {
        "configuration": configuration,
        "requirements": load("requirements") if requirements is None else requirements,
        "project": load("project") if project is None else project,
        "proposal": proposal,
    }
    return lambda kind, ref: documents.get(kind)


def test_warning_requires_an_explicit_acknowledgement():
    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    assert _warning_ids(proposal), "the demonstration proposal must carry a warning"

    with pytest.raises(ApprovalError) as refusal:
        new_approval_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=proposal,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason=REASON,
            acknowledged_findings=[],
            requirements=load("requirements"),
            project=load("project"),
        )
    assert CODE_ACKNOWLEDGEMENT_MISMATCH in str(refusal.value)

    granted = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=_warning_ids(proposal),
        requirements=load("requirements"),
        project=load("project"),
    )
    assert granted["acknowledged_findings"] == sorted(_warning_ids(proposal))


def test_warning_cannot_proceed_unacknowledged_into_composition():
    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    granted = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=_warning_ids(proposal),
        requirements=load("requirements"),
        project=load("project"),
    )
    ledger = ApprovalLedger.empty("demo_shop").append(granted)

    # A grant whose acknowledgement set no longer matches (here: an approval
    # carrying no acknowledgement at all) is not effective, so composition is
    # refused — the warning cannot proceed unacknowledged.
    silent = plain(granted)
    silent["acknowledged_findings"] = []
    silent = with_digest(
        {key: value for key, value in silent.items() if key != "digest"}
    )
    assert (
        approval_effective(
            silent,
            configuration=configuration,
            proposal=proposal,
            ledger=None,
            requirements=load("requirements"),
            project=load("project"),
        )
        is False
    )
    reasons = approval_ineffectiveness_reasons(
        silent,
        configuration=configuration,
        proposal=proposal,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert any(CODE_ACKNOWLEDGEMENT_MISMATCH in item for item in reasons)
    with pytest.raises(CompositionRequestError):
        new_request_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=proposal,
            approval=silent,
            ledger=ledger,
            requirements=load("requirements"),
            project=load("project"),
        )


def test_error_cannot_be_acknowledged():
    configuration = build_v2()
    proposal = build_proposal(configuration=configuration, requirements=None)
    assert proposal["state"] == "blocked"
    assert any(
        item["code"] == CODE_REQUIREMENTS_UNKNOWN for item in proposal["findings"]
    )

    # A distinct, exactly supplied input chain yields a blocking Composer error;
    # it can be re-derived, so the acknowledgement rule itself is exercised at
    # the Approval boundary rather than relying on a missing Requirements doc.
    blocked_configuration = build_v2(
        configuration={
            "identity": {
                "current_platform_id": "demo_shop_platform",
                "session_ttl_seconds": 3600,
            }
        }
    )
    requirements = load("requirements")
    project = load("project")
    blocked = build_proposal(
        configuration=blocked_configuration,
        requirements=requirements,
        project=project,
    )
    error_ids = [
        item["finding_id"]
        for item in blocked["findings"]
        if item["severity"] == "error"
    ]
    warning_ids = _warning_ids(blocked)

    for acknowledged in (error_ids, error_ids + warning_ids, warning_ids):
        with pytest.raises(ApprovalError) as refusal:
            new_approval_record(
                project_ref="demo_shop",
                configuration=blocked_configuration,
                proposal=blocked,
                decision="granted",
                approver=dict(APPROVER),
                decided_at=DECIDED_AT,
                reason="bypass attempt",
                acknowledged_findings=acknowledged,
                requirements=load("requirements"),
                project=load("project"),
            )
        message = str(refusal.value)
        assert (
            CODE_ERROR_ACKNOWLEDGED in message
            or "blocked proposal" in message
            or CODE_ACKNOWLEDGEMENT_MISMATCH in message
        )

    with pytest.raises(ApprovalError) as explicit:
        new_approval_record(
            project_ref="demo_shop",
            configuration=blocked_configuration,
            proposal=blocked,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason="bypass attempt",
            acknowledged_findings=error_ids,
            requirements=requirements,
            project=project,
        )
    assert CODE_ERROR_ACKNOWLEDGED in str(explicit.value)


def test_error_cannot_be_bypassed_even_by_a_fabricated_record():
    configuration = build_v2()
    blocked_configuration = build_v2(
        configuration={
            "identity": {
                "current_platform_id": "demo_shop_platform",
                "session_ttl_seconds": 3600,
            }
        }
    )
    requirements = load("requirements")
    project = load("project")
    blocked = build_proposal(
        configuration=blocked_configuration,
        requirements=requirements,
        project=project,
    )
    good = build_proposal(configuration=configuration)
    granted = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=good,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=_warning_ids(good),
        requirements=load("requirements"),
        project=load("project"),
    )

    fabricated = plain(granted)
    fabricated["configuration_ref"] = {
        "configuration_id": blocked_configuration["configuration_id"],
        "digest": blocked_configuration["digest"],
    }
    fabricated["proposal_ref"] = {
        "proposal_id": blocked["proposal_id"],
        "digest": blocked["digest"],
    }
    fabricated["acknowledged_findings"] = _warning_ids(blocked)
    fabricated = with_digest(
        {key: value for key, value in fabricated.items() if key != "digest"}
    )
    ledger = ApprovalLedger.empty("demo_shop").append(fabricated)

    reasons = approval_ineffectiveness_reasons(
        fabricated,
        configuration=blocked_configuration,
        requirements=requirements,
        project=project,
        proposal=blocked,
        ledger=ledger,
    )
    assert any("carries blocking errors" in item for item in reasons)
    fabricated["proposal_ref"] = {
        "proposal_id": good["proposal_id"],
        "digest": good["digest"],
    }
    fabricated = with_digest(
        {key: value for key, value in fabricated.items() if key != "digest"}
    )
    reasons = approval_ineffectiveness_reasons(
        fabricated,
        configuration=blocked_configuration,
        requirements=requirements,
        project=project,
        proposal=blocked,
        ledger=ledger,
    )
    assert any("not the analysed proposal" in item for item in reasons)
    with pytest.raises(CompositionRequestError):
        new_request_record(
            project_ref="demo_shop",
            configuration=blocked_configuration,
            requirements=requirements,
            project=project,
            proposal=blocked,
            approval=fabricated,
            ledger=ledger,
        )


def test_delegated_composer_errors_also_block_acknowledgement():
    configuration = build_v2(
        configuration={
            "identity": {
                "current_platform_id": "demo_shop_platform",
                "session_ttl_seconds": 3600,
            }
        }
    )
    proposal = build_proposal(configuration=configuration)
    assert proposal["state"] == "blocked"
    assert any(item["code"] == "CP-DEP-000" for item in proposal["findings"]), proposal[
        "findings"
    ]
    with pytest.raises(ApprovalError) as refusal:
        new_approval_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=proposal,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason="bypass attempt",
            acknowledged_findings=_warning_ids(proposal),
            requirements=load("requirements"),
            project=load("project"),
        )
    assert "blocked proposal" in str(refusal.value)


def test_info_requires_no_acknowledgement():
    resolved_requirements = documents.new_requirements_version(
        project_ref="demo_shop",
        entries=[
            {
                "req_id": "req-001",
                "kind": "capability",
                "statement": {"summary": "Shoppers can check out with a basket."},
                "source_channels": ["form"],
                "refs": [],
                "status": "resolved",
            }
        ],
    )
    configuration = build_v2(requirements_ref=resolved_requirements)
    proposal = build_proposal(
        configuration=configuration, requirements=resolved_requirements
    )
    assert proposal["state"] == "ready"
    assert warnings_of(proposal["findings"]) == []
    assert any(item["code"] == INFO_CODE for item in proposal["findings"])

    granted = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=[],
        requirements=resolved_requirements,
        project=load("project"),
    )
    ledger = ApprovalLedger.empty("demo_shop").append(granted)
    assert approval_effective(
        granted,
        configuration=configuration,
        proposal=proposal,
        ledger=ledger,
        requirements=resolved_requirements,
        project=load("project"),
    )
    record = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=granted,
        ledger=ledger,
        requirements=resolved_requirements,
        project=load("project"),
    )
    assert record["request_id"] == "demo_shop/requests/1"


def test_acknowledgement_set_must_be_exact():
    configuration = build_v2(
        manifest_predecessor={
            "manifest_id": "demo_shop_platform",
            "manifest_version": "0.9.0",
            "manifest_digest": "sha256:" + "a" * 64,
        }
    )
    proposal = build_proposal(configuration=configuration)
    warning_ids = _warning_ids(proposal)
    assert len(warning_ids) == 2, proposal["findings"]

    with pytest.raises(ApprovalError) as unknown:
        new_approval_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=proposal,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason=REASON,
            acknowledged_findings=["fnd-000000000000"],
            requirements=load("requirements"),
            project=load("project"),
        )
    assert "does not carry" in str(unknown.value)

    with pytest.raises(ApprovalError) as partial:
        new_approval_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=proposal,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason=REASON,
            acknowledged_findings=warning_ids[:1],
            requirements=load("requirements"),
            project=load("project"),
        )
    assert CODE_ACKNOWLEDGEMENT_MISMATCH in str(partial.value)

    granted = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=list(reversed(warning_ids)),
        requirements=load("requirements"),
        project=load("project"),
    )
    assert granted["acknowledged_findings"] == sorted(warning_ids)


def test_acknowledgements_are_recorded_and_auditable():
    configuration = build_v2(
        manifest_predecessor={
            "manifest_id": "demo_shop_platform",
            "manifest_version": "0.9.0",
            "manifest_digest": "sha256:" + "b" * 64,
        }
    )
    proposal = build_proposal(configuration=configuration)
    warning_ids = _warning_ids(proposal)
    granted = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=warning_ids,
        requirements=load("requirements"),
        project=load("project"),
    )
    ledger = ApprovalLedger.load("demo_shop", [granted])
    view = ledger.audit_view(_resolver(configuration, proposal))

    assert granted["acknowledged_findings"] == sorted(warning_ids)
    assert view[0]["acknowledged_findings"] == sorted(warning_ids)
    assert view[0]["effective"] is True
    for item in proposal["findings"]:
        if item["severity"] == "warning":
            assert finding_id(item) == item["finding_id"]


def test_regenerated_proposal_must_be_re_acknowledged():
    """A new analysis (new proposal version) cannot inherit an acknowledgement."""
    configuration = build_v2()
    first = build_proposal(configuration=configuration)
    granted = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=first,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=_warning_ids(first),
        requirements=load("requirements"),
        project=load("project"),
    )

    widened_requirements = documents.new_requirements_version(
        project_ref="demo_shop",
        predecessor=load("requirements"),
        entries=[
            {
                "req_id": "req-001",
                "kind": "capability",
                "statement": {"summary": "Shoppers can check out with a basket."},
                "source_channels": ["form"],
                "refs": [],
                "status": "open",
            },
            {
                "req_id": "req-002",
                "kind": "constraint",
                "statement": {"summary": "EU data residency."},
                "source_channels": ["api"],
                "refs": [],
                "status": "open",
            },
        ],
    )
    regenerated = proposals.new_proposal_version(
        project_ref="demo_shop",
        configuration=configuration,
        requirements=widened_requirements,
        predecessor=first,
    )
    assert regenerated["proposal_id"] == "demo_shop/proposals/2"
    assert regenerated["digest"] != first["digest"]
    assert len(_warning_ids(regenerated)) == 1

    reasons = approval_ineffectiveness_reasons(
        granted,
        configuration=configuration,
        proposal=regenerated,
        requirements=widened_requirements,
        project=load("project"),
        proposal_predecessor=first,
    )
    assert any("proposal is not the analysed proposal" in item for item in reasons)
    assert copy.deepcopy(granted)["digest"] == granted["digest"]
