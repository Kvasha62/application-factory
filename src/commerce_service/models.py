"""Internal records of the Commerce component (SCS-002).

These types describe the owned data of the component (logical schema
``commerce``). They are not part of the public contract; consumers receive
:mod:`commerce_service.contracts` values.

The model implements the ratified ADR-0013 contour:

* ``Product → Offer → Price`` — a Product is what can be offered for sale
  (domain-agnostic, no Learning internals); an Offer is the sellable
  proposition of exactly one Product; a Price is a first-class price
  concept attached to the sellable proposition. There is deliberately no
  ``Product.price`` and no pricing machinery (no price books, tiers,
  engines, promotions);
* ``Cart → Checkout → Order → OrderLine`` — a Cart is mutable pre-order
  state bound to a buyer; Checkout is a business command, not an entity,
  so no Checkout record exists; an Order is the central commercial fact
  whose purchase facts are immutable after creation, while the payment
  state bound to it is a mutable lifecycle/result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OwnedProduct:
    """One product owned by this component.

    ``owner_component`` is singular by construction: one value, one owner.
    ``tenant_id`` is the Tenant the product belongs to — a fact of this
    store derived from the verified identity through the published chain,
    never a caller claim (LAW-16a). ``created_by`` is an opaque reference
    to the verified identity that created the product; the profile is owned
    outside Commerce.

    A Product is *what can be offered for sale*. It is domain-agnostic: it
    carries no Learning internals, no foreign keys into another schema and
    no ``price`` — the price of a sellable proposition lives on
    :class:`OwnedPrice`, never here. Lifecycle is exactly ``ACTIVE`` in
    this slice; catalog lifecycle transitions arrive in a later slice.
    """

    product_id: str
    tenant_id: str
    owner_component: str
    name: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OwnedOffer:
    """One sellable proposition of a Product, owned by this component.

    An Offer links exactly one Product to a sellable proposition within
    the same Tenant: the hierarchy is registered, never declared by a
    caller. An Offer for an unknown Product, or for a Product of another
    Tenant, cannot be registered. Lifecycle is exactly ``ACTIVE`` in this
    slice; the exact Offer lifecycle beyond that is a later-slice decision
    that must not reintroduce a hidden ``Product.price``.
    """

    offer_id: str
    product_id: str
    tenant_id: str
    owner_component: str
    name: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OwnedPrice:
    """One price of a sellable proposition, owned by this component.

    A Price belongs to exactly one Offer of the same Tenant. ``amount`` is
    in minor currency units (an integer — money is never a float) and
    ``currency`` is the ISO currency code. A Price record is append-only
    history: changing the current price registers a new record and never
    rewrites a past one, so a historical OrderLine snapshot can never be
    reached through the current catalog. Which registered Price is *the*
    current price of an Offer is resolved by the checkout command of a
    later slice, not by this record.
    """

    price_id: str
    offer_id: str
    tenant_id: str
    owner_component: str
    amount: int
    currency: str
    created_at: str


@dataclass(frozen=True, slots=True)
class OwnedCart:
    """Mutable pre-order state of one buyer, owned by this component.

    ``buyer_identity_id`` is an opaque reference to the buyer identity
    owned outside Commerce (Identity owns the profile); Commerce stores
    only the reference, never profile/account data. Unlike the Order —
    whose purchase facts are immutable once created — the Cart is *not* a
    commercial fact: it may change, be emptied or be abandoned without any
    commercial consequence. Retention and addressing rules are
    later-slice decisions.
    """

    cart_id: str
    tenant_id: str
    owner_component: str
    buyer_identity_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OwnedCartLine:
    """One selected offer inside a Cart, owned by this component.

    A line names exactly one Offer of the same Tenant as the Cart, with a
    positive ``quantity``. At most one line per (Cart, Offer) exists: a
    second selection of the same Offer mutates the line (a later-slice
    cart command), it never registers a second line.
    """

    cart_id: str
    offer_id: str
    tenant_id: str
    owner_component: str
    quantity: int


@dataclass(frozen=True, slots=True)
class OwnedOrder:
    """The central commercial fact, owned by this component.

    An Order records what a specific buyer bought in a specific tenant.
    The purchase facts — the buyer, the tenant, the bought positions
    (:class:`OwnedOrderLine`) — are immutable after creation: the record
    is frozen and the store offers no mutation of them.

    ``payment_state`` is deliberately not among those frozen facts: it is
    the mutable commercial state/result of payment associated with the
    Order (``PENDING_PAYMENT → PAID``) and is changed only through the
    store's payment-state operation, which never touches the purchase
    facts. There is no Payment Service, no provider and no gateway: the
    payment state is Commerce-owned data bound to the Order.
    """

    order_id: str
    tenant_id: str
    owner_component: str
    buyer_identity_id: str
    payment_state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OwnedOrderLine:
    """One bought position of an Order, owned by this component.

    Each line captures one bought proposition with a **price snapshot at
    order time**: ``amount``/``currency`` are copied values, never a
    reference to the current catalog. ``offer_id``/``product_id`` identify
    what was bought and remain meaningful even if the current catalog
    changes — a historical line is never recomputed from it. The line is
    frozen at creation together with the rest of the purchase facts.
    """

    order_line_id: str
    order_id: str
    tenant_id: str
    owner_component: str
    offer_id: str
    product_id: str
    amount: int
    currency: str


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    """Standard observability context (ARCHITECTURE.md §26 / ADR-0010 AMD-10).

    ``saga_id`` is absent on purpose: this component runs no inter-component
    sagas.
    """

    timestamp: str
    environment: str
    platform_id: str | None
    component_id: str
    component_version: str
    request_id: str
    trace_id: str
    correlation_id: str
    tenant_id: str | None
    subject_id: str | None


@dataclass(frozen=True, slots=True)
class AccessAuditEvent:
    """Append-only audit record of one access attempt — served or refused.

    ``subject_id`` and ``tenant_id`` are what the decision stated where
    known: before the decision this component knows nothing about the
    caller, because it verifies no credential itself. ``claimed_tenant_id``
    is recorded as security-relevant context of the attempt — the value the
    caller supplied as a cross-check — and never as tenant identity.
    ``resource_id`` is the URL target of the attempt; ``details`` carries
    the commerce record identifiers in scope (product, offer, cart, order)
    where applicable.
    """

    event_id: str
    action: str
    decision: str
    reason: str | None
    subject_id: str | None
    tenant_id: str | None
    resource_id: str | None
    resource_tenant_id: str | None
    claimed_tenant_id: str | None
    platform_id: str | None
    request_id: str
    correlation_id: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)
