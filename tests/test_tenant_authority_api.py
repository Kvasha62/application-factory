"""HTTP контракт Tenant Authority (`/api/v1`) — поведение опубликованной поверхности."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.api import create_app
from tests.conftest import tenant_authority

ADMIN = {"Authorization": "Bearer svc-token-admin"}
READ_ONLY = {"Authorization": "Bearer svc-token-identity"}


def client_with_authority():
    authority = tenant_authority()
    return TestClient(create_app(authority)), authority


def test_health_and_readiness_identify_the_component_and_platform():
    client, authority = client_with_authority()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "component_id": COMPONENT_ID,
        "version": COMPONENT_VERSION,
        "platform_id": authority.current_platform_id,
    }
    assert client.get("/ready").json()["status"] == "ready"


def test_create_tenant_through_the_api_starts_in_provisioning():
    client, authority = client_with_authority()
    response = client.post(
        "/api/v1/tenants", json={"tenant_id": "ten_http"}, headers=ADMIN
    )
    assert response.status_code == 201
    body = response.json()
    assert body == {
        "tenant_id": "ten_http",
        "platform_id": "plt_demo",
        "state": "provisioning",
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
        "state_source": "tenant_authority",
    }
    assert authority.store.tenants["ten_http"].state.value == "provisioning"


def test_write_operations_require_a_verified_service_identity():
    client, _ = client_with_authority()
    unauthenticated = client.post("/api/v1/tenants", json={"tenant_id": "ten_anon"})
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["reason"] == "missing_service_identity"

    read_only = client.post(
        "/api/v1/tenants/ten_a/transitions",
        json={"to_state": "suspended"},
        headers=READ_ONLY,
    )
    assert read_only.status_code == 403
    assert read_only.json()["detail"]["reason"] == "insufficient_authorization"


def test_transition_through_the_api_writes_an_auditable_record():
    client, _authority = client_with_authority()
    response = client.post(
        "/api/v1/tenants/ten_a/transitions",
        json={"to_state": "suspended", "reason": "abuse desk"},
        headers={
            **ADMIN,
            "X-Request-Id": "req-http-1",
            "X-Correlation-Id": "cor-http-1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "transition_id": response.json()["transition_id"],
        "tenant_id": "ten_a",
        "previous_state": "active",
        "new_state": "suspended",
        "actor_id": "svc_tenant_admin",
        "request_id": "req-http-1",
        "correlation_id": "cor-http-1",
        "timestamp": response.json()["timestamp"],
        "reason": "abuse desk",
    }
    history = client.get("/api/v1/tenants/ten_a/transitions", headers=ADMIN).json()
    assert [item["new_state"] for item in history] == ["suspended"]


def test_invalid_transition_is_rejected_with_a_conflict_status():
    client, authority = client_with_authority()
    response = client.post(
        "/api/v1/tenants/ten_deleted/transitions",
        json={"to_state": "active"},
        headers=ADMIN,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "invalid_transition"
    assert response.json()["detail"]["request_id"] is None
    assert authority.engine.lookup("ten_deleted").state.value == "deleted"


def test_idempotent_transition_replay_over_http():
    client, authority = client_with_authority()
    headers = {**ADMIN, "Idempotency-Key": "ik-http"}
    first = client.post(
        "/api/v1/tenants/ten_a/transitions",
        json={"to_state": "suspended"},
        headers=headers,
    )
    replay = client.post(
        "/api/v1/tenants/ten_a/transitions",
        json={"to_state": "suspended"},
        headers=headers,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json()["transition_id"] == replay.json()["transition_id"]
    assert len(authority.store.transitions) == 1


def test_lifecycle_endpoint_reports_enforcement_for_callers():
    client, _ = client_with_authority()
    suspended = client.get("/api/v1/tenants/ten_suspended/lifecycle", headers=READ_ONLY)
    active = client.get("/api/v1/tenants/ten_a/lifecycle", headers=READ_ONLY)
    assert suspended.json() == {
        "tenant_id": "ten_suspended",
        "platform_id": "plt_demo",
        "state": "suspended",
        "permitted": False,
        "reason": "tenant_suspended",
    }
    assert active.json()["permitted"] is True
    assert active.json()["reason"] == "permitted"


def test_registry_listing_is_scoped_to_the_current_platform():
    client, _ = client_with_authority()
    everything = client.get("/api/v1/tenants", headers=ADMIN).json()
    assert {item["platform_id"] for item in everything} == {"plt_demo"}
    assert "ten_foreign" not in {item["tenant_id"] for item in everything}

    deleted_only = client.get("/api/v1/tenants?state=deleted", headers=ADMIN).json()
    assert [item["tenant_id"] for item in deleted_only] == ["ten_deleted"]

    unauthenticated = client.get("/api/v1/tenants")
    assert unauthenticated.status_code == 401


def test_unknown_request_fields_are_rejected_not_ignored():
    client, _ = client_with_authority()
    response = client.post(
        "/api/v1/tenants", json={"tenant_id": "ten_x", "state": "active"}, headers=ADMIN
    )
    assert response.status_code == 422

    illegal_state = client.post(
        "/api/v1/tenants/ten_a/transitions",
        json={"to_state": "archived"},
        headers=ADMIN,
    )
    assert illegal_state.status_code == 422


def test_published_surface_exposes_no_internal_data():
    client, _ = client_with_authority()
    declared = client.get("/openapi.json").json()["paths"]
    assert set(declared) == {
        "/api/v1/lifecycle",
        "/api/v1/tenants",
        "/api/v1/tenants/{tenant_id}",
        "/api/v1/tenants/{tenant_id}/lifecycle",
        "/api/v1/tenants/{tenant_id}/transitions",
        "/health",
        "/ready",
    }
    for path in (
        "/api/v1/audit",
        "/internal/tenants",
        "/api/v1/store",
        "/api/v1/services",
    ):
        assert client.get(path).status_code == 404
    assert client.delete("/api/v1/tenants/ten_a", headers=ADMIN).status_code == 405
    assert (
        client.patch(
            "/api/v1/tenants/ten_a", json={"state": "active"}, headers=ADMIN
        ).status_code
        == 405
    )


def test_state_machine_is_published_not_embedded_in_callers():
    client, _ = client_with_authority()
    published = client.get("/api/v1/lifecycle").json()
    assert published["lifecycle"] == [
        "provisioning",
        "active",
        "suspended",
        "deletion_requested",
        "deleted",
    ]
    assert published["allowed_transitions"] == {
        "provisioning": ["active"],
        "active": ["deletion_requested", "suspended"],
        "suspended": ["active", "deletion_requested"],
        "deletion_requested": ["deleted"],
        "deleted": [],
    }
    assert published["terminal_states"] == ["deleted"]
