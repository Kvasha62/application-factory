"""SCS-002 Commerce Stage 4 — security / idempotency / audit quality gates.

Systematic negative matrix over the implemented business logic: tenant
isolation with no-mutation proofs, authorization (unknown subject,
missing grant, no substitution, fail-closed dependencies), idempotency
per state-changing command including binding rules, checkout/payment
concurrency, immutable purchase facts and price snapshots, audit
completeness with append-only semantics, public-API surface and boundary
scans. No new business behavior is declared here — every test pins down
already-published semantics.

Determinism rules for this file: no sleeps, no wall-clock waits, no
timing assertions. Concurrency tests synchronize with a barrier and join
every thread; only outcome counts are asserted, never which thread wins.
Every test owns its harness and store; idempotency keys are unique per
test, so no test depends on order or on another test's state.
"""

from __future__ import annotations

import ast
import dataclasses
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from commerce_service.consumed import DecisionAnswer, TenantContextAnswer
from commerce_service.deployment import build_deployment as build_commerce
from commerce_service.models import OwnedCart, OwnedOrder
from commerce_service.reader import CommerceClient
from commerce_service.store import CommerceStore
from tests.conftest import PLATFORM_ID
from tests.test_commerce_boundary import (
    FORBIDDEN_PACKAGES,
    StubAuthorizationPort,
    commerce_deployment,
)
from tests.test_commerce_cart import add_item, open_cart, priced_offer
from tests.test_commerce_catalog import create_product, define_offer, set_price
from tests.test_commerce_checkout import checkout, stocked_cart
from tests.test_commerce_order import pay_order, placed_order, read_order
from tests.test_commerce_skeleton import (
    SUBJECT_A,
    SUBJECT_B,
    SUBJECT_C,
    commerce_harness,
    envelope_of,
)

ALLOW_A = DecisionAnswer(
    decision="ALLOW", reason="permitted", subject_id="idn_human_a", tenant_id="ten_a"
)
DENY_GRANT = DecisionAnswer(
    decision="DENY",
    reason="permission_not_granted",
    subject_id="idn_human_a",
    tenant_id="ten_a",
)
CTX_A = TenantContextAnswer(identity_id="idn_human_a", tenant_id="ten_a")


class StubTenantContext:
    """A tenant-context port with a fixed outcome: a value or an exception."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def resolve(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "claimed_tenant_id": claimed_tenant_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
            }
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class StubIdempotency:
    """A command-safety port that always fails: the IS-005 outage double."""

    def __init__(self, outcome: BaseException) -> None:
        self.outcome = outcome

    def execute(
        self,
        key: str | None,
        *,
        identity: str | None,
        tenant_id: str | None,
        operation: str,
        resource: str | None,
        fingerprint: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
        effect: Any,
    ) -> Any:
        raise self.outcome


def chain(http: Any, subject: str, product_id: str, prefix: str) -> dict:
    """One priced offer, one stocked open cart and one placed order."""
    offer = priced_offer(http, subject, product_id, f"{prefix}-cat")
    cart_id = open_cart(http, subject, f"{prefix}-open").json()["cart_id"]
    added = add_item(http, subject, f"{prefix}-add", cart_id, offer["offer_id"], 2)
    assert added.status_code == 200
    order_cart = open_cart(http, subject, f"{prefix}-oopen").json()["cart_id"]
    stocked = add_item(
        http, subject, f"{prefix}-oadd", order_cart, offer["offer_id"], 1
    )
    assert stocked.status_code == 200
    order = checkout(http, subject, f"{prefix}-co", order_cart)
    assert order.status_code == 201
    return {"offer": offer, "cart_id": cart_id, "order": order.json()}


def world(prefix: str) -> dict:
    """Fresh harness with a full chain for Tenant A and Tenant B."""
    harness = commerce_harness()
    http = harness.http()
    return {
        "harness": harness,
        "http": http,
        "a": chain(http, SUBJECT_A, "prd_a1", f"{prefix}-a"),
        "b": chain(http, SUBJECT_B, "prd_b1", f"{prefix}-b"),
    }


def snapshot(store: CommerceStore) -> dict:
    """Value snapshot of every business dataset (records are frozen)."""
    return {
        "products": dict(store.products),
        "offers": dict(store.offers),
        "prices": dict(store.prices),
        "carts": dict(store.carts),
        "cart_lines": dict(store.cart_lines),
        "orders": dict(store.orders),
        "order_lines": dict(store.order_lines),
    }


def audit_by_request(store: CommerceStore, request_id: str) -> list:
    return [event for event in store.audit if event.request_id == request_id]


# ------------------------------------------------------- tenant isolation
def test_tenant_isolation_products():
    env = world("til-products")
    http, store = env["http"], env["harness"].commerce.store
    assert (
        http.get(
            "/api/v1/commerce/products/prd_a1",
            headers={"authorization": f"Bearer {SUBJECT_A}"},
        ).status_code
        == 200
    )
    before = snapshot(store)
    refused = http.get(
        "/api/v1/commerce/products/prd_b1",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert refused.status_code == 403
    assert envelope_of(refused)["error"]["code"] == "AUTHORIZATION_DENIED"
    assert snapshot(store) == before


def test_tenant_isolation_offers():
    env = world("til-offers")
    http, store = env["http"], env["harness"].commerce.store
    own = define_offer(http, SUBJECT_A, "til-offers-own", "prd_a1", "Own offer")
    assert own.status_code == 201
    before = snapshot(store)
    foreign = define_offer(http, SUBJECT_A, "til-offers-for", "prd_b1", "Foreign")
    assert foreign.status_code == 404
    assert envelope_of(foreign)["error"]["details"]["reason"] == "product_unknown"
    assert snapshot(store) == before


def test_tenant_isolation_prices():
    env = world("til-prices")
    http, store = env["http"], env["harness"].commerce.store
    own = set_price(
        http, SUBJECT_A, "til-prices-own", env["a"]["offer"]["offer_id"], 100, "EUR"
    )
    assert own.status_code == 201
    before = snapshot(store)
    foreign = set_price(
        http, SUBJECT_A, "til-prices-for", env["b"]["offer"]["offer_id"], 100, "EUR"
    )
    assert foreign.status_code == 404
    assert envelope_of(foreign)["error"]["details"]["reason"] == "offer_unknown"
    assert snapshot(store) == before


def test_tenant_isolation_carts():
    env = world("til-carts")
    http, store = env["http"], env["harness"].commerce.store
    cart_a, cart_b = env["a"]["cart_id"], env["b"]["cart_id"]
    read = http.get(
        f"/api/v1/commerce/carts/{cart_a}",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert read.status_code == 200
    before = snapshot(store)
    cross_read = http.get(
        f"/api/v1/commerce/carts/{cart_b}",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert cross_read.status_code == 403
    cross_mutate = add_item(
        http, SUBJECT_A, "til-carts-x", cart_b, env["a"]["offer"]["offer_id"], 1
    )
    assert cross_mutate.status_code == 403
    # Same tenant, another buyer: the owned-data buyer check refuses.
    same_tenant = add_item(
        http, SUBJECT_C, "til-carts-c", cart_a, env["a"]["offer"]["offer_id"], 1
    )
    assert same_tenant.status_code == 403
    assert envelope_of(same_tenant)["error"]["details"]["reason"] == "buyer_mismatch"
    assert snapshot(store) == before


def test_tenant_isolation_orders():
    env = world("til-orders")
    http, store = env["http"], env["harness"].commerce.store
    order_a, order_b = env["a"]["order"]["order_id"], env["b"]["order"]["order_id"]
    assert read_order(http, SUBJECT_A, order_a).status_code == 200
    before = snapshot(store)
    cross_read = read_order(http, SUBJECT_A, order_b)
    assert cross_read.status_code == 403
    cross_pay = pay_order(http, SUBJECT_A, "til-orders-pay", order_b)
    assert cross_pay.status_code == 403
    assert snapshot(store) == before
    assert store.orders[order_b].payment_state == "PENDING_PAYMENT"


def test_denied_access_leaks_no_foreign_data():
    env = world("til-leak")
    http = env["http"]
    cart_b, order_b = env["b"]["cart_id"], env["b"]["order"]["order_id"]
    responses = [
        http.get(
            "/api/v1/commerce/products/prd_b1",
            headers={"authorization": f"Bearer {SUBJECT_A}"},
        ),
        http.get(
            f"/api/v1/commerce/carts/{cart_b}",
            headers={"authorization": f"Bearer {SUBJECT_A}"},
        ),
        read_order(http, SUBJECT_A, order_b),
        http.get(
            "/api/v1/commerce/products/missing",
            headers={"authorization": f"Bearer {SUBJECT_A}"},
        ),
    ]
    for response in responses:
        assert response.status_code in (403, 404)
        envelope_of(response)
        for foreign in ("prd_b1", "ten_b", "idn_human_b", cart_b, order_b):
            assert foreign not in response.text, (response.text, foreign)


# ---------------------------------------------------------- authorization
OPERATION_REQUESTS = {
    "create_product": (
        "POST",
        "/api/v1/commerce/products",
        {"name": "Gate product", "description": "Created by a gate test."},
    ),
    "read_product": ("GET", "/api/v1/commerce/products/prd_a1", None),
    "create_offer": (
        "POST",
        "/api/v1/commerce/offers",
        {"product_id": "prd_a1", "name": "Gate offer"},
    ),
    "create_price": (
        "POST",
        "/api/v1/commerce/prices",
        {"offer_id": "OFFER", "amount": 100, "currency": "EUR"},
    ),
    "create_cart": ("POST", "/api/v1/commerce/carts", None),
    "read_cart": ("GET", "/api/v1/commerce/carts/CART", None),
    "add_cart_item": (
        "POST",
        "/api/v1/commerce/carts/CART/items",
        {"offer_id": "OFFER", "quantity": 1},
    ),
    "set_cart_item_quantity": (
        "POST",
        "/api/v1/commerce/carts/CART/items/OFFER/quantity",
        {"quantity": 5},
    ),
    "remove_cart_item": (
        "POST",
        "/api/v1/commerce/carts/CART/items/OFFER/remove",
        None,
    ),
    "checkout": ("POST", "/api/v1/commerce/checkout", {"cart_id": "CART"}),
    "read_order": ("GET", "/api/v1/commerce/orders/ORDER", None),
    "pay_order": ("POST", "/api/v1/commerce/orders/ORDER/pay", None),
}


def gate_request(http: Any, env: dict, name: str, key: str, credential: str | None):
    """One published operation with placeholders resolved from the world."""
    method, path, payload = OPERATION_REQUESTS[name]
    path = (
        path.replace("CART", env["a"]["cart_id"])
        .replace("OFFER", env["a"]["offer"]["offer_id"])
        .replace("ORDER", env["a"]["order"]["order_id"])
    )
    if isinstance(payload, dict):
        payload = {
            key: (
                value.replace("CART", env["a"]["cart_id"]).replace(
                    "OFFER", env["a"]["offer"]["offer_id"]
                )
                if isinstance(value, str)
                else value
            )
            for key, value in payload.items()
        }
    headers: dict[str, str] = {"idempotency-key": key}
    if credential is not None:
        headers["authorization"] = f"Bearer {credential}"
    if method == "GET":
        return http.get(path, headers=headers)
    if payload is None:
        return http.post(path, headers=headers)
    return http.post(path, headers=headers, json=payload)


@pytest.mark.parametrize("name", sorted(OPERATION_REQUESTS))
def test_unknown_subject_is_refused_on_every_operation(name):
    env = world(f"gate-unknown-{name}")
    store = env["harness"].commerce.store
    before = snapshot(store)
    response = gate_request(env["http"], env, name, f"gate-unknown-{name}", None)
    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == "missing_identity"
    assert snapshot(store) == before


@pytest.mark.parametrize(
    "name", ["create_product", "read_cart", "checkout", "pay_order"]
)
def test_garbage_credential_is_refused(name):
    env = world(f"gate-garbage-{name}")
    store = env["harness"].commerce.store
    before = snapshot(store)
    response = gate_request(env["http"], env, name, f"gate-garbage-{name}", "garbage")
    assert response.status_code == 401
    assert envelope_of(response)["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert snapshot(store) == before


@pytest.mark.parametrize(
    "name",
    [
        "create_product",
        "create_offer",
        "create_price",
        "create_cart",
        "read_cart",
        "read_order",
    ],
)
def test_subject_without_grant_is_denied_without_mutation(name):
    # Subject C holds the cart/checkout/pay mutation grants but none of the
    # read or catalog-create grants, so these six deny at the decision.
    env = world(f"gate-nogrant-{name}")
    store = env["harness"].commerce.store
    before = snapshot(store)
    response = gate_request(env["http"], env, name, f"gate-nogrant-{name}", SUBJECT_C)
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["details"]["reason"] == "permission_not_granted"
    assert snapshot(store) == before


def test_decision_denies_even_when_record_exists():
    port = StubAuthorizationPort(DENY_GRANT)
    deployment = commerce_deployment(port)
    http = TestClient(deployment.contract_app())
    before = snapshot(deployment.store)
    response = http.get(
        "/api/v1/commerce/products/prd_a1",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"] == "permission_not_granted"
    )
    assert port.calls == [
        {
            "operation": "commerce.products.read",
            "resource_type": "product",
            "resource_id": "prd_a1",
            "resource_tenant_id": "ten_a",
            "claimed_tenant_id": None,
        }
    ]
    assert snapshot(deployment.store) == before


def test_engine_forwards_the_published_question_per_operation():
    """No substitution: the engine asks IS-003 exactly the published question.

    Twelve operations in one stubbed deployment; every recorded question
    carries the published operation, resource type, resource id and owning
    tenant — nothing of the engine's own invention.
    """
    store = CommerceStore()
    store.seed_demo()
    port = StubAuthorizationPort(ALLOW_A)
    deployment = build_commerce(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        authorization=port,
        tenant_context=StubTenantContext(CTX_A),
        store=store,
        seed_demo=False,
        with_http=True,
    )
    http = TestClient(deployment.contract_app())
    auth = {"authorization": f"Bearer {SUBJECT_A}"}

    def post(path, key, payload=None):
        headers = {**auth, "idempotency-key": key}
        if payload is None:
            return http.post(path, headers=headers)
        return http.post(path, headers=headers, json=payload)

    product = post(
        "/api/v1/commerce/products",
        "gate-q-p",
        {"name": "Q", "description": "Q"},
    ).json()
    assert http.get(f"/api/v1/commerce/products/{product['product_id']}", headers=auth)
    offer = post(
        "/api/v1/commerce/offers",
        "gate-q-o",
        {"product_id": product["product_id"], "name": "Q"},
    ).json()
    priced = post(
        "/api/v1/commerce/prices",
        "gate-q-pr",
        {"offer_id": offer["offer_id"], "amount": 100, "currency": "EUR"},
    )
    assert priced.status_code == 201
    cart = post("/api/v1/commerce/carts", "gate-q-c").json()
    cart_id = cart["cart_id"]
    assert (
        http.get(f"/api/v1/commerce/carts/{cart_id}", headers=auth).status_code == 200
    )
    assert (
        post(
            f"/api/v1/commerce/carts/{cart_id}/items",
            "gate-q-add",
            {"offer_id": offer["offer_id"], "quantity": 1},
        ).status_code
        == 200
    )
    assert (
        post(
            f"/api/v1/commerce/carts/{cart_id}/items/{offer['offer_id']}/quantity",
            "gate-q-set",
            {"quantity": 2},
        ).status_code
        == 200
    )
    assert (
        post(
            f"/api/v1/commerce/carts/{cart_id}/items/{offer['offer_id']}/remove",
            "gate-q-del",
        ).status_code
        == 200
    )
    assert (
        post(
            f"/api/v1/commerce/carts/{cart_id}/items",
            "gate-q-add2",
            {"offer_id": offer["offer_id"], "quantity": 3},
        ).status_code
        == 200
    )
    order = post("/api/v1/commerce/checkout", "gate-q-co", {"cart_id": cart_id}).json()
    assert read_order(http, SUBJECT_A, order["order_id"]).status_code == 200
    assert (
        pay_order(http, SUBJECT_A, "gate-q-pay", order["order_id"]).status_code == 200
    )

    expected = [
        ("commerce.products.create", "product", ("prd_", True), "ten_a"),
        ("commerce.products.read", "product", (product["product_id"], False), "ten_a"),
        ("commerce.offers.create", "offer", ("off_", True), "ten_a"),
        ("commerce.prices.create", "price", ("prc_", True), "ten_a"),
        ("commerce.carts.create", "cart", ("crt_", True), "ten_a"),
        ("commerce.carts.read", "cart", (cart_id, False), "ten_a"),
        ("commerce.carts.update", "cart", (cart_id, False), "ten_a"),
        ("commerce.carts.update", "cart", (cart_id, False), "ten_a"),
        ("commerce.carts.update", "cart", (cart_id, False), "ten_a"),
        ("commerce.carts.update", "cart", (cart_id, False), "ten_a"),
        ("commerce.checkout.create", "cart", (cart_id, False), "ten_a"),
        ("commerce.orders.read", "order", (order["order_id"], False), "ten_a"),
        ("commerce.orders.pay", "order", (order["order_id"], False), "ten_a"),
    ]
    assert len(port.calls) == len(expected)
    for call, (operation, resource_type, (resource, is_prefix), tenant) in zip(
        port.calls, expected
    ):
        assert call["operation"] == operation
        assert call["resource_type"] == resource_type
        assert call["resource_tenant_id"] == tenant
        if is_prefix:
            assert call["resource_id"].startswith(resource), call
        else:
            assert call["resource_id"] == resource, call


def test_delete_cart_is_not_a_published_operation():
    http = commerce_harness().http()
    assert "delete" not in {
        name for name, _ in vars(CommerceClient).items() if not name.startswith("_")
    }
    assert "delete_cart" not in CommerceClient.__dict__
    import json

    contract = json.loads(
        Path("components/commerce/contract/component_contract.json").read_text()
    )
    guarded = contract["authz"]["operations_guarded"]
    assert not [operation for operation in guarded if "delete" in operation]
    response = http.request(
        "DELETE",
        "/api/v1/commerce/carts/crt_x",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert response.status_code == 405


def test_route_inventory_is_exactly_the_published_surface():
    routes = commerce_harness().commerce.contract_app().routes
    framework = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (path, method.lower())
        for route in routes
        for path in [getattr(route, "path", "")]
        for method in getattr(route, "methods", set())
        if path and path not in framework and method not in {"HEAD", "OPTIONS"}
    }
    assert implemented == {
        ("/api/v1/commerce/products", "post"),
        ("/api/v1/commerce/products/{product_id}", "get"),
        ("/api/v1/commerce/offers", "post"),
        ("/api/v1/commerce/prices", "post"),
        ("/api/v1/commerce/carts", "post"),
        ("/api/v1/commerce/carts/{cart_id}", "get"),
        ("/api/v1/commerce/carts/{cart_id}/items", "post"),
        ("/api/v1/commerce/carts/{cart_id}/items/{offer_id}/quantity", "post"),
        ("/api/v1/commerce/carts/{cart_id}/items/{offer_id}/remove", "post"),
        ("/api/v1/commerce/checkout", "post"),
        ("/api/v1/commerce/orders/{order_id}", "get"),
        ("/api/v1/commerce/orders/{order_id}/pay", "post"),
        ("/health", "get"),
        ("/ready", "get"),
    }


# ---------------------------------------------------------- idempotency
def test_cart_create_replay_and_conflict():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    headers = {"authorization": f"Bearer {SUBJECT_A}", "idempotency-key": "gate-ccart"}
    first = http.post("/api/v1/commerce/carts", headers=headers)
    second = http.post("/api/v1/commerce/carts", headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(harness.commerce.store.carts) == 1
    # The same key with another identity is a different binding: conflict.
    foreign = http.post(
        "/api/v1/commerce/carts",
        headers={
            "authorization": f"Bearer {SUBJECT_B}",
            "idempotency-key": "gate-ccart",
        },
    )
    assert foreign.status_code == 409
    assert envelope_of(foreign)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.commerce.store.carts) == 1


def test_set_quantity_replay_and_conflict():
    env = world("gate-cset")
    http = env["http"]
    cart_id, offer_id = env["a"]["cart_id"], env["a"]["offer"]["offer_id"]
    headers = {"authorization": f"Bearer {SUBJECT_A}", "idempotency-key": "gate-cset"}
    path = f"/api/v1/commerce/carts/{cart_id}/items/{offer_id}/quantity"
    first = http.post(path, headers=headers, json={"quantity": 9})
    second = http.post(path, headers=headers, json={"quantity": 9})
    assert first.status_code == 200
    assert second.json() == first.json()
    assert second.json()["items"][0]["quantity"] == 9
    conflict = http.post(path, headers=headers, json={"quantity": 10})
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_remove_replay_returns_the_recorded_cart():
    env = world("gate-cdel")
    http = env["http"]
    cart_id, offer_id = env["a"]["cart_id"], env["a"]["offer"]["offer_id"]
    headers = {"authorization": f"Bearer {SUBJECT_A}", "idempotency-key": "gate-cdel"}
    path = f"/api/v1/commerce/carts/{cart_id}/items/{offer_id}/remove"
    first = http.post(path, headers=headers)
    second = http.post(path, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        second.json()
        == first.json()
        == {
            "cart_id": cart_id,
            "buyer_identity_id": "idn_human_a",
            "created_at": first.json()["created_at"],
            "updated_at": first.json()["updated_at"],
            "items": [],
        }
    )


# ------------------------------------------------------------- binding
def test_key_cannot_move_across_resources():
    harness = commerce_harness()
    http = harness.http()
    offer_id = priced_offer(http, SUBJECT_A, "prd_a1", "gate-bind-res")["offer_id"]
    first_cart = open_cart(http, SUBJECT_A, "gate-bind-res-o1").json()["cart_id"]
    second_cart = open_cart(http, SUBJECT_A, "gate-bind-res-o2").json()["cart_id"]
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "gate-bind-res",
    }
    first = http.post(
        f"/api/v1/commerce/carts/{first_cart}/items",
        headers=headers,
        json={"offer_id": offer_id, "quantity": 1},
    )
    assert first.status_code == 200
    before = snapshot(harness.commerce.store)
    conflict = http.post(
        f"/api/v1/commerce/carts/{second_cart}/items",
        headers=headers,
        json={"offer_id": offer_id, "quantity": 1},
    )
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert snapshot(harness.commerce.store) == before
    assert harness.commerce.store.lines_of_cart(second_cart) == []


def test_key_cannot_move_across_tenants():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    headers_a = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "gate-bind-t",
    }
    created = http.post(
        "/api/v1/commerce/products",
        headers=headers_a,
        json={"name": "Tenant key", "description": "Binding probe."},
    )
    assert created.status_code == 201
    product_a = created.json()["product_id"]
    before = snapshot(harness.commerce.store)
    headers_b = {
        "authorization": f"Bearer {SUBJECT_B}",
        "idempotency-key": "gate-bind-t",
    }
    conflict = http.post(
        "/api/v1/commerce/products",
        headers=headers_b,
        json={"name": "Tenant key", "description": "Binding probe."},
    )
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # No foreign result leaks and no second product exists anywhere.
    assert product_a not in conflict.text
    assert snapshot(harness.commerce.store) == before


def test_key_cannot_move_across_commands():
    harness = commerce_harness()
    http = harness.http()
    headers = {"authorization": f"Bearer {SUBJECT_A}", "idempotency-key": "gate-bind-c"}
    offer = http.post(
        "/api/v1/commerce/offers",
        headers=headers,
        json={"product_id": "prd_a1", "name": "Bound offer"},
    )
    assert offer.status_code == 201
    before = snapshot(harness.commerce.store)
    conflict = http.post(
        "/api/v1/commerce/prices",
        headers=headers,
        json={"offer_id": offer.json()["offer_id"], "amount": 1, "currency": "EUR"},
    )
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert snapshot(harness.commerce.store) == before


def test_key_cannot_move_between_cart_commands():
    env = world("gate-bind-u")
    http = env["http"]
    cart_id, offer_id = env["a"]["cart_id"], env["a"]["offer"]["offer_id"]
    headers = {"authorization": f"Bearer {SUBJECT_A}", "idempotency-key": "gate-bind-u"}
    added = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items",
        headers=headers,
        json={"offer_id": offer_id, "quantity": 1},
    )
    assert added.status_code == 200
    before = snapshot(env["harness"].commerce.store)
    conflict = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items/{offer_id}/quantity",
        headers=headers,
        json={"quantity": 1},
    )
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    removed = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items/{offer_id}/remove",
        headers=headers,
    )
    assert removed.status_code == 409
    assert envelope_of(removed)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert snapshot(env["harness"].commerce.store) == before


def test_checkout_key_is_bound_to_the_cart():
    harness = commerce_harness()
    http = harness.http()
    first_cart, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "gate-bind-co1")
    second_cart, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "gate-bind-co2")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "gate-bind-co",
    }
    first = http.post(
        "/api/v1/commerce/checkout", headers=headers, json={"cart_id": first_cart}
    )
    assert first.status_code == 201
    conflict = http.post(
        "/api/v1/commerce/checkout", headers=headers, json={"cart_id": second_cart}
    )
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.commerce.store.orders) == 1
    assert harness.commerce.store.lines_of_cart(second_cart) != []


def test_pay_key_is_bound_to_the_order():
    harness = commerce_harness()
    http = harness.http()
    first = placed_order(http, SUBJECT_A, "prd_a1", "gate-bind-pay1")
    second = placed_order(http, SUBJECT_A, "prd_a1", "gate-bind-pay2")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "gate-bind-pay",
    }
    paid = http.post(
        f"/api/v1/commerce/orders/{first['order_id']}/pay", headers=headers
    )
    assert paid.status_code == 200
    conflict = http.post(
        f"/api/v1/commerce/orders/{second['order_id']}/pay", headers=headers
    )
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert read_order(http, SUBJECT_A, second["order_id"]).json()["payment_state"] == (
        "PENDING_PAYMENT"
    )


# ----------------------------------------------------------- concurrency
def threaded_posts(
    app: Any, subject: str, path: str, payloads: list[dict | None], prefix: str
) -> list:
    """POST the payloads concurrently, each with its own idempotency key."""
    calls: list[tuple[str, str, dict | None]] = []
    for index, payload in enumerate(payloads):
        body = dict(payload) if payload else {}
        body["_headers"] = {
            "authorization": f"Bearer {subject}",
            "idempotency-key": f"{prefix}-{index}",
        }
        calls.append(("POST", path, body))
    barrier = threading.Barrier(len(calls), timeout=60)
    results: list = [None] * len(calls)

    def worker(index: int) -> None:
        _, path, body = calls[index]
        headers = body.pop("_headers")
        client = TestClient(app)
        try:
            barrier.wait()
            if body:
                response = client.post(path, headers=headers, json=body)
            else:
                response = client.post(path, headers=headers)
            results[index] = (response.status_code, response.json())
        except Exception as exc:  # pragma: no cover - worker must report any failure
            results[index] = exc
        finally:
            client.close()

    threads = [
        threading.Thread(target=worker, args=(index,)) for index in range(len(calls))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_concurrent_checkouts_create_exactly_one_order():
    harness = commerce_harness()
    http = harness.http()
    cart_id, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "gate-race-co")
    app = harness.commerce.contract_app()
    results = threaded_posts(
        app,
        SUBJECT_A,
        "/api/v1/commerce/checkout",
        [{"cart_id": cart_id}] * 3,
        "gate-race-co",
    )
    statuses = sorted(status for status, _ in results)
    assert statuses == [201, 409, 409], results
    for status, body in results:
        if status == 409:
            assert body["error"]["details"]["reason"] == "cart_empty", body
    store = harness.commerce.store
    assert len(store.orders) == 1
    # Domain serialization — not key dedupe: distinct keys were used, and
    # the audit shows consumption refusals, never an idempotency conflict.
    reasons = [event.reason for event in store.audit]
    assert reasons.count("permitted") >= 1
    assert reasons.count("cart_empty") == 2
    assert "idempotency_conflict" not in reasons


def test_concurrent_payments_move_once_with_facts_intact():
    harness = commerce_harness()
    http = harness.http()
    order = placed_order(http, SUBJECT_A, "prd_a1", "gate-race-pay")
    order_id = order["order_id"]
    before_lines = list(harness.commerce.store.lines_of_order(order_id))
    app = harness.commerce.contract_app()
    results = threaded_posts(
        app,
        SUBJECT_A,
        f"/api/v1/commerce/orders/{order_id}/pay",
        [None, None, None],
        "gate-race-pay",
    )
    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409, 409], results
    for status, body in results:
        if status == 409:
            assert body["error"]["details"]["reason"] == "invalid_state_transition"
    store = harness.commerce.store
    assert store.orders[order_id].payment_state == "PAID"
    assert list(store.lines_of_order(order_id)) == before_lines
    assert store.orders[order_id].buyer_identity_id == "idn_human_a"
    assert store.orders[order_id].tenant_id == "ten_a"


# ------------------------------------------------------ immutable facts
def test_public_api_cannot_mutate_purchase_facts():
    harness = commerce_harness()
    http = harness.http()
    order = placed_order(http, SUBJECT_A, "prd_a1", "gate-facts")
    order_id = order["order_id"]
    store = harness.commerce.store
    before = dataclasses.asdict(store.orders[order_id])
    before_lines = [dataclasses.asdict(line) for line in store.lines_of_order(order_id)]

    assert read_order(http, SUBJECT_A, order_id).status_code == 200
    assert pay_order(http, SUBJECT_A, "gate-facts-pay", order_id).status_code == 200
    # The catalog moves on; the bought position must not follow it.
    repriced = set_price(
        http, SUBJECT_A, "gate-facts-re", order["lines"][0]["offer_id"], 1, "EUR"
    )
    assert repriced.status_code == 201
    assert read_order(http, SUBJECT_A, order_id).status_code == 200
    # A second purchase touches only the second order.
    second = placed_order(http, SUBJECT_A, "prd_a1", "gate-facts-2")
    assert second["order_id"] != order_id

    after = dataclasses.asdict(store.orders[order_id])
    assert after["payment_state"] == "PAID"
    for field in (
        "tenant_id",
        "buyer_identity_id",
        "order_id",
        "owner_component",
        "created_at",
    ):
        assert after[field] == before[field], field
    assert [dataclasses.asdict(line) for line in store.lines_of_order(order_id)] == (
        before_lines
    )
    for line in before_lines:
        assert set(line) == {
            "order_line_id",
            "order_id",
            "tenant_id",
            "owner_component",
            "offer_id",
            "product_id",
            "amount",
            "currency",
            "quantity",
        }


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "buyer_identity_id",
        "offer_id",
        "product_id",
        "amount",
        "currency",
        "quantity",
    ],
)
def test_order_records_are_frozen(field):
    store = CommerceStore()
    store.seed_demo()
    order = OwnedOrder(
        order_id="ord_f",
        tenant_id="ten_a",
        owner_component="commerce",
        buyer_identity_id="idn_human_a",
        payment_state="PENDING_PAYMENT",
        created_at="2026-09-13T00:00:00+00:00",
        updated_at="2026-09-13T00:00:00+00:00",
    )
    from commerce_service.models import OwnedOrderLine

    line = OwnedOrderLine(
        order_line_id="orl_f",
        order_id="ord_f",
        tenant_id="ten_a",
        owner_component="commerce",
        offer_id="off_f",
        product_id="prd_a1",
        amount=100,
        currency="EUR",
        quantity=1,
    )
    store.register_order(order)
    store.register_order_line(line)
    if field in ("tenant_id", "buyer_identity_id"):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(store.orders["ord_f"], field, "mutated")
    else:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(store.order_lines["orl_f"], field, "mutated")


def test_store_exposes_no_order_mutation_api():
    verbs = ("update", "delete", "remove", "clear", "patch", "mutate", "set_")
    names = set(dir(CommerceStore))
    hits = {
        name
        for name in names
        if "order" in name and name.startswith(verbs) and name != "set_payment_state"
    }
    assert hits == set(), hits
    assert "set_payment_state" in names  # the single sanctioned state mutation
    assert {"clear", "reset", "drop"} & names == set()
    store = CommerceStore()
    store.seed_demo()
    order = OwnedOrder(
        order_id="ord_m",
        tenant_id="ten_a",
        owner_component="commerce",
        buyer_identity_id="idn_human_a",
        payment_state="PENDING_PAYMENT",
        created_at="2026-09-13T00:00:00+00:00",
        updated_at="2026-09-13T00:00:00+00:00",
    )
    store.register_order(order)
    before = dataclasses.asdict(store.orders["ord_m"])
    store.set_payment_state("ord_m", "PAID", now="2026-09-14T00:00:00+00:00")
    after = dataclasses.asdict(store.orders["ord_m"])
    changed = {key for key in after if after[key] != before[key]}
    assert changed == {"payment_state", "updated_at"}


def test_pay_with_unexpected_body_changes_only_the_state():
    harness = commerce_harness()
    http = harness.http()
    order = placed_order(http, SUBJECT_A, "prd_a1", "gate-paybody")
    order_id = order["order_id"]
    before_lines = list(harness.commerce.store.lines_of_order(order_id))
    response = http.post(
        f"/api/v1/commerce/orders/{order_id}/pay",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "gate-paybody",
        },
        json={"amount": 1, "currency": "USD", "quantity": 99, "buyer_identity_id": "x"},
    )
    assert response.status_code == 200
    store = harness.commerce.store
    assert store.orders[order_id].payment_state == "PAID"
    assert list(store.lines_of_order(order_id)) == before_lines


# ------------------------------------------------------------ snapshot
def test_snapshot_survives_two_reprices_and_payment():
    harness = commerce_harness()
    http = harness.http()
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "gate-snap")
    offer_id = offer["offer_id"]
    cart_id = open_cart(http, SUBJECT_A, "gate-snap-open").json()["cart_id"]
    assert (
        add_item(http, SUBJECT_A, "gate-snap-add", cart_id, offer_id, 2).status_code
        == 200
    )
    order = checkout(http, SUBJECT_A, "gate-snap-co", cart_id).json()
    order_id = order["order_id"]
    assert order["lines"] == [
        {
            "order_line_id": order["lines"][0]["order_line_id"],
            "offer_id": offer_id,
            "product_id": "prd_a1",
            "amount": 1999,
            "currency": "EUR",
            "quantity": 2,
        }
    ]
    for index, amount in enumerate((2499, 100)):
        repriced = set_price(
            http, SUBJECT_A, f"gate-snap-re{index}", offer_id, amount, "EUR"
        )
        assert repriced.status_code == 201
        reread = read_order(http, SUBJECT_A, order_id).json()
        assert reread["lines"] == order["lines"]
    assert pay_order(http, SUBJECT_A, "gate-snap-pay", order_id).status_code == 200
    assert read_order(http, SUBJECT_A, order_id).json()["lines"] == order["lines"]


# ----------------------------------------------------------------- audit
def test_audit_records_every_outcome_with_full_context():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    store = harness.commerce.store
    auth_a = {"authorization": f"Bearer {SUBJECT_A}"}
    auth_c = {"authorization": f"Bearer {SUBJECT_C}"}

    def audited(request_id, action, decision, reason):
        events = [
            event
            for event in audit_by_request(store, request_id)
            if event.action == action
        ]
        assert len(events) == 1, (request_id, events)
        event = events[0]
        assert event.action == action
        assert event.decision == decision
        assert event.reason == reason
        assert event.correlation_id == f"corr-{request_id}"
        assert event.timestamp
        return event

    product = create_product(http, SUBJECT_A, "gate-audit-p")
    assert product["product_id"]
    assert len(audit_by_request(store, "")) == 0  # generated ids never match ""
    # From here on every request carries explicit observability linkage.
    offer = http.post(
        "/api/v1/commerce/offers",
        headers={
            **auth_a,
            "idempotency-key": "gate-audit-o",
            "x-request-id": "req-o",
            "x-correlation-id": "corr-req-o",
        },
        json={"product_id": product["product_id"], "name": "Audited"},
    )
    assert offer.status_code == 201
    event = audited("req-o", "commerce.offers.create", "ALLOW", "permitted")
    assert (event.subject_id, event.tenant_id) == ("idn_human_a", "ten_a")
    assert event.resource_id == offer.json()["offer_id"]

    cart_id = open_cart(http, SUBJECT_A, "gate-audit-c").json()["cart_id"]
    added = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items",
        headers={
            **auth_a,
            "idempotency-key": "gate-audit-a",
            "x-request-id": "req-a",
            "x-correlation-id": "corr-req-a",
        },
        json={"offer_id": offer.json()["offer_id"], "quantity": 1},
    )
    assert added.status_code == 200
    event = audited("req-a", "commerce.carts.update", "ALLOW", "permitted")
    assert event.resource_id == cart_id

    # Price the offer, then check out with linkage.
    priced = set_price(
        http, SUBJECT_A, "gate-audit-pr", offer.json()["offer_id"], 50, "EUR"
    )
    assert priced.status_code == 201
    created = http.post(
        "/api/v1/commerce/checkout",
        headers={
            **auth_a,
            "idempotency-key": "gate-audit-co",
            "x-request-id": "req-co",
            "x-correlation-id": "corr-req-co",
        },
        json={"cart_id": cart_id},
    )
    assert created.status_code == 201
    event = audited("req-co", "commerce.checkout.create", "ALLOW", "permitted")
    order_id = created.json()["order_id"]
    assert event.resource_id == order_id

    paid = http.post(
        f"/api/v1/commerce/orders/{order_id}/pay",
        headers={
            **auth_a,
            "idempotency-key": "gate-audit-pay",
            "x-request-id": "req-pay",
            "x-correlation-id": "corr-req-pay",
        },
    )
    assert paid.status_code == 200
    event = audited("req-pay", "commerce.orders.pay", "ALLOW", "permitted")
    assert event.resource_id == order_id

    # Refusals: each audited, none mutating the business datasets.
    before = snapshot(store)
    denied = http.post(
        "/api/v1/commerce/offers",
        headers={
            **auth_c,
            "idempotency-key": "gate-audit-d",
            "x-request-id": "req-d",
            "x-correlation-id": "corr-req-d",
        },
        json={"product_id": product["product_id"], "name": "Denied"},
    )
    assert denied.status_code == 403
    event = audited("req-d", "commerce.offers.create", "DENY", "permission_not_granted")
    assert (event.subject_id, event.tenant_id) == ("idn_human_c", "ten_a")
    assert snapshot(store) == before

    before = snapshot(store)
    missing = http.get(
        "/api/v1/commerce/products/missing",
        headers={
            **auth_a,
            "x-request-id": "req-404",
            "x-correlation-id": "corr-req-404",
        },
    )
    assert missing.status_code == 404
    event = audited("req-404", "commerce.products.read", "DENY", "product_unknown")
    assert event.resource_id == "missing"
    assert event.subject_id is None and event.tenant_id is None
    assert snapshot(store) == before

    before = snapshot(store)
    conflict = http.post(
        "/api/v1/commerce/products",
        headers={
            **auth_a,
            "idempotency-key": "gate-audit-p",
            "x-request-id": "req-409",
            "x-correlation-id": "corr-req-409",
        },
        json={"name": "Changed", "description": "Conflict probe."},
    )
    assert conflict.status_code == 409
    event = audited(
        "req-409", "commerce.products.create", "DENY", "idempotency_conflict"
    )
    assert event.resource_id.startswith("prd_")
    guard_conflicts = [
        event
        for event in audit_by_request(store, "req-409")
        if event.action == "idempotency_conflict"
    ]
    assert len(guard_conflicts) == 1
    assert guard_conflicts[0].details["key"] == "gate-audit-p"
    assert guard_conflicts[0].details["operation"] == "commerce.products.create"
    assert snapshot(store) == before

    empty_cart = open_cart(http, SUBJECT_A, "gate-audit-empty").json()["cart_id"]
    before = snapshot(store)
    empty = http.post(
        "/api/v1/commerce/checkout",
        headers={
            **auth_a,
            "idempotency-key": "gate-audit-e",
            "x-request-id": "req-e",
            "x-correlation-id": "corr-req-e",
        },
        json={"cart_id": empty_cart},
    )
    assert empty.status_code == 409
    event = audited("req-e", "commerce.checkout.create", "DENY", "cart_empty")
    assert event.resource_id == empty_cart
    assert snapshot(store) == before

    owned_cart = open_cart(http, SUBJECT_A, "gate-audit-own").json()["cart_id"]
    before = snapshot(store)
    foreign = http.post(
        f"/api/v1/commerce/carts/{owned_cart}/items",
        headers={
            **auth_c,
            "idempotency-key": "gate-audit-f",
            "x-request-id": "req-f",
            "x-correlation-id": "corr-req-f",
        },
        json={"offer_id": offer.json()["offer_id"], "quantity": 1},
    )
    assert foreign.status_code == 403
    event = audited("req-f", "commerce.carts.update", "DENY", "buyer_mismatch")
    assert event.resource_id == owned_cart
    assert snapshot(store) == before


def test_checkout_replay_is_audited_without_second_effect():
    harness = commerce_harness()
    http = harness.http()
    cart_id, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "gate-audit-re")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "gate-audit-re-co",
    }
    first = http.post(
        "/api/v1/commerce/checkout", headers=headers, json={"cart_id": cart_id}
    )
    second = http.post(
        "/api/v1/commerce/checkout", headers=headers, json={"cart_id": cart_id}
    )
    assert first.status_code == 201
    assert second.json() == first.json()
    store = harness.commerce.store
    assert len(store.orders) == 1
    replays = [event for event in store.audit if event.action == "idempotency_replay"]
    assert len(replays) == 1
    assert replays[0].decision == "ALLOW"
    assert (replays[0].subject_id, replays[0].tenant_id) == ("idn_human_a", "ten_a")


# ------------------------------------------------------- audit immutability
def test_audit_journal_is_append_only():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    store = harness.commerce.store
    create_product(http, SUBJECT_A, "gate-apx-p")
    prefix = list(store.audit)
    assert prefix
    http.get(
        "/api/v1/commerce/products/missing",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    create_product(http, SUBJECT_A, "gate-apx-p2")
    assert store.audit[: len(prefix)] == prefix
    assert len(store.audit) > len(prefix)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prefix[0].reason = "mutated"  # type: ignore[misc]
    audit_attrs = {name for name in dir(store) if "audit" in name}
    assert audit_attrs == {"audit"}, audit_attrs
    assert isinstance(store.audit, list)


# ------------------------------------------------------------- fail-closed
def stubbed_deployment(
    authz_outcome: Any,
    *,
    tenant_outcome: Any = "wired",
    idempotency_outcome: Any = "real",
) -> Any:
    store = CommerceStore()
    store.seed_demo()
    tenant_context = (
        None
        if tenant_outcome == "unwired"
        else StubTenantContext(CTX_A if tenant_outcome == "wired" else tenant_outcome)
    )
    return build_commerce(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        authorization=StubAuthorizationPort(authz_outcome),
        tenant_context=tenant_context,
        idempotency=(
            None
            if idempotency_outcome == "real"
            else StubIdempotency(idempotency_outcome)
        ),
        store=store,
        seed_demo=False,
        with_http=True,
    )


def stocked_stub_store() -> CommerceStore:
    """Seeded store with a priced offer, a stocked cart and a pending order."""
    from commerce_service.models import (
        OwnedCartLine,
        OwnedOffer,
        OwnedOrderLine,
        OwnedPrice,
    )

    store = CommerceStore()
    store.seed_demo()
    store.register_offer(
        OwnedOffer(
            offer_id="off_stub",
            product_id="prd_a1",
            tenant_id="ten_a",
            owner_component="commerce",
            name="Stub offer",
            status="ACTIVE",
            created_by="idn_human_a",
            created_at="2026-09-13T00:00:00+00:00",
            updated_at="2026-09-13T00:00:00+00:00",
        )
    )
    store.register_price(
        OwnedPrice(
            price_id="prc_stub",
            offer_id="off_stub",
            tenant_id="ten_a",
            owner_component="commerce",
            amount=100,
            currency="EUR",
            created_at="2026-09-13T00:00:00+00:00",
        )
    )
    store.register_cart(
        OwnedCart(
            cart_id="crt_stub",
            tenant_id="ten_a",
            owner_component="commerce",
            buyer_identity_id="idn_human_a",
            created_at="2026-09-13T00:00:00+00:00",
            updated_at="2026-09-13T00:00:00+00:00",
        )
    )
    store.register_cart_line(
        OwnedCartLine(
            cart_id="crt_stub",
            offer_id="off_stub",
            tenant_id="ten_a",
            owner_component="commerce",
            quantity=1,
        )
    )
    store.register_order(
        OwnedOrder(
            order_id="ord_stub",
            tenant_id="ten_a",
            owner_component="commerce",
            buyer_identity_id="idn_human_a",
            payment_state="PENDING_PAYMENT",
            created_at="2026-09-13T00:00:00+00:00",
            updated_at="2026-09-13T00:00:00+00:00",
        )
    )
    store.register_order_line(
        OwnedOrderLine(
            order_line_id="orl_stub",
            order_id="ord_stub",
            tenant_id="ten_a",
            owner_component="commerce",
            offer_id="off_stub",
            product_id="prd_a1",
            amount=100,
            currency="EUR",
            quantity=1,
        )
    )
    return store


@pytest.mark.parametrize(
    "make_call",
    [
        lambda http, auth: http.get("/api/v1/commerce/products/prd_a1", headers=auth),
        lambda http, auth: http.post(
            "/api/v1/commerce/checkout",
            headers={**auth, "idempotency-key": "gate-fc-co"},
            json={"cart_id": "crt_stub"},
        ),
        lambda http, auth: http.post(
            "/api/v1/commerce/orders/ord_stub/pay",
            headers={**auth, "idempotency-key": "gate-fc-pay"},
        ),
    ],
    ids=["read", "checkout", "pay"],
)
def test_authorization_failure_fails_closed(make_call):
    deployment = stubbed_deployment(RuntimeError("authorization is down"))
    deployment.store = stocked_stub_store()
    deployment.engine.store = deployment.store
    http = TestClient(deployment.contract_app())
    before = snapshot(deployment.store)
    response = make_call(http, {"authorization": f"Bearer {SUBJECT_A}"})
    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"]["reason"] == "authorization_unavailable"
    assert snapshot(deployment.store) == before
    assert deployment.store.audit[-1].reason == "authorization_unavailable"


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/v1/commerce/products", {"name": "X", "description": "X"}),
        ("/api/v1/commerce/offers", {"product_id": "prd_a1", "name": "X"}),
        (
            "/api/v1/commerce/prices",
            {"offer_id": "off_x", "amount": 1, "currency": "EUR"},
        ),
        ("/api/v1/commerce/carts", None),
    ],
    ids=["product", "offer", "price", "cart"],
)
def test_tenant_context_failure_fails_closed_on_creates(path, payload):
    deployment = stubbed_deployment(
        ALLOW_A, tenant_outcome=RuntimeError("identity is down")
    )
    http = TestClient(deployment.contract_app())
    before = snapshot(deployment.store)
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": f"gate-fc-tc-{path}",
    }
    response = (
        http.post(path, headers=headers, json=payload)
        if payload
        else http.post(path, headers=headers)
    )
    assert response.status_code == 503
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "authorization_unavailable"
    )
    assert snapshot(deployment.store) == before


def test_unwired_tenant_context_fails_closed_without_caller_fallback():
    deployment = commerce_deployment(StubAuthorizationPort(ALLOW_A))
    http = TestClient(deployment.contract_app())
    before = snapshot(deployment.store)
    response = http.post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "gate-fc-unwired",
            "x-tenant-id": "ten_b",
        },
        json={"name": "X", "description": "X"},
    )
    assert response.status_code == 503
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "authorization_unavailable"
    )
    assert snapshot(deployment.store) == before


def test_claimed_tenant_never_selects_the_effective_tenant():
    deployment = stubbed_deployment(ALLOW_A)
    http = TestClient(deployment.contract_app())
    response = http.post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "gate-fc-claimed",
            "x-tenant-id": "ten_b",
        },
        json={"name": "Claimed", "description": "Cross-check probe."},
    )
    assert response.status_code == 201
    created = deployment.store.products[response.json()["product_id"]]
    assert created.tenant_id == "ten_a"
    assert deployment.store.audit[-1].claimed_tenant_id == "ten_b"


@pytest.mark.parametrize(
    "make_call",
    [
        lambda http, auth: http.post(
            "/api/v1/commerce/products",
            headers={**auth, "idempotency-key": "gate-fc-idem-p"},
            json={"name": "X", "description": "X"},
        ),
        lambda http, auth: http.post(
            "/api/v1/commerce/carts/crt_stub/items",
            headers={**auth, "idempotency-key": "gate-fc-idem-a"},
            json={"offer_id": "off_stub", "quantity": 1},
        ),
        lambda http, auth: http.post(
            "/api/v1/commerce/checkout",
            headers={**auth, "idempotency-key": "gate-fc-idem-co"},
            json={"cart_id": "crt_stub"},
        ),
        lambda http, auth: http.post(
            "/api/v1/commerce/orders/ord_stub/pay",
            headers={**auth, "idempotency-key": "gate-fc-idem-pay"},
        ),
    ],
    ids=["create", "add", "checkout", "pay"],
)
def test_idempotency_failure_fails_closed_without_mutation(make_call):
    deployment = stubbed_deployment(
        ALLOW_A, idempotency_outcome=RuntimeError("is-005 is down")
    )
    deployment.store = stocked_stub_store()
    deployment.engine.store = deployment.store
    http = TestClient(deployment.contract_app())
    before = snapshot(deployment.store)
    response = make_call(http, {"authorization": f"Bearer {SUBJECT_A}"})
    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"]["reason"] == "authorization_unavailable"
    assert "RuntimeError" not in response.text
    assert snapshot(deployment.store) == before
    refused = deployment.store.audit[-1]
    assert refused.decision == "DENY"
    assert refused.details["failing_dependency"] == "idempotency"


# ------------------------------------------------------------ public API
def test_internal_operations_are_not_routable():
    internal = {
        "create_product",
        "serve_product",
        "create_offer",
        "create_price",
        "create_cart",
        "add_to_cart",
        "set_cart_line_quantity",
        "remove_cart_line",
        "cart_contents",
        "checkout_cart",
        "order_contents",
        "set_payment_state",
        "current_price",
        "lines_of_order",
        "lines_of_cart",
        "register_product",
        "register_offer",
        "ownership_of_product",
    }
    routes = commerce_harness().commerce.contract_app().routes
    for route in routes:
        path = getattr(route, "path", "")
        for name in internal:
            assert name not in path, (path, name)
    text = Path("components/commerce/contract/openapi.yaml").read_text()
    for name in internal:
        assert name not in text, name


def test_engine_contains_no_policy_vocabulary():
    """Commerce decides nothing: no grant, policy, role or permission logic."""
    stems = ("grant", "policy", "polic", "role", "permission", "privilege")
    for module in ("engine", "store", "api"):
        tree = ast.parse(Path(f"src/commerce_service/{module}.py").read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [node.name]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.arg):
                names = [node.arg]
            for name in names:
                lowered = name.lower()
                assert not any(stem in lowered for stem in stems), (module, name)


# -------------------------------------------------------------- boundary
def test_no_foreign_system_identifiers_in_commerce():
    stems = (
        "refund",
        "coupon",
        "subscri",
        "loyalty",
        "warehouse",
        "shipping",
        "crm",
        "analy",
        "marketplace",
        "promo",
        "discount",
        "gateway",
        "provider",
        "customer",
        "profile",
        "enroll",
        "bundle",
        "saga",
    )
    package = Path("src/commerce_service")
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert (
                        alias.name.split(".")[0] not in FORBIDDEN_PACKAGES
                    ), alias.name
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".")[0]
                assert module not in FORBIDDEN_PACKAGES, node.module
                continue
            names = []
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [node.name]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.arg):
                names = [node.arg]
            for name in names:
                lowered = name.lower()
                assert not any(stem in lowered for stem in stems), (path.name, name)


def test_public_surface_lists_no_extra_operations():
    import json

    contract = json.loads(
        Path("components/commerce/contract/component_contract.json").read_text()
    )
    declared = contract["api"]["consumer_surface"]["operations"]
    methods = {
        name
        for name, member in vars(CommerceClient).items()
        if callable(member) and not name.startswith("_")
    }
    assert methods == set(declared)
    assert len(methods) == 12


# ---------------------------------------------------------------- quality
def test_commerce_tests_use_no_sleeps_or_wall_clock_waits():
    needles = ["sl" + "eep(", "time" + ".time(", "datetime" + ".now("]
    for path in sorted(Path("tests").glob("test_commerce_*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, (path.name, needle)
