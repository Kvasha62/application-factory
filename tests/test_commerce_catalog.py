"""SCS-002 Commerce Stage 3 — catalog commands through the chain.

Define one Offer and set one Price against the fully composed Platform
Instance: success, same-tenant parentage, append-only price history,
validation, grant denials, idempotent replay and conflict, and audit.
"""

from __future__ import annotations

from typing import Any

import pytest

from commerce_service.errors import AccessRefused
from commerce_service.store import CommerceStore
from tests.test_commerce_skeleton import (
    SUBJECT_A,
    SUBJECT_B,
    SUBJECT_C,
    commerce_harness,
    envelope_of,
)


def create_product(http: Any, subject: str, key: str) -> dict:
    created = http.post(
        "/api/v1/commerce/products",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
        json={"name": "Catalog product", "description": "Created by a catalog test."},
    )
    assert created.status_code == 201, created.text
    return created.json()


def define_offer(
    http: Any, subject: str, key: str, product_id: str, name: str = "Offer"
) -> Any:
    return http.post(
        "/api/v1/commerce/offers",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
        json={"product_id": product_id, "name": name},
    )


def set_price(
    http: Any, subject: str, key: str, offer_id: str, amount: int, currency: str
) -> Any:
    return http.post(
        "/api/v1/commerce/prices",
        headers={"authorization": f"Bearer {subject}", "idempotency-key": key},
        json={"offer_id": offer_id, "amount": amount, "currency": currency},
    )


# ------------------------------------------------------------------ success
def test_offer_defined_and_price_appended_to_history():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    product = create_product(http, SUBJECT_A, "catalog-flow-product")

    created = define_offer(http, SUBJECT_A, "catalog-flow-offer", product["product_id"])
    assert created.status_code == 201, created.text
    offer = created.json()
    assert offer["product_id"] == product["product_id"]
    assert offer["status"] == "ACTIVE"
    assert offer["created_by"] == "idn_human_a"
    assert "tenant_id" not in offer

    first = set_price(
        http, SUBJECT_A, "catalog-flow-price-1", offer["offer_id"], 1999, "EUR"
    )
    assert first.status_code == 201, first.text
    second = set_price(
        http, SUBJECT_A, "catalog-flow-price-2", offer["offer_id"], 2499, "EUR"
    )
    assert second.status_code == 201, second.text
    assert second.json()["price_id"] != first.json()["price_id"]

    history = [
        price
        for price in harness.commerce.store.prices.values()
        if price.offer_id == offer["offer_id"]
    ]
    assert {(price.amount, price.currency) for price in history} == {
        (1999, "EUR"),
        (2499, "EUR"),
    }


def test_published_client_defines_offer_and_sets_price():
    harness = commerce_harness(store=CommerceStore())
    client = harness.client()
    product = client.create_product(
        SUBJECT_A,
        name="Client product",
        description="Created through the published client.",
        idempotency_key="catalog-client-product",
    )
    offer = client.create_offer(
        SUBJECT_A,
        product_id=product.product_id,
        name="Client offer",
        idempotency_key="catalog-client-offer",
    )
    assert offer.product_id == product.product_id
    price = client.create_price(
        SUBJECT_A,
        offer_id=offer.offer_id,
        amount=1999,
        currency="EUR",
        idempotency_key="catalog-client-price",
    )
    assert price.offer_id == offer.offer_id
    assert (price.amount, price.currency) == (1999, "EUR")
    with pytest.raises(AccessRefused) as exc:
        client.create_offer(
            SUBJECT_A,
            product_id="missing",
            name="No parent",
            idempotency_key="catalog-client-missing",
        )
    assert exc.value.reason == "product_unknown"


# ----------------------------------------------------------------- parentage
def test_offer_requires_a_parent_product_of_the_same_tenant():
    harness = commerce_harness()
    http = harness.http()

    missing = define_offer(http, SUBJECT_A, "catalog-parent-1", "missing")
    assert missing.status_code == 404
    body = envelope_of(missing)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "product_unknown"

    # Tenant B's product is not a usable parent in Tenant A: merged with
    # the missing parent, disclosing nothing about Tenant B.
    foreign = define_offer(http, SUBJECT_A, "catalog-parent-2", "prd_b1")
    assert foreign.status_code == 404
    body = envelope_of(foreign)
    assert body["error"]["details"]["reason"] == "product_unknown"


def test_price_requires_a_parent_offer_of_the_same_tenant():
    harness = commerce_harness()
    http = harness.http()

    missing = set_price(http, SUBJECT_A, "catalog-pparent-1", "missing", 1999, "EUR")
    assert missing.status_code == 404
    body = envelope_of(missing)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "offer_unknown"

    foreign_offer = define_offer(http, SUBJECT_B, "catalog-pparent-2", "prd_b1")
    assert foreign_offer.status_code == 201
    foreign = set_price(
        http,
        SUBJECT_A,
        "catalog-pparent-3",
        foreign_offer.json()["offer_id"],
        1999,
        "EUR",
    )
    assert foreign.status_code == 404
    body = envelope_of(foreign)
    assert body["error"]["details"]["reason"] == "offer_unknown"


# ---------------------------------------------------------------- validation
def test_blank_offer_name_is_a_validation_error():
    harness = commerce_harness()
    response = define_offer(
        harness.http(), SUBJECT_A, "catalog-invalid-1", "prd_a1", "   "
    )
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "validation_error"
    assert harness.commerce.store.offers == {}


def test_negative_amount_is_a_validation_error():
    harness = commerce_harness()
    http = harness.http()
    offer = define_offer(http, SUBJECT_A, "catalog-amount-1", "prd_a1").json()
    response = set_price(
        http, SUBJECT_A, "catalog-amount-2", offer["offer_id"], -1, "EUR"
    )
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "validation_error"


def test_fractional_amount_is_a_schema_refusal():
    harness = commerce_harness()
    http = harness.http()
    offer = define_offer(http, SUBJECT_A, "catalog-frac-1", "prd_a1").json()
    response = http.post(
        "/api/v1/commerce/prices",
        headers={
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "catalog-frac-2",
        },
        json={"offer_id": offer["offer_id"], "amount": 19.99, "currency": "EUR"},
    )
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["reason"] == "malformed_request"


def test_currency_must_be_a_short_non_empty_code():
    harness = commerce_harness()
    http = harness.http()
    offer = define_offer(http, SUBJECT_A, "catalog-cur-1", "prd_a1").json()
    for key, currency in (
        ("catalog-cur-2", ""),
        ("catalog-cur-3", "EURO"),
    ):
        response = set_price(http, SUBJECT_A, key, offer["offer_id"], 1999, currency)
        assert response.status_code == 422, currency
        body = envelope_of(response)
        assert body["error"]["details"]["reason"] == "validation_error"
    assert harness.commerce.store.prices == {}


# -------------------------------------------------------------------- grants
def test_subject_without_the_grant_cannot_define_an_offer():
    harness = commerce_harness()
    response = define_offer(harness.http(), SUBJECT_C, "catalog-nogrant-1", "prd_a1")
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["details"]["reason"] == "permission_not_granted"
    assert harness.commerce.store.offers == {}


def test_subject_without_the_grant_cannot_set_a_price():
    harness = commerce_harness()
    http = harness.http()
    offer = define_offer(http, SUBJECT_A, "catalog-nogrant-2", "prd_a1").json()
    response = set_price(
        http, SUBJECT_C, "catalog-nogrant-3", offer["offer_id"], 1999, "EUR"
    )
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["details"]["reason"] == "permission_not_granted"
    assert harness.commerce.store.prices == {}


# --------------------------------------------------------------- idempotency
def test_offer_replay_returns_the_recorded_offer():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    product = create_product(http, SUBJECT_A, "catalog-replay-product")
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "catalog-replay-offer",
    }
    payload = {"product_id": product["product_id"], "name": "Offer"}
    first = http.post("/api/v1/commerce/offers", headers=headers, json=payload)
    second = http.post("/api/v1/commerce/offers", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(harness.commerce.store.offers) == 1


def test_offer_conflict_on_a_changed_payload():
    harness = commerce_harness()
    http = harness.http()
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "catalog-conflict-offer",
    }
    first = http.post(
        "/api/v1/commerce/offers",
        headers=headers,
        json={"product_id": "prd_a1", "name": "First"},
    )
    assert first.status_code == 201
    second = http.post(
        "/api/v1/commerce/offers",
        headers=headers,
        json={"product_id": "prd_a1", "name": "Second"},
    )
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.commerce.store.offers) == 1


def test_price_replay_returns_the_recorded_price():
    harness = commerce_harness()
    http = harness.http()
    offer = define_offer(http, SUBJECT_A, "catalog-replay-p1", "prd_a1").json()
    headers = {
        "authorization": f"Bearer {SUBJECT_A}",
        "idempotency-key": "catalog-replay-price",
    }
    payload = {"offer_id": offer["offer_id"], "amount": 1999, "currency": "EUR"}
    first = http.post("/api/v1/commerce/prices", headers=headers, json=payload)
    second = http.post("/api/v1/commerce/prices", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(harness.commerce.store.prices) == 1


# --------------------------------------------------------------------- audit
def test_catalog_commands_are_audited_with_linkage():
    harness = commerce_harness(store=CommerceStore())
    http = harness.http()
    product = create_product(http, SUBJECT_A, "catalog-audit-product")
    offer = define_offer(http, SUBJECT_A, "catalog-audit-offer", product["product_id"])
    assert offer.status_code == 201
    price = set_price(
        http, SUBJECT_A, "catalog-audit-price", offer.json()["offer_id"], 1999, "EUR"
    )
    assert price.status_code == 201

    journal = harness.commerce.store.audit
    offers = [event for event in journal if event.action == "commerce.offers.create"]
    assert len(offers) == 1
    assert offers[0].decision == "ALLOW"
    assert offers[0].subject_id == "idn_human_a"
    assert offers[0].tenant_id == "ten_a"
    assert offers[0].resource_id == offer.json()["offer_id"]
    prices = [event for event in journal if event.action == "commerce.prices.create"]
    assert len(prices) == 1
    assert prices[0].decision == "ALLOW"
    assert prices[0].resource_id == price.json()["price_id"]
