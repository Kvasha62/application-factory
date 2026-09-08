"""HTTP contract of IS-003: the same behaviour a remote consumer would see.

The published API is the boundary; the in-process client of the Level 0 monolith
merely executes it. These tests drive the application over HTTP and check the
two status families the contract defines: a refused *question* (401/403/422) and
an answered one (200 with ALLOW or DENY).
"""

from __future__ import annotations

import pytest

from tests.conftest import monolith

DATA_OWNER = {"Authorization": "Bearer authz-svc-token-records"}


def body(operation="records.read", resource_id="rec_a1", tenant_id="ten_a", resource_type="record"):
    return {
        "operation": operation,
        "resource": {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "tenant_id": tenant_id,
        },
    }


def subject(token: str | None) -> dict:
    headers = dict(DATA_OWNER)
    if token is not None:
        headers["X-Subject-Authorization"] = f"Bearer {token}"
    return headers


def test_health_and_readiness_are_published():
    client = monolith().authorization_http()
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["component_id"] == "authorization"
    assert client.get("/ready").json() == {"status": "ready", "component_id": "authorization"}


def test_an_allow_is_answered_with_the_published_decision_model():
    client = monolith().authorization_http()
    response = client.post(
        "/api/v1/decisions",
        json=body(),
        headers={**subject("token-human-a"), "X-Request-Id": "req-http", "X-Correlation-Id": "cor-http"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"] == "ALLOW"
    assert payload["reason"] == "permitted"
    assert payload["subject_id"] == "idn_human_a"
    assert payload["tenant_id"] == "ten_a"
    assert payload["operation"] == "records.read"
    assert payload["resource"] == {
        "resource_type": "record",
        "resource_id": "rec_a1",
        "tenant_id": "ten_a",
    }
    assert payload["request_id"] == "req-http"
    assert payload["correlation_id"] == "cor-http"
    assert payload["decided_by"] == "authorization"


@pytest.mark.parametrize(
    "token, resource, reason",
    [
        ("token-human-b", body(), "resource_tenant_mismatch"),
        (None, body(), "missing_identity"),
        ("token-invalid", body(), "invalid_identity"),
        ("token-human-c", body(operation="records.write"), "permission_not_granted"),
    ],
)
def test_a_denial_is_an_answer_not_a_transport_failure(token, resource, reason):
    client = monolith().authorization_http()
    response = client.post("/api/v1/decisions", json=resource, headers=subject(token))
    assert response.status_code == 200
    assert response.json()["decision"] == "DENY"
    assert response.json()["reason"] == reason


@pytest.mark.parametrize(
    "credential, status, reason",
    [
        (None, 401, "missing_service_identity"),
        ("Bearer not-a-service-token", 401, "invalid_service_identity"),
        ("Bearer authz-svc-token-unknown", 401, "unknown_service"),
        ("Bearer authz-svc-token-reporting", 403, "insufficient_authorization"),
        ("Bearer authz-svc-token-foreign", 403, "platform_mismatch"),
    ],
)
def test_the_asking_component_is_authenticated_and_authorized(credential, status, reason):
    client = monolith().authorization_http()
    headers = {"X-Subject-Authorization": "Bearer token-human-a", "X-Request-Id": "req-refused"}
    if credential is not None:
        headers["Authorization"] = credential
    response = client.post("/api/v1/decisions", json=body(), headers=headers)
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["decision"] == "DENY"
    assert detail["reason"] == reason
    assert detail["request_id"] == "req-refused"


def test_an_unanswerable_question_is_refused_and_no_decision_is_invented():
    client = monolith().authorization_http()
    empty_operation = client.post(
        "/api/v1/decisions", json=body(operation="   "), headers=subject("token-human-a")
    )
    assert empty_operation.status_code == 422
    assert empty_operation.json()["detail"]["reason"] == "malformed_request"

    # An undeclared field is rejected instead of being ignored: a consumer must
    # not be able to smuggle a claim past the published request model.
    smuggled = client.post(
        "/api/v1/decisions",
        json={**body(), "subject_id": "idn_human_a", "tenant_id": "ten_a"},
        headers=subject("token-human-b"),
    )
    assert smuggled.status_code == 422


def test_the_api_publishes_nothing_but_the_decision_operation():
    instance = monolith()
    client = instance.authorization_http()
    document = client.get("/openapi.json").json()
    assert set(document["paths"]) == {"/health", "/ready", "/api/v1/decisions"}
    for path in document["paths"]:
        assert "grant" not in path and "audit" not in path and "internal" not in path
    assert client.get("/api/v1/grants").status_code == 404
    assert client.get("/api/v1/audit").status_code == 404
    # No route of another component is served here.
    assert client.get("/api/v1/tenants/ten_a").status_code == 404
    assert client.get("/api/v1/context").status_code == 404
