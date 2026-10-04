"""Slice 2 proposal-lifecycle proofs.

Proves: a proposal is an immutable, digest-covered report; regeneration is
idempotent; successors are appended and never mutate their predecessor; the
requirements existence and digest checks fire; a blocked proposal can never be
approved; and the deferred proposal features (alternatives, conflicts,
selection) are refused rather than silently accepted.
"""

from __future__ import annotations

import copy

import pytest
from factory_control_plane import documents, findings, proposals, validation
from factory_control_plane.approvals import ApprovalError, new_approval_record
from factory_control_plane.digests import digest_of
from factory_control_plane.validation import (
    ControlPlaneError,
    proposal_version_errors,
)
from slice2_fixtures import (
    APPROVER,
    DECIDED_AT,
    REASON,
    REPOSITORY_ROOT,
    build_proposal,
    build_v2,
)


def _violated_requirements():
    """A requirements version whose digest does not match the recorded reference."""
    version = documents.new_requirements_version(
        project_ref="demo_shop",
        entries=[
            {
                "req_id": "req-001",
                "kind": "capability",
                "statement": {"summary": "A different statement."},
                "source_channels": ["form"],
                "refs": [],
                "status": "open",
            }
        ],
    )
    return version


def _grantable_warnings(proposal):
    return [item["finding_id"] for item in findings.warnings_of(proposal["findings"])]


def test_proposal_is_immutable_and_append_only():
    first = build_proposal()
    snapshot = copy.deepcopy(first)

    successor = build_proposal(
        configuration=build_v2(predecessor=build_v2(), manifest_version="1.0.1"),
        predecessor=first,
    )
    assert successor["proposal_id"] == "demo_shop/proposals/2"
    assert successor["predecessor"] == {
        "proposal_id": first["proposal_id"],
        "digest": first["digest"],
    }
    assert first == snapshot

    configuration = build_v2()
    requirements = load_requirements()
    project = load_project()
    assert (
        proposals.verify_proposal_version(
            first,
            configuration=configuration,
            requirements=requirements,
            project=project,
            root=REPOSITORY_ROOT,
        )
        == []
    )
    tampered = copy.deepcopy(first)
    tampered["state"] = "ready" if first["state"] == "blocked" else "blocked"
    assert (
        proposals.verify_proposal_version(
            tampered,
            configuration=configuration,
            requirements=requirements,
            project=project,
            root=REPOSITORY_ROOT,
        )
        != []
    )

    with pytest.raises(ControlPlaneError):
        build_proposal(predecessor=None, project_ref="other_project")


def test_regeneration_is_idempotent():
    first = build_proposal()
    again = build_proposal()
    assert first == again
    # A successor built from the same predecessor and inputs is identical too.
    configuration = build_v2(predecessor=None, manifest_version="1.0.2")
    left = build_proposal(configuration=configuration, predecessor=first)
    right = build_proposal(configuration=configuration, predecessor=first)
    assert left == right
    assert left["digest"] == right["digest"]


def test_requirements_existence_is_verified():
    proposal = build_proposal(requirements=None)
    assert proposal["state"] == "blocked"
    codes = {item["code"] for item in proposal["findings"]}
    assert "CP-SEM-REQ-UNKNOWN" in codes
    assert proposals.proposal_is_ready(proposal) is False
    with pytest.raises(ApprovalError):
        new_approval_record(
            project_ref="demo_shop",
            configuration=build_v2(),
            proposal=proposal,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason=REASON,
            acknowledged_findings=[],
            requirements=load_requirements(),
            project=load_project(),
        )


def test_requirements_digest_mismatch_is_verified():
    """A mutated requirements_ref digest slips past Slice 1 and is caught here."""
    version = build_v2()
    forged = _resettle(
        {
            **version,
            "requirements_ref": dict(
                version["requirements_ref"], digest="sha256:" + "e" * 64
            ),
        }
    )
    assert forged["digest"] != version["digest"]
    assert validation.configuration_version_v2_errors(forged) == []

    proposal = build_proposal(configuration=forged)
    codes = {item["code"] for item in proposal["findings"]}
    assert "CP-SEM-REQ-DIGEST" in codes
    assert proposal["state"] == "blocked"

    # A report may faithfully describe the mismatch as a finding, but it is
    # not a valid approval chain: the Configuration's typed Requirements ref
    # does not equal the supplied RequirementsVersion pair.
    assert (
        proposals.verify_proposal_version(
            proposal,
            configuration=forged,
            requirements=load_requirements(),
            project=load_project(),
            root=REPOSITORY_ROOT,
        )
        != []
    )
    with pytest.raises(ApprovalError):
        new_approval_record(
            project_ref="demo_shop",
            configuration=forged,
            proposal=proposal,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason=REASON,
            acknowledged_findings=[],
            requirements=load_requirements(),
            project=load_project(),
        )


def test_the_analysis_is_reproducible_from_its_inputs():
    configuration = build_v2()
    requirements = load_requirements()
    project = load_project()
    proposal = build_proposal(
        configuration=configuration, requirements=requirements, project=project
    )
    assert (
        proposals.verify_proposal_version(
            proposal,
            configuration=configuration,
            requirements=requirements,
            project=project,
            root=REPOSITORY_ROOT,
        )
        == []
    )

    # Everything the proposal claims must be re-derivable: a digest is not
    # evidence on its own (see proposals.verify_proposal_version docstring).
    lying = _resettle({**proposal, "findings": [], "state": "blocked"})
    problems = proposals.verify_proposal_version(
        lying,
        configuration=configuration,
        requirements=requirements,
        project=project,
        root=REPOSITORY_ROOT,
    )
    assert any("findings" in item for item in problems)
    assert any("state" in item for item in problems)


def load_requirements():
    from slice2_fixtures import load

    return load("requirements")


def load_project():
    from slice2_fixtures import load

    return load("project")


def test_a_blocked_proposal_can_never_be_approved():
    configuration = build_v2(
        configuration={
            "identity": {
                "current_platform_id": "demo_shop_platform",
                "session_ttl_seconds": 3600,
            }
        }
    )
    requirements = load_requirements()
    project = load_project()
    blocked = build_proposal(
        configuration=configuration, requirements=requirements, project=project
    )
    assert blocked["state"] == "blocked"
    with pytest.raises(ApprovalError) as refusal:
        new_approval_record(
            project_ref="demo_shop",
            configuration=configuration,
            proposal=blocked,
            decision="granted",
            approver=dict(APPROVER),
            decided_at=DECIDED_AT,
            reason="attempted bypass",
            acknowledged_findings=_grantable_warnings(blocked),
            requirements=requirements,
            project=project,
        )
    assert "blocked proposal" in str(refusal.value)


def test_deferred_proposal_features_are_refused_not_silently_accepted():
    proposal = build_proposal()
    assert not hasattr(proposals, "select_alternative")
    assert not hasattr(proposals, "materialize")

    for key, value in (
        ("alternatives", [{"alternative_id": "alt-000000000000"}]),
        ("selection", {"alternative_id": "alt-000000000000"}),
        ("conflicts", []),
        ("candidate", {"manifest_id": "demo_shop_platform"}),
    ):
        smuggled = dict(proposal, **{key: value})
        problems = proposal_version_errors(_resettle(smuggled))
        assert problems, f"a proposal carrying {key!r} must be refused"
        assert any(key in item for item in problems), problems

    # The shipped schema agrees with the validator.
    schema = _schema("proposal_version")
    assert schema.get("additionalProperties") is False
    assert set(schema["properties"]) == {
        "schema_version",
        "proposal_id",
        "project_ref",
        "requirements_ref",
        "configuration_ref",
        "inputs",
        "findings",
        "state",
        "predecessor",
        "digest",
    }


def test_findings_carry_the_proposal_state():
    ready = build_proposal()
    assert ready["state"] == "ready"
    assert proposals.proposal_state(ready) == "ready"
    assert findings.has_blocking_findings(ready["findings"]) is False
    assert proposals.proposal_warning_ids(ready) == findings.warning_ids(
        ready["findings"]
    )

    blocked = build_proposal(requirements=None)
    assert blocked["state"] == "blocked"
    assert findings.has_blocking_findings(blocked["findings"]) is True
    assert proposals.proposal_warning_ids(blocked) == []


def _resettle(document):
    """Recompute the digest of a deliberately hand-edited document."""
    payload = {key: value for key, value in document.items() if key != "digest"}
    return {**payload, "digest": digest_of(payload)}


def _schema(name):
    import json

    from slice2_fixtures import SUBTREE_ROOT

    path = SUBTREE_ROOT / "schema" / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))
