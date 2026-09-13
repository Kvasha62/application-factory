"""SCS-002 Commerce Stage 3 — cart commands through the chain.

Open, read and mutate one owned Cart against the fully composed Platform
Instance: success, increment semantics, own-carts-only, tenant isolation,
validation, grant denials, idempotent replay and conflict, and audit.
"""

from __future__ import annotations

from typing import Any

from commerce_service.contracts import PUBLISHED_DENY_REASONS
from commerce_service.store import CommerceStore
from tests.test_commerce_skeleton import (
    SUBJECT_A,
    SUBJECT_B,
    SUBJECT_C,
    commerce_harness,
    envelope_of,
)


def priced_offer(http: Any, subject: str, product_id: str, prefix: str) -> dict:
    offer = http.post(
        "/api/v1/commerce/offers",
        headers={
            "authorization": f"Bearer {subject}",
            "idempotency-key": f"{prefix}-offer",
        },
        json={"product_id": product_id, "name": "Cart offer"},
    )
    assert offer.status_code == 201, offer.text
    priced = http.post(
        "/api/v1/commerce/prices",
        headers={
            "authorization": f"Bearer {subject}",
            "idempotency-key": f"{prefix}-price",
        },
        json={"offer_id": offer.json()["offer_id"], "amount": 1999, "currency": "EUR"},
    )
    assert priced.status_code == 201, priced.text
    return offer.json()


def open_cart(http: Any, subject: str, key: str) -> Any:
    return http.post(
        "/api/v1/commerce/carts",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
    )


def add_item(
    http: Any, subject: str, key: str, cart_id: str, offer_id: str, quantity: int
) -> Any:
    return http.post(
        f"/api/v1/commerce/carts/{cart_id}/items",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
        json={"offer_id": offer_id, "quantity": quantity},
    )


def set_quantity(
    http: Any, subject: str, key: str, cart_id: str, offer_id: str, quantity: int
) -> Any:
    return http.post(
        f"/api/v1/commerce/carts/{cart_id}/items/{offer_id}/quantity",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
        json={"quantity": quantity},
    )


def remove_item(http: Any, subject: str, key: str, cart_id: str, offer_id: str) -> Any:
    return http.post(
        f"/api/v1/commerce/carts/{cart_id}/items/{offer_id}/remove",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
    )


# ------------------------------------------------------------------ success
def test_cart_opened_read_and_mutated():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    product = http.post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "cart-flow-p",
        },
        json={"name": "Cart product", "description": "Created by a cart test."},
    ).json()
    offer = priced_offer(http, SUBJECT_A, product["product_id"], "cart-flow")

    opened = open_cart(http, SUBJECT_A, "cart-flow-open")
    assert opened.status_code == 201, opened.text
    cart = opened.json()
    assert cart["items"] == []
    assert cart["buyer_identity_id"] == "idn_human_a"
    assert "tenant_id" not in cart
    cart_id = cart["cart_id"]

    read = http.get(
        f"/api/v1/commerce/carts/{cart_id}",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert read.status_code == 200
    assert read.json() == cart

    added = add_item(http, SUBJECT_A, "cart-flow-add1", cart_id, offer["offer_id"], 2)
    assert added.status_code == 200, added.text
    assert added.json()["items"] == [
        {
            "offer_id": offer["offer_id"],
            "product_id": product["product_id"],
            "quantity": 2,
        }
    ]

    # A repeated offer increments its line: no second line ever exists.
    again = add_item(http, SUBJECT_A, "cart-flow-add2", cart_id, offer["offer_id"], 3)
    assert again.status_code == 200
    assert again.json()["items"] == [
        {
            "offer_id": offer["offer_id"],
            "product_id": product["product_id"],
            "quantity": 5,
        }
    ]

    absolute = set_quantity(
        http, SUBJECT_A, "cart-flow-set", cart_id, offer["offer_id"], 7
    )
    assert absolute.status_code == 200
    assert absolute.json()["items"][0]["quantity"] == 7

    removed = remove_item(http, SUBJECT_A, "cart-flow-del", cart_id, offer["offer_id"])
    assert removed.status_code == 200
    assert removed.json()["items"] == []


def test_published_client_runs_the_cart_workflow():
    harness = commerce_harness(store=CommerceStore())
    client = harness.client()
    product = client.create_product(
        SUBJECT_A,
        name="Client product",
        description="Created through the published client.",
        idempotency_key="cart-client-p",
    )
    offer = client.create_offer(
        SUBJECT_A,
        product_id=product.product_id,
        name="Client offer",
        idempotency_key="cart-client-o",
    )
    cart = client.create_cart(SUBJECT_A, idempotency_key="cart-client-open")
    assert cart.items == ()
    cart = client.add_cart_item(
        SUBJECT_A,
        cart.cart_id,
        offer_id=offer.offer_id,
        quantity=2,
        idempotency_key="cart-client-add",
    )
    assert [(item.offer_id, item.quantity) for item in cart.items] == [
        (offer.offer_id, 2)
    ]
    read = client.read_cart(SUBJECT_A, cart.cart_id)
    assert read == cart


# ------------------------------------------------------------- item parentage
def test_add_requires_an_offer_of_the_carts_tenant():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-parent-open").json()["cart_id"]

    missing = add_item(http, SUBJECT_A, "cart-parent-1", cart_id, "missing", 1)
    assert missing.status_code == 404
    body = envelope_of(missing)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "offer_unknown"

    foreign = priced_offer(http, SUBJECT_B, "prd_b1", "cart-parent")
    response = add_item(
        http, SUBJECT_A, "cart-parent-2", cart_id, foreign["offer_id"], 1
    )
    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["details"]["reason"] == "offer_unknown"


def test_set_and_remove_require_an_existing_line():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-line-open").json()["cart_id"]
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "cart-line")

    missing_set = set_quantity(
        http, SUBJECT_A, "cart-line-1", cart_id, offer["offer_id"], 3
    )
    assert missing_set.status_code == 404
    body = envelope_of(missing_set)
    assert body["error"]["details"]["reason"] == "cart_line_unknown"

    missing_remove = remove_item(
        http, SUBJECT_A, "cart-line-2", cart_id, offer["offer_id"]
    )
    assert missing_remove.status_code == 404
    body = envelope_of(missing_remove)
    assert body["error"]["details"]["reason"] == "cart_line_unknown"


def test_unknown_cart_is_a_404():
    harness = commerce_harness()
    http = harness.http()
    read = http.get(
        "/api/v1/commerce/carts/missing",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert read.status_code == 404
    assert envelope_of(read)["error"]["details"]["reason"] == "cart_unknown"
    response = add_item(http, SUBJECT_A, "cart-missing-1", "missing", "off_x", 1)
    assert response.status_code == 404
    assert envelope_of(response)["error"]["details"]["reason"] == "cart_unknown"


# ---------------------------------------------------------------- validation
def test_non_positive_quantity_is_a_validation_error():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-qty-open").json()["cart_id"]
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "cart-qty")
    for key, call in (
        (
            "cart-qty-1",
            lambda: add_item(
                http, SUBJECT_A, "cart-qty-1", cart_id, offer["offer_id"], 0
            ),
        ),
        (
            "cart-qty-2",
            lambda: set_quantity(
                http, SUBJECT_A, "cart-qty-2", cart_id, offer["offer_id"], 0
            ),
        ),
    ):
        response = call()
        assert response.status_code == 422, key
        body = envelope_of(response)
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["details"]["reason"] == "validation_error"


# ------------------------------------------------------- ownership isolation
def test_cross_tenant_cart_access_is_denied_by_the_authority():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-own-open").json()["cart_id"]
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "cart-own")
    added = add_item(http, SUBJECT_A, "cart-own-add", cart_id, offer["offer_id"], 1)
    assert added.status_code == 200

    read = http.get(
        f"/api/v1/commerce/carts/{cart_id}",
        headers={"authorization": f"Bearer {SUBJECT_B}"},
    )
    assert read.status_code == 403
    body = envelope_of(read)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] in PUBLISHED_DENY_REASONS
    assert body["error"]["details"]["reason"] != "cart_unknown"

    mutate = add_item(http, SUBJECT_B, "cart-own-mut", cart_id, offer["offer_id"], 1)
    assert mutate.status_code == 403
    body = envelope_of(mutate)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] not in ("cart_unknown", "buyer_mismatch")

    read_back = http.get(
        f"/api/v1/commerce/carts/{cart_id}",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert read_back.json()["items"][0]["quantity"] == 1


def test_same_tenant_buyer_mutates_only_own_carts():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-mut-open").json()["cart_id"]
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "cart-mut")
    added = add_item(http, SUBJECT_A, "cart-mut-add", cart_id, offer["offer_id"], 1)
    assert added.status_code == 200

    for key, call in (
        (
            "cart-mut-1",
            lambda: add_item(
                http, SUBJECT_C, "cart-mut-1", cart_id, offer["offer_id"], 1
            ),
        ),
        (
            "cart-mut-2",
            lambda: set_quantity(
                http, SUBJECT_C, "cart-mut-2", cart_id, offer["offer_id"], 9
            ),
        ),
        (
            "cart-mut-3",
            lambda: remove_item(
                http, SUBJECT_C, "cart-mut-3", cart_id, offer["offer_id"]
            ),
        ),
    ):
        response = call()
        assert response.status_code == 403, key
        assert envelope_of(response)["error"]["details"]["reason"] == "buyer_mismatch"

    read = http.get(
        f"/api/v1/commerce/carts/{cart_id}",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert read.json()["items"][0]["quantity"] == 1


def test_subject_without_the_grant_cannot_read_a_cart():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-grant-open").json()["cart_id"]
    response = http.get(
        f"/api/v1/commerce/carts/{cart_id}",
        headers={"authorization": f"Bearer {SUBJECT_C}"},
    )
    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"] == "permission_not_granted"
    )


# --------------------------------------------------------------- idempotency
def test_add_replay_returns_the_recorded_cart():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-replay-open").json()["cart_id"]
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "cart-replay")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "cart-replay-add",
    }
    payload = {"offer_id": offer["offer_id"], "quantity": 2}
    first = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items", headers=headers, json=payload
    )
    second = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items", headers=headers, json=payload
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["items"][0]["quantity"] == 2


def test_add_conflict_on_a_changed_quantity():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-conflict-open").json()["cart_id"]
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "cart-conflict")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "cart-conflict-add",
    }
    first = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items",
        headers=headers,
        json={"offer_id": offer["offer_id"], "quantity": 2},
    )
    assert first.status_code == 200
    second = http.post(
        f"/api/v1/commerce/carts/{cart_id}/items",
        headers=headers,
        json={"offer_id": offer["offer_id"], "quantity": 3},
    )
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# --------------------------------------------------------------------- audit
def test_cart_commands_are_audited_with_linkage():
    harness = commerce_harness()
    http = harness.http()
    cart_id = open_cart(http, SUBJECT_A, "cart-audit-open").json()["cart_id"]
    offer = priced_offer(http, SUBJECT_A, "prd_a1", "cart-audit")
    added = add_item(http, SUBJECT_A, "cart-audit-add", cart_id, offer["offer_id"], 1)
    assert added.status_code == 200

    journal = harness.commerce.store.audit
    opened = [event for event in journal if event.action == "commerce.carts.create"]
    assert len(opened) == 1
    assert opened[0].decision == "ALLOW"
    assert opened[0].subject_id == "idn_human_a"
    assert opened[0].tenant_id == "ten_a"
    assert opened[0].resource_id == cart_id
    updated = [event for event in journal if event.action == "commerce.carts.update"]
    assert len(updated) == 1
    assert updated[0].decision == "ALLOW"
    assert updated[0].resource_id == cart_id
