"""SCS-002 Commerce Stage 3 — order reads and payment through the chain.

Read one owned Order and record its payment against the fully composed
Platform Instance: success, immutable purchase facts, the single
PENDING_PAYMENT → PAID move, own-orders-only, grant denials, idempotent
replay and conflict, and audit. No Payment Service exists: recording the
payment moves Commerce-owned state only.
"""

from __future__ import annotations

from typing import Any

from commerce_service.contracts import PUBLISHED_DENY_REASONS
from commerce_service.store import CommerceStore
from tests.test_commerce_checkout import checkout, stocked_cart
from tests.test_commerce_skeleton import (
    SUBJECT_A,
    SUBJECT_B,
    SUBJECT_C,
    commerce_harness,
    envelope_of,
)


def read_order(http: Any, subject: str, order_id: str) -> Any:
    return http.get(
        f"/api/v1/commerce/orders/{order_id}",
        headers={"authorization": f"Bearer {subject}"},
    )


def pay_order(http: Any, subject: str, key: str, order_id: str) -> Any:
    return http.post(
        f"/api/v1/commerce/orders/{order_id}/pay",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
    )


def placed_order(http: Any, subject: str, product_id: str, prefix: str) -> dict:
    """One PENDING_PAYMENT order of ``subject`` for two priced units."""
    cart_id, _ = stocked_cart(http, subject, product_id, f"{prefix}-stock")
    created = checkout(http, subject, f"{prefix}-co", cart_id)
    assert created.status_code == 201, created.text
    return created.json()


# ------------------------------------------------------------------ success
def test_order_read_and_paid_with_facts_untouched():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    product = http.post(
        "/api/v1/commerce/products",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "ord-flow-p",
        },
        json={"name": "Order product", "description": "Created by an order test."},
    ).json()
    order = placed_order(http, SUBJECT_A, product["product_id"], "ord-flow")
    order_id = order["order_id"]

    read = read_order(http, SUBJECT_A, order_id)
    assert read.status_code == 200
    assert read.json() == order

    paid = pay_order(http, SUBJECT_A, "ord-flow-pay", order_id)
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert body["payment_state"] == "PAID"
    assert body["lines"] == order["lines"]
    assert body["buyer_identity_id"] == order["buyer_identity_id"]

    reread = read_order(http, SUBJECT_A, order_id)
    assert reread.status_code == 200
    assert reread.json() == body


def test_published_client_reads_and_pays():
    harness = commerce_harness(store=CommerceStore())
    client = harness.client()
    product = client.create_product(
        SUBJECT_A,
        name="Client product",
        description="Created through the published client.",
        idempotency_key="ord-client-p",
    )
    offer = client.create_offer(
        SUBJECT_A,
        product_id=product.product_id,
        name="Client offer",
        idempotency_key="ord-client-o",
    )
    client.create_price(
        SUBJECT_A,
        offer_id=offer.offer_id,
        amount=1999,
        currency="EUR",
        idempotency_key="ord-client-pr",
    )
    cart = client.create_cart(SUBJECT_A, idempotency_key="ord-client-open")
    client.add_cart_item(
        SUBJECT_A,
        cart.cart_id,
        offer_id=offer.offer_id,
        quantity=1,
        idempotency_key="ord-client-add",
    )
    order = client.checkout(
        SUBJECT_A, cart_id=cart.cart_id, idempotency_key="ord-client-co"
    )
    assert client.read_order(SUBJECT_A, order.order_id) == order
    paid = client.pay_order(SUBJECT_A, order.order_id, idempotency_key="ord-client-pay")
    assert paid.payment_state == "PAID"
    assert paid.lines == order.lines


# -------------------------------------------------------------------- refusal
def test_only_a_pending_order_may_be_recorded_as_paid():
    harness = commerce_harness()
    http = harness.http()
    order = placed_order(http, SUBJECT_A, "prd_a1", "ord-state")
    first = pay_order(http, SUBJECT_A, "ord-state-pay1", order["order_id"])
    assert first.status_code == 200

    second = pay_order(http, SUBJECT_A, "ord-state-pay2", order["order_id"])
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert body["error"]["details"]["reason"] == "invalid_state_transition"


def test_unknown_order_is_a_404():
    harness = commerce_harness()
    http = harness.http()
    read = read_order(http, SUBJECT_A, "missing")
    assert read.status_code == 404
    assert envelope_of(read)["error"]["details"]["reason"] == "order_unknown"
    pay = pay_order(http, SUBJECT_A, "ord-missing-pay", "missing")
    assert pay.status_code == 404
    assert envelope_of(pay)["error"]["details"]["reason"] == "order_unknown"


def test_subject_reaches_only_own_orders():
    harness = commerce_harness()
    http = harness.http()
    order = placed_order(http, SUBJECT_A, "prd_a1", "ord-own")

    # Same tenant, another buyer, no read grant: the authority denies first.
    no_grant = read_order(http, SUBJECT_C, order["order_id"])
    assert no_grant.status_code == 403
    assert (
        envelope_of(no_grant)["error"]["details"]["reason"] == "permission_not_granted"
    )

    # Same tenant, another buyer, grant held: the owned-data buyer check.
    same_tenant = pay_order(http, SUBJECT_C, "ord-own-pay-c", order["order_id"])
    assert same_tenant.status_code == 403
    assert envelope_of(same_tenant)["error"]["details"]["reason"] == "buyer_mismatch"

    # Another tenant: the authority denies before any owned-data check.
    cross = read_order(http, SUBJECT_B, order["order_id"])
    assert cross.status_code == 403
    body = envelope_of(cross)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] in PUBLISHED_DENY_REASONS
    assert body["error"]["details"]["reason"] != "order_unknown"

    reread = read_order(http, SUBJECT_A, order["order_id"])
    assert reread.json()["payment_state"] == "PENDING_PAYMENT"


# --------------------------------------------------------------- idempotency
def test_pay_replay_returns_the_recorded_order():
    harness = commerce_harness()
    http = harness.http()
    order = placed_order(http, SUBJECT_A, "prd_a1", "ord-replay")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "ord-replay-pay",
    }
    first = http.post(
        f"/api/v1/commerce/orders/{order['order_id']}/pay", headers=headers
    )
    second = http.post(
        f"/api/v1/commerce/orders/{order['order_id']}/pay", headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["payment_state"] == "PAID"


def test_pay_conflict_on_a_changed_order():
    harness = commerce_harness()
    http = harness.http()
    first = placed_order(http, SUBJECT_A, "prd_a1", "ord-conflict-1")
    second = placed_order(http, SUBJECT_A, "prd_a1", "ord-conflict-2")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "ord-conflict-pay",
    }
    paid = http.post(
        f"/api/v1/commerce/orders/{first['order_id']}/pay", headers=headers
    )
    assert paid.status_code == 200
    conflict = http.post(
        f"/api/v1/commerce/orders/{second['order_id']}/pay", headers=headers
    )
    assert conflict.status_code == 409
    body = envelope_of(conflict)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert read_order(http, SUBJECT_A, second["order_id"]).json()["payment_state"] == (
        "PENDING_PAYMENT"
    )


# --------------------------------------------------------------------- audit
def test_order_commands_are_audited_with_linkage():
    harness = commerce_harness()
    http = harness.http()
    order = placed_order(http, SUBJECT_A, "prd_a1", "ord-audit")
    paid = pay_order(http, SUBJECT_A, "ord-audit-pay", order["order_id"])
    assert paid.status_code == 200

    journal = harness.commerce.store.audit
    reads = [event for event in journal if event.action == "commerce.orders.read"]
    assert reads == []
    payments = [event for event in journal if event.action == "commerce.orders.pay"]
    assert len(payments) == 1
    assert payments[0].decision == "ALLOW"
    assert payments[0].subject_id == "idn_human_a"
    assert payments[0].tenant_id == "ten_a"
    assert payments[0].resource_id == order["order_id"]
