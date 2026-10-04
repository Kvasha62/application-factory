"""S2-1/S2-2 proofs: the approved composition path and its proof chain.

Proves: the record wraps the exact projection of the approved configuration and
no control-plane provenance leaks into the payload; the record cannot be built
without an effective approval (and there is no parameter through which another
composition could be smuggled in); the chain re-derives offline; composition
still ends at a **draft** manifest, because the manifest lifecycle remains the
sole publication authority.
"""

from __future__ import annotations

import copy

import pytest
from factory_control_plane import configuration_v2
from factory_control_plane.approvals import (
    ApprovalLedger,
    new_approval_record,
    new_approval_revocation,
    plain,
)
from factory_control_plane.composition_requests import (
    GENERATOR_VERSION,
    CompositionRequestError,
    new_request_record,
    request_fingerprint,
    same_request,
    verify_request_record,
)
from factory_control_plane.digests import with_digest
from factory_control_plane.validation import composition_request_record_errors
from slice2_fixtures import (
    APPROVER,
    DECIDED_AT,
    REASON,
    REPOSITORY_ROOT,
    build_proposal,
    build_v2,
    load,
)

from composer import (
    compose_diagnostics,
    compose_request_document,
    validate_request_document,
)
from platform_manifest import validate_document as validate_manifest_document


def _approved():
    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    warning_ids = [
        item["finding_id"]
        for item in proposal["findings"]
        if item["severity"] == "warning"
    ]
    approval = new_approval_record(
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
    ledger = ApprovalLedger.empty("demo_shop").append(approval)
    return configuration, proposal, approval, ledger


def test_record_wraps_the_exact_projection_of_the_approved_configuration():
    configuration, proposal, approval, ledger = _approved()
    record = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert composition_request_record_errors(record) == []
    assert record["generator_version"] == GENERATOR_VERSION
    assert record["configuration_ref"] == {
        "configuration_id": configuration["configuration_id"],
        "digest": configuration["digest"],
    }
    assert record["approval_ref"] == {
        "approval_id": approval["approval_id"],
        "digest": approval["digest"],
    }
    expected = configuration_v2.generate_request_payload(
        configuration, root=REPOSITORY_ROOT
    )
    assert record["request"] == expected
    assert "digest" not in record["request"]
    assert "schema_version" not in record["request"]


def test_record_requires_an_effective_approval():
    configuration, proposal, approval, ledger = _approved()
    with pytest.raises(CompositionRequestError):
        new_request_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=proposal,
            approval={**plain(approval), "decision": "rejected"},
            ledger=ledger,
            root=REPOSITORY_ROOT,
            requirements=load("requirements"),
            project=load("project"),
        )

    revoked = new_approval_revocation(
        project_ref="demo_shop",
        revoked=approval,
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason="withdrawn",
        predecessor=approval,
    )
    extended = ledger.append(revoked)
    with pytest.raises(CompositionRequestError) as refusal:
        new_request_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=proposal,
            approval=approval,
            ledger=extended,
            root=REPOSITORY_ROOT,
            requirements=load("requirements"),
            project=load("project"),
        )
    assert "revoked by" in str(refusal.value)


def test_an_approval_for_another_configuration_cannot_be_reused():
    configuration, proposal, approval, ledger = _approved()
    other = build_v2(predecessor=configuration, manifest_version="1.0.1")
    with pytest.raises(CompositionRequestError):
        new_request_record(
            project_ref="demo_shop",
            configuration=other,
            proposal=proposal,
            approval=approval,
            ledger=ledger,
            root=REPOSITORY_ROOT,
            requirements=load("requirements"),
            project=load("project"),
        )


def test_verify_recomputes_the_whole_chain():
    configuration, proposal, approval, ledger = _approved()
    record = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert (
        verify_request_record(
            record,
            configuration=configuration,
            proposal=proposal,
            approval=approval,
            ledger=ledger,
            root=REPOSITORY_ROOT,
            requirements=load("requirements"),
            project=load("project"),
        )
        == []
    )

    tampered_payload = copy.deepcopy(record)
    tampered_payload["request"]["manifest"]["manifest_version"] = "9.9.9"
    problems = verify_request_record(
        tampered_payload,
        configuration=configuration,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert any("digest does not match" in item for item in problems)
    assert any("not the exact projection" in item for item in problems)

    mislabelled = plain(record)
    mislabelled["configuration_ref"] = {
        "configuration_id": configuration["configuration_id"],
        "digest": "sha256:" + "c" * 64,
    }
    mislabelled = with_digest(
        {key: value for key, value in mislabelled.items() if key != "digest"}
    )
    problems = verify_request_record(
        mislabelled,
        configuration=configuration,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert any("configuration_ref does not reference" in item for item in problems)

    bad_approval = plain(approval)
    bad_approval["acknowledged_findings"] = []
    bad_approval = with_digest(
        {key: value for key, value in bad_approval.items() if key != "digest"}
    )
    problems = verify_request_record(
        record,
        configuration=configuration,
        proposal=proposal,
        approval=bad_approval,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert any("approval_ref does not reference" in item for item in problems)
    assert any("approval is not effective" in item for item in problems)


def test_composition_still_ends_at_a_draft_manifest():
    configuration, proposal, approval, ledger = _approved()
    record = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert validate_request_document(record["request"], root=REPOSITORY_ROOT) == []
    assert compose_diagnostics(record["request"], root=REPOSITORY_ROOT) == []

    composition = compose_request_document(record["request"], root=REPOSITORY_ROOT)
    assert composition.lifecycle_state == "draft"
    again = compose_request_document(record["request"], root=REPOSITORY_ROOT)
    assert again.document == composition.document
    assert again.manifest_digest == composition.manifest_digest
    assert composition.document["approval"] is None
    assert composition.document["publication"] is None

    # The publication gate stays where it was: a manifest cannot be published on
    # the strength of a control-plane approval — the lifecycle demands its own
    # publication metadata, and this slice produced none.
    forced = copy.deepcopy(composition.document)
    forced["lifecycle"] = {"state": "published"}
    problems = validate_manifest_document(forced, root=REPOSITORY_ROOT)
    assert any("publication" in item for item in problems)


def test_request_identity_is_idempotent():
    configuration, proposal, approval, ledger = _approved()
    first = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    second = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert first == second
    assert first["digest"] == second["digest"]
    assert request_fingerprint(first) == request_fingerprint(second)
    assert same_request(first, second)

    # The fingerprint is the approved pair, never the sequence: the same pair
    # re-asked is the same request, and any other pair is a different one.
    successor = build_v2(predecessor=configuration, manifest_version="1.0.1")
    successor_proposal = build_proposal(configuration=successor)
    successor_approval = new_approval_record(
        project_ref="demo_shop",
        configuration=successor,
        proposal=successor_proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=[
            item["finding_id"]
            for item in successor_proposal["findings"]
            if item["severity"] == "warning"
        ],
        requirements=load("requirements"),
        project=load("project"),
    )
    other = new_request_record(
        project_ref="demo_shop",
        configuration=successor,
        proposal=successor_proposal,
        approval=successor_approval,
        ledger=ApprovalLedger.empty("demo_shop").append(successor_approval),
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert request_fingerprint(other) != request_fingerprint(first)
    assert same_request(first, other) is False
    assert same_request(first, dict(first, request_id="demo_shop/requests/9"))


def test_request_id_follows_the_chain():
    configuration, proposal, approval, ledger = _approved()
    first = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    second = new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        predecessor=first,
        root=REPOSITORY_ROOT,
        requirements=load("requirements"),
        project=load("project"),
    )
    assert first["request_id"] == "demo_shop/requests/1"
    assert second["request_id"] == "demo_shop/requests/2"
    assert request_fingerprint(first) == request_fingerprint(second)


def test_v1_configuration_never_reaches_a_request_record():
    from slice2_fixtures import (
        load,
    )

    version = load("v1_version")
    proposal = build_proposal(configuration=version)
    assert proposal["state"] == "blocked"
    assert any(
        item["code"] == "CP-CONF-V1-NOT-PROJECTABLE" for item in proposal["findings"]
    )

    with pytest.raises(CompositionRequestError):
        new_request_record(
            project_ref="demo_shop",
            configuration=version,
            proposal=proposal,
            approval={
                "approval_id": "demo_shop/approvals/1",
                "digest": "sha256:" + "d" * 64,
            },
            ledger=None,
            root=REPOSITORY_ROOT,
            requirements=load("requirements"),
            project=load("project"),
        )

    # Even a structurally valid, retargeted grant cannot open the path: the v1
    # proposal is blocked, so the approval is not effective and the projection
    # refusal would stop the record anyway.
    _, _, good_approval, _ = _approved()
    retargeted = plain(good_approval)
    retargeted["configuration_ref"] = {
        "configuration_id": version["configuration_id"],
        "digest": version["digest"],
    }
    retargeted["proposal_ref"] = {
        "proposal_id": proposal["proposal_id"],
        "digest": proposal["digest"],
    }
    retargeted["requirements_ref"] = proposal["requirements_ref"]
    retargeted["acknowledged_findings"] = []
    retargeted = with_digest(
        {key: value for key, value in retargeted.items() if key != "digest"}
    )
    assert composition_request_record_errors({}) != []
    with pytest.raises(CompositionRequestError):
        new_request_record(
            project_ref="demo_shop",
            configuration=version,
            proposal=proposal,
            approval=retargeted,
            ledger=None,
            root=REPOSITORY_ROOT,
            requirements=load("requirements"),
            project=load("project"),
        )
