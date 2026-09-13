"""SCS-002 Commerce Stage 2 — skeleton construction and the live slice.

Create and read one owned Product against the fully composed Platform
Instance (IS-001 + IS-002 + IS-003 + Commerce): construction, success,
tenant isolation, the approved error envelope, idempotent replay and
conflict, and audit. This module also defines the shared composition
helper used by the contract and boundary tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from authorization_service.models import ServiceAccess
from authorization_service.store import CONSUMER_PERMISSIONS
from commerce_service import COMPONENT_ID, COMPONENT_VERSION, OWNER_COMPONENT
from commerce_service.adapters import authorization_port, tenant_context_port
from commerce_service.contracts import (
    OPERATION_PRODUCT_CREATE,
    OPERATION_PRODUCT_READ,
    PUBLISHED_DENY_REASONS,
)
from commerce_service.deployment import CommerceDeployment
from commerce_service.deployment import build_deployment as build_commerce
from commerce_service.errors import AccessRefused
from commerce_service.store import CommerceStore
from tests.conftest import PLATFORM_ID, monolith

SUBJECT_A = "token-human-a"  # idn_human_a / ten_a
SUBJECT_B = "token-human-b"  # idn_human_b / ten_b
SUBJECT_C = "token-human-c"  # idn_human_c / ten_a, product reads only

COMMERCE_CREDENTIAL = "authz-svc-token-commerce"


@dataclass
class CommerceHarness:
    instance: Any
    commerce: CommerceDeployment

    def http(self) -> TestClient:
        return TestClient(self.commerce.contract_app())

    def client(self) -> Any:
        return self.commerce.publish()


def commerce_harness(store: CommerceStore | None = None) -> CommerceHarness:
    """Compose IS-001/IS-002/IS-003 plus Commerce for one test.

    Grants are the composition root's job (IS-003 publishes no grant API).
    Subjects A and B hold both product grants in their own Tenants; subject
    C holds the read grant only, so the create denial is exercisable.
    """
    instance = monolith()
    # Commerce's own service identity for asking IS-003.
    instance.authorization.store.services["svc_commerce"] = ServiceAccess(
        "svc_commerce", PLATFORM_ID, CONSUMER_PERMISSIONS
    )
    instance.authorization.store.service_tokens[COMMERCE_CREDENTIAL] = "svc_commerce"
    instance.authorization.store.grant("ten_a", "idn_human_a", OPERATION_PRODUCT_CREATE)
    instance.authorization.store.grant("ten_b", "idn_human_b", OPERATION_PRODUCT_CREATE)
    instance.authorization.store.grant("ten_a", "idn_human_a", OPERATION_PRODUCT_READ)
    instance.authorization.store.grant("ten_b", "idn_human_b", OPERATION_PRODUCT_READ)
    instance.authorization.store.grant("ten_a", "idn_human_c", OPERATION_PRODUCT_READ)
    commerce = build_commerce(
        {"platform_id": instance.authority.current_platform_id, "environment": "test"},
        authorization=authorization_port(
            instance.authorization.publish(credential=COMMERCE_CREDENTIAL)
        ),
        tenant_context=tenant_context_port(instance.identity_context_client),
        store=store,
        seed_demo=store is None,
        with_http=True,
    )
    return CommerceHarness(instance=instance, commerce=commerce)


def envelope_of(response: Any) -> dict:
    """The approved error envelope — and nothing else.

    Exact key sets prove there is no parallel legacy envelope: no top-level
    ``detail`` key exists anywhere in the refusal representation.
    """
    body = response.json()
    assert set(body) == {"error", "request_id", "correlation_id"}, body
    assert set(body["error"]) == {"code", "message", "details"}, body
    assert set(body["error"]["details"]) == {"reason"}, body
    assert body["request_id"] and body["correlation_id"]
    return body


def create_payload(name="Skeleton product") -> dict:
    return {"name": name, "description": "Created by a skeleton test."}


# ------------------------------------------------------------- construction
def test_component_identity_is_stable():
    assert COMPONENT_ID == "commerce"
    assert OWNER_COMPONENT == "commerce"
    assert COMPONENT_VERSION == "0.1.0"


def test_health_and_readiness_answer():
    http = commerce_harness().http()
    health = http.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "component_id": "commerce",
        "version": "0.1.0",
        "platform_id": PLATFORM_ID,
    }
    ready = http.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "component_id": "commerce"}


# ------------------------------------------------------------------ success
def test_create_then_read_one_owned_product():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()

    created = http.post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "skeleton-create-1",
        },
        json=create_payload(),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Skeleton product"
    assert body["status"] == "ACTIVE"
    assert body["created_by"] == "idn_human_a"
    assert "tenant_id" not in body
    product_id = body["product_id"]

    read = http.get(
        f"/api/v1/commerce/products/{product_id}",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert read.status_code == 200
    assert read.json() == body


def test_published_client_creates_and_reads_values():
    harness = commerce_harness(store=CommerceStore())
    client = harness.client()
    created = client.create_product(
        SUBJECT_A,
        name="Client product",
        description="Created through the published client.",
        idempotency_key="skeleton-client-1",
    )
    assert created.name == "Client product"
    read = client.read_product(SUBJECT_A, created.product_id)
    assert read == created
    with pytest.raises(AccessRefused) as exc:
        client.read_product(SUBJECT_A, "missing")
    assert exc.value.reason == "product_unknown"


# --------------------------------------------------------------- idempotency
def test_replay_returns_the_recorded_product_without_a_second_effect():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "skeleton-replay-1",
    }
    first = http.post(
        "/api/v1/commerce/products", headers=headers, json=create_payload()
    )
    second = http.post(
        "/api/v1/commerce/products", headers=headers, json=create_payload()
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(harness.commerce.store.products) == 1
    replays = [
        event
        for event in harness.commerce.store.audit
        if event.action == "idempotency_replay"
    ]
    assert len(replays) == 1


def test_changed_binding_with_the_same_key_is_a_conflict():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "skeleton-conflict-1",
    }
    first = http.post(
        "/api/v1/commerce/products", headers=headers, json=create_payload()
    )
    assert first.status_code == 201
    second = http.post(
        "/api/v1/commerce/products",
        headers=headers,
        json=create_payload("Another name"),
    )
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert body["error"]["details"]["reason"] == "idempotency_conflict"
    assert len(harness.commerce.store.products) == 1


def test_command_without_a_key_is_refused_before_any_effect():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    response = http.post(
        "/api/v1/commerce/products",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
        json=create_payload(),
    )
    assert response.status_code == 400
    body = envelope_of(response)
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert harness.commerce.store.products == {}


# -------------------------------------------------------------------- refusal
def test_unknown_product_is_a_404_without_asking_the_decision():
    harness = commerce_harness()
    response = harness.http().get(
        "/api/v1/commerce/products/missing",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "product_unknown"


def test_cross_tenant_read_is_denied():
    harness = commerce_harness()
    response = harness.http().get(
        "/api/v1/commerce/products/prd_b1",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] in PUBLISHED_DENY_REASONS
    assert body["error"]["details"]["reason"] != "product_unknown"


def test_subject_without_the_grant_cannot_create():
    harness = commerce_harness(store=CommerceStore())
    response = harness.http().post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_C}",
            "idempotency-key": "skeleton-nogrant-1",
        },
        json=create_payload(),
    )
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["details"]["reason"] == "permission_not_granted"
    assert harness.commerce.store.products == {}


def test_missing_credential_is_a_401():
    harness = commerce_harness()
    response = harness.http().get("/api/v1/commerce/products/prd_a1")
    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == "missing_identity"


def test_blank_name_is_a_validation_error_before_any_decision():
    harness = commerce_harness(store=CommerceStore())
    response = harness.http().post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "skeleton-invalid-1",
        },
        json={"name": "   ", "description": ""},
    )
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "validation_error"
    assert harness.commerce.store.products == {}


def test_unknown_body_field_is_a_schema_refusal():
    harness = commerce_harness()
    response = harness.http().post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "skeleton-schema-1",
        },
        json={"name": "x", "description": "y", "price": 10},
    )
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["reason"] == "malformed_request"


# --------------------------------------------------------------------- audit
def test_served_access_and_refusal_are_audited_with_linkage():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    created = http.post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "skeleton-audit-1",
            "x-request-id": "req-skeleton-1",
            "x-correlation-id": "corr-skeleton-1",
        },
        json=create_payload(),
    )
    assert created.status_code == 201
    refused = http.get(
        "/api/v1/commerce/products/missing",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert refused.status_code == 404

    journal = harness.commerce.store.audit
    served = [event for event in journal if event.action == OPERATION_PRODUCT_CREATE]
    assert len(served) == 1
    assert served[0].decision == "ALLOW"
    assert served[0].reason == "permitted"
    assert served[0].subject_id == "idn_human_a"
    assert served[0].tenant_id == "ten_a"
    assert served[0].request_id == "req-skeleton-1"
    assert served[0].correlation_id == "corr-skeleton-1"
    denials = [event for event in journal if event.decision == "DENY"]
    assert {event.reason for event in denials} == {"product_unknown"}
