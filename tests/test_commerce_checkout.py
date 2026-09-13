"""SCS-002 Commerce Stage 3 — checkout through the chain.

Convert one owned Cart into an Order against the fully composed Platform
Instance: snapshots, cart consumption, current-price selection, replay
without a second order, empty-cart and missing-price refusals, buyer
isolation, and audit. Checkout is a business command, not an entity.
"""

from __future__ import annotations

from typing import Any

from commerce_service.contracts import PUBLISHED_DENY_REASONS
from commerce_service.store import CommerceStore
from tests.test_commerce_cart import add_item, open_cart, priced_offer
from tests.test_commerce_skeleton import (
    SUBJECT_A,
    SUBJECT_B,
    SUBJECT_C,
    commerce_harness,
    envelope_of,
)


def checkout(http: Any, subject: str, key: str, cart_id: str) -> Any:
    return http.post(
        "/api/v1/commerce/checkout",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
        json={"cart_id": cart_id},
    )


def stocked_cart(
    http: Any, subject: str, product_id: str, prefix: str
) -> tuple[str, dict]:
    """One cart of ``subject`` holding two units of a priced offer."""
    offer = priced_offer(http, subject, product_id, f"{prefix}-cat")
    cart_id = open_cart(http, subject, f"{prefix}-open").json()["cart_id"]
    added = add_item(http, subject, f"{prefix}-add", cart_id, offer["offer_id"], 2)
    assert added.status_code == 200, added.text
    return cart_id, offer


# ------------------------------------------------------------------ success
def test_checkout_creates_an_order_and_consumes_the_cart():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    product = http.post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "co-flow-p",
        },
        json={"name": "Checkout product", "description": "Created by a checkout test."},
    ).json()
    cart_id, offer = stocked_cart(http, SUBJECT_A, product["product_id"], "co-flow")

    created = checkout(http, SUBJECT_A, "co-flow-co", cart_id)
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["payment_state"] == "PENDING_PAYMENT"
    assert order["buyer_identity_id"] == "idn_human_a"
    assert "tenant_id" not in order
    assert order["lines"] == [
        {
            "order_line_id": order["lines"][0]["order_line_id"],
            "offer_id": offer["offer_id"],
            "product_id": product["product_id"],
            "amount": 1999,
            "currency": "EUR",
            "quantity": 2,
        }
    ]

    read = http.get(
        f"/api/v1/commerce/carts/{cart_id}",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert read.status_code == 200
    assert read.json()["items"] == []


def test_checkout_uses_the_current_price_of_each_offer():
    harness = commerce_harness()
    http = harness.http()
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "co-cur")
    cart_id = open_cart(http, SUBJECT_A, "co-cur-open").json()["cart_id"]
    added = add_item(http, SUBJECT_A, "co-cur-add", cart_id, offer["offer_id"], 1)
    assert added.status_code == 200
    repriced = http.post(
        "/api/v1/commerce/prices",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "co-cur-reprice",
        },
        json={"offer_id": offer["offer_id"], "amount": 2499, "currency": "EUR"},
    )
    assert repriced.status_code == 201

    created = checkout(http, SUBJECT_A, "co-cur-co", cart_id)
    assert created.status_code == 201
    assert created.json()["lines"][0]["amount"] == 2499


def test_published_client_checks_out():
    harness = commerce_harness(store=CommerceStore())
    client = harness.client()
    product = client.create_product(
        SUBJECT_A,
        name="Client product",
        description="Created through the published client.",
        idempotency_key="co-client-p",
    )
    offer = client.create_offer(
        SUBJECT_A,
        product_id=product.product_id,
        name="Client offer",
        idempotency_key="co-client-o",
    )
    client.create_price(
        SUBJECT_A,
        offer_id=offer.offer_id,
        amount=1999,
        currency="EUR",
        idempotency_key="co-client-pr",
    )
    cart = client.create_cart(SUBJECT_A, idempotency_key="co-client-open")
    client.add_cart_item(
        SUBJECT_A,
        cart.cart_id,
        offer_id=offer.offer_id,
        quantity=1,
        idempotency_key="co-client-add",
    )
    order = client.checkout(
        SUBJECT_A, cart_id=cart.cart_id, idempotency_key="co-client-co"
    )
    assert order.payment_state == "PENDING_PAYMENT"
    assert len(order.lines) == 1
    assert client.read_cart(SUBJECT_A, cart.cart_id).items == ()


# --------------------------------------------------------------- idempotency
def test_checkout_replay_returns_the_recorded_order_without_a_second_order():
    harness = commerce_harness()
    http = harness.http()
    cart_id, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "co-replay")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "co-replay-co",
    }
    payload = {"cart_id": cart_id}
    first = http.post("/api/v1/commerce/checkout", headers=headers, json=payload)
    second = http.post("/api/v1/commerce/checkout", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(harness.commerce.store.orders) == 1

    # The cart was consumed by the first checkout: a new key refuses.
    again = checkout(http, SUBJECT_A, "co-replay-again", cart_id)
    assert again.status_code == 409
    assert envelope_of(again)["error"]["details"]["reason"] == "cart_empty"


def test_checkout_conflict_on_a_changed_cart():
    harness = commerce_harness()
    http = harness.http()
    first_cart, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "co-conflict-1")
    second_cart, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "co-conflict-2")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "co-conflict-co",
    }
    first = http.post(
        "/api/v1/commerce/checkout", headers=headers, json={"cart_id": first_cart}
    )
    assert first.status_code == 201
    second = http.post(
        "/api/v1/commerce/checkout", headers=headers, json={"cart_id": second_cart}
    )
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.commerce.store.orders) == 1


# -------------------------------------------------------------------- refusal
def test_checkout_of_an_empty_cart_is_a_409():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "co-empty-open").json()["cart_id"]
    response = checkout(http, SUBJECT_A, "co-empty-co", cart_id)
    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert body["error"]["details"]["reason"] == "cart_empty"
    assert harness.commerce.store.orders == {}


def test_checkout_without_a_current_price_is_a_404():
    harness = commerce_harness()
    http = harness.http()
    offer = http.post(
        "/api/v1/commerce/offers",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "co-noprice-o",
        },
        json={"product_id": "prd_a1", "name": "Unpriced offer"},
    ).json()
    cart_id = open_cart(http, SUBJECT_A, "co-noprice-open").json()["cart_id"]
    added = add_item(http, SUBJECT_A, "co-noprice-add", cart_id, offer["offer_id"], 1)
    assert added.status_code == 200
    response = checkout(http, SUBJECT_A, "co-noprice-co", cart_id)
    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "price_unknown"
    assert harness.commerce.store.orders == {}


def test_checkout_of_an_unknown_cart_is_a_404():
    harness = commerce_harness()
    response = checkout(harness.http(), SUBJECT_A, "co-missing-co", "missing")
    assert response.status_code == 404
    assert envelope_of(response)["error"]["details"]["reason"] == "cart_unknown"


def test_checkout_of_anothers_cart_is_refused():
    harness = commerce_harness()
    http = harness.http()
    cart_id, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "co-foreign")

    # Same tenant, another buyer: the owned-data buyer check refuses.
    same_tenant = checkout(http, SUBJECT_C, "co-foreign-c", cart_id)
    assert same_tenant.status_code == 403
    assert envelope_of(same_tenant)["error"]["details"]["reason"] == "buyer_mismatch"

    # Another tenant: the authority denies before any owned-data check.
    cross = checkout(http, SUBJECT_B, "co-foreign-b", cart_id)
    assert cross.status_code == 403
    body = envelope_of(cross)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] in PUBLISHED_DENY_REASONS
    assert body["error"]["details"]["reason"] != "cart_unknown"

    assert harness.commerce.store.orders == {}


# --------------------------------------------------------------------- audit
def test_checkout_is_audited_with_linkage():
    harness = commerce_harness()
    http = harness.http()
    cart_id, _ = stocked_cart(http, SUBJECT_A, "prd_a1", "co-audit")
    created = checkout(http, SUBJECT_A, "co-audit-co", cart_id)
    assert created.status_code == 201

    journal = harness.commerce.store.audit
    checkouts = [
        event for event in journal if event.action == "commerce.checkout.create"
    ]
    assert len(checkouts) == 1
    assert checkouts[0].decision == "ALLOW"
    assert checkouts[0].subject_id == "idn_human_a"
    assert checkouts[0].tenant_id == "ten_a"
    assert checkouts[0].resource_id == created.json()["order_id"]
