"""Published data contract of the Commerce component (SCS-002).

This module publishes the whole vocabulary a consumer needs: the immutable
values an allowed access returns, the closed reason sets a refusal may
carry, and the error classes. Internal modules (``engine``, ``store``,
``models``, ``api``, ``deployment``, ``transport``, ``ports``,
``adapters``) are not part of the contract and must not be imported or
reached through the published surface (ARCHITECTURE.md §1.1, LAW-04).

The object a consumer receives is ``commerce_service.reader.CommerceClient``
— the published operations over the published API (create and read one
owned Product; define one Offer; set one Price; open, read and mutate one
owned Cart; checkout; read one owned Order; record its payment), values in
and values out.

Two facts about the model are deliberate:

* views are the whole record and nothing more: the business fields of the
  representation. A view is produced only by the owned-data operation at
  the end of the enforcement chain, so holding a view means the access was
  allowed — and a refusal can never be mistaken for one. No tenant data is
  carried: identity references are opaque and owned outside Commerce;
* the published reason vocabulary is closed: the reasons of this
  component's own boundary (see :class:`OwnDenyReason`) plus the published
  denial reasons of IS-003 passed through unchanged. A refusal outside that
  vocabulary cannot happen: a non-authoritative answer of the dependency is
  reported as ``authorization_unavailable`` and denied.
"""

from __future__ import annotations

from dataclasses import dataclass

from commerce_service.consumed import DENY_REASONS as _DECISION_DENIALS
from commerce_service.errors import AccessRefused, ConfigurationError, ContractViolation

#: This component: the single data owner of every record it serves.
OWNER_COMPONENT = "commerce"

#: Resource types this component owns.
RESOURCE_TYPE_PRODUCT = "product"
RESOURCE_TYPE_OFFER = "offer"
RESOURCE_TYPE_PRICE = "price"
RESOURCE_TYPE_CART = "cart"
RESOURCE_TYPE_ORDER = "order"

#: The grant vocabulary the data owner asks IS-003 about. Reads change no
#: state beyond the append-only audit journal; every other operation is a
#: state-changing command guarded by IS-005. One operation, one grant, one
#: question per access — no policy logic lives inside Commerce.
OPERATION_PRODUCT_CREATE = "commerce.products.create"
OPERATION_PRODUCT_READ = "commerce.products.read"
OPERATION_OFFER_CREATE = "commerce.offers.create"
OPERATION_PRICE_CREATE = "commerce.prices.create"
OPERATION_CART_CREATE = "commerce.carts.create"
OPERATION_CART_READ = "commerce.carts.read"
OPERATION_CART_UPDATE = "commerce.carts.update"
OPERATION_CHECKOUT_CREATE = "commerce.checkout.create"
OPERATION_ORDER_READ = "commerce.orders.read"
OPERATION_ORDER_PAY = "commerce.orders.pay"


class OwnDenyReason:
    """Reasons of this component's own enforcement boundary (closed set).

    ``product_unknown`` / ``offer_unknown`` / ``price_unknown`` /
    ``cart_unknown`` / ``order_unknown`` — no such record is owned here. An
    offer of another Tenant named by a cart command, and an offer that
    vanished before checkout, are also ``offer_unknown``: from the cart's
    perspective the offer is not available, and the merge discloses nothing
    about records of other Tenants. ``price_unknown`` additionally covers
    an offer that has no current price at checkout time.
    ``cart_line_unknown`` — the cart holds no line for that offer.
    ``owner_mismatch`` — a record whose single owner is another component
    can never be served through this boundary.
    ``buyer_mismatch`` — the cart or order belongs to another buyer of the
    same Tenant: a subject reaches only its own carts and orders.
    ``cart_empty`` — checkout of a cart that holds no lines.
    ``invalid_state_transition`` — the command does not apply to the
    record's current state: only a ``PENDING_PAYMENT`` order may be
    recorded as paid.
    ``authorization_unavailable`` — the decision dependency did not answer,
    or answered outside its published contract: fail closed.
    ``idempotency_key_required`` — a state-changing command was sent without
    the mandatory ``Idempotency-Key`` header.
    ``idempotency_conflict`` — the ``Idempotency-Key`` was already used with
    a different binding (identity, tenant, operation, target or command).
    ``validation_error`` — the command payload is schema-valid but violates
    the content rules of the capability (empty name, negative amount,
    non-positive quantity): a refusal before any access decision.
    """

    PRODUCT_UNKNOWN = "product_unknown"
    OFFER_UNKNOWN = "offer_unknown"
    PRICE_UNKNOWN = "price_unknown"
    CART_UNKNOWN = "cart_unknown"
    ORDER_UNKNOWN = "order_unknown"
    CART_LINE_UNKNOWN = "cart_line_unknown"
    OWNER_MISMATCH = "owner_mismatch"
    BUYER_MISMATCH = "buyer_mismatch"
    CART_EMPTY = "cart_empty"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    VALIDATION_ERROR = "validation_error"


#: The closed set of own reasons, as values.
OWN_DENY_REASONS: frozenset[str] = frozenset(
    {
        OwnDenyReason.PRODUCT_UNKNOWN,
        OwnDenyReason.OFFER_UNKNOWN,
        OwnDenyReason.PRICE_UNKNOWN,
        OwnDenyReason.CART_UNKNOWN,
        OwnDenyReason.ORDER_UNKNOWN,
        OwnDenyReason.CART_LINE_UNKNOWN,
        OwnDenyReason.OWNER_MISMATCH,
        OwnDenyReason.BUYER_MISMATCH,
        OwnDenyReason.CART_EMPTY,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
        OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
        OwnDenyReason.IDEMPOTENCY_CONFLICT,
        OwnDenyReason.VALIDATION_ERROR,
    }
)

#: Published denial reasons of IS-003 this boundary passes through unchanged
#: when the decision denies: the fact itself belongs to the authority.
PASSED_THROUGH_DENIALS: frozenset[str] = _DECISION_DENIALS

#: Every reason a published refusal may carry.
PUBLISHED_DENY_REASONS: frozenset[str] = OWN_DENY_REASONS | PASSED_THROUGH_DENIALS


@dataclass(frozen=True, slots=True)
class ProductView:
    """The published representation of one owned product.

    Exactly the eight business fields — ``product_id``, ``name``,
    ``description``, ``status``, ``created_by``, ``created_at``,
    ``updated_at`` — and nothing else. No tenant or owner data is exposed:
    the view is the value an allowed access returns, immutable and
    granting nothing by existing. ``created_by`` is the opaque reference
    to the verified identity that created the product. There is
    deliberately no price here: a price belongs to a sellable proposition
    (:class:`commerce_service.models.OwnedPrice`), never to the catalog
    object.
    """

    product_id: str
    name: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OfferView:
    """The published representation of one sellable proposition.

    Exactly the seven business fields — ``offer_id``, ``product_id``,
    ``name``, ``status``, ``created_by``, ``created_at``, ``updated_at`` —
    and nothing else. No tenant or owner data is exposed. ``product_id``
    is the one Product this Offer is a proposition of, in the same Tenant.
    """

    offer_id: str
    product_id: str
    name: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PriceView:
    """The published representation of one price of a sellable proposition.

    Exactly the five business fields — ``price_id``, ``offer_id``,
    ``amount``, ``currency``, ``created_at`` — and nothing else.
    ``amount`` is in minor currency units.
    """

    price_id: str
    offer_id: str
    amount: int
    currency: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CartLineView:
    """The published representation of one selected offer inside a cart."""

    offer_id: str
    product_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class CartView:
    """The published representation of one owned cart and its lines.

    ``items`` holds the selected offers in deterministic order; an empty
    cart answers with an empty tuple. ``buyer_identity_id`` is the opaque
    reference to the verified subject that owns the cart.
    """

    cart_id: str
    buyer_identity_id: str
    created_at: str
    updated_at: str
    items: tuple[CartLineView, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderLineView:
    """The published representation of one bought position of an order.

    ``amount``/``currency`` are the immutable price snapshot copied at
    checkout time — never a reference to the current catalog.
    ``quantity`` is how many units were bought at that snapshot price.
    """

    order_line_id: str
    offer_id: str
    product_id: str
    amount: int
    currency: str
    quantity: int


@dataclass(frozen=True, slots=True)
class OrderView:
    """The published representation of one owned order and its lines.

    ``lines`` holds the bought positions in deterministic order.
    ``payment_state`` is the mutable commercial state/result of the order
    (``PENDING_PAYMENT → PAID``); everything else shown here is an
    immutable purchase fact.
    """

    order_id: str
    buyer_identity_id: str
    payment_state: str
    created_at: str
    updated_at: str
    lines: tuple[OrderLineView, ...] = ()


__all__ = [
    "OPERATION_CART_CREATE",
    "OPERATION_CART_READ",
    "OPERATION_CART_UPDATE",
    "OPERATION_CHECKOUT_CREATE",
    "OPERATION_OFFER_CREATE",
    "OPERATION_ORDER_PAY",
    "OPERATION_ORDER_READ",
    "OPERATION_PRICE_CREATE",
    "OPERATION_PRODUCT_CREATE",
    "OPERATION_PRODUCT_READ",
    "OWNER_COMPONENT",
    "OWN_DENY_REASONS",
    "PASSED_THROUGH_DENIALS",
    "PUBLISHED_DENY_REASONS",
    "RESOURCE_TYPE_CART",
    "RESOURCE_TYPE_OFFER",
    "RESOURCE_TYPE_ORDER",
    "RESOURCE_TYPE_PRICE",
    "RESOURCE_TYPE_PRODUCT",
    "AccessRefused",
    "CartLineView",
    "CartView",
    "ConfigurationError",
    "ContractViolation",
    "OfferView",
    "OrderLineView",
    "OrderView",
    "OwnDenyReason",
    "PriceView",
    "ProductView",
]
