"""Acceptance proof D — dependency failure (IS-004, Issue #14).

Authorization unavailable/invalid → DENY → the owned-data operation is not
invoked.

"Unavailable" and "invalid" are separate attacks with one required outcome:

* the dependency raises anything at all — a transport fault, a published
  refusal, an internal crash — the access fails closed;
* the dependency answers, but outside its published contract — an unknown
  decision value, an ALLOW with the wrong reason, a DENY with an unknown
  reason, an answer missing its shape — the answer is non-authoritative and
  fails closed; a non-authoritative answer pointing in the permissive
  direction is the dangerous case, and it must deny exactly like the rest.

Every case asserts the refusal, the untouched owned data and the audit trail.
"""

from __future__ import annotations

import pytest

from records_service.adapters import authorization_port
from records_service.consumed import DecisionAnswer
from records_service.contracts import OwnDenyReason
from records_service.errors import AccessRefused, ContractViolation
from tests.conftest import (
    DATA_OWNER_CREDENTIAL,
    CountingRecordsStore,
    StubAuthorizationPort,
    monolith,
    records_deployment,
)

TOKEN_A = "token-human-a"

UNAVAILABLE = OwnDenyReason.AUTHORIZATION_UNAVAILABLE


def assert_failed_closed(refused, store) -> None:
    assert refused.value.reason == UNAVAILABLE
    assert refused.value.status_code == 503
    assert store.served_reads == 0
    assert store.applied_transitions == 0


def test_a_dependency_that_raises_fails_closed():
    store = CountingRecordsStore()
    port = StubAuthorizationPort(RuntimeError("boom"))
    deployment = records_deployment(port, store=store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_a1")
    assert_failed_closed(refused, store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.transition_resource(TOKEN_A, "rec_a2", "activate")
    assert_failed_closed(refused, store)
    assert store.ownership_of("rec_a2").state == "draft"


def test_a_dependency_refusal_with_a_stated_code_is_recorded():
    class PublishedRefusal(Exception):
        def __init__(self) -> None:
            self.reason = "insufficient_authorization"
            super().__init__("refused")

    store = CountingRecordsStore()
    deployment = records_deployment(StubAuthorizationPort(PublishedRefusal()), store=store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_a1")

    assert_failed_closed(refused, store)
    # The stated code is audit context — the outcome is still the component's
    # own fail-closed reason, not the dependency's vocabulary adopted as fact.
    event = store.audit[-1]
    assert event.reason == UNAVAILABLE
    assert event.details.get("stated_code") == "insufficient_authorization"


@pytest.mark.parametrize(
    "answer",
    [
        DecisionAnswer(decision="MAYBE", reason="permitted"),
        DecisionAnswer(decision="ALLOW", reason="permitted_and_more"),
        DecisionAnswer(decision="ALLOW", reason=""),
        DecisionAnswer(decision="DENY", reason="permitted"),  # contradiction
        DecisionAnswer(decision="DENY", reason="trust_me"),  # unknown reason
        DecisionAnswer(decision="", reason=""),
    ],
)
def test_a_non_authoritative_answer_fails_closed_including_permissive_ones(answer):
    store = CountingRecordsStore()
    deployment = records_deployment(StubAuthorizationPort(answer), store=store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_a1")

    assert_failed_closed(refused, store)
    event = store.audit[-1]
    assert event.reason == UNAVAILABLE
    assert event.details.get("nonauthoritative_answer") == {
        "decision": answer.decision,
        "reason": answer.reason,
    }


def test_the_adapter_turns_provider_exceptions_into_refusals():
    """Through the real adapter, a raising client never becomes an ALLOW."""
    class RaisingClient:
        def decide(self, *args, **kwargs):
            raise ContractViolation("the authorization contract channel is closed")

    store = CountingRecordsStore()
    deployment = records_deployment(authorization_port(RaisingClient()), store=store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_a1")
    assert_failed_closed(refused, store)


def test_the_adapter_rejects_answers_outside_the_documented_shape():
    class GarbageAnswer:  # has attributes, but not the documented ones
        decision = object()
        reason = None

    class GarbageClient:
        def decide(self, *args, **kwargs):
            return GarbageAnswer()

    store = CountingRecordsStore()
    deployment = records_deployment(authorization_port(GarbageClient()), store=store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_a1")
    assert_failed_closed(refused, store)


def test_a_closed_decision_channel_fails_closed_end_to_end():
    """Real composition: the IS-003 channel under the records component is
    revoked — the data owner must deny, not fail open and not crash."""
    from authorization_service.transport import close_channel

    instance = monolith()
    orphaned = instance.authorization.publish(credential=DATA_OWNER_CREDENTIAL)
    close_channel(orphaned._channel)
    instance.records.engine.authorization = authorization_port(orphaned)

    client = instance.records_http()
    response = client.get(
        "/api/v1/resources/rec_a1",
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["decision"] == "DENY"
    assert detail["reason"] == UNAVAILABLE
    # The audit trail exists even though the authority never answered.
    event = instance.records.store.audit[-1]
    assert event.reason == UNAVAILABLE
    assert event.resource_id == "rec_a1"


def test_dependency_failures_are_observable_through_the_published_client():
    store = CountingRecordsStore()
    deployment = records_deployment(StubAuthorizationPort(RuntimeError("boom")), store=store)
    consumer = deployment.publish()

    with pytest.raises(AccessRefused) as refused:
        consumer.read_resource(TOKEN_A, "rec_a1")
    assert refused.value.reason == UNAVAILABLE
    assert refused.value.status_code == 503
    assert store.served_reads == 0
