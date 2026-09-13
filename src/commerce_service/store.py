"""Owned data of the Commerce component (logical schema `commerce`).

This module is internal. No other component has direct access to it:
products, offers, prices, carts, orders and the audit journal are reachable
only through the published contract of this component (ARCHITECTURE.md §1.1,
LAW-04).

The store separates the two ways its data is touched, and the separation
is the enforcement story of the component:

* :meth:`CommerceStore.ownership_of_product` and its siblings —
  enforcement metadata: whether a record exists here and who its single
  owner is. The engine reads them to form the question it asks IS-003;
  they serve nothing to a caller;
* :meth:`CommerceStore.create_product`, :meth:`CommerceStore.create_offer`,
  :meth:`CommerceStore.create_price`, :meth:`CommerceStore.create_cart`,
  the cart-line mutations, :meth:`CommerceStore.cart_contents`,
  :meth:`CommerceStore.checkout_cart`, :meth:`CommerceStore.order_contents`
  and :meth:`CommerceStore.set_payment_state` — the owned-data operations
  themselves. The engine calls them only after the enforcement chain
  produced an ``ALLOW``.

Registration (:meth:`CommerceStore.register_product` and its siblings) is
where the ADR-0013 structural invariants live: every child is registered
under an existing parent of the same Tenant — or the registration is
refused outright. Registration is never a caller declaration: the engine
commands of later slices call the same methods inside their IS-005
effects. The purchase facts of an Order are immutable by construction:
records are frozen, and no store operation mutates an Order, an OrderLine,
or a registered Price.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from commerce_service import OWNER_COMPONENT
from commerce_service.models import (
    AccessAuditEvent,
    OwnedCart,
    OwnedCartLine,
    OwnedOffer,
    OwnedOrder,
    OwnedOrderLine,
    OwnedPrice,
    OwnedProduct,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


#: Product lifecycle of this slice — exactly one state. Catalog lifecycle
#: transitions arrive in a later slice.
PRODUCT_STATES: tuple[str, ...] = ("ACTIVE",)

#: Offer lifecycle of this slice — exactly one state.
OFFER_STATES: tuple[str, ...] = ("ACTIVE",)

#: Payment lifecycle/result of an Order (ADR-0013): the mutable part of an
#: Order. Exactly these two states; a payment-state change never touches
#: the immutable purchase facts.
PAYMENT_STATES: tuple[str, ...] = ("PENDING_PAYMENT", "PAID")

#: The only payment-state moves that exist: recording a pending order as
#: paid. No backward move and no second recording exist.
_PAYMENT_TRANSITIONS: dict[str, tuple[str, ...]] = {"PENDING_PAYMENT": ("PAID",)}

#: Payload content rules shared by the catalog records.
NAME_MAX = 512
DESCRIPTION_MAX = 4096
CURRENCY_MAX = 3


class DomainRefusal(Exception):
    """A domain-level refusal of an otherwise allowed operation.

    Raised only inside the owned-data step of the chain: an orphan or
    cross-tenant child registration, a duplicate identifier, an unknown
    payment state, or a cart line that violates the one-line-per-offer
    rule. The engine maps these internal reasons to the published refusal
    vocabulary and audits them like every refusal.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class CommerceStore:
    """In-memory owned storage of the Commerce data and the audit journal.

    The store owns every record of the Commerce logical schema: products,
    offers, prices, carts, cart lines, orders, order lines and the access
    audit journal. The ``Product → Offer → Price`` parentage, the
    same-tenant rule of every child, the price-snapshot shape of OrderLines
    and the payment-state vocabulary are facts of this store, not claims of
    a caller.
    """

    products: dict[str, OwnedProduct] = field(default_factory=dict)
    offers: dict[str, OwnedOffer] = field(default_factory=dict)
    prices: dict[str, OwnedPrice] = field(default_factory=dict)
    carts: dict[str, OwnedCart] = field(default_factory=dict)
    cart_lines: dict[tuple[str, str], OwnedCartLine] = field(default_factory=dict)
    orders: dict[str, OwnedOrder] = field(default_factory=dict)
    order_lines: dict[str, OwnedOrderLine] = field(default_factory=dict)
    audit: list[AccessAuditEvent] = field(default_factory=list)

    #: One domain critical section per Order: the payment-state change of
    #: one Order is serialized against every other mutation of the same
    #: Order, so two concurrent transitions can never interleave between
    #: the state read and the write. This is Level-0 domain serialization
    #: inside this component, not a second idempotency mechanism: IS-005
    #: stays exactly-once per key, while the section makes the state change
    #: exclusive across different keys.
    _order_locks: dict[str, threading.RLock] = field(
        default_factory=dict, repr=False, compare=False
    )
    _order_locks_guard: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    #: One domain critical section per Cart: every mutation of one Cart —
    #: the three line commands and the checkout that consumes the lines —
    #: is serialized against every other mutation of the same Cart, so two
    #: concurrent commands with different Idempotency-Keys can never
    #: interleave between the lines read and the write. In particular two
    #: concurrent checkouts of the same Cart cannot both pass the non-empty
    #: check: the first one consumes the lines and the second one refuses
    #: with ``cart_empty``. Level-0 domain serialization, not a second
    #: idempotency mechanism.
    _cart_locks: dict[str, threading.RLock] = field(
        default_factory=dict, repr=False, compare=False
    )
    _cart_locks_guard: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # ------------------------------------------------------------------ static
    @staticmethod
    def _text(value: object, *, what: str, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise DomainRefusal(f"{what}_invalid")
        if len(value) > max_length:
            raise DomainRefusal(f"{what}_invalid")
        return value

    def _order_lock(self, order_id: str) -> threading.RLock:
        with self._order_locks_guard:
            lock = self._order_locks.get(order_id)
            if lock is None:
                lock = threading.RLock()
                self._order_locks[order_id] = lock
            return lock

    @contextmanager
    def order_section(self, order_id: str) -> Iterator[None]:
        """Hold the domain critical section of one Order."""
        with self._order_lock(order_id):
            yield

    def _cart_lock(self, cart_id: str) -> threading.RLock:
        with self._cart_locks_guard:
            lock = self._cart_locks.get(cart_id)
            if lock is None:
                lock = threading.RLock()
                self._cart_locks[cart_id] = lock
            return lock

    @contextmanager
    def cart_section(self, cart_id: str) -> Iterator[None]:
        """Hold the domain critical section of one Cart."""
        with self._cart_lock(cart_id):
            yield

    # ------------------------------------------------------------ registration
    def register_product(self, product: OwnedProduct) -> OwnedProduct:
        """Register one product, refusing duplicates and invalid content."""
        if product.product_id in self.products:
            raise DomainRefusal("duplicate_registration")
        if product.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("owner_mismatch")
        if product.status not in PRODUCT_STATES:
            raise DomainRefusal("unknown_product_state")
        self._text(product.name, what="name", max_length=NAME_MAX)
        if not isinstance(product.description, str):
            raise DomainRefusal("description_invalid")
        if len(product.description) > DESCRIPTION_MAX:
            raise DomainRefusal("description_invalid")
        self.products[product.product_id] = product
        return product

    def register_offer(self, offer: OwnedOffer) -> OwnedOffer:
        """Register one offer under an existing Product of the same Tenant."""
        if offer.offer_id in self.offers:
            raise DomainRefusal("duplicate_registration")
        if offer.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("owner_mismatch")
        product = self.products.get(offer.product_id)
        if product is None:
            raise DomainRefusal("product_unknown")
        if offer.tenant_id != product.tenant_id:
            raise DomainRefusal("tenant_mismatch")
        if offer.status not in OFFER_STATES:
            raise DomainRefusal("unknown_offer_state")
        self._text(offer.name, what="name", max_length=NAME_MAX)
        self.offers[offer.offer_id] = offer
        return offer

    def register_price(self, price: OwnedPrice) -> OwnedPrice:
        """Register one price under an existing Offer of the same Tenant.

        Prices are append-only history: a new registration never rewrites
        a past record, and no update operation exists.
        """
        if price.price_id in self.prices:
            raise DomainRefusal("duplicate_registration")
        if price.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("owner_mismatch")
        offer = self.offers.get(price.offer_id)
        if offer is None:
            raise DomainRefusal("offer_unknown")
        if price.tenant_id != offer.tenant_id:
            raise DomainRefusal("tenant_mismatch")
        if (
            isinstance(price.amount, bool)
            or not isinstance(price.amount, int)
            or price.amount < 0
        ):
            raise DomainRefusal("amount_invalid")
        self._text(price.currency, what="currency", max_length=CURRENCY_MAX)
        self.prices[price.price_id] = price
        return price

    def register_cart(self, cart: OwnedCart) -> OwnedCart:
        """Register one cart: mutable pre-order state of one buyer."""
        if cart.cart_id in self.carts:
            raise DomainRefusal("duplicate_registration")
        if cart.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("owner_mismatch")
        if not cart.buyer_identity_id:
            raise DomainRefusal("buyer_unknown")
        self.carts[cart.cart_id] = cart
        return cart

    def register_cart_line(self, line: OwnedCartLine) -> OwnedCartLine:
        """Register one cart line: one Offer selected in one Cart.

        The Cart and the Offer must both exist and belong to the same
        Tenant as the line; at most one line per (Cart, Offer) exists.
        """
        key = (line.cart_id, line.offer_id)
        if key in self.cart_lines:
            raise DomainRefusal("duplicate_cart_line")
        if line.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("owner_mismatch")
        cart = self.carts.get(line.cart_id)
        if cart is None:
            raise DomainRefusal("cart_unknown")
        offer = self.offers.get(line.offer_id)
        if offer is None:
            raise DomainRefusal("offer_unknown")
        if line.tenant_id != cart.tenant_id or line.tenant_id != offer.tenant_id:
            raise DomainRefusal("tenant_mismatch")
        if (
            isinstance(line.quantity, bool)
            or not isinstance(line.quantity, int)
            or line.quantity < 1
        ):
            raise DomainRefusal("quantity_invalid")
        self.cart_lines[key] = line
        return line

    def register_order(self, order: OwnedOrder) -> OwnedOrder:
        """Register one order: the central commercial fact.

        The purchase facts carried by the record are frozen at creation;
        the only later mutation of an Order is its payment state through
        :meth:`CommerceStore.set_payment_state`.
        """
        if order.order_id in self.orders:
            raise DomainRefusal("duplicate_registration")
        if order.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("owner_mismatch")
        if not order.buyer_identity_id:
            raise DomainRefusal("buyer_unknown")
        if order.payment_state not in PAYMENT_STATES:
            raise DomainRefusal("unknown_payment_state")
        self.orders[order.order_id] = order
        return order

    def register_order_line(self, line: OwnedOrderLine) -> OwnedOrderLine:
        """Register one order line with its immutable price snapshot.

        The Order must exist and belong to the same Tenant; the snapshot
        (``amount``/``currency``) is a copied value, so later catalog
        changes can never reach it. No update operation exists.
        """
        if line.order_line_id in self.order_lines:
            raise DomainRefusal("duplicate_registration")
        if line.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("owner_mismatch")
        order = self.orders.get(line.order_id)
        if order is None:
            raise DomainRefusal("order_unknown")
        if line.tenant_id != order.tenant_id:
            raise DomainRefusal("tenant_mismatch")
        if (
            isinstance(line.amount, bool)
            or not isinstance(line.amount, int)
            or line.amount < 0
        ):
            raise DomainRefusal("amount_invalid")
        self._text(line.currency, what="currency", max_length=CURRENCY_MAX)
        self.order_lines[line.order_line_id] = line
        return line

    # ---------------------------------------------------- enforcement metadata
    def ownership_of_product(self, product_id: str) -> OwnedProduct | None:
        """Whether a product exists here and who its single owner is."""
        return self.products.get(product_id)

    def ownership_of_offer(self, offer_id: str) -> OwnedOffer | None:
        """Whether an offer exists here and who its single owner is."""
        return self.offers.get(offer_id)

    def ownership_of_price(self, price_id: str) -> OwnedPrice | None:
        """Whether a price exists here and who its single owner is."""
        return self.prices.get(price_id)

    def ownership_of_cart(self, cart_id: str) -> OwnedCart | None:
        """Whether a cart exists here and who its single owner is."""
        return self.carts.get(cart_id)

    def ownership_of_order(self, order_id: str) -> OwnedOrder | None:
        """Whether an order exists here and who its single owner is."""
        return self.orders.get(order_id)

    def ownership_of_order_line(self, order_line_id: str) -> OwnedOrderLine | None:
        """Whether an order line exists here and who its single owner is."""
        return self.order_lines.get(order_line_id)

    # ---------------------------------------------------- owned-data operations
    def create_product(
        self,
        *,
        tenant_id: str,
        name: str,
        description: str,
        created_by: str,
        now: str,
    ) -> OwnedProduct:
        """The owned-data creation effect of the create-product command.

        The Tenant is the effective tenant of the verified identity as stated
        by the enforcement chain — never a caller claim. A new product is
        created ``ACTIVE``; nothing else in the store is touched.
        """
        return self.register_product(
            OwnedProduct(
                product_id=_new_id("prd"),
                tenant_id=tenant_id,
                owner_component=OWNER_COMPONENT,
                name=name,
                description=description,
                status="ACTIVE",
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
        )

    def create_offer(
        self,
        *,
        tenant_id: str,
        product_id: str,
        name: str,
        created_by: str,
        now: str,
    ) -> OwnedOffer:
        """The owned-data creation effect of the define-offer command.

        The Tenant is the effective tenant of the verified identity as
        stated by the enforcement chain — never a caller claim. The parent
        Product must exist here and belong to that same Tenant, or the
        registration is refused. A new offer is created ``ACTIVE``.
        """
        return self.register_offer(
            OwnedOffer(
                offer_id=_new_id("off"),
                product_id=product_id,
                tenant_id=tenant_id,
                owner_component=OWNER_COMPONENT,
                name=name,
                status="ACTIVE",
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
        )

    def create_price(
        self,
        *,
        tenant_id: str,
        offer_id: str,
        amount: int,
        currency: str,
        now: str,
    ) -> OwnedPrice:
        """The owned-data creation effect of the set-price command.

        The Tenant is the effective tenant of the verified identity as
        stated by the enforcement chain — never a caller claim. The parent
        Offer must exist here and belong to that same Tenant. The new
        record is appended to the price history; no past record is touched.
        """
        return self.register_price(
            OwnedPrice(
                price_id=_new_id("prc"),
                offer_id=offer_id,
                tenant_id=tenant_id,
                owner_component=OWNER_COMPONENT,
                amount=amount,
                currency=currency,
                created_at=now,
            )
        )

    def create_cart(
        self,
        *,
        tenant_id: str,
        buyer_identity_id: str,
        now: str,
    ) -> OwnedCart:
        """The owned-data creation effect of the open-cart command.

        The Tenant is the effective tenant of the verified identity and
        the buyer is the verified subject itself, both as stated by the
        enforcement chain — never caller claims. A new cart holds no lines.
        """
        return self.register_cart(
            OwnedCart(
                cart_id=_new_id("crt"),
                tenant_id=tenant_id,
                owner_component=OWNER_COMPONENT,
                buyer_identity_id=buyer_identity_id,
                created_at=now,
                updated_at=now,
            )
        )

    def serve_product(self, product_id: str) -> OwnedProduct:
        """Serve one owned product. Raises ``KeyError`` when it is gone."""
        return self.products[product_id]

    def add_to_cart(
        self, cart_id: str, offer_id: str, *, quantity: int, now: str
    ) -> OwnedCartLine:
        """Add one offer to a cart, incrementing the line when present.

        The Cart and the Offer must both exist here and belong to the same
        Tenant. Adding an offer that is already selected increments its
        line — a second line for the same (Cart, Offer) never exists. The
        whole sequence runs inside the Cart's domain critical section.
        """
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise DomainRefusal("quantity_invalid")
        if quantity < 1:
            raise DomainRefusal("quantity_invalid")
        with self.cart_section(cart_id):
            cart = self.carts.get(cart_id)
            if cart is None:
                raise DomainRefusal("cart_unknown")
            offer = self.offers.get(offer_id)
            if offer is None:
                raise DomainRefusal("offer_unknown")
            if offer.tenant_id != cart.tenant_id:
                raise DomainRefusal("tenant_mismatch")
            key = (cart_id, offer_id)
            existing = self.cart_lines.get(key)
            if existing is None:
                line = OwnedCartLine(
                    cart_id=cart_id,
                    offer_id=offer_id,
                    tenant_id=cart.tenant_id,
                    owner_component=OWNER_COMPONENT,
                    quantity=quantity,
                )
            else:
                line = replace(existing, quantity=existing.quantity + quantity)
            self.cart_lines[key] = line
            self.carts[cart_id] = replace(cart, updated_at=now)
            return line

    def set_cart_line_quantity(
        self, cart_id: str, offer_id: str, *, quantity: int, now: str
    ) -> OwnedCartLine:
        """Set the quantity of one cart line to an absolute value.

        The line must exist: setting the quantity of a line that was never
        added is a refusal, not an insertion. Zero does not delete — the
        explicit remove operation deletes. Runs inside the Cart's section.
        """
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise DomainRefusal("quantity_invalid")
        if quantity < 1:
            raise DomainRefusal("quantity_invalid")
        with self.cart_section(cart_id):
            cart = self.carts.get(cart_id)
            if cart is None:
                raise DomainRefusal("cart_unknown")
            key = (cart_id, offer_id)
            existing = self.cart_lines.get(key)
            if existing is None:
                raise DomainRefusal("cart_line_unknown")
            line = replace(existing, quantity=quantity)
            self.cart_lines[key] = line
            self.carts[cart_id] = replace(cart, updated_at=now)
            return line

    def remove_cart_line(self, cart_id: str, offer_id: str, *, now: str) -> None:
        """Remove one offer from a cart. The line must exist.

        Runs inside the Cart's domain critical section.
        """
        with self.cart_section(cart_id):
            cart = self.carts.get(cart_id)
            if cart is None:
                raise DomainRefusal("cart_unknown")
            key = (cart_id, offer_id)
            if key not in self.cart_lines:
                raise DomainRefusal("cart_line_unknown")
            del self.cart_lines[key]
            self.carts[cart_id] = replace(cart, updated_at=now)

    def cart_contents(self, cart_id: str) -> tuple[OwnedCart, list[OwnedCartLine]]:
        """Serve one owned cart with its lines. Raises ``KeyError`` when gone."""
        return (self.carts[cart_id], self.lines_of_cart(cart_id))

    def order_contents(self, order_id: str) -> tuple[OwnedOrder, list[OwnedOrderLine]]:
        """Serve one owned order with its lines. Raises ``KeyError`` when gone."""
        return (self.orders[order_id], self.lines_of_order(order_id))

    def current_price(self, offer_id: str) -> OwnedPrice | None:
        """The current price of one offer: the latest registered record.

        Prices are append-only history; the current price is the latest
        registration, ordered by (created_at, price_id) for determinism.
        ``None`` when the offer has no price at all.
        """
        candidates = [p for p in self.prices.values() if p.offer_id == offer_id]
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p.created_at, p.price_id))

    def product_id_of_offer(self, offer_id: str) -> str:
        """The Product one offer is a proposition of. ``KeyError`` when gone."""
        return self.offers[offer_id].product_id

    def checkout_cart(
        self, cart_id: str, *, buyer_identity_id: str, now: str
    ) -> tuple[OwnedOrder, list[OwnedOrderLine]]:
        """Convert the selected contents of one cart into an Order.

        The whole critical sequence — lines read, offer and price
        validation, order and order-line creation, cart consumption — runs
        inside the Cart's domain critical section, so two concurrent
        checkouts of the same cart cannot both pass the non-empty check.
        Every line is validated: the offer must exist here and belong to
        the cart's Tenant, and it must have a current price. The order is
        created ``PENDING_PAYMENT`` with one order line per cart line,
        each carrying the quantity and the copied price snapshot; then the
        cart lines are consumed and the cart is left holding nothing.
        """
        with self.cart_section(cart_id):
            cart = self.carts[cart_id]
            if cart.buyer_identity_id != buyer_identity_id:
                raise DomainRefusal("buyer_mismatch")
            lines = self.lines_of_cart(cart_id)
            if not lines:
                raise DomainRefusal("cart_empty")
            snapshots: list[tuple[OwnedCartLine, OwnedOffer, OwnedPrice]] = []
            for line in lines:
                offer = self.offers.get(line.offer_id)
                if offer is None or offer.tenant_id != cart.tenant_id:
                    raise DomainRefusal("offer_unknown")
                price = self.current_price(line.offer_id)
                if price is None:
                    raise DomainRefusal("price_unknown")
                snapshots.append((line, offer, price))
            order = self.register_order(
                OwnedOrder(
                    order_id=_new_id("ord"),
                    tenant_id=cart.tenant_id,
                    owner_component=OWNER_COMPONENT,
                    buyer_identity_id=cart.buyer_identity_id,
                    payment_state="PENDING_PAYMENT",
                    created_at=now,
                    updated_at=now,
                )
            )
            created: list[OwnedOrderLine] = []
            for line, offer, price in snapshots:
                created.append(
                    self.register_order_line(
                        OwnedOrderLine(
                            order_line_id=_new_id("orl"),
                            order_id=order.order_id,
                            tenant_id=cart.tenant_id,
                            owner_component=OWNER_COMPONENT,
                            offer_id=offer.offer_id,
                            product_id=offer.product_id,
                            amount=price.amount,
                            currency=price.currency,
                            quantity=line.quantity,
                        )
                    )
                )
            for line in lines:
                del self.cart_lines[(cart_id, line.offer_id)]
            self.carts[cart_id] = replace(cart, updated_at=now)
            created.sort(key=lambda item: item.order_line_id)
            return (order, created)

    def set_payment_state(
        self, order_id: str, payment_state: str, *, now: str
    ) -> OwnedOrder:
        """Move the mutable payment state/result of one Order.

        The whole change runs inside the Order's domain critical section.
        Only the ``PENDING_PAYMENT → PAID`` transition exists: any other
        move — including recording an already paid order as paid again —
        is refused. Only ``payment_state`` and ``updated_at`` change: the
        buyer, the tenant and the bought positions — the immutable purchase
        facts — are carried over untouched, and no reorder of the lines
        happens. Raises ``KeyError`` when the Order is gone and
        ``DomainRefusal`` for an unknown payment state or an invalid
        transition.
        """
        if payment_state not in PAYMENT_STATES:
            raise DomainRefusal("unknown_payment_state")
        with self.order_section(order_id):
            order = self.orders[order_id]
            allowed = _PAYMENT_TRANSITIONS.get(order.payment_state, ())
            if payment_state not in allowed:
                raise DomainRefusal("invalid_state_transition")
            updated = replace(order, payment_state=payment_state, updated_at=now)
            self.orders[order_id] = updated
            return updated

    def lines_of_order(self, order_id: str) -> list[OwnedOrderLine]:
        """The bought positions of one Order in deterministic order."""
        return sorted(
            (line for line in self.order_lines.values() if line.order_id == order_id),
            key=lambda line: line.order_line_id,
        )

    def lines_of_cart(self, cart_id: str) -> list[OwnedCartLine]:
        """The selected offers of one Cart in deterministic order."""
        return sorted(
            (line for line in self.cart_lines.values() if line.cart_id == cart_id),
            key=lambda line: line.offer_id,
        )

    # ------------------------------------------------------------------- demo
    def seed_demo(self) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        Tenant identifiers are the demo ones of IS-002: this component does
        not define Tenants, it only states which Tenant each record belongs
        to. Identity references are the demo ones of IS-001 (opaque ids —
        no profile data is stored here). The subjects/permissions are the
        demo ones of IS-003, granted by the composition root, so the full
        decision chain is exercisable against this store.
        """
        self.products = {}
        self.offers = {}
        self.prices = {}
        self.carts = {}
        self.cart_lines = {}
        # Orders reseed empty: this slice establishes the store and its
        # invariants, and the fixed demo data deliberately states no
        # commercial purchase fact of its own.
        self.orders = {}
        self.order_lines = {}
        self.audit = []

        when = "2026-09-13T00:00:00+00:00"
        self.register_product(
            OwnedProduct(
                product_id="prd_a1",
                tenant_id="ten_a",
                owner_component=OWNER_COMPONENT,
                name="Demo product of Tenant A",
                description="Fixed demonstration product.",
                status="ACTIVE",
                created_by="idn_human_a",
                created_at=when,
                updated_at=when,
            )
        )
        self.register_product(
            OwnedProduct(
                product_id="prd_b1",
                tenant_id="ten_b",
                owner_component=OWNER_COMPONENT,
                name="Demo product of Tenant B",
                description="Fixed demonstration product.",
                status="ACTIVE",
                created_by="idn_human_b",
                created_at=when,
                updated_at=when,
            )
        )
