"""Acceptance proof C — authorization enforcement (IS-004, Issue #14).

IS-003 DENY → the owned-data operation is not invoked.

"Invoked" is proven behaviorally: the store under test counts exactly the two
owned-data operations (serving a resource, applying a transition), and a
denied access must leave both counters at zero — in addition to raising the
refusal and changing no state. The same fact is shown against the fully
composed Platform Instance and against a stub port, so the enforcement
property does not depend on the provider's implementation.
"""

from __future__ import annotations

import pytest

from records_service.consumed import DENY_REASONS, DecisionAnswer
from records_service.contracts import OwnDenyReason
from records_service.errors import AccessRefused
from tests.conftest import (
    CountingRecordsStore,
    StubAuthorizationPort,
    monolith,
    records_deployment,
)

TOKEN_A = "token-human-a"


def deny(reason: str) -> DecisionAnswer:
    return DecisionAnswer(
        decision="DENY",
        reason=reason,
        subject_id="idn_human_a",
        tenant_id="ten_a",
    )


@pytest.mark.parametrize("reason", sorted(DENY_REASONS))
def test_every_published_deny_keeps_the_owned_data_untouched(reason):
    store = CountingRecordsStore()
    deployment = records_deployment(StubAuthorizationPort(deny(reason)), store=store)
    engine = deployment.engine

    with pytest.raises(AccessRefused) as refused:
        engine.read_resource(TOKEN_A, "rec_a1")

    assert refused.value.reason == reason  # the authority's fact, passed through
    assert store.served_reads == 0
    assert store.applied_transitions == 0

    with pytest.raises(AccessRefused):
        engine.transition_resource(TOKEN_A, "rec_a2", "activate")

    assert store.served_reads == 0
    assert store.applied_transitions == 0
    assert store.ownership_of("rec_a2").state == "draft"


def test_an_allow_invokes_the_owned_data_operation_exactly_once():
    store = CountingRecordsStore()
    allow = DecisionAnswer(
        decision="ALLOW",
        reason="permitted",
        subject_id="idn_human_a",
        tenant_id="ten_a",
    )
    deployment = records_deployment(StubAuthorizationPort(allow), store=store)
    engine = deployment.engine

    view, _, _ = engine.read_resource(TOKEN_A, "rec_a1")
    assert view.resource_id == "rec_a1"
    assert store.served_reads == 1

    engine.transition_resource(TOKEN_A, "rec_a2", "activate")
    assert store.applied_transitions == 1
    assert store.ownership_of("rec_a2").state == "active"


def test_denied_write_never_reaches_the_state_machine_in_the_monolith():
    """`idn_service_jobs` holds `records.read` only — its write must bounce."""
    instance = monolith()
    client = instance.records_http()
    assert instance.records.store.ownership_of("rec_a2").state == "draft"

    response = client.post(
        "/api/v1/resources/rec_a2/transitions",
        json={"transition": "activate"},
        headers={"authorization": "Bearer token-service"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "permission_not_granted"
    assert instance.records.store.ownership_of("rec_a2").state == "draft"

    # The audit journal shows the attempt never became an operation: the
    # refusal is recorded, and no ALLOW exists for it.
    event = instance.records.store.audit[-1]
    assert event.decision == "DENY"
    assert event.reason == "permission_not_granted"
    assert event.action == "records.write"
    assert not any(
        e.action == "records.write" and e.decision == "ALLOW"
        for e in instance.records.store.audit
    )


def test_denial_order_is_not_bypassed_by_a_grant_in_an_unservable_tenant():
    """A grant in a suspended Tenant compensates nothing: the lifecycle denial
    stands and the owned data stays untouched."""
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_s1",
        headers={"authorization": f"Bearer {TOKEN_A}", "x-tenant-id": "ten_suspended"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "tenant_suspended"
    assert instance.records.store.ownership_of("rec_s1").state == "active"


def test_unknown_resource_is_denied_without_asking_the_authority():
    """The owner asks IS-003 no questions about resources it does not own."""
    port = StubAuthorizationPort(deny("permission_not_granted"))
    store = CountingRecordsStore()
    deployment = records_deployment(port, store=store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_does_not_exist")

    assert refused.value.reason == OwnDenyReason.RESOURCE_UNKNOWN
    assert refused.value.status_code == 404
    assert port.calls == []  # the authority was never asked
    assert store.served_reads == 0


def test_domain_refusal_after_allow_still_never_writes_invalid_state():
    """An allowed write that asks an inapplicable transition is a domain
    refusal: the owner enforces its state machine itself."""
    store = CountingRecordsStore()
    allow = DecisionAnswer(
        decision="ALLOW",
        reason="permitted",
        subject_id="idn_human_a",
        tenant_id="ten_a",
    )
    deployment = records_deployment(StubAuthorizationPort(allow), store=store)
    engine = deployment.engine

    with pytest.raises(AccessRefused) as refused:
        engine.transition_resource(TOKEN_A, "rec_a1", "activate")  # active ≠ draft
    assert refused.value.reason == OwnDenyReason.INVALID_TRANSITION
    assert refused.value.status_code == 409
    assert store.applied_transitions == 0
    assert store.ownership_of("rec_a1").state == "active"

    with pytest.raises(AccessRefused) as refused:
        engine.transition_resource(TOKEN_A, "rec_a1", "levitate")  # unknown op
    assert refused.value.reason == OwnDenyReason.INVALID_TRANSITION
    assert store.applied_transitions == 0
