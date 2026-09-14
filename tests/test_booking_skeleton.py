"""SCS-003 Booking — construction and the Resource slice (Issue #57, ADR-0014).

Create and read one owned Resource against the fully composed Platform
Instance (IS-001 + IS-002 + IS-003 + Booking): construction, success,
tenant isolation, the approved error envelope, idempotent replay and
conflict, and audit. This module also defines the shared composition
helper used by the availability, reservation, concurrency, contract and
boundary tests: subjects A and B hold every Booking grant in their own
Tenants; subject C (Tenant A) holds every grant except
``booking.resources.create``, so both a grant denial and the same-tenant
booker isolation (``booker_mismatch``) are exercisable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from authorization_service.models import ServiceAccess
from authorization_service.store import CONSUMER_PERMISSIONS
from booking_service import COMPONENT_ID, COMPONENT_VERSION, OWNER_COMPONENT, SCS_ID
from booking_service.adapters import authorization_port, tenant_context_port
from booking_service.contracts import (
    OPERATION_AVAILABILITY_CREATE,
    OPERATION_AVAILABILITY_READ,
    OPERATION_RESERVATION_CANCEL,
    OPERATION_RESERVATION_CREATE,
    OPERATION_RESERVATION_READ,
    OPERATION_RESOURCE_CREATE,
    OPERATION_RESOURCE_READ,
    PUBLISHED_DENY_REASONS,
)
from booking_service.deployment import BookingDeployment
from booking_service.deployment import build_deployment as build_booking
from booking_service.errors import AccessRefused
from booking_service.store import BookingStore
from tests.conftest import PLATFORM_ID, monolith

SUBJECT_A = "token-human-a"  # idn_human_a / ten_a
SUBJECT_B = "token-human-b"  # idn_human_b / ten_b
SUBJECT_C = "token-human-c"  # idn_human_c / ten_a, no resource-create grant

BOOKING_CREDENTIAL = "authz-svc-token-booking"
BASE = "/api/v1/booking"

ALL_OPERATIONS = (
    OPERATION_RESOURCE_CREATE,
    OPERATION_RESOURCE_READ,
    OPERATION_AVAILABILITY_CREATE,
    OPERATION_AVAILABILITY_READ,
    OPERATION_RESERVATION_CREATE,
    OPERATION_RESERVATION_READ,
    OPERATION_RESERVATION_CANCEL,
)

#: A fixed, timezone-aware demo window on 2026-10-01 in UTC.
WINDOW_START = "2026-10-01T08:00:00+00:00"
WINDOW_END = "2026-10-01T18:00:00+00:00"


@dataclass
class BookingHarness:
    instance: Any
    booking: BookingDeployment

    def http(self) -> TestClient:
        return TestClient(self.booking.contract_app())

    def client(self) -> Any:
        return self.booking.publish()


def booking_harness(store: BookingStore | None = None) -> BookingHarness:
    """Compose IS-001/IS-002/IS-003 plus Booking for one test.

    Grants are the composition root's job (IS-003 publishes no grant API).
    """
    instance = monolith()
    instance.authorization.store.services["svc_booking"] = ServiceAccess(
        "svc_booking", PLATFORM_ID, CONSUMER_PERMISSIONS
    )
    instance.authorization.store.service_tokens[BOOKING_CREDENTIAL] = "svc_booking"
    for operation in ALL_OPERATIONS:
        instance.authorization.store.grant("ten_a", "idn_human_a", operation)
        instance.authorization.store.grant("ten_b", "idn_human_b", operation)
        if operation != OPERATION_RESOURCE_CREATE:
            instance.authorization.store.grant("ten_a", "idn_human_c", operation)
    booking = build_booking(
        {"platform_id": instance.authority.current_platform_id, "environment": "test"},
        authorization=authorization_port(
            instance.authorization.publish(credential=BOOKING_CREDENTIAL)
        ),
        tenant_context=tenant_context_port(instance.identity_context_client),
        store=store,
        seed_demo=store is None,
        with_http=True,
    )
    return BookingHarness(instance=instance, booking=booking)


def envelope_of(response: Any) -> dict:
    """The approved error envelope — and nothing else."""
    body = response.json()
    assert set(body) == {"error", "request_id", "correlation_id"}, body
    assert set(body["error"]) == {"code", "message", "details"}, body
    assert set(body["error"]["details"]) == {"reason"}, body
    assert body["request_id"] and body["correlation_id"]
    return body


def auth(token: str, key: str | None = None, **extra: str) -> dict[str, str]:
    headers = {"authorization": f"Bearer {token}", **extra}
    if key:
        headers["idempotency-key"] = key
    return headers


def create_resource(
    http: TestClient, key: str, *, token: str = SUBJECT_A, **payload: Any
) -> Any:
    body = {"name": "Room 1", **payload}
    return http.post(f"{BASE}/resources", headers=auth(token, key), json=body)


def create_availability(
    http: TestClient,
    resource_id: str,
    key: str,
    *,
    token: str = SUBJECT_A,
    start_at: str = WINDOW_START,
    end_at: str = WINDOW_END,
) -> Any:
    return http.post(
        f"{BASE}/resources/{resource_id}/availability",
        headers=auth(token, key),
        json={"start_at": start_at, "end_at": end_at},
    )


def create_reservation(
    http: TestClient,
    resource_id: str,
    key: str,
    *,
    token: str = SUBJECT_A,
    start_at: str,
    end_at: str,
    **extra: str,
) -> Any:
    return http.post(
        f"{BASE}/reservations",
        headers=auth(token, key, **extra),
        json={"resource_id": resource_id, "start_at": start_at, "end_at": end_at},
    )


def cancel_reservation(
    http: TestClient, reservation_id: str, key: str, *, token: str = SUBJECT_A
) -> Any:
    return http.post(
        f"{BASE}/reservations/{reservation_id}/cancel", headers=auth(token, key)
    )


def bookable_resource(harness: BookingHarness, tag: str, *, token: str = SUBJECT_A):
    """One ACTIVE Resource with the demo window declared; returns its id."""
    http = harness.http()
    created = create_resource(http, f"{tag}-res", token=token)
    assert created.status_code == 201, created.text
    resource_id = created.json()["resource_id"]
    window = create_availability(http, resource_id, f"{tag}-avl", token=token)
    assert window.status_code == 201, window.text
    return resource_id


# ------------------------------------------------------------- construction
def test_component_identity_is_stable():
    assert COMPONENT_ID == "booking"
    assert OWNER_COMPONENT == "booking"
    assert SCS_ID == "SCS-003"
    assert COMPONENT_VERSION == "0.1.0"


def test_health_and_readiness_answer():
    http = booking_harness().http()
    health = http.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "component_id": "booking",
        "version": "0.1.0",
        "platform_id": PLATFORM_ID,
    }
    ready = http.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "component_id": "booking"}


# ------------------------------------------------------------------ success
def test_create_then_read_one_owned_resource():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    created = create_resource(http, "skeleton-create-1")
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {
        "resource_id",
        "name",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert body["name"] == "Room 1"
    assert body["status"] == "ACTIVE"
    assert body["created_by"] == "idn_human_a"
    assert "tenant_id" not in body

    read = http.get(f"{BASE}/resources/{body['resource_id']}", headers=auth(SUBJECT_A))
    assert read.status_code == 200
    assert read.json() == body
    # The Tenant is a store fact derived from the verified identity.
    assert harness.booking.store.resources[body["resource_id"]].tenant_id == "ten_a"


def test_resource_can_be_declared_inactive_and_status_is_closed():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    inactive = create_resource(http, "skeleton-inactive", status="INACTIVE")
    assert inactive.status_code == 201
    assert inactive.json()["status"] == "INACTIVE"
    unknown = create_resource(http, "skeleton-unknown-status", status="ARCHIVED")
    assert unknown.status_code == 422
    assert envelope_of(unknown)["error"]["code"] == "VALIDATION_ERROR"
    assert len(harness.booking.store.resources) == 1


def test_published_client_creates_and_reads_values():
    harness = booking_harness(store=BookingStore())
    client = harness.client()
    created = client.create_resource(
        SUBJECT_A, name="Client room", idempotency_key="skeleton-client-1"
    )
    assert created.name == "Client room"
    assert created.status == "ACTIVE"
    assert client.read_resource(SUBJECT_A, created.resource_id) == created
    with pytest.raises(AccessRefused) as exc:
        client.read_resource(SUBJECT_A, "missing")
    assert exc.value.reason == "resource_not_found"


# --------------------------------------------------------------- idempotency
def test_replay_returns_the_recorded_resource_without_a_second_effect():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    first = create_resource(http, "skeleton-replay-1")
    second = create_resource(http, "skeleton-replay-1")
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(harness.booking.store.resources) == 1
    replays = [
        e for e in harness.booking.store.audit if e.action == "idempotency_replay"
    ]
    assert len(replays) == 1


def test_changed_binding_with_the_same_key_is_a_conflict():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    assert create_resource(http, "skeleton-conflict-1").status_code == 201
    second = create_resource(http, "skeleton-conflict-1", name="Another room")
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert body["error"]["details"]["reason"] == "idempotency_conflict"
    assert len(harness.booking.store.resources) == 1


def test_command_without_a_key_is_refused_before_any_effect():
    harness = booking_harness(store=BookingStore())
    response = harness.http().post(
        f"{BASE}/resources", headers=auth(SUBJECT_A), json={"name": "Room"}
    )
    assert response.status_code == 400
    assert envelope_of(response)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert harness.booking.store.resources == {}


# -------------------------------------------------------------------- refusal
def test_unknown_resource_is_a_404_without_asking_the_decision():
    harness = booking_harness()
    response = harness.http().get(f"{BASE}/resources/missing", headers=auth(SUBJECT_A))
    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "resource_not_found"
    # No decision was requested: the only audit entry is this refusal.
    assert harness.instance.authorization.store.audit == []


def test_cross_tenant_read_is_denied():
    harness = booking_harness()
    response = harness.http().get(f"{BASE}/resources/res_b1", headers=auth(SUBJECT_A))
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] in PUBLISHED_DENY_REASONS
    assert body["error"]["details"]["reason"] != "resource_not_found"


def test_claimed_tenant_mismatch_is_rejected_by_the_foundation():
    harness = booking_harness()
    response = harness.http().get(
        f"{BASE}/resources/res_a1", headers=auth(SUBJECT_A, **{"x-tenant-id": "ten_b"})
    )
    assert response.status_code == 403
    assert envelope_of(response)["error"]["details"]["reason"] == "tenant_mismatch"


def test_claimed_tenant_never_selects_the_effective_tenant_on_create():
    harness = booking_harness(store=BookingStore())
    response = harness.http().post(
        f"{BASE}/resources",
        headers=auth(SUBJECT_A, "skeleton-claim", **{"x-tenant-id": "ten_b"}),
        json={"name": "Claimed"},
    )
    # The cross-check disagrees with the verified identity: refused by the
    # foundation, and nothing is created in either Tenant.
    assert response.status_code == 403
    assert envelope_of(response)["error"]["details"]["reason"] == "tenant_mismatch"
    assert harness.booking.store.resources == {}


def test_subject_without_the_grant_cannot_create():
    harness = booking_harness(store=BookingStore())
    response = create_resource(harness.http(), "skeleton-nogrant", token=SUBJECT_C)
    assert response.status_code == 403
    assert envelope_of(response)["error"]["details"]["reason"] == (
        "permission_not_granted"
    )
    assert harness.booking.store.resources == {}


def test_missing_credential_is_a_401():
    harness = booking_harness()
    response = harness.http().get(f"{BASE}/resources/res_a1")
    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == "missing_identity"


def test_blank_name_is_a_validation_error_before_any_decision():
    harness = booking_harness(store=BookingStore())
    response = create_resource(harness.http(), "skeleton-invalid", name="   ")
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert harness.booking.store.resources == {}
    assert harness.instance.authorization.store.audit == []


def test_name_of_512_characters_is_accepted_and_513_is_refused():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    at_limit = create_resource(http, "skeleton-name-512", name="x" * 512)
    assert at_limit.status_code == 201, at_limit.text
    assert at_limit.json()["name"] == "x" * 512
    over_limit = create_resource(http, "skeleton-name-513", name="x" * 513)
    assert over_limit.status_code == 422
    body = envelope_of(over_limit)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "validation_error"
    assert len(harness.booking.store.resources) == 1
    # Refused before any decision: only the 512-name create asked IS-003.
    assert len(harness.instance.authorization.store.audit) == 1


def test_unknown_body_field_is_a_schema_refusal():
    harness = booking_harness()
    response = harness.http().post(
        f"{BASE}/resources",
        headers=auth(SUBJECT_A, "skeleton-schema"),
        json={"name": "x", "price": 10},
    )
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["reason"] == "malformed_request"


# --------------------------------------------------------------------- audit
def test_served_access_and_refusal_are_audited_with_linkage():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    created = http.post(
        f"{BASE}/resources",
        headers=auth(
            SUBJECT_A,
            "skeleton-audit-1",
            **{"x-request-id": "req-skeleton-1", "x-correlation-id": "corr-skel-1"},
        ),
        json={"name": "Audited"},
    )
    assert created.status_code == 201
    refused = http.get(f"{BASE}/resources/missing", headers=auth(SUBJECT_A))
    assert refused.status_code == 404

    journal = harness.booking.store.audit
    served = [e for e in journal if e.action == OPERATION_RESOURCE_CREATE]
    assert len(served) == 1
    assert served[0].decision == "ALLOW"
    assert served[0].reason == "permitted"
    assert served[0].subject_id == "idn_human_a"
    assert served[0].tenant_id == "ten_a"
    assert served[0].request_id == "req-skeleton-1"
    assert served[0].correlation_id == "corr-skel-1"
    denials = [e for e in journal if e.decision == "DENY"]
    assert {e.reason for e in denials} == {"resource_not_found"}
    assert denials[0].request_id == envelope_of(refused)["request_id"]
