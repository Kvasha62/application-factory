"""Acceptance proof B — tenant isolation (IS-004, Issue #14).

Identity A / Tenant A requesting a Tenant B resource → DENY. The isolation is
attacked through every knob a caller has: the ``resource_id``, a
caller-supplied ``tenant_id`` and the write path — and through both published
access routes (HTTP and the Level 0 client). The resource's tenant is always
the fact of the owner's store, never a statement of the caller (LAW-16a).
"""

from __future__ import annotations

import pytest

from records_service.errors import AccessRefused
from tests.conftest import monolith

TOKEN_A = "token-human-a"
TOKEN_B = "token-human-b"


def test_tenant_a_subject_cannot_read_tenant_b_resource_by_id():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_b1",
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["decision"] == "DENY"
    # The owner stated the resource's tenant from its own store; the decision
    # authority compared it with the subject's effective tenant.
    assert detail["reason"] == "resource_tenant_mismatch"


def test_swapping_resource_ids_gains_nothing():
    """A successful read of one's own resource grants no reach into another."""
    instance = monolith()
    client = instance.records_http()
    headers = {"authorization": f"Bearer {TOKEN_A}"}

    own = client.get("/api/v1/resources/rec_a1", headers=headers)
    assert own.status_code == 200

    for foreign in ("rec_b1", "rec_b2"):
        response = client.get(f"/api/v1/resources/{foreign}", headers=headers)
        assert response.status_code == 403, foreign
        assert response.json()["detail"]["reason"] == "resource_tenant_mismatch"


def test_caller_supplied_tenant_id_cannot_select_the_tenant():
    """A claimed tenant is a cross-check, never a source of tenant identity."""
    instance = monolith()
    client = instance.records_http()

    # Claiming one's own tenant changes nothing about a foreign resource.
    claimed_own = client.get(
        "/api/v1/resources/rec_b1",
        headers={"authorization": f"Bearer {TOKEN_A}", "x-tenant-id": "ten_a"},
    )
    assert claimed_own.status_code == 403
    assert claimed_own.json()["detail"]["reason"] == "resource_tenant_mismatch"

    # Claiming the resource's tenant does not adopt it: the claim mismatches
    # the subject's verified effective tenant and is rejected as such.
    claimed_foreign = client.get(
        "/api/v1/resources/rec_b1",
        headers={"authorization": f"Bearer {TOKEN_A}", "x-tenant-id": "ten_b"},
    )
    assert claimed_foreign.status_code == 403
    assert claimed_foreign.json()["detail"]["reason"] == "tenant_mismatch"

    # And claiming a tenant the subject has no association with at all:
    unbound = client.get(
        "/api/v1/resources/rec_a1",
        headers={"authorization": f"Bearer {TOKEN_A}", "x-tenant-id": "ten_b"},
    )
    assert unbound.status_code == 403
    assert unbound.json()["detail"]["reason"] == "tenant_mismatch"


def test_tenant_b_subject_cannot_read_tenant_a_resource():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_a1",
        headers={"authorization": f"Bearer {TOKEN_B}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "resource_tenant_mismatch"


def test_cross_tenant_write_is_denied_and_state_stays():
    instance = monolith()
    client = instance.records_http()
    assert instance.records.store.ownership_of("rec_b2").state == "draft"

    response = client.post(
        "/api/v1/resources/rec_b2/transitions",
        json={"transition": "activate"},
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "resource_tenant_mismatch"
    # Behavioral proof: nothing was written.
    assert instance.records.store.ownership_of("rec_b2").state == "draft"


def test_isolation_holds_through_the_published_client_too():
    instance = monolith()
    consumer = instance.records_client()

    with pytest.raises(AccessRefused) as refused:
        consumer.read_resource(TOKEN_A, "rec_b1")
    assert refused.value.reason == "resource_tenant_mismatch"
    assert refused.value.status_code == 403

    with pytest.raises(AccessRefused) as refused:
        consumer.transition_resource(TOKEN_A, "rec_b2", "activate")
    assert refused.value.reason == "resource_tenant_mismatch"
    assert instance.records.store.ownership_of("rec_b2").state == "draft"


def test_cross_tenant_denials_are_audited_with_the_decision_context():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_b1",
        headers={
            "authorization": f"Bearer {TOKEN_A}",
            "x-request-id": "req-iso-1",
            "x-correlation-id": "corr-iso-1",
        },
    )
    assert response.status_code == 403

    event = instance.records.store.audit[-1]
    assert event.decision == "DENY"
    assert event.reason == "resource_tenant_mismatch"
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_id == "rec_b1"
    # The audited resource tenant is the fact of the owner's store — not the
    # caller's claim, which was never supplied here.
    assert event.resource_tenant_id == "ten_b"
    assert event.request_id == "req-iso-1"
    assert event.correlation_id == "corr-iso-1"
