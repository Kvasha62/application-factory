"""Audit and observability of the Authorization Boundary (Issue #12, A-009).

A security-sensitive denial that leaves no trace is not a boundary. These tests
check the journal of live decisions: every outcome is recorded, with the caller,
the subject, the tenant, the resource, the operation and the request/correlation
pair that produced it — and the same pair travels to IS-001 and IS-002, so one
decision can be followed across the three journals of the Platform Instance.
"""

from __future__ import annotations

import pytest

from authorization_service import COMPONENT_ID, COMPONENT_VERSION
from authorization_service.contracts import Decision, Reason, ResourceRef
from authorization_service.errors import CallerNotAuthenticated, CallerNotAuthorized
from tests.conftest import monolith


def last(instance):
    return instance.authorization.store.audit[-1]


def test_every_decision_is_audited_with_caller_subject_tenant_and_resource():
    instance = monolith()
    instance.authorization_client.decide(
        "token-human-a",
        operation="records.read",
        resource=ResourceRef("record", "rec_a1", "ten_a"),
        request_id="req-allow-1",
        correlation_id="cor-allow-1",
    )
    event = last(instance)
    assert event.action == "authorization.decide"
    assert event.decision is Decision.ALLOW
    assert event.reason is Reason.PERMITTED
    assert event.actor_id == "svc_records"  # the component that asked
    assert event.subject_id == "idn_human_a"  # the subject it asked about
    assert event.tenant_id == "ten_a"
    assert event.operation == "records.read"
    assert (event.resource_type, event.resource_id, event.resource_tenant_id) == (
        "record",
        "rec_a1",
        "ten_a",
    )
    assert (event.request_id, event.correlation_id) == ("req-allow-1", "cor-allow-1")
    assert event.platform_id == instance.authorization.current_platform_id


@pytest.mark.parametrize(
    "credential, resource, reason",
    [
        (None, ResourceRef("record", "rec_a1", "ten_a"), Reason.MISSING_IDENTITY),
        ("token-invalid", ResourceRef("record", "rec_a1", "ten_a"), Reason.INVALID_IDENTITY),
        (
            "token-human-b",
            ResourceRef("record", "rec_a1", "ten_a"),
            Reason.RESOURCE_TENANT_MISMATCH,
        ),
        ("token-human-c", ResourceRef("record", "rec_a1", "ten_a"), None),
    ],
)
def test_a_denial_is_recorded_with_its_reason_and_request_context(credential, resource, reason):
    instance = monolith()
    operation = "records.write" if reason is None else "records.read"
    answer = instance.authorization_client.decide(
        credential,
        operation=operation,
        resource=resource,
        request_id="req-deny",
        correlation_id="cor-deny",
    )
    assert answer.decision is Decision.DENY
    event = last(instance)
    assert event.decision is Decision.DENY
    assert event.reason is answer.reason
    assert event.reason is (reason or Reason.PERMISSION_NOT_GRANTED)
    assert (event.request_id, event.correlation_id) == ("req-deny", "cor-deny")
    # The decision a consumer holds points at the audited record.
    assert (answer.request_id, answer.correlation_id) == ("req-deny", "cor-deny")


def test_a_refused_question_is_audited_too():
    """Not answering is also security-relevant: the attempt is recorded."""
    instance = monolith()
    app = instance.authorization.contract_app()
    from authorization_service.reader import build_client

    unauthenticated = build_client(app, credential="authz-svc-token-nope")
    with pytest.raises(CallerNotAuthenticated):
        unauthenticated.decide(
            "token-human-a",
            operation="records.read",
            resource=ResourceRef("record", "rec_a1", "ten_a"),
            request_id="req-refused",
        )
    event = last(instance)
    assert event.decision is Decision.DENY
    assert event.reason is None  # no decision was made about the subject
    assert event.details["refused"] == "invalid_service_identity"
    assert event.request_id == "req-refused"
    assert event.actor_id is None
    assert event.subject_id is None

    not_allowed = build_client(app, credential="authz-svc-token-reporting")
    with pytest.raises(CallerNotAuthorized):
        not_allowed.decide(
            "token-human-a",
            operation="records.read",
            resource=ResourceRef("record", "rec_a1", "ten_a"),
        )
    refusal = last(instance)
    assert refusal.details["refused"] == "insufficient_authorization"
    assert refusal.actor_id == "svc_reporting"


def test_the_request_and_correlation_ids_reach_the_journals_of_both_authorities():
    """One decision, one correlation id, three journals (ARCHITECTURE.md §26)."""
    instance = monolith()
    instance.authorization_client.decide(
        "token-human-a",
        operation="records.read",
        resource=ResourceRef("record", "rec_a1", "ten_a"),
        request_id="req-traced",
        correlation_id="cor-traced",
    )
    assert any(
        event.request_id == "req-traced" and event.correlation_id == "cor-traced"
        for event in instance.authorization.store.audit
    )
    assert any(
        event.request_id == "req-traced" and event.correlation_id == "cor-traced"
        for event in instance.identity.store.audit
    ), "the identity context read must carry the caller's request context"
    assert any(
        event.request_id == "req-traced" and event.correlation_id == "cor-traced"
        for event in instance.authority.store.audit
    ), "the lifecycle read must carry the caller's request context"


def test_absent_request_headers_produce_a_bound_pair_not_an_empty_one():
    instance = monolith()
    answer = instance.authorization_client.decide(
        "token-human-a", operation="records.read", resource=ResourceRef("record", "rec_a1", "ten_a")
    )
    event = last(instance)
    assert answer.request_id and answer.correlation_id
    assert answer.request_id == answer.correlation_id  # generated pair, still bound
    assert (event.request_id, event.correlation_id) == (
        answer.request_id,
        answer.correlation_id,
    )


def test_the_observability_context_is_the_standard_one():
    instance = monolith()
    engine = instance.authorization.engine
    _, obs, _ = engine.decide(
        "authz-svc-token-records",
        operation="records.read",
        resource=ResourceRef("record", "rec_a1", "ten_a"),
        subject_credential="token-human-a",
        request_id="req-obs",
        correlation_id="cor-obs",
    )
    assert obs.component_id == COMPONENT_ID
    assert obs.component_version == COMPONENT_VERSION
    assert obs.environment == "test"
    assert obs.platform_id == instance.authorization.current_platform_id
    assert (obs.request_id, obs.correlation_id, obs.trace_id) == ("req-obs", "cor-obs", "req-obs")
    assert obs.tenant_id == "ten_a"
    assert obs.subject_id == "idn_human_a"
    assert obs.service_id == "svc_records"
    assert obs.timestamp
    # `saga_id` is absent on purpose: this component runs no inter-component saga.
    assert not hasattr(obs, "saga_id")


def test_the_audit_journal_is_append_only_from_the_outside():
    """A consumer cannot read the journal, and a decision cannot erase one."""
    instance = monolith()
    before = len(instance.authorization.store.audit)
    instance.authorization_client.decide(
        "token-human-a", operation="records.read", resource=ResourceRef("record", "rec_a1", "ten_a")
    )
    instance.authorization_client.decide(
        "token-human-b", operation="records.read", resource=ResourceRef("record", "rec_a1", "ten_a")
    )
    assert len(instance.authorization.store.audit) == before + 2
    assert not hasattr(instance.authorization_client, "audit")
    published = {name for name in dir(instance.authorization_client) if not name.startswith("_")}
    assert published == {"decide"}
