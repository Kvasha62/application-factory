"""Published data contract of the Commerce component (SCS-002).

This module publishes the whole vocabulary a consumer needs: the immutable
values an allowed access returns, the closed reason sets a refusal may
carry, and the error classes. Internal modules (``engine``, ``store``,
``models``, ``api``, ``deployment``, ``transport``, ``ports``,
``adapters``) are not part of the contract and must not be imported or
reached through the published surface (ARCHITECTURE.md §1.1, LAW-04).

The object a consumer receives is ``commerce_service.reader.CommerceClient``
— the published operations over the published API (Stage 2: create and
read one owned Product), values in and values out.

Two facts about the model are deliberate:

* :class:`ProductView` is the whole product and nothing more: the eight
  fields of the business representation. A view is produced only by the
  owned-data operation at the end of the enforcement chain, so holding a
  view means the access was allowed — and a refusal can never be mistaken
  for one. No tenant data is carried: ``created_by`` is an opaque
  reference owned outside Commerce;
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

#: Resource types this component owns in this slice.
RESOURCE_TYPE_PRODUCT = "product"

#: The grant vocabulary the data owner asks IS-003 about. The read changes
#: no state beyond the append-only audit journal; the create command is the
#: state-changing operation of this slice, guarded by IS-005. Offer, Price,
#: Cart, Order and payment-state grants arrive with their own slices — one
#: operation, one grant, one question per access.
OPERATION_PRODUCT_CREATE = "commerce.products.create"
OPERATION_PRODUCT_READ = "commerce.products.read"


class OwnDenyReason:
    """Reasons of this component's own enforcement boundary (closed set).

    ``product_unknown`` — no such product is owned here.
    ``owner_mismatch`` — a record whose single owner is another component
    can never be served through this boundary.
    ``authorization_unavailable`` — the decision dependency did not answer,
    or answered outside its published contract: fail closed.
    ``idempotency_key_required`` — a state-changing command was sent without
    the mandatory ``Idempotency-Key`` header.
    ``idempotency_conflict`` — the ``Idempotency-Key`` was already used with
    a different binding (identity, tenant, operation, target or command).
    ``validation_error`` — the command payload is schema-valid but violates
    the content rules of the capability (empty name, oversized text): a
    refusal before any access decision.
    """

    PRODUCT_UNKNOWN = "product_unknown"
    OWNER_MISMATCH = "owner_mismatch"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    VALIDATION_ERROR = "validation_error"


#: The closed set of own reasons, as values.
OWN_DENY_REASONS: frozenset[str] = frozenset(
    {
        OwnDenyReason.PRODUCT_UNKNOWN,
        OwnDenyReason.OWNER_MISMATCH,
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


__all__ = [
    "OPERATION_PRODUCT_CREATE",
    "OPERATION_PRODUCT_READ",
    "OWNER_COMPONENT",
    "OWN_DENY_REASONS",
    "PASSED_THROUGH_DENIALS",
    "PUBLISHED_DENY_REASONS",
    "RESOURCE_TYPE_PRODUCT",
    "AccessRefused",
    "ConfigurationError",
    "ContractViolation",
    "OwnDenyReason",
    "ProductView",
]
