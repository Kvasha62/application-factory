"""Invariant 7 — security-sensitive refusals are auditable and observable
(IS-004, Issue #14).

Every access attempt — served, denied by the authority, denied by the
ownership boundary, failed closed against a silent dependency, or unreadable
by the contract schema — leaves exactly one audit record carrying the
request/correlation context and the actor/tenant context where known. The
journal is append-only and is not readable through any published route.
"""

from __future__ import annotations

import json

import pytest

from records_service.consumed import DependencyRefusal
from records_service.errors import AccessRefused
from tests.conftest import (
    CountingRecordsStore,
    StubAuthorizationPort,
    monolith,
    records_deployment,
)

TOKEN_A = "token-human-a"


def test_each_attempt_produces_exactly_one_record():
    instance = monolith()
    client = instance.records_http()
    headers = {"authorization": f"Bearer {TOKEN_A}"}

    client.get("/api/v1/resources/rec_a1", headers=headers)  # ALLOW
    client.get("/api/v1/resources/rec_b1", headers=headers)  # DENY
    client.get("/api/v1/resources/missing", headers=headers)  # unknown
    client.get("/api/v1/resources/rec_a1")  # no identity
    client.post(  # unreadable
        "/api/v1/resources/rec_a2/transitions",
        json={"transition": "activate", "extra": 1},
        headers=headers,
    )

    audit = instance.records.store.audit
    assert len(audit) == 5
    assert [event.decision for event in audit] == [
        "ALLOW",
        "DENY",
        "DENY",
        "DENY",
        "DENY",
    ]
    assert [event.reason for event in audit] == [
        "permitted",
        "resource_tenant_mismatch",
        "resource_unknown",
        "missing_identity",
        "malformed_request",
    ]


def test_denial_records_carry_request_and_correlation_context():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_b1",
        headers={
            "authorization": f"Bearer {TOKEN_A}",
            "x-request-id": "req-aud-1",
            "x-correlation-id": "corr-aud-1",
        },
    )
    assert response.status_code == 403

    event = instance.records.store.audit[-1]
    assert event.request_id == "req-aud-1"
    assert event.correlation_id == "corr-aud-1"
    assert event.platform_id == "plt_demo"


def test_denial_records_carry_actor_and_tenant_context_where_known():
    instance = monolith()
    client = instance.records_http()

    # Known subject: the decision stated it.
    client.get(
        "/api/v1/resources/rec_b1", headers={"authorization": f"Bearer {TOKEN_A}"}
    )
    event = instance.records.store.audit[-1]
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"

    # No credential: the subject is genuinely unknown — recorded as such,
    # not invented.
    client.get("/api/v1/resources/rec_a1")
    event = instance.records.store.audit[-1]
    assert event.subject_id is None
    assert event.tenant_id is None


def test_the_caller_claim_is_audited_separately_from_the_resource_fact():
    instance = monolith()
    client = instance.records_http()

    client.get(
        "/api/v1/resources/rec_a1",
        headers={"authorization": f"Bearer {TOKEN_A}", "x-tenant-id": "ten_b"},
    )

    event = instance.records.store.audit[-1]
    assert event.decision == "DENY"
    assert event.reason == "tenant_mismatch"
    assert event.claimed_tenant_id == "ten_b"  # the caller's statement
    assert event.resource_tenant_id == "ten_a"  # the store's fact


def test_fail_closed_attempts_are_audited():
    silent = StubAuthorizationPort(DependencyRefusal(None))
    store = CountingRecordsStore()
    deployment = records_deployment(silent, store=store)
    consumer = deployment.publish()

    with pytest.raises(AccessRefused):
        consumer.read_resource(
            TOKEN_A, "rec_a1", request_id="req-df", correlation_id="corr-df"
        )

    event = store.audit[-1]
    assert event.decision == "DENY"
    assert event.reason == "authorization_unavailable"
    assert event.resource_id == "rec_a1"
    assert event.resource_tenant_id == "ten_a"
    assert event.request_id == "req-df"
    assert event.correlation_id == "corr-df"


def test_unreadable_requests_are_audited_without_their_payload_values():
    instance = monolith()
    client = instance.records_http()
    marker = "secret-payload-value"

    response = client.post(
        "/api/v1/resources/rec_a2/transitions",
        json={"transition": [marker]},  # a list where the schema demands a string
        headers={
            "authorization": f"Bearer {TOKEN_A}",
            "x-request-id": "req-unreadable",
        },
    )
    assert response.status_code == 422

    event = instance.records.store.audit[-1]
    assert event.reason == "malformed_request"
    assert event.action == "records.access"
    assert event.request_id == "req-unreadable"
    assert event.details.get("refused") == "malformed_request"
    assert event.details.get("schema_problems"), "the kind of problem is recorded"
    # The value never reaches the journal — only the location/kind of it.
    assert marker not in json.dumps(event.details)

    # An extra field is equally unreadable — and the value of the extra field
    # stays out of the journal exactly like the value of a declared one:
    response = client.post(
        "/api/v1/resources/rec_a2/transitions",
        json={"transition": "activate", "undeclared": marker},
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )
    assert response.status_code == 422
    event = instance.records.store.audit[-1]
    assert marker not in json.dumps(event.details)


def test_the_audit_journal_is_not_readable_through_any_published_route():
    instance = monolith()
    client = instance.records_http()
    headers = {"authorization": f"Bearer {TOKEN_A}"}
    client.get("/api/v1/resources/rec_a1", headers=headers)
    client.get("/api/v1/resources/rec_b1", headers=headers)
    event_ids = {event.event_id for event in instance.records.store.audit}
    assert event_ids

    openapi = client.get("/openapi.json").json()
    for path, item in openapi["paths"].items():
        for method in item:
            if method not in {"get", "post"}:
                continue
            if method == "get":
                response = client.get(
                    path.replace("{resource_id}", "rec_a1"), headers=headers
                )
            else:
                response = client.post(
                    path.replace("{resource_id}", "rec_a1"),
                    json={"transition": "activate"},
                    headers=headers,
                )
            body = response.text
            assert not any(event_id in body for event_id in event_ids), (method, path)


def test_the_journal_is_append_only_over_a_session():
    instance = monolith()
    client = instance.records_http()
    headers = {"authorization": f"Bearer {TOKEN_A}"}

    seen: list[str] = []
    for resource_id in ("rec_a1", "rec_b1", "rec_a1"):
        client.get(f"/api/v1/resources/{resource_id}", headers=headers)
        audit = instance.records.store.audit
        ids = [event.event_id for event in audit]
        assert ids[: len(seen)] == seen  # nothing rewritten, nothing removed
        seen = ids
    assert len(seen) == 3
