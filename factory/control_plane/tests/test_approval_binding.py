"""S2-2 proofs: the immutable, append-only approval ledger.

Proves: the record contract; immutability after creation (frozen records, no
update or delete path, no aliasing of caller data); append semantics with a
strict chain; derived — never stored — effectiveness; revocation as an appended
record; the RBAC attachment point; and deterministic retrieval/audit output.
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest
from factory_control_plane import approvals
from factory_control_plane.approvals import (
    AllowAllAuthority,
    ApprovalError,
    ApprovalLedger,
    ApprovalNotAuthorizedError,
    AuthorityDecision,
    approval_effective,
    approval_ineffectiveness_reasons,
    new_approval_record,
    new_approval_revocation,
    plain,
    verify_ledger,
)
from factory_control_plane.findings import (
    CODE_ACKNOWLEDGEMENT_MISMATCH,
    CODE_APPROVAL_NOT_EFFECTIVE,
)
from factory_control_plane.validation import (
    ControlPlaneError,
    approval_record_errors,
)
from slice2_fixtures import APPROVER, DECIDED_AT, REASON, build_proposal, build_v2


def _grant(configuration=None, proposal=None, **overrides):
    configuration = configuration if configuration is not None else build_v2()
    proposal = (
        proposal
        if proposal is not None
        else build_proposal(configuration=configuration)
    )
    arguments = {
        "project_ref": "demo_shop",
        "configuration": configuration,
        "proposal": proposal,
        "decision": "granted",
        "approver": dict(APPROVER),
        "decided_at": DECIDED_AT,
        "reason": REASON,
        "acknowledged_findings": [
            item["finding_id"]
            for item in proposal["findings"]
            if item["severity"] == "warning"
        ],
    }
    arguments.update(overrides)
    return new_approval_record(**arguments), configuration, proposal


def _resolver(configuration, proposal):
    def resolve(kind, reference):
        return {"configuration": configuration, "proposal": proposal}.get(kind)

    return resolve


def test_record_contract_and_digest():
    record, _, _ = _grant()
    assert approval_record_errors(record) == []
    assert record["decision"] == "granted"
    assert record["approval_id"] == "demo_shop/approvals/1"
    assert record["predecessor"] is None
    assert record["revokes"] is None
    assert record["approver"]["authority_ref"] is None

    unknown_field = dict(record, extra="x")
    assert any(
        "unknown field 'extra'" in item
        for item in approval_record_errors(unknown_field)
    )

    with pytest.raises(ControlPlaneError):
        new_approval_record(
            project_ref="demo_shop",
            configuration=build_v2(),
            proposal=build_proposal(),
            decision="revoked",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason=REASON,
        )


def test_records_are_frozen_after_creation():
    record, _, _ = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(record)
    stored = ledger.records[0]

    with pytest.raises(TypeError):
        stored["decision"] = "rejected"  # type: ignore[index]
    with pytest.raises(TypeError):
        stored["approver"]["kind"] = "agent"  # type: ignore[index]
    assert isinstance(ledger.records, tuple)


def test_appending_never_aliases_caller_data():
    record, _, _ = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(record)
    before = json.dumps(ledger.documents(), sort_keys=True)

    record["reason"] = "mutated after append"
    record["approver"]["reference"] = "someone_else"
    assert json.dumps(ledger.documents(), sort_keys=True) == before


def test_no_update_or_delete_path_exists():
    ledger = ApprovalLedger.empty("demo_shop")
    for name in (
        "update",
        "delete",
        "remove",
        "pop",
        "clear",
        "__setitem__",
        "__delitem__",
    ):
        assert not hasattr(ledger, name), f"ledger exposes a mutating path: {name}"
    assert [field.name for field in dataclasses.fields(ledger)] == [
        "project_ref",
        "records",
    ]


def test_append_keeps_earlier_records_byte_identical():
    first, _, _ = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(first)
    snapshot = json.dumps(ledger.documents(), sort_keys=True)

    revoked = new_approval_revocation(
        project_ref="demo_shop",
        revoked=first,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="superseded by a corrected approval",
        predecessor=first,
    )
    extended = ledger.append(revoked)
    assert json.dumps(ledger.documents(), sort_keys=True) == snapshot
    assert extended.records[0] == ledger.records[0]
    assert extended.head()["decision"] == "revoked"
    assert extended.head()["revokes"] == {
        "approval_id": first["approval_id"],
        "digest": first["digest"],
    }


def test_append_is_strictly_append_only():
    first, _, _ = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(first)

    second, _, _ = _grant(
        configuration=build_v2(manifest_version="1.0.1"),
        predecessor=None,
    )
    # The second record claims to be approval #2 but does not link #1 as its head.
    stale = dict(second, predecessor=None)
    import hashlib

    from factory_control_plane.canonical import canonical_bytes

    stale = plain(stale)
    payload = {key: value for key, value in stale.items() if key != "digest"}
    stale["digest"] = "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()
    with pytest.raises(ApprovalError) as broken_link:
        ledger.append(stale)
    assert "predecessor" in str(broken_link.value)

    foreign_project = dict(first, project_ref="other_project")
    with pytest.raises(ApprovalError):
        ledger.append(foreign_project)

    with pytest.raises(ApprovalError):
        ledger.append(first)


def test_sequence_must_continue_the_chain():
    first, _, _ = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(first)
    second, _, _ = _grant(configuration=build_v2(manifest_version="1.0.1"))
    assert second["approval_id"] == "demo_shop/approvals/1"
    skipped = plain(second)
    skipped["approval_id"] = "demo_shop/approvals/3"
    skipped["predecessor"] = {
        "approval_id": first["approval_id"],
        "digest": first["digest"],
    }
    from factory_control_plane.digests import digest_of

    skipped["digest"] = digest_of(skipped)
    with pytest.raises(ApprovalError) as refusal:
        ledger.append(skipped)
    assert "sequence" in str(refusal.value)


def test_ledger_verification_detects_tampering_and_broken_links():
    first, _, _ = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(first)
    assert verify_ledger(ledger.documents(), project_ref="demo_shop") == []

    tampered = copy.deepcopy(ledger.documents()[0])
    tampered["reason"] = "rewritten"
    problems = verify_ledger([tampered])
    assert any("digest does not verify" in item for item in problems)

    wrong_predecessor = copy.deepcopy(ledger.documents()[0])
    wrong_predecessor["predecessor"] = {
        "approval_id": "demo_shop/approvals/0",
        "digest": "sha256:" + "0" * 64,
    }
    problems = verify_ledger([wrong_predecessor])
    assert any("predecessor link is broken" in item for item in problems)


def test_effectiveness_is_derived_and_never_stored():
    record, configuration, proposal = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(record)
    resolve = _resolver(configuration, proposal)
    snapshot = json.dumps(ledger.documents(), sort_keys=True)

    assert approval_effective(
        record, configuration=configuration, proposal=proposal, ledger=ledger
    )
    assert ledger.effective_records(resolve) == ledger.documents()

    changed = build_v2(predecessor=configuration, manifest_version="1.0.1")
    assert changed["configuration_id"] != configuration["configuration_id"]
    reasons = approval_ineffectiveness_reasons(
        record, configuration=changed, proposal=proposal, ledger=ledger
    )
    assert any(CODE_APPROVAL_NOT_EFFECTIVE in item for item in reasons)
    assert any("digest" in item for item in reasons)
    assert any("configuration_id" in item for item in reasons)

    revoked = new_approval_revocation(
        project_ref="demo_shop",
        revoked=record,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="withdrawn",
        predecessor=record,
    )
    extended = ledger.append(revoked)
    reasons = approval_ineffectiveness_reasons(
        record, configuration=configuration, proposal=proposal, ledger=extended
    )
    assert any("revoked by" in item for item in reasons)
    assert (
        approval_effective(
            revoked, configuration=configuration, proposal=proposal, ledger=extended
        )
        is False
    )

    # Derivation changed the answers; the stored bytes never changed.
    assert json.dumps(ledger.documents(), sort_keys=True) == snapshot
    assert record["decision"] == "granted"


def test_proposal_errors_make_an_approval_ineffective():
    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    record, _, _ = _grant(configuration=configuration, proposal=proposal)
    blocked = build_proposal(configuration=configuration, requirements=None)
    assert blocked["state"] == "blocked"
    reasons = approval_ineffectiveness_reasons(
        record, configuration=configuration, proposal=blocked
    )
    assert any("not the analysed proposal" in item for item in reasons)


def test_acknowledgement_mismatch_is_a_derived_reason():
    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    record, _, _ = _grant(configuration=configuration, proposal=proposal)
    widened = build_proposal(
        configuration=configuration,
        project={"project_id": "demo_shop", "status": "archived"},
    )
    reasons = approval_ineffectiveness_reasons(
        record, configuration=configuration, proposal=widened
    )
    assert any("not the analysed proposal" in item for item in reasons)

    re_acknowledged = plain(record)
    re_acknowledged["acknowledged_findings"] = []
    from factory_control_plane.digests import digest_of

    re_acknowledged["digest"] = digest_of(re_acknowledged)
    reasons = approval_ineffectiveness_reasons(
        re_acknowledged, configuration=configuration, proposal=proposal
    )
    assert any(CODE_ACKNOWLEDGEMENT_MISMATCH in item for item in reasons)


def test_registry_drift_invalidates_without_writing_anything(monkeypatch):
    record, configuration, proposal = _grant()
    snapshot = json.dumps(record, sort_keys=True)
    monkeypatch.setattr(
        approvals, "component_reference_errors", lambda components: ["drift: retired"]
    )
    reasons = approval_ineffectiveness_reasons(
        record, configuration=configuration, proposal=proposal
    )
    assert any("no longer admits the approved references" in item for item in reasons)
    assert json.dumps(record, sort_keys=True) == snapshot


def test_rbac_attachment_point_is_the_only_authorization_seam():
    calls = []

    class RecordingPolicy:
        def authorize(self, *, approver, action, approval_id, configuration_ref):
            calls.append((approver, action, approval_id, configuration_ref))
            return AuthorityDecision(allowed=True)

    record, configuration, _ = _grant(authority=RecordingPolicy())
    assert calls and calls[0][1] == "grant"
    assert calls[0][2] == record["approval_id"]
    assert calls[0][3] == {
        "configuration_id": configuration["configuration_id"],
        "digest": configuration["digest"],
    }

    class DenyPolicy:
        def authorize(self, *, approver, action, approval_id, configuration_ref):
            return AuthorityDecision(allowed=False, reason="no role assignment")

    with pytest.raises(ApprovalNotAuthorizedError) as refusal:
        _grant(authority=DenyPolicy())
    assert "no role assignment" in str(refusal.value)

    assert isinstance(AllowAllAuthority().policy_id, str)
    with pytest.raises(ControlPlaneError) as attachment:
        _grant(approver={**APPROVER, "authority_ref": "role:owner"})
    assert "RBAC attachment point" in str(attachment.value)


def test_revocation_is_an_appended_record_and_grant_is_untouched():
    record, _, _ = _grant()
    ledger = ApprovalLedger.empty("demo_shop").append(record)
    snapshot = json.dumps(ledger.documents()[0], sort_keys=True)

    revocation = new_approval_revocation(
        project_ref="demo_shop",
        revoked=record,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="withdrawn by the owner",
        predecessor=record,
    )
    ledger = ledger.append(revocation)
    assert len(ledger.records) == 2
    assert json.dumps(ledger.documents()[0], sort_keys=True) == snapshot
    assert approval_record_errors(revocation) == []
    assert revocation["acknowledged_findings"] == []

    with pytest.raises(ApprovalError):
        new_approval_revocation(
            project_ref="demo_shop",
            revoked=revocation,
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason="double revocation",
            predecessor=revocation,
        )


def test_loading_a_ledger_revalidates_every_record():
    record, _, _ = _grant()
    ledger = ApprovalLedger.load("demo_shop", [record])
    assert list(ledger.documents()) == [record]

    tampered = copy.deepcopy(record)
    tampered["reason"] = "rewritten"
    with pytest.raises(ApprovalError):
        ApprovalLedger.load("demo_shop", [tampered])


def test_audit_view_is_deterministic_and_ordered():
    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    granted, _, _ = _grant(configuration=configuration, proposal=proposal)
    rejected = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="rejected",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="not acceptable yet",
        predecessor=granted,
    )
    ledger = ApprovalLedger.load("demo_shop", [granted, rejected])
    resolve = _resolver(configuration, proposal)

    first = ledger.audit_view(resolve)
    second = ledger.audit_view(resolve)
    assert first == second
    assert [entry["sequence"] for entry in first] == [1, 2]
    assert [entry["decision"] for entry in first] == ["granted", "rejected"]
    assert first[0]["effective"] is True
    assert first[0]["acknowledged_findings"] == sorted(
        entry["finding_id"]
        for entry in proposal["findings"]
        if entry["severity"] == "warning"
    )
    assert first[1]["effective"] is False
    assert first[1]["ineffective_reasons"] == sorted(first[1]["ineffective_reasons"])

    effective = ledger.effective_records(resolve)
    assert [entry["approval_id"] for entry in effective] == [granted["approval_id"]]
    assert ledger.index_of(granted["approval_id"]) == 0
    assert len(ledger.records_after(granted)) == 1
