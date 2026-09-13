"""Commerce engine: the enforcement chain of the data owner.

This component is the final enforcement boundary for the Commerce data it
owns (ARCHITECTURE.md §5.3, §6.2): IS-003 decides, and this component
applies the decision at its own boundary — including refusing when the
decision cannot be obtained at all. The chain is fixed, published, and
every step can only deny:

```text
Request (subject credential, record id, operation, [claimed tenant])
      ↓
ownership_boundary       the record exists here and its single owner is
                         this component                  else DENY
      ↓
authorization_decision   IS-003 decides through the port; the default
                         outcome is DENY; a dependency that does not answer
                         or answers outside its contract fails closed
      ↓
owned_data_operation     only now is the owned data read or written
```

An ``ALLOW`` is not data access: the owned-data operation runs only here,
at the end, and a request denied earlier never reaches it. Both the served
accesses and every refusal are audited with ``request_id`` and
``correlation_id``.

Stage 2 publishes exactly two operations on this chain: create one owned
Product and read one owned Product. The create command derives the
effective tenant from the verified identity (its target does not exist
yet), asks IS-003 about the product-to-be, and delivers the creation
effect to the IS-005 guard exactly once. Stage 3 adds the rest of the
business logic on the same chain: define one Offer, set one Price, open,
read and mutate one owned Cart, checkout, read one owned Order and record
its payment.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from commerce_service import COMPONENT_ID, COMPONENT_VERSION
from commerce_service.config import CommerceConfig
from commerce_service.consumed import (
    ALLOW,
    DENY,
    DENY_REASONS,
    PERMITTED,
    DependencyRefusal,
    TenantContextRefusal,
)
from commerce_service.contracts import (
    OPERATION_CART_CREATE,
    OPERATION_CART_READ,
    OPERATION_CART_UPDATE,
    OPERATION_CHECKOUT_CREATE,
    OPERATION_OFFER_CREATE,
    OPERATION_ORDER_PAY,
    OPERATION_ORDER_READ,
    OPERATION_PRICE_CREATE,
    OPERATION_PRODUCT_CREATE,
    OPERATION_PRODUCT_READ,
    CartLineView,
    CartView,
    OfferView,
    OrderLineView,
    OrderView,
    OwnDenyReason,
    PriceView,
    ProductView,
)
from commerce_service.errors import AccessRefused
from commerce_service.models import (
    AccessAuditEvent,
    ObservabilityContext,
    OwnedCart,
    OwnedCartLine,
    OwnedOffer,
    OwnedOrder,
    OwnedOrderLine,
    OwnedPrice,
    OwnedProduct,
)
from commerce_service.ports import (
    AuthorizationPort,
    CommandSafetyPort,
    TenantContextPort,
)
from commerce_service.store import CommerceStore, DomainRefusal
from idempotency.errors import IdempotencyConflict
from idempotency.guard import IdempotencyGuard

#: The published order of enforcement. Also used by the contract tests: a
#: change of order is a change of behaviour and must be visible.
ENFORCEMENT_CHAIN: tuple[str, ...] = (
    "ownership_boundary",
    "authorization_decision",
    "owned_data_operation",
)

#: The audit action of a request the published schema rejected before any
#: handler ran: an access attempt that could not even be read.
ACTION_UNREADABLE = "commerce.access"

#: Identity-family denial reasons of IS-003 map to 401; every other denial
#: is an authorization refusal (403). An unmapped reason never falls through
#: to a permissive status.
_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)

_NOT_FOUND_REASONS = frozenset(
    {
        OwnDenyReason.PRODUCT_UNKNOWN,
        OwnDenyReason.OFFER_UNKNOWN,
        OwnDenyReason.PRICE_UNKNOWN,
        OwnDenyReason.CART_UNKNOWN,
        OwnDenyReason.ORDER_UNKNOWN,
        OwnDenyReason.CART_LINE_UNKNOWN,
    }
)

#: Domain refusals of the store that carry a published reason unchanged.
#: Any other internal refusal reaching the engine is a disagreement between
#: the enforcement chain and the store — unreachable through the published
#: commands, which validate up front — and fails closed as
#: ``authorization_unavailable`` with the internal rule stated in the audit
#: details for diagnosis.
_PUBLISHED_DOMAIN_REASONS = frozenset(
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
    }
)

#: Payload content rules of the catalog commands. A payload that violates
#: them is schema-valid but refused as ``validation_error`` before any
#: access decision: an unreadable command is not an access attempt.
NAME_MAX = 512
DESCRIPTION_MAX = 4096
CURRENCY_MAX = 3


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _status_for(reason: str) -> int:
    """HTTP status of a refusal: 401 authentication, 404 unknown, 409 state,
    emptiness or idempotency conflict, 400 missing idempotency key, 422
    invalid payload, 503 fail-closed dependency, 403 everything else."""
    if reason in _AUTHENTICATION_DENIALS:
        return 401
    if reason in _NOT_FOUND_REASONS:
        return 404
    if reason in (
        OwnDenyReason.CART_EMPTY,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.IDEMPOTENCY_CONFLICT,
    ):
        return 409
    if reason == OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED:
        return 400
    if reason == OwnDenyReason.VALIDATION_ERROR:
        return 422
    if reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE:
        return 503
    return 403


def _payload_fingerprint(operation: str, payload: dict[str, Any]) -> str:
    """The command identity of one command: operation plus payload.

    The fingerprint carries the payload, so a changed payload with the same
    key is a conflict. The create command has no pre-existing target: the
    IS-005 resource binding is empty and the tenant binding is the effective
    tenant of the chain.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{operation}\n{canonical}\n".encode()).hexdigest()


def _domain_reason(reason: str) -> str:
    """Map an internal store refusal to the published reason vocabulary.

    Reasons the store shares with the published contract pass through
    unchanged; anything else fails closed as ``authorization_unavailable``
    (see :data:`_PUBLISHED_DOMAIN_REASONS`).
    """
    if reason in _PUBLISHED_DOMAIN_REASONS:
        return reason
    return OwnDenyReason.AUTHORIZATION_UNAVAILABLE


def _name_problem(name: Any) -> str | None:
    """The content rule a name violates, or ``None`` when it violates none."""
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string"
    if len(name) > NAME_MAX:
        return f"name must be at most {NAME_MAX} characters"
    return None


def _validation_problem(name: Any, description: Any) -> str | None:
    """The first content rule a create-product payload violates, or ``None``.

    ``name`` must be a non-empty string of at most :data:`NAME_MAX` chars;
    ``description`` must be a string of at most :data:`DESCRIPTION_MAX`
    chars.
    """
    problem = _name_problem(name)
    if problem is not None:
        return problem
    if not isinstance(description, str):
        return "description must be a string"
    if len(description) > DESCRIPTION_MAX:
        return f"description must be at most {DESCRIPTION_MAX} characters"
    return None


def _amount_problem(amount: Any) -> str | None:
    """The content rule a price amount violates, or ``None``.

    A price amount is a non-negative integer in minor currency units —
    money is never a float and never a boolean.
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        return "amount must be an integer"
    if amount < 0:
        return "amount must not be negative"
    return None


def _currency_problem(currency: Any) -> str | None:
    """The content rule a price currency violates, or ``None``."""
    if not isinstance(currency, str) or not currency.strip():
        return "currency must be a non-empty string"
    if len(currency) > CURRENCY_MAX:
        return f"currency must be at most {CURRENCY_MAX} characters"
    return None


def _quantity_problem(quantity: Any) -> str | None:
    """The content rule a cart-line quantity violates, or ``None``.

    A quantity is a positive integer: zero does not delete and negatives
    do not exist.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        return "quantity must be an integer"
    if quantity < 1:
        return "quantity must be positive"
    return None


def _product_view_of(product: OwnedProduct) -> ProductView:
    return ProductView(
        product_id=product.product_id,
        name=product.name,
        description=product.description,
        status=product.status,
        created_by=product.created_by,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def _offer_view_of(offer: OwnedOffer) -> OfferView:
    return OfferView(
        offer_id=offer.offer_id,
        product_id=offer.product_id,
        name=offer.name,
        status=offer.status,
        created_by=offer.created_by,
        created_at=offer.created_at,
        updated_at=offer.updated_at,
    )


def _price_view_of(price: OwnedPrice) -> PriceView:
    return PriceView(
        price_id=price.price_id,
        offer_id=price.offer_id,
        amount=price.amount,
        currency=price.currency,
        created_at=price.created_at,
    )


def _cart_view_of(
    cart: OwnedCart, lines: list[OwnedCartLine], product_of: dict[str, str]
) -> CartView:
    return CartView(
        cart_id=cart.cart_id,
        buyer_identity_id=cart.buyer_identity_id,
        created_at=cart.created_at,
        updated_at=cart.updated_at,
        items=tuple(
            CartLineView(
                offer_id=line.offer_id,
                product_id=product_of[line.offer_id],
                quantity=line.quantity,
            )
            for line in lines
        ),
    )


def _order_view_of(order: OwnedOrder, lines: list[OwnedOrderLine]) -> OrderView:
    return OrderView(
        order_id=order.order_id,
        buyer_identity_id=order.buyer_identity_id,
        payment_state=order.payment_state,
        created_at=order.created_at,
        updated_at=order.updated_at,
        lines=tuple(
            OrderLineView(
                order_line_id=line.order_line_id,
                offer_id=line.offer_id,
                product_id=line.product_id,
                amount=line.amount,
                currency=line.currency,
                quantity=line.quantity,
            )
            for line in lines
        ),
    )


@dataclass
class CommerceEngine:
    """The enforcement boundary of Commerce in one Platform Instance.

    It owns the commerce records and an access audit journal. It owns no identity, no
    permission, no tenant registry and no tenant state: the decision is
    read, per access, from the published contract of IS-003 through the
    port. It owns no second idempotency mechanism either: the create
    command is delivered to the IS-005 guard through the port.
    """

    store: CommerceStore
    config: CommerceConfig
    authorization: AuthorizationPort
    clock: Callable[[], str] = field(default=_utc_now)
    idempotency: CommandSafetyPort | None = field(default=None)
    tenant_context: TenantContextPort | None = field(default=None)

    def __post_init__(self) -> None:
        if self.idempotency is None:
            # No second idempotency mechanism: the default is the IS-005 guard
            # itself, reporting its replays and conflicts into this component's
            # audit journal.
            self.idempotency = IdempotencyGuard(audit_sink=self._idempotency_audit)

    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (never a caller claim)."""
        return self.config.platform_id

    # ------------------------------------------------------------- operations
    def create_product(
        self,
        subject_credential: str | None,
        *,
        name: str,
        description: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ProductView, ObservabilityContext, AccessAuditEvent]:
        """Create one owned Product — the command ``commerce.products.create``.

        The enforced chain is the published one, with the effective tenant
        resolved first because the target does not exist yet: the tenant-
        context port (IS-001) derives it from the verified identity, then
        IS-003 decides ``commerce.products.create`` for a product-to-be in
        that tenant, then the IS-005 guard executes the creation effect
        exactly once. The Product's Tenant is the effective tenant of the
        chain — never a caller claim — and a new Product is created
        ``ACTIVE``.
        """
        action = OPERATION_PRODUCT_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _validation_problem(name, description)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=None,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )

        # --- step 1: the effective tenant of the verified subject (IS-001) ---
        obs, _, effective_tenant = self._effective_tenant(
            subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            resource_id=None,
        )
        product_id = _new_id("prd")

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="product",
            resource_id=product_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs
        if tenant_id != effective_tenant:
            # The decision and the identity context must agree about the
            # effective tenant; a disagreement is a non-authoritative answer.
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_disagreement"},
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action, {"name": name, "description": description}
        )
        try:
            created = self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=None,
                fingerprint=fingerprint,
                effect=lambda: self.store.create_product(
                    tenant_id=tenant_id,
                    name=name,
                    description=description,
                    created_by=subject_id or "",
                    now=self.clock(),
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=created.product_id,
            resource_tenant_id=created.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_product_view_of(created), obs, event)

    def read_product(
        self,
        subject_credential: str | None,
        product_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ProductView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned Product — and only after the chain allowed it."""
        action = OPERATION_PRODUCT_READ
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        product = self.store.ownership_of_product(product_id)
        if product is None:
            raise self._refuse(
                OwnDenyReason.PRODUCT_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=product_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if product.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=product_id,
                resource_tenant_id=product.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": product.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="product",
            resource_id=product.product_id,
            resource_tenant_id=product.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served = self.store.serve_product(product.product_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.PRODUCT_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=product.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=product_id,
            resource_tenant_id=product.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_product_view_of(served), obs, event)

    # ------------------------------------------------------------------ chain
    def _effective_tenant(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None,
        obs: ObservabilityContext,
        action: str,
        resource_id: str | None,
    ) -> tuple[ObservabilityContext, str, str]:
        """Resolve the effective tenant of the verified subject (IS-001).

        Consumed by create-product only: its target does not exist yet, so
        the resource Tenant of the authorization question can only be the
        effective tenant of the verified identity. The tenant-context port
        is the published identity contract adapted by the composition root;
        a port that is absent, does not answer or answers outside its
        contract fails closed. The caller-supplied tenant identifier is
        forwarded as the cross-check only — it can never select the
        effective tenant.
        """
        if self.tenant_context is None:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_port_not_wired"},
            )
        try:
            answer = self.tenant_context.resolve(
                subject_credential,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception:
            answer = TenantContextRefusal(None)
        if isinstance(answer, TenantContextRefusal):
            code = answer.reason_code
            if code in _AUTHENTICATION_DENIALS or code in DENY_REASONS:
                reason, subject_id, tenant_id = code, None, None
            else:
                reason = OwnDenyReason.AUTHORIZATION_UNAVAILABLE
                subject_id, tenant_id = None, None
            raise self._refuse(
                reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details=(
                    None
                    if reason != OwnDenyReason.AUTHORIZATION_UNAVAILABLE
                    else {"stated_code": code}
                ),
            )
        return (
            self.observability(
                tenant_id=answer.tenant_id,
                subject_id=answer.identity_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            ),
            answer.identity_id,
            answer.tenant_id,
        )

    # ------------------------------------------------------------------ catalog
    def create_offer(
        self,
        subject_credential: str | None,
        *,
        product_id: str,
        name: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[OfferView, ObservabilityContext, AccessAuditEvent]:
        """Define one sellable proposition of a Product — ``commerce.offers.create``.

        Like create-product, the effective tenant is resolved first because
        the target does not exist yet: IS-001 derives it from the verified
        identity, then IS-003 decides ``commerce.offers.create`` for an
        offer-to-be in that tenant, then the IS-005 guard executes the
        creation effect exactly once. The parent Product must exist here
        and belong to the effective tenant — a missing and a foreign parent
        are both ``product_unknown``: the caller learns nothing about
        products of other Tenants. A new Offer is created ``ACTIVE``.
        """
        action = OPERATION_OFFER_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _name_problem(name)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=None,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )

        # --- step 1: the effective tenant of the verified subject (IS-001) ---
        obs, identity_id, effective_tenant = self._effective_tenant(
            subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            resource_id=None,
        )
        offer_id = _new_id("off")

        # --- step 1b: the parent exists here and in the effective tenant -----
        parent = self.store.ownership_of_product(product_id)
        if parent is None or parent.tenant_id != effective_tenant:
            raise self._refuse(
                OwnDenyReason.PRODUCT_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=identity_id,
                tenant_id=effective_tenant,
                resource_id=product_id,
                resource_tenant_id=(None if parent is None else parent.tenant_id),
                claimed_tenant_id=claimed_tenant_id,
            )
        if parent.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=identity_id,
                tenant_id=effective_tenant,
                resource_id=product_id,
                resource_tenant_id=parent.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": parent.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="offer",
            resource_id=offer_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs
        if tenant_id != effective_tenant:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=offer_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_disagreement"},
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=offer_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action, {"name": name, "product_id": product_id}
        )
        try:
            created = self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=offer_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=None,
                fingerprint=fingerprint,
                effect=lambda: self.store.create_offer(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    name=name,
                    created_by=subject_id or "",
                    now=self.clock(),
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=offer_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                _domain_reason(exc.reason),
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "refused": "vanished_after_decision",
                    "stated_rule": exc.reason,
                },
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=created.offer_id,
            resource_tenant_id=created.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_offer_view_of(created), obs, event)

    def create_price(
        self,
        subject_credential: str | None,
        *,
        offer_id: str,
        amount: int,
        currency: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[PriceView, ObservabilityContext, AccessAuditEvent]:
        """Set the current price of one Offer — ``commerce.prices.create``.

        The chain mirrors define-offer: IS-001 resolves the effective
        tenant, IS-003 decides ``commerce.prices.create`` for a price-to-be
        in that tenant, the IS-005 guard executes the append exactly once.
        The parent Offer must exist here and belong to the effective tenant
        — a missing and a foreign parent are both ``offer_unknown``. The
        new record is appended to the price history; no past record is
        touched.
        """
        action = OPERATION_PRICE_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _amount_problem(amount)
        if problem is None:
            problem = _currency_problem(currency)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=None,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )

        # --- step 1: the effective tenant of the verified subject (IS-001) ---
        obs, identity_id, effective_tenant = self._effective_tenant(
            subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            resource_id=None,
        )
        price_id = _new_id("prc")

        # --- step 1b: the parent exists here and in the effective tenant -----
        parent = self.store.ownership_of_offer(offer_id)
        if parent is None or parent.tenant_id != effective_tenant:
            raise self._refuse(
                OwnDenyReason.OFFER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=identity_id,
                tenant_id=effective_tenant,
                resource_id=offer_id,
                resource_tenant_id=(None if parent is None else parent.tenant_id),
                claimed_tenant_id=claimed_tenant_id,
            )
        if parent.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=identity_id,
                tenant_id=effective_tenant,
                resource_id=offer_id,
                resource_tenant_id=parent.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": parent.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="price",
            resource_id=price_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs
        if tenant_id != effective_tenant:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=price_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_disagreement"},
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=price_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action, {"amount": amount, "currency": currency, "offer_id": offer_id}
        )
        try:
            created = self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=price_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=None,
                fingerprint=fingerprint,
                effect=lambda: self.store.create_price(
                    tenant_id=tenant_id,
                    offer_id=offer_id,
                    amount=amount,
                    currency=currency,
                    now=self.clock(),
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=price_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                _domain_reason(exc.reason),
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=offer_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "refused": "vanished_after_decision",
                    "stated_rule": exc.reason,
                },
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=created.price_id,
            resource_tenant_id=created.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_price_view_of(created), obs, event)

    # -------------------------------------------------------------------- cart
    def _serve_cart_for_view(
        self, cart_id: str
    ) -> tuple[OwnedCart, list[OwnedCartLine], dict[str, str]]:
        """One owned cart with its lines and the product of each line.

        Raises ``KeyError`` when the cart vanished and ``DomainRefusal``
        with ``offer_unknown`` when a selected offer vanished: the caller
        maps both to the published refusal vocabulary.
        """
        cart, lines = self.store.cart_contents(cart_id)
        product_of: dict[str, str] = {}
        for line in lines:
            try:
                product_of[line.offer_id] = self.store.product_id_of_offer(
                    line.offer_id
                )
            except KeyError:
                raise DomainRefusal("offer_unknown") from None
        return (cart, lines, product_of)

    def create_cart(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CartView, ObservabilityContext, AccessAuditEvent]:
        """Open one cart — the command ``commerce.carts.create``.

        The command carries no body: the buyer is the verified subject and
        the Tenant is the effective tenant of the chain, so a subject can
        only ever open its own cart. IS-001 resolves the effective tenant,
        IS-003 decides ``commerce.carts.create`` for a cart-to-be in that
        tenant, and the IS-005 guard executes the creation effect exactly
        once. A new cart holds no lines.
        """
        action = OPERATION_CART_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the effective tenant of the verified subject (IS-001) ---
        obs, _, effective_tenant = self._effective_tenant(
            subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            resource_id=None,
        )
        cart_id = _new_id("crt")

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="cart",
            resource_id=cart_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs
        if tenant_id != effective_tenant:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_disagreement"},
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(action, {})
        try:
            created = self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=cart_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=None,
                fingerprint=fingerprint,
                effect=lambda: self.store.create_cart(
                    tenant_id=tenant_id,
                    buyer_identity_id=subject_id or "",
                    now=self.clock(),
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                _domain_reason(exc.reason),
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_rule": exc.reason},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=created.cart_id,
            resource_tenant_id=created.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_cart_view_of(created, [], {}), obs, event)

    def read_cart(
        self,
        subject_credential: str | None,
        cart_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CartView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned cart — and only after the chain allowed it.

        A subject reads only its own carts: a cart of another buyer is
        refused with ``buyer_mismatch`` even within the same Tenant — a
        check of the owned data after the decision, not a second decision.
        """
        action = OPERATION_CART_READ
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        cart = self.store.ownership_of_cart(cart_id)
        if cart is None:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if cart.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": cart.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="cart",
            resource_id=cart.cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 2b: the cart is the subject's own ---------------------------
        if cart.buyer_identity_id != subject_id:
            raise self._refuse(
                OwnDenyReason.BUYER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served, lines, product_of = self._serve_cart_for_view(cart.cart_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                OwnDenyReason.OFFER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "refused": "vanished_after_decision",
                    "stated_rule": exc.reason,
                },
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_cart_view_of(served, lines, product_of), obs, event)

    def add_cart_item(
        self,
        subject_credential: str | None,
        cart_id: str,
        *,
        offer_id: str,
        quantity: int,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CartView, ObservabilityContext, AccessAuditEvent]:
        """Add one offer to a cart — the command ``commerce.carts.update``.

        The cart must exist here and be the subject's own; the offer must
        exist here and belong to the cart's Tenant — a missing and a
        foreign offer are both ``offer_unknown``. Adding an offer that is
        already selected increments its line. The mutation runs inside the
        IS-005 guard bound to the cart.
        """
        action = OPERATION_CART_UPDATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _quantity_problem(quantity)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )

        # --- step 1: the ownership boundary of this component ----------------
        cart = self.store.ownership_of_cart(cart_id)
        if cart is None:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if cart.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": cart.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="cart",
            resource_id=cart.cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 2b: the cart is the subject's own ---------------------------
        if cart.buyer_identity_id != subject_id:
            raise self._refuse(
                OwnDenyReason.BUYER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action,
            {
                "command": "add_cart_item",
                "cart_id": cart_id,
                "offer_id": offer_id,
                "quantity": quantity,
            },
        )
        try:
            self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=cart_id,
                fingerprint=fingerprint,
                effect=lambda: self.store.add_to_cart(
                    cart_id, offer_id, quantity=quantity, now=self.clock()
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            reason = _domain_reason(exc.reason)
            if exc.reason == "tenant_mismatch":
                # The offer named belongs to another Tenant: from this
                # cart's perspective the offer is not available — merged
                # with the missing offer so the refusal discloses nothing
                # about records of other Tenants.
                reason = OwnDenyReason.OFFER_UNKNOWN
            raise self._refuse(
                reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_rule": exc.reason},
            )

        try:
            served, lines, product_of = self._serve_cart_for_view(cart_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                OwnDenyReason.OFFER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "refused": "vanished_after_decision",
                    "stated_rule": exc.reason,
                },
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_cart_view_of(served, lines, product_of), obs, event)

    def set_cart_item_quantity(
        self,
        subject_credential: str | None,
        cart_id: str,
        offer_id: str,
        *,
        quantity: int,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CartView, ObservabilityContext, AccessAuditEvent]:
        """Set the quantity of one cart line — ``commerce.carts.update``.

        The cart must exist here and be the subject's own; the line must
        exist — setting the quantity of a line that was never added is
        ``cart_line_unknown``, not an insertion. The quantity is absolute
        and positive. The mutation runs inside the IS-005 guard bound to
        the cart.
        """
        action = OPERATION_CART_UPDATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _quantity_problem(quantity)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )

        # --- step 1: the ownership boundary of this component ----------------
        cart = self.store.ownership_of_cart(cart_id)
        if cart is None:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if cart.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": cart.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="cart",
            resource_id=cart.cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 2b: the cart is the subject's own ---------------------------
        if cart.buyer_identity_id != subject_id:
            raise self._refuse(
                OwnDenyReason.BUYER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action,
            {
                "command": "set_cart_line_quantity",
                "cart_id": cart_id,
                "offer_id": offer_id,
                "quantity": quantity,
            },
        )
        try:
            self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=cart_id,
                fingerprint=fingerprint,
                effect=lambda: self.store.set_cart_line_quantity(
                    cart_id, offer_id, quantity=quantity, now=self.clock()
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                _domain_reason(exc.reason),
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_rule": exc.reason},
            )

        try:
            served, lines, product_of = self._serve_cart_for_view(cart_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                OwnDenyReason.OFFER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "refused": "vanished_after_decision",
                    "stated_rule": exc.reason,
                },
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_cart_view_of(served, lines, product_of), obs, event)

    def remove_cart_item(
        self,
        subject_credential: str | None,
        cart_id: str,
        offer_id: str,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CartView, ObservabilityContext, AccessAuditEvent]:
        """Remove one offer from a cart — ``commerce.carts.update``.

        The cart must exist here and be the subject's own; the line must
        exist — removing a line that was never added is
        ``cart_line_unknown``. The mutation runs inside the IS-005 guard
        bound to the cart.
        """
        action = OPERATION_CART_UPDATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        cart = self.store.ownership_of_cart(cart_id)
        if cart is None:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if cart.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": cart.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="cart",
            resource_id=cart.cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 2b: the cart is the subject's own ---------------------------
        if cart.buyer_identity_id != subject_id:
            raise self._refuse(
                OwnDenyReason.BUYER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action,
            {"command": "remove_cart_line", "cart_id": cart_id, "offer_id": offer_id},
        )
        try:
            self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=cart_id,
                fingerprint=fingerprint,
                effect=lambda: self.store.remove_cart_line(
                    cart_id, offer_id, now=self.clock()
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                _domain_reason(exc.reason),
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_rule": exc.reason},
            )

        try:
            served, lines, product_of = self._serve_cart_for_view(cart_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                OwnDenyReason.OFFER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "refused": "vanished_after_decision",
                    "stated_rule": exc.reason,
                },
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_cart_view_of(served, lines, product_of), obs, event)

    # --------------------------------------------------------------- checkout
    def checkout(
        self,
        subject_credential: str | None,
        *,
        cart_id: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[OrderView, ObservabilityContext, AccessAuditEvent]:
        """Convert one owned cart into an order — ``commerce.checkout.create``.

        Checkout is a business command, not an entity: the cart must exist
        here and be the subject's own; IS-003 decides
        ``commerce.checkout.create`` for the cart; then the IS-005 guard
        executes the conversion exactly once. The conversion validates the
        selected offers and their current prices, creates the Order with
        immutable purchase facts and price snapshots, consumes the cart
        lines, and sets the initial ``PENDING_PAYMENT`` state. The buyer
        check runs inside the owned-data operation, atomically with the
        consumption of the lines.
        """
        action = OPERATION_CHECKOUT_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        cart = self.store.ownership_of_cart(cart_id)
        if cart is None:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if cart.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": cart.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="cart",
            resource_id=cart.cart_id,
            resource_tenant_id=cart.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(action, {"cart_id": cart_id})
        try:
            created = self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=cart_id,
                fingerprint=fingerprint,
                effect=lambda: self.store.checkout_cart(
                    cart_id,
                    buyer_identity_id=subject_id or "",
                    now=self.clock(),
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                _domain_reason(exc.reason),
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_rule": exc.reason},
            )
        except KeyError:
            raise self._refuse(
                OwnDenyReason.CART_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=cart_id,
                resource_tenant_id=cart.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        order, lines = created
        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=order.order_id,
            resource_tenant_id=order.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_order_view_of(order, lines), obs, event)

    # ------------------------------------------------------------------- order
    def read_order(
        self,
        subject_credential: str | None,
        order_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[OrderView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned order — and only after the chain allowed it.

        A subject reads only its own orders: an order of another buyer is
        refused with ``buyer_mismatch`` even within the same Tenant — a
        check of the owned data after the decision, not a second decision.
        """
        action = OPERATION_ORDER_READ
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        order = self.store.ownership_of_order(order_id)
        if order is None:
            raise self._refuse(
                OwnDenyReason.ORDER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=order_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if order.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": order.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="order",
            resource_id=order.order_id,
            resource_tenant_id=order.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 2b: the order is the subject's own --------------------------
        if order.buyer_identity_id != subject_id:
            raise self._refuse(
                OwnDenyReason.BUYER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served, lines = self.store.order_contents(order.order_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.ORDER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=order_id,
            resource_tenant_id=order.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_order_view_of(served, lines), obs, event)

    def pay_order(
        self,
        subject_credential: str | None,
        order_id: str,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[OrderView, ObservabilityContext, AccessAuditEvent]:
        """Record one owned order as paid — ``commerce.orders.pay``.

        The order must exist here and be the subject's own; only a
        ``PENDING_PAYMENT`` order may be recorded as paid — any other move
        is ``invalid_state_transition``. Recording the payment moves the
        mutable payment state/result inside the IS-005 guard bound to the
        order and never touches the immutable purchase facts.
        """
        action = OPERATION_ORDER_PAY
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        order = self.store.ownership_of_order(order_id)
        if order is None:
            raise self._refuse(
                OwnDenyReason.ORDER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=order_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if order.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": order.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="order",
            resource_id=order.order_id,
            resource_tenant_id=order.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 2b: the order is the subject's own --------------------------
        if order.buyer_identity_id != subject_id:
            raise self._refuse(
                OwnDenyReason.BUYER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(action, {"order_id": order_id})
        try:
            self._guarded_execute(
                idempotency_key,
                action=action,
                obs=obs,
                subject_id=subject_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=order_id,
                fingerprint=fingerprint,
                effect=lambda: self.store.set_payment_state(
                    order_id, "PAID", now=self.clock()
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                _domain_reason(exc.reason),
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_rule": exc.reason},
            )
        except KeyError:
            raise self._refuse(
                OwnDenyReason.ORDER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        try:
            served, lines = self.store.order_contents(order_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.ORDER_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=order_id,
                resource_tenant_id=order.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=order_id,
            resource_tenant_id=order.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_order_view_of(served, lines), obs, event)

    def refuse_unreadable_request(
        self,
        *,
        path: str,
        schema_problems: list[str],
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AccessRefused:
        """Refuse — and audit — a request the published schema rejected."""
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        exc = AccessRefused(
            "malformed_request",
            status_code=422,
            details={
                "refused": "malformed_request",
                "schema_problems": list(schema_problems),
                "path": path,
            },
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        self.audit(
            action=ACTION_UNREADABLE,
            decision=DENY,
            reason="malformed_request",
            obs=obs,
            subject_id=None,
            tenant_id=None,
            resource_id=None,
            resource_tenant_id=None,
            claimed_tenant_id=None,
            details=dict(exc.details),
        )
        return exc

    # ------------------------------------------------------------------ chain
    def _decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        obs: ObservabilityContext,
        action: str,
    ) -> tuple[ObservabilityContext, str | None, str | None]:
        """Ask the port and enforce the answer; returns the decided context.

        Raises the audited refusal for every outcome that is not an explicit
        authoritative ALLOW. The owned-data operation runs only after this
        returns.
        """
        try:
            answer = self.authorization.decide(
                subject_credential,
                operation=operation,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:
            code = getattr(exc, "reason", None)
            code = getattr(code, "value", code)
            answer = DependencyRefusal(code if isinstance(code, str) and code else None)
        if isinstance(answer, DependencyRefusal):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_code": answer.reason_code},
            )
        if answer.decision == DENY and answer.reason in DENY_REASONS:
            raise self._refuse(
                answer.reason,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if not (answer.decision == ALLOW and answer.reason == PERMITTED):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "nonauthoritative_answer": {
                        "decision": answer.decision,
                        "reason": answer.reason,
                    }
                },
            )
        decided_obs = self.observability(
            tenant_id=answer.tenant_id,
            subject_id=answer.subject_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        return (decided_obs, answer.subject_id, answer.tenant_id)

    # -------------------------------------------------------------- outcomes
    def _guarded_execute(
        self,
        key: str | None,
        *,
        action: str,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        identity: str | None,
        operation: str,
        resource: str | None,
        fingerprint: str,
        effect: Callable[[], Any],
    ) -> Any:
        """Deliver one command to the IS-005 guard; its failure fails closed.

        ``IdempotencyConflict``, ``DomainRefusal`` and ``KeyError`` pass
        through to the caller's mapping — they are answers about the
        command, not about the dependency. Anything else means the safety
        dependency itself failed: the command is refused as
        ``authorization_unavailable`` — audited, without a business effect
        — exactly like a decision dependency that does not answer.
        """
        try:
            return self.idempotency.execute(
                key,
                identity=identity,
                tenant_id=tenant_id,
                operation=operation,
                resource=resource,
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=effect,
            )
        except (IdempotencyConflict, DomainRefusal, KeyError, AccessRefused):
            raise
        except Exception as exc:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "failing_dependency": "idempotency",
                    "stated_error": type(exc).__name__,
                },
            )

    def _refuse(
        self,
        reason: str,
        *,
        action: str,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessRefused:
        """Refuse one access attempt and audit the refusal on the way out."""
        self.audit(
            action=action,
            decision=DENY,
            reason=reason,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            details=details,
        )
        return AccessRefused(
            reason,
            status_code=_status_for(reason),
            details=details,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )

    # --------------------------------------------------------- observability
    def observability(
        self,
        *,
        tenant_id: str | None,
        subject_id: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ObservabilityContext:
        rid = request_id or _new_id("req")
        cid = correlation_id or rid
        return ObservabilityContext(
            timestamp=self.clock(),
            environment=self.config.environment,
            platform_id=self.config.platform_id,
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            request_id=rid,
            trace_id=rid,
            correlation_id=cid,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    def audit(
        self,
        *,
        action: str,
        decision: str,
        reason: str | None,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessAuditEvent:
        event = AccessAuditEvent(
            event_id=_new_id("aud"),
            action=action,
            decision=decision,
            reason=reason,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            platform_id=obs.platform_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            timestamp=self.clock(),
            details=details or {},
        )
        self.store.audit.append(event)
        return event

    # ------------------------------------------------------ IS-005 observability
    def _idempotency_audit(self, action: str, details: dict[str, Any]) -> None:
        """Map a report of the IS-005 guard into this component's audit journal.

        The guard reports ``idempotency_replay`` and ``idempotency_conflict``:
        both are security-relevant (a replayed command produced no second
        effect, a differing binding was refused) and are recorded in the same
        journal as every other access outcome.
        """
        request_id = details.get("request_id")
        correlation_id = details.get("correlation_id") or request_id
        obs = self.observability(
            tenant_id=details.get("tenant_id"),
            subject_id=details.get("identity"),
            request_id=request_id,
            correlation_id=correlation_id,
        )
        self.audit(
            action=action,
            decision=ALLOW if action == "idempotency_replay" else DENY,
            reason=action,
            obs=obs,
            subject_id=details.get("identity"),
            tenant_id=details.get("tenant_id"),
            resource_id=details.get("resource"),
            resource_tenant_id=None,
            claimed_tenant_id=None,
            details={key: value for key, value in details.items() if key != "resource"},
        )


__all__ = ["ACTION_UNREADABLE", "ENFORCEMENT_CHAIN", "CommerceEngine"]
