"""A request the contract cannot even read is refused — and audited (Issue #12).

The published schema of ``POST /api/v1/decisions`` rejects a malformed request
before any decision logic of this component runs. That refusal is
security-relevant: it says that a component holding a service identity of this
Platform Instance asked this authority something unreadable. Invariant 9 gives
it no exemption, so it is recorded in the audit journal exactly like every other
refusal — with the caller, the ``request_id`` and the ``correlation_id``.

What must stay different is the *kind* of the event: a transport-level refusal
(``422``) is not an authorization answer. No decision was made, so none is
reported, and a consumer cannot confuse "your question was unreadable" with
"the answer is no".
"""

from __future__ import annotations

import pytest

from authorization_service.contracts import Decision, Reason, ResourceRef
from authorization_service.engine import DECISION_ACTION
from authorization_service.errors import CallerDenyReason, MalformedDecisionRequest
from tests.conftest import DATA_OWNER_CREDENTIAL, monolith

VALID_RESOURCE = {"resource_type": "record", "resource_id": "rec_a1", "tenant_id": "ten_a"}
RESOURCE = ResourceRef("record", "rec_a1", "ten_a")

#: The three ways the published schema can reject a decision request.
MALFORMED_BODIES = {
    "missing_required_field": {"resource": VALID_RESOURCE},
    "forbidden_extra_field": {
        "operation": "records.read",
        "resource": VALID_RESOURCE,
        "subject_id": "idn_human_a",
    },
    "wrong_type": {"operation": ["records.read"], "resource": VALID_RESOURCE},
}


def headers(**extra: str) -> dict[str, str]:
    base = {
        "Authorization": f"Bearer {DATA_OWNER_CREDENTIAL}",
        "X-Subject-Authorization": "Bearer token-human-a",
    }
    base.update(extra)
    return base


def refusals(instance) -> list:
    return [
        event
        for event in instance.authorization.store.audit
        if event.details.get("refused") == CallerDenyReason.MALFORMED_REQUEST.value
    ]


@pytest.mark.parametrize("case", sorted(MALFORMED_BODIES))
def test_a_request_the_schema_rejects_is_refused_and_audited(case):
    instance = monolith()
    http = instance.authorization_http()

    response = http.post(
        "/api/v1/decisions",
        headers=headers(**{"X-Request-Id": f"req-{case}", "X-Correlation-Id": f"cor-{case}"}),
        json=MALFORMED_BODIES[case],
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {
        "decision": "DENY",
        "reason": "malformed_request",
        "request_id": f"req-{case}",
    }

    recorded = refusals(instance)
    assert len(recorded) == 1, case
    event = recorded[0]
    assert event.action == DECISION_ACTION
    assert event.decision is Decision.DENY
    assert event.request_id == f"req-{case}"
    assert event.correlation_id == f"cor-{case}"
    assert event.details["refused"] == "malformed_request"
    assert event.details["path"] == "/api/v1/decisions"
    # The caller is attributed: an unreadable request is a defect of a component
    # that has a name.
    assert event.actor_id == "svc_records"
    assert event.platform_id == instance.authorization.current_platform_id
    # No decision was made, so nothing decision-like is recorded.
    assert event.reason is None
    assert event.subject_id is None
    assert event.tenant_id is None
    assert event.operation is None


@pytest.mark.parametrize(
    "case, expected_problem",
    [
        ("missing_required_field", "body.operation: missing"),
        ("forbidden_extra_field", "body.subject_id: extra_forbidden"),
        ("wrong_type", "body.operation: string_type"),
    ],
)
def test_the_journal_states_what_was_unreadable(case, expected_problem):
    instance = monolith()
    instance.authorization_http().post(
        "/api/v1/decisions", headers=headers(), json=MALFORMED_BODIES[case]
    )
    problems = refusals(instance)[0].details["schema_problems"]
    assert expected_problem in problems


def test_the_journal_records_the_problem_and_not_the_payload():
    """An audit journal is not a copy of what a consumer sent."""
    instance = monolith()
    instance.authorization_http().post(
        "/api/v1/decisions",
        headers=headers(),
        json={
            "operation": "records.read",
            "resource": VALID_RESOURCE,
            "note": "secret-value-42",
        },
    )
    event = refusals(instance)[0]
    assert "body.note: extra_forbidden" in event.details["schema_problems"]
    assert "secret-value-42" not in repr(event.details)


def test_an_unreadable_body_is_refused_and_audited_too():
    instance = monolith()
    response = instance.authorization_http().post(
        "/api/v1/decisions",
        headers={**headers(), "Content-Type": "application/json"},
        content=b"{not json at all",
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "malformed_request"
    assert len(refusals(instance)) == 1


def test_a_refusal_without_a_request_context_still_gets_one():
    """A caller that sent no ids is still traceable to its own record."""
    instance = monolith()
    response = instance.authorization_http().post(
        "/api/v1/decisions", headers=headers(), json={"resource": VALID_RESOURCE}
    )
    event = refusals(instance)[0]
    assert event.request_id and event.correlation_id == event.request_id
    # The generated id is handed back, so the consumer can point at the record.
    assert response.json()["detail"]["request_id"] == event.request_id


def test_an_unidentified_caller_sending_garbage_is_audited_as_unknown():
    instance = monolith()
    response = instance.authorization_http().post(
        "/api/v1/decisions",
        headers={"X-Request-Id": "req-anonymous"},
        json={"resource": VALID_RESOURCE},
    )
    assert response.status_code == 422
    event = refusals(instance)[0]
    assert event.actor_id is None
    assert event.request_id == "req-anonymous"


def test_a_caller_of_another_platform_instance_is_not_attributed_to_this_one():
    instance = monolith()
    instance.authorization_http().post(
        "/api/v1/decisions",
        headers={"Authorization": "Bearer authz-svc-token-foreign", "X-Request-Id": "req-foreign"},
        json={"resource": VALID_RESOURCE},
    )
    event = refusals(instance)[0]
    assert event.actor_id is None
    assert event.request_id == "req-foreign"


def test_the_published_client_sees_a_refusal_and_never_a_decision():
    """Transport-level refusal and authorization DENY stay different facts."""
    instance = monolith()
    engine = instance.authorization.engine

    with pytest.raises(MalformedDecisionRequest) as exc:
        # An empty operation is refused by the engine itself; the schema refuses
        # earlier cases — both are refusals, neither is an answer.
        instance.authorization_client.decide(
            "token-human-a", operation="   ", resource=RESOURCE
        )
    assert exc.value.reason is CallerDenyReason.MALFORMED_REQUEST
    assert set(exc.value.details) == {"decision", "reason", "request_id"}

    denied = instance.authorization_client.decide(
        "token-human-b", operation="records.read", resource=RESOURCE
    )
    assert denied.decision is Decision.DENY
    assert denied.reason is Reason.RESOURCE_TENANT_MISMATCH

    journal = instance.authorization.store.audit
    refused_event = next(event for event in journal if event.details.get("refused"))
    answered_event = next(
        event for event in journal if event.reason is Reason.RESOURCE_TENANT_MISMATCH
    )
    # A refusal states no reason code of the decision vocabulary; an answer does
    # and states no refusal.
    assert refused_event.reason is None
    assert "refused" not in answered_event.details
    assert engine.store.audit is journal


def test_every_refusal_of_the_contract_reaches_the_journal():
    """401, 403 and 422 alike: no refusal of this component is silent."""
    instance = monolith()
    http = instance.authorization_http()
    valid_body = {"operation": "records.read", "resource": VALID_RESOURCE}

    cases = [
        ({}, valid_body, 401, "missing_service_identity"),
        ({"Authorization": "Bearer nope"}, valid_body, 401, "invalid_service_identity"),
        (
            {"Authorization": "Bearer authz-svc-token-unknown"},
            valid_body,
            401,
            "unknown_service",
        ),
        (
            {"Authorization": "Bearer authz-svc-token-reporting"},
            valid_body,
            403,
            "insufficient_authorization",
        ),
        (
            {"Authorization": "Bearer authz-svc-token-foreign"},
            valid_body,
            403,
            "platform_mismatch",
        ),
        (
            {"Authorization": f"Bearer {DATA_OWNER_CREDENTIAL}"},
            {"resource": VALID_RESOURCE},
            422,
            "malformed_request",
        ),
    ]

    for index, (extra_headers, body, status, reason) in enumerate(cases):
        request_id = f"req-refusal-{index}"
        response = http.post(
            "/api/v1/decisions",
            headers={**extra_headers, "X-Request-Id": request_id, "X-Correlation-Id": "cor-all"},
            json=body,
        )
        assert response.status_code == status, reason
        assert response.json()["detail"]["reason"] == reason
        assert response.json()["detail"]["request_id"] == request_id

        event = next(
            item for item in instance.authorization.store.audit if item.request_id == request_id
        )
        assert event.decision is Decision.DENY
        assert event.details["refused"] == reason
        assert event.correlation_id == "cor-all"
