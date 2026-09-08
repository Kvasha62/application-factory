"""Acceptance proof A — authorized access (IS-004, Issue #14).

Identity A → effective Tenant A → IS-003 ALLOW → Resource A → success.

The proof runs against the fully composed Platform Instance (IS-001 + IS-002 +
IS-003 + IS-004): nothing is stubbed, the ALLOW travels through the real
decision chain, and the resource is served by its owner. Every assertion is
about observable behavior: response payloads, store state changes, audit
entries.
"""

from __future__ import annotations

from tests.conftest import monolith

TOKEN_A = "token-human-a"
TOKEN_B = "token-human-b"


def test_identity_a_reads_its_own_tenant_resource():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_a1",
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 200
    payload = response.json()
    # The whole published resource and nothing more than its five fields.
    assert payload == {
        "resource_id": "rec_a1",
        "resource_type": "record",
        "owner_component": "records",
        "tenant_id": "ten_a",
        "state": "active",
    }


def test_identity_b_reads_its_own_tenant_resource():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_b1",
        headers={"authorization": f"Bearer {TOKEN_B}"},
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "ten_b"
    assert response.json()["resource_id"] == "rec_b1"


def test_identity_a_transitions_its_own_resource():
    instance = monolith()
    client = instance.records_http()
    assert instance.records.store.ownership_of("rec_a2").state == "draft"

    response = client.post(
        "/api/v1/resources/rec_a2/transitions",
        json={"transition": "activate"},
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "active"
    # The owned state actually changed — behavior, not a promise.
    assert instance.records.store.ownership_of("rec_a2").state == "active"

    # The state machine continues: active -> archived.
    response = client.post(
        "/api/v1/resources/rec_a2/transitions",
        json={"transition": "archive"},
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "archived"
    assert instance.records.store.ownership_of("rec_a2").state == "archived"


def test_the_same_access_is_served_through_the_published_client():
    """The Level 0 consumer surface executes the identical contract."""
    instance = monolith()
    consumer = instance.records_client()

    view = consumer.read_resource(TOKEN_A, "rec_a1")

    assert view.resource_id == "rec_a1"
    assert view.tenant_id == "ten_a"
    assert view.owner_component == "records"
    assert view.state == "active"

    changed = consumer.transition_resource(TOKEN_A, "rec_a2", "activate")
    assert changed.state == "active"
    assert instance.records.store.ownership_of("rec_a2").state == "active"


def test_allowed_access_is_audited_with_the_caller_request_context():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_a1",
        headers={
            "authorization": f"Bearer {TOKEN_A}",
            "x-request-id": "req-proof-a",
            "x-correlation-id": "corr-proof-a",
        },
    )
    assert response.status_code == 200

    audit = instance.records.store.audit
    assert len(audit) == 1
    event = audit[0]
    assert event.action == "records.read"
    assert event.decision == "ALLOW"
    assert event.reason == "permitted"
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_id == "rec_a1"
    assert event.resource_tenant_id == "ten_a"
    assert event.request_id == "req-proof-a"
    assert event.correlation_id == "corr-proof-a"


def test_generated_request_ids_stay_traceable_when_the_caller_sends_none():
    instance = monolith()
    client = instance.records_http()

    response = client.get(
        "/api/v1/resources/rec_a1",
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )
    assert response.status_code == 200

    event = instance.records.store.audit[0]
    # The boundary generated a request context of its own: the access is
    # traceable even when the caller supplied no ids.
    assert event.request_id
    assert event.correlation_id


def test_read_only_subject_can_read_but_not_write():
    """`records.read` granted without `records.write` is exactly that."""
    instance = monolith()
    client = instance.records_http()

    read = client.get(
        "/api/v1/resources/rec_a1",
        headers={"authorization": "Bearer token-human-c"},
    )
    assert read.status_code == 200

    write = client.post(
        "/api/v1/resources/rec_a2/transitions",
        json={"transition": "activate"},
        headers={"authorization": "Bearer token-human-c"},
    )
    assert write.status_code == 403
    assert write.json()["detail"]["reason"] == "permission_not_granted"
    assert instance.records.store.ownership_of("rec_a2").state == "draft"
