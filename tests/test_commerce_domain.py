"""SCS-002 Commerce Stage 3 — domain and storage behavior.

The owned Commerce data behavior: the ``OwnedProduct`` / ``OwnedOffer`` /
``OwnedPrice`` / ``OwnedCart`` / ``OwnedOrder`` / ``OwnedOrderLine`` models
and the store-level registration, reads, tenant isolation, parentage,
cart mutations, checkout conversion, snapshot and payment-state
invariants. These tests touch only the model and the store — no HTTP
surface, no engine and no contract participates: they prove the
owned-data operations behind the published commands.
"""

from __future__ import annotations

import dataclasses

import pytest

from commerce_service.models import (
    OwnedCart,
    OwnedCartLine,
    OwnedOffer,
    OwnedOrder,
    OwnedOrderLine,
    OwnedPrice,
    OwnedProduct,
)
from commerce_service.store import (
    PAYMENT_STATES,
    CommerceStore,
    DomainRefusal,
)

WHEN = "2026-09-13T00:00:00+00:00"


def product(product_id="prd_t1", tenant_id="ten_a") -> OwnedProduct:
    return OwnedProduct(
        product_id=product_id,
        tenant_id=tenant_id,
        owner_component="commerce",
        name="Test product",
        description="A product for domain tests.",
        status="ACTIVE",
        created_by="idn_human_a",
        created_at=WHEN,
        updated_at=WHEN,
    )


def offer(offer_id="off_t1", product_id="prd_t1", tenant_id="ten_a") -> OwnedOffer:
    return OwnedOffer(
        offer_id=offer_id,
        product_id=product_id,
        tenant_id=tenant_id,
        owner_component="commerce",
        name="Test offer",
        status="ACTIVE",
        created_by="idn_human_a",
        created_at=WHEN,
        updated_at=WHEN,
    )


def price(
    price_id="prc_t1", offer_id="off_t1", tenant_id="ten_a", amount=1999
) -> OwnedPrice:
    return OwnedPrice(
        price_id=price_id,
        offer_id=offer_id,
        tenant_id=tenant_id,
        owner_component="commerce",
        amount=amount,
        currency="EUR",
        created_at=WHEN,
    )


def cart(cart_id="crt_t1", tenant_id="ten_a") -> OwnedCart:
    return OwnedCart(
        cart_id=cart_id,
        tenant_id=tenant_id,
        owner_component="commerce",
        buyer_identity_id="idn_human_c",
        created_at=WHEN,
        updated_at=WHEN,
    )


def order(order_id="ord_t1", tenant_id="ten_a") -> OwnedOrder:
    return OwnedOrder(
        order_id=order_id,
        tenant_id=tenant_id,
        owner_component="commerce",
        buyer_identity_id="idn_human_c",
        payment_state="PENDING_PAYMENT",
        created_at=WHEN,
        updated_at=WHEN,
    )


def order_line(
    order_line_id="orl_t1",
    order_id="ord_t1",
    tenant_id="ten_a",
    amount=1999,
    quantity=2,
) -> OwnedOrderLine:
    return OwnedOrderLine(
        order_line_id=order_line_id,
        order_id=order_id,
        tenant_id=tenant_id,
        owner_component="commerce",
        offer_id="off_t1",
        product_id="prd_t1",
        amount=amount,
        currency="EUR",
        quantity=quantity,
    )


def catalog(store: CommerceStore) -> None:
    """Register the Product → Offer → Price chain of Tenant A."""
    store.register_product(product())
    store.register_offer(offer())
    store.register_price(price())


# ------------------------------------------------------- ownership metadata
def test_every_record_carries_tenant_and_single_owner():
    store = CommerceStore()
    catalog(store)
    store.register_cart(cart())
    store.register_cart_line(
        OwnedCartLine(
            cart_id="crt_t1",
            offer_id="off_t1",
            tenant_id="ten_a",
            owner_component="commerce",
            quantity=2,
        )
    )
    store.register_order(order())
    store.register_order_line(order_line())
    assert store.ownership_of_product("prd_t1").tenant_id == "ten_a"
    assert store.ownership_of_offer("off_t1").tenant_id == "ten_a"
    assert store.ownership_of_price("prc_t1").tenant_id == "ten_a"
    assert store.ownership_of_cart("crt_t1").tenant_id == "ten_a"
    assert store.ownership_of_order("ord_t1").tenant_id == "ten_a"
    assert store.ownership_of_order_line("orl_t1").tenant_id == "ten_a"
    for record in (
        store.products["prd_t1"],
        store.offers["off_t1"],
        store.prices["prc_t1"],
        store.carts["crt_t1"],
        store.orders["ord_t1"],
        store.order_lines["orl_t1"],
    ):
        assert record.owner_component == "commerce"
    assert store.cart_lines[("crt_t1", "off_t1")].owner_component == "commerce"


def test_unknown_records_have_no_ownership():
    store = CommerceStore()
    assert store.ownership_of_product("missing") is None
    assert store.ownership_of_offer("missing") is None
    assert store.ownership_of_price("missing") is None
    assert store.ownership_of_cart("missing") is None
    assert store.ownership_of_order("missing") is None
    assert store.ownership_of_order_line("missing") is None


def test_foreign_owner_is_never_registered():
    store = CommerceStore()
    foreign = dataclasses.replace(product(), owner_component="learning")
    with pytest.raises(DomainRefusal) as exc:
        store.register_product(foreign)
    assert exc.value.reason == "owner_mismatch"
    assert store.ownership_of_product("prd_t1") is None


# ---------------------------------------------------------- Product → Offer
def test_offer_requires_an_existing_product_of_the_same_tenant():
    store = CommerceStore()
    with pytest.raises(DomainRefusal) as exc:
        store.register_offer(offer())
    assert exc.value.reason == "product_unknown"

    store.register_product(product())
    store.register_product(product("prd_t2", "ten_b"))
    with pytest.raises(DomainRefusal) as exc:
        store.register_offer(offer("off_t9", "prd_t2", "ten_a"))
    assert exc.value.reason == "tenant_mismatch"

    registered = store.register_offer(offer())
    assert registered.product_id == "prd_t1"
    assert registered.tenant_id == "ten_a"


def test_price_requires_an_existing_offer_of_the_same_tenant():
    store = CommerceStore()
    store.register_product(product())
    with pytest.raises(DomainRefusal) as exc:
        store.register_price(price())
    assert exc.value.reason == "offer_unknown"

    store.register_offer(offer())
    store.register_product(product("prd_t2", "ten_b"))
    store.register_offer(offer("off_t2", "prd_t2", "ten_b"))
    with pytest.raises(DomainRefusal) as exc:
        store.register_price(price("prc_t9", "off_t2", "ten_a"))
    assert exc.value.reason == "tenant_mismatch"

    registered = store.register_price(price())
    assert registered.offer_id == "off_t1"
    assert registered.amount == 1999
    assert registered.currency == "EUR"


def test_price_history_is_append_only():
    store = CommerceStore()
    catalog(store)
    store.register_price(price("prc_t2", amount=2499))
    assert store.prices["prc_t1"].amount == 1999
    assert store.prices["prc_t2"].amount == 2499


def test_price_amount_is_a_non_negative_integer():
    store = CommerceStore()
    catalog(store)
    for bad in (-1, True, 19.99, "1999"):
        with pytest.raises(DomainRefusal) as exc:
            store.register_price(price("prc_bad", amount=bad))
        assert exc.value.reason == "amount_invalid"


def test_duplicate_identifiers_are_refused():
    store = CommerceStore()
    catalog(store)
    with pytest.raises(DomainRefusal) as exc:
        store.register_product(product())
    assert exc.value.reason == "duplicate_registration"
    with pytest.raises(DomainRefusal) as exc:
        store.register_offer(offer())
    assert exc.value.reason == "duplicate_registration"
    with pytest.raises(DomainRefusal) as exc:
        store.register_price(price())
    assert exc.value.reason == "duplicate_registration"


# ------------------------------------------------------------------ no price
def test_product_and_offer_carry_no_price():
    product_fields = {f.name for f in dataclasses.fields(OwnedProduct)}
    offer_fields = {f.name for f in dataclasses.fields(OwnedOffer)}
    assert "price" not in product_fields
    assert "price" not in offer_fields
    assert "amount" not in product_fields
    assert "amount" not in offer_fields
    price_fields = {f.name for f in dataclasses.fields(OwnedPrice)}
    assert {"amount", "currency"} <= price_fields


# ------------------------------------------------------------------- cart
def test_cart_line_requires_cart_and_offer_of_the_same_tenant():
    store = CommerceStore()
    catalog(store)
    store.register_cart(cart())
    line = OwnedCartLine(
        cart_id="crt_t1",
        offer_id="off_t1",
        tenant_id="ten_a",
        owner_component="commerce",
        quantity=1,
    )
    assert store.register_cart_line(line) is line

    store.register_product(product("prd_t2", "ten_b"))
    store.register_offer(offer("off_t2", "prd_t2", "ten_b"))
    foreign_line = dataclasses.replace(line, offer_id="off_t2")
    with pytest.raises(DomainRefusal) as exc:
        store.register_cart_line(foreign_line)
    assert exc.value.reason == "tenant_mismatch"

    missing_cart = dataclasses.replace(line, cart_id="crt_missing")
    with pytest.raises(DomainRefusal) as exc:
        store.register_cart_line(missing_cart)
    assert exc.value.reason == "cart_unknown"

    missing_offer = dataclasses.replace(dataclasses.replace(line), offer_id="off_x")
    with pytest.raises(DomainRefusal) as exc:
        store.register_cart_line(missing_offer)
    assert exc.value.reason == "offer_unknown"


def test_one_line_per_cart_and_offer():
    store = CommerceStore()
    catalog(store)
    store.register_cart(cart())
    line = OwnedCartLine(
        cart_id="crt_t1",
        offer_id="off_t1",
        tenant_id="ten_a",
        owner_component="commerce",
        quantity=1,
    )
    store.register_cart_line(line)
    with pytest.raises(DomainRefusal) as exc:
        store.register_cart_line(line)
    assert exc.value.reason == "duplicate_cart_line"


def test_cart_line_quantity_is_a_positive_integer():
    for bad in (0, -2, True, "2"):
        store = CommerceStore()
        catalog(store)
        store.register_cart(cart())
        line = OwnedCartLine(
            cart_id="crt_t1",
            offer_id="off_t1",
            tenant_id="ten_a",
            owner_component="commerce",
            quantity=bad,
        )
        with pytest.raises(DomainRefusal) as exc:
            store.register_cart_line(line)
        assert exc.value.reason == "quantity_invalid"


def test_cart_and_order_reference_an_opaque_buyer_only():
    cart_fields = {f.name for f in dataclasses.fields(OwnedCart)}
    order_fields = {f.name for f in dataclasses.fields(OwnedOrder)}
    assert "buyer_identity_id" in cart_fields
    assert "buyer_identity_id" in order_fields
    forbidden = {"customer", "user", "profile", "email", "phone", "name", "address"}
    assert forbidden.isdisjoint(cart_fields), cart_fields
    assert forbidden.isdisjoint(order_fields), order_fields


# ------------------------------------------------------------------ order
def test_order_line_requires_an_existing_order_of_the_same_tenant():
    store = CommerceStore()
    with pytest.raises(DomainRefusal) as exc:
        store.register_order_line(order_line())
    assert exc.value.reason == "order_unknown"

    store.register_order(order())
    store.register_order(order("ord_t2", "ten_b"))
    with pytest.raises(DomainRefusal) as exc:
        store.register_order_line(order_line("orl_t9", "ord_t2", "ten_a"))
    assert exc.value.reason == "tenant_mismatch"

    registered = store.register_order_line(order_line())
    assert registered.order_id == "ord_t1"
    assert (registered.amount, registered.currency) == (1999, "EUR")


def test_price_snapshot_is_never_recomputed_from_the_catalog():
    store = CommerceStore()
    catalog(store)
    store.register_order(order())
    store.register_order_line(order_line())
    # The current price changes after the purchase: a new Price record.
    store.register_price(price("prc_t2", amount=2499))
    line = store.order_lines["orl_t1"]
    assert (line.amount, line.currency) == (1999, "EUR")
    assert (line.offer_id, line.product_id) == ("off_t1", "prd_t1")


def test_purchase_facts_are_immutable_after_creation():
    store = CommerceStore()
    catalog(store)
    store.register_order(order())
    stored = store.orders["ord_t1"]
    store.register_order_line(order_line())
    stored_line = store.order_lines["orl_t1"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        stored.buyer_identity_id = "idn_other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        stored_line.amount = 1  # type: ignore[misc]
    assert "set_order" not in dir(store)
    assert "update_order" not in dir(store)
    assert "update_order_line" not in dir(store)


def test_payment_state_is_mutable_and_nothing_else_moves_with_it():
    store = CommerceStore()
    catalog(store)
    store.register_order(order())
    store.register_order_line(order_line())
    updated = store.set_payment_state("ord_t1", "PAID", now="2026-09-14T00:00:00+00:00")
    assert updated.payment_state == "PAID"
    assert updated.updated_at == "2026-09-14T00:00:00+00:00"
    assert updated.buyer_identity_id == "idn_human_c"
    assert updated.tenant_id == "ten_a"
    assert store.order_lines["orl_t1"].amount == 1999
    assert PAYMENT_STATES == ("PENDING_PAYMENT", "PAID")
    with pytest.raises(DomainRefusal) as exc:
        store.set_payment_state("ord_t1", "REFUNDED", now=WHEN)
    assert exc.value.reason == "unknown_payment_state"
    with pytest.raises(KeyError):
        store.set_payment_state("ord_missing", "PAID", now=WHEN)


def test_payment_state_allows_only_pending_to_paid():
    store = CommerceStore()
    catalog(store)
    store.register_order(order())
    with pytest.raises(DomainRefusal) as exc:
        store.set_payment_state("ord_t1", "PENDING_PAYMENT", now=WHEN)
    assert exc.value.reason == "invalid_state_transition"
    store.set_payment_state("ord_t1", "PAID", now=WHEN)
    with pytest.raises(DomainRefusal) as exc:
        store.set_payment_state("ord_t1", "PAID", now=WHEN)
    assert exc.value.reason == "invalid_state_transition"
    assert store.orders["ord_t1"].payment_state == "PAID"


def test_order_children_read_back_in_deterministic_order():
    store = CommerceStore()
    catalog(store)
    store.register_order(order())
    store.register_order_line(order_line("orl_b"))
    store.register_order_line(order_line("orl_a"))
    assert [line.order_line_id for line in store.lines_of_order("ord_t1")] == [
        "orl_a",
        "orl_b",
    ]


# ------------------------------------------------- Stage 3 creation effects
def test_create_offer_price_and_cart_effects():
    store = CommerceStore()
    store.register_product(product())
    created_offer = store.create_offer(
        tenant_id="ten_a",
        product_id="prd_t1",
        name="Effect offer",
        created_by="idn_human_a",
        now=WHEN,
    )
    assert created_offer.status == "ACTIVE"
    assert created_offer.tenant_id == "ten_a"
    created_price = store.create_price(
        tenant_id="ten_a",
        offer_id=created_offer.offer_id,
        amount=1999,
        currency="EUR",
        now=WHEN,
    )
    assert created_price.offer_id == created_offer.offer_id
    created_cart = store.create_cart(
        tenant_id="ten_a", buyer_identity_id="idn_human_a", now=WHEN
    )
    assert created_cart.buyer_identity_id == "idn_human_a"
    assert store.lines_of_cart(created_cart.cart_id) == []


def test_creation_effects_refuse_an_unusable_parent():
    store = CommerceStore()
    with pytest.raises(DomainRefusal) as exc:
        store.create_offer(
            tenant_id="ten_a",
            product_id="missing",
            name="No parent",
            created_by="idn_human_a",
            now=WHEN,
        )
    assert exc.value.reason == "product_unknown"
    with pytest.raises(DomainRefusal) as exc:
        store.create_price(
            tenant_id="ten_a",
            offer_id="missing",
            amount=1999,
            currency="EUR",
            now=WHEN,
        )
    assert exc.value.reason == "offer_unknown"


# ------------------------------------------------- Stage 3 cart line effects
def stocked(store: CommerceStore) -> str:
    """The catalog of Tenant A plus one cart holding two units of off_t1."""
    catalog(store)
    store.register_cart(cart())
    store.add_to_cart("crt_t1", "off_t1", quantity=2, now=WHEN)
    return "crt_t1"


def test_add_to_cart_increments_the_single_line():
    store = CommerceStore()
    cart_id = stocked(store)
    line = store.add_to_cart(cart_id, "off_t1", quantity=3, now=WHEN)
    assert line.quantity == 5
    assert store.lines_of_cart(cart_id) == [line]
    assert store.carts[cart_id].updated_at == WHEN


def test_add_to_cart_requires_cart_and_offer_of_the_same_tenant():
    store = CommerceStore()
    catalog(store)
    store.register_cart(cart())
    with pytest.raises(DomainRefusal) as exc:
        store.add_to_cart("crt_t1", "missing", quantity=1, now=WHEN)
    assert exc.value.reason == "offer_unknown"
    with pytest.raises(DomainRefusal) as exc:
        store.add_to_cart("missing", "off_t1", quantity=1, now=WHEN)
    assert exc.value.reason == "cart_unknown"
    store.register_product(product("prd_t2", "ten_b"))
    store.register_offer(offer("off_t2", "prd_t2", "ten_b"))
    with pytest.raises(DomainRefusal) as exc:
        store.add_to_cart("crt_t1", "off_t2", quantity=1, now=WHEN)
    assert exc.value.reason == "tenant_mismatch"
    for bad in (0, -1, True, "2"):
        with pytest.raises(DomainRefusal) as exc:
            store.add_to_cart("crt_t1", "off_t1", quantity=bad, now=WHEN)
        assert exc.value.reason == "quantity_invalid"


def test_set_quantity_is_absolute_and_requires_the_line():
    store = CommerceStore()
    cart_id = stocked(store)
    line = store.set_cart_line_quantity(cart_id, "off_t1", quantity=7, now=WHEN)
    assert line.quantity == 7
    with pytest.raises(DomainRefusal) as exc:
        store.set_cart_line_quantity(cart_id, "off_missing", quantity=1, now=WHEN)
    assert exc.value.reason == "cart_line_unknown"
    with pytest.raises(DomainRefusal) as exc:
        store.set_cart_line_quantity(cart_id, "off_t1", quantity=0, now=WHEN)
    assert exc.value.reason == "quantity_invalid"


def test_remove_cart_line_deletes_the_line():
    store = CommerceStore()
    cart_id = stocked(store)
    store.remove_cart_line(cart_id, "off_t1", now=WHEN)
    assert store.lines_of_cart(cart_id) == []
    with pytest.raises(DomainRefusal) as exc:
        store.remove_cart_line(cart_id, "off_t1", now=WHEN)
    assert exc.value.reason == "cart_line_unknown"


def test_cart_and_order_contents_serve_records_with_lines():
    store = CommerceStore()
    cart_id = stocked(store)
    served_cart, lines = store.cart_contents(cart_id)
    assert served_cart.cart_id == cart_id
    assert [line.offer_id for line in lines] == ["off_t1"]
    store.register_order(order())
    store.register_order_line(order_line())
    served_order, order_lines = store.order_contents("ord_t1")
    assert served_order.order_id == "ord_t1"
    assert [line.order_line_id for line in order_lines] == ["orl_t1"]
    with pytest.raises(KeyError):
        store.cart_contents("missing")
    with pytest.raises(KeyError):
        store.order_contents("missing")


def test_current_price_is_the_latest_registration():
    store = CommerceStore()
    catalog(store)
    assert store.current_price("off_t1").price_id == "prc_t1"
    store.register_price(price("prc_t2", amount=2499))
    latest = store.current_price("off_t1")
    assert (latest.price_id, latest.amount) == ("prc_t2", 2499)
    assert store.current_price("off_missing") is None


def test_product_id_of_offer_resolves_the_parent():
    store = CommerceStore()
    catalog(store)
    assert store.product_id_of_offer("off_t1") == "prd_t1"
    with pytest.raises(KeyError):
        store.product_id_of_offer("missing")


# ------------------------------------------------------- Stage 3 checkout
def test_checkout_cart_converts_lines_and_consumes_the_cart():
    store = CommerceStore()
    cart_id = stocked(store)
    order_record, lines = store.checkout_cart(
        cart_id, buyer_identity_id="idn_human_c", now=WHEN
    )
    assert order_record.payment_state == "PENDING_PAYMENT"
    assert order_record.buyer_identity_id == "idn_human_c"
    assert order_record.tenant_id == "ten_a"
    assert len(lines) == 1
    (line,) = lines
    assert line.order_id == order_record.order_id
    assert (line.offer_id, line.product_id) == ("off_t1", "prd_t1")
    assert (line.amount, line.currency, line.quantity) == (1999, "EUR", 2)
    assert store.lines_of_cart(cart_id) == []
    assert store.lines_of_order(order_record.order_id) == [line]


def test_checkout_cart_refuses_foreign_buyer_empty_cart_and_gaps():
    store = CommerceStore()
    cart_id = stocked(store)
    with pytest.raises(DomainRefusal) as exc:
        store.checkout_cart(cart_id, buyer_identity_id="idn_other", now=WHEN)
    assert exc.value.reason == "buyer_mismatch"

    store.register_cart(cart("crt_empty"))
    with pytest.raises(DomainRefusal) as exc:
        store.checkout_cart("crt_empty", buyer_identity_id="idn_human_c", now=WHEN)
    assert exc.value.reason == "cart_empty"

    del store.prices["prc_t1"]
    with pytest.raises(DomainRefusal) as exc:
        store.checkout_cart(cart_id, buyer_identity_id="idn_human_c", now=WHEN)
    assert exc.value.reason == "price_unknown"

    store.register_price(price())
    del store.offers["off_t1"]
    with pytest.raises(DomainRefusal) as exc:
        store.checkout_cart(cart_id, buyer_identity_id="idn_human_c", now=WHEN)
    assert exc.value.reason == "offer_unknown"


def test_checkout_copies_quantity_into_the_immutable_snapshot():
    store = CommerceStore()
    catalog(store)
    store.register_cart(cart())
    store.add_to_cart("crt_t1", "off_t1", quantity=3, now=WHEN)
    _, lines = store.checkout_cart("crt_t1", buyer_identity_id="idn_human_c", now=WHEN)
    (line,) = lines
    assert line.quantity == 3
    # The catalog moves on; the bought position never follows it.
    store.register_price(price("prc_t2", amount=2499))
    assert (line.amount, line.currency, line.quantity) == (1999, "EUR", 3)
    assert store.order_lines[line.order_line_id] == line
