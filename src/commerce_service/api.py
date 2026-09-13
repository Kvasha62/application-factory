"""HTTP surface of Commerce (SCS-002) — the published contract.

Everything else (engine, stores, audit journal, ports, adapters,
deployment, transport) is internal: no other component may reach it
directly (ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this
contract in-process through ``commerce_service.transport`` and receive the
value-only client from ``commerce_service.reader``.

The surface is deliberately narrow: the catalog commands (create product,
define offer, set price), the cart commands (open, read, add, set
quantity, remove), the checkout command, the order read and the record-
payment command behind the same enforcement chain, plus health and
readiness. There is no bulk export, no search, no pagination and no
direct store path — no access path that bypasses the enforcement boundary
exists to bypass with.

``Authorization`` carries the credential presented by the subject; this
component verifies no credential itself — the decision dependency has it
verified by IS-001 inside the decision. ``X-Tenant-Id`` is a
caller-supplied cross-check only and is forwarded as such; it can never
select the effective tenant (LAW-16a).

Errors use the approved SCS-002 envelope: ``error.code`` /
``error.message`` / ``error.details`` plus top-level ``request_id`` /
``correlation_id``. The stable internal reason is preserved in
``error.details.reason``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from commerce_service import COMPONENT_ID, COMPONENT_VERSION
from commerce_service.deployment import CommerceDeployment
from commerce_service.errors import AccessRefused

__all__ = [
    "ERROR_CODES",
    "CartItemAddIn",
    "CartItemQuantityIn",
    "CartLineOut",
    "CartOut",
    "CheckoutIn",
    "ErrorBody",
    "ErrorEnvelope",
    "OfferCreateIn",
    "OfferOut",
    "OrderLineOut",
    "OrderOut",
    "PriceCreateIn",
    "PriceOut",
    "ProductCreateIn",
    "ProductOut",
    "create_app",
    "error_code_for",
]


class ProductOut(BaseModel):
    """The published product representation: the eight fields, nothing else."""

    product_id: str
    name: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class ProductCreateIn(BaseModel):
    """The create-product command payload."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str


class OfferOut(BaseModel):
    """The published offer representation: the seven fields, nothing else."""

    offer_id: str
    product_id: str
    name: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class OfferCreateIn(BaseModel):
    """The define-offer command payload: the parent Product and the name."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    name: str


class PriceOut(BaseModel):
    """The published price representation: the five fields, nothing else."""

    price_id: str
    offer_id: str
    amount: int
    currency: str
    created_at: str


class PriceCreateIn(BaseModel):
    """The set-price command payload: the parent Offer and the price."""

    model_config = ConfigDict(extra="forbid")

    offer_id: str
    amount: int
    currency: str


class CartLineOut(BaseModel):
    """The published representation of one selected offer inside a cart."""

    offer_id: str
    product_id: str
    quantity: int


class CartOut(BaseModel):
    """The published cart representation with its lines in order."""

    cart_id: str
    buyer_identity_id: str
    created_at: str
    updated_at: str
    items: list[CartLineOut]


class CartItemAddIn(BaseModel):
    """The add-to-cart command payload: the offer and the quantity."""

    model_config = ConfigDict(extra="forbid")

    offer_id: str
    quantity: int


class CartItemQuantityIn(BaseModel):
    """The set-quantity command payload: the absolute quantity."""

    model_config = ConfigDict(extra="forbid")

    quantity: int


class CheckoutIn(BaseModel):
    """The checkout command payload: the cart to convert into an order."""

    model_config = ConfigDict(extra="forbid")

    cart_id: str


class OrderLineOut(BaseModel):
    """The published representation of one bought position of an order."""

    order_line_id: str
    offer_id: str
    product_id: str
    amount: int
    currency: str
    quantity: int


class OrderOut(BaseModel):
    """The published order representation with its lines in order."""

    order_id: str
    buyer_identity_id: str
    payment_state: str
    created_at: str
    updated_at: str
    lines: list[OrderLineOut]


class ErrorBody(BaseModel):
    """The ``error`` object of the approved SCS-002 envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any]


class ErrorEnvelope(BaseModel):
    """The approved SCS-002 error envelope of every refusal."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str
    correlation_id: str


#: The minimal published error-code vocabulary of SCS-002. The HTTP status
#: selects the code; the stable internal reason is preserved in
#: ``error.details.reason``.
ERROR_CODES: dict[str, dict[str, Any]] = {
    "AUTHENTICATION_REQUIRED": {
        "status": 401,
        "message": "Subject authentication is required.",
    },
    "AUTHORIZATION_DENIED": {
        "status": 403,
        "message": "Operation is not permitted.",
    },
    "NOT_FOUND": {
        "status": 404,
        "message": "Requested resource was not found.",
    },
    "INVALID_REQUEST": {
        "status": 422,
        "message": "Request does not match the published contract.",
    },
    "VALIDATION_ERROR": {
        "status": 422,
        "message": "Request payload is not valid.",
    },
    "INVALID_STATE_TRANSITION": {
        "status": 409,
        "message": "Operation is not valid for the current state.",
    },
    "IDEMPOTENCY_KEY_REQUIRED": {
        "status": 400,
        "message": "An Idempotency-Key header is required for this operation.",
    },
    "IDEMPOTENCY_CONFLICT": {
        "status": 409,
        "message": "The Idempotency-Key was already used with a different request.",
    },
    "DEPENDENCY_UNAVAILABLE": {
        "status": 503,
        "message": "Authorization dependency is unavailable.",
    },
}

_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)

_NOT_FOUND_REASONS = frozenset(
    {
        "product_unknown",
        "offer_unknown",
        "price_unknown",
        "cart_unknown",
        "order_unknown",
        "cart_line_unknown",
    }
)


def error_code_for(reason: str) -> str:
    """The published error code for one internal refusal reason.

    The mapping follows the HTTP status of the refusal; every reason that
    denies with 403 shares ``AUTHORIZATION_DENIED`` while its own stable
    value is preserved in ``error.details.reason``.
    """
    if reason in _AUTHENTICATION_DENIALS:
        return "AUTHENTICATION_REQUIRED"
    if reason in _NOT_FOUND_REASONS:
        return "NOT_FOUND"
    if reason == "malformed_request":
        return "INVALID_REQUEST"
    if reason == "validation_error":
        return "VALIDATION_ERROR"
    if reason in ("invalid_state_transition", "cart_empty"):
        return "INVALID_STATE_TRANSITION"
    if reason == "idempotency_key_required":
        return "IDEMPOTENCY_KEY_REQUIRED"
    if reason == "idempotency_conflict":
        return "IDEMPOTENCY_CONFLICT"
    if reason == "authorization_unavailable":
        return "DEPENDENCY_UNAVAILABLE"
    return "AUTHORIZATION_DENIED"


def _token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def _refused(exc: AccessRefused) -> JSONResponse:
    """Render one audited refusal in the approved SCS-002 envelope."""
    code = error_code_for(exc.reason)
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=str(ERROR_CODES[code]["message"]),
            details={"reason": exc.reason},
        ),
        request_id=exc.request_id or "",
        correlation_id=exc.correlation_id or exc.request_id or "",
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope.model_dump(),
    )


def _schema_problems(exc: RequestValidationError) -> list[str]:
    """Describe what the contract could not read — never the values it was sent."""
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location or 'body'}: {error.get('type', 'invalid')}")
    return sorted(problems)


def create_app(deployment: CommerceDeployment) -> FastAPI:
    """Bind the published HTTP contract to one Commerce deployment."""
    engine = deployment.engine
    app = FastAPI(
        title="SCS-002 Commerce",
        version=COMPONENT_VERSION,
    )

    @app.exception_handler(RequestValidationError)
    async def unreadable_request(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Refuse — and audit — a request the contract schema rejected."""
        refusal = engine.refuse_unreadable_request(
            path=request.url.path,
            schema_problems=_schema_problems(exc),
            request_id=request.headers.get("x-request-id"),
            correlation_id=request.headers.get("x-correlation-id"),
        )
        return _refused(refusal)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "component_id": COMPONENT_ID,
            "version": COMPONENT_VERSION,
            "platform_id": engine.current_platform_id,
        }

    @app.get("/ready")
    def ready() -> dict:
        return {"status": "ready", "component_id": COMPONENT_ID}

    def _cart_out(view: Any) -> CartOut:
        return CartOut(
            cart_id=view.cart_id,
            buyer_identity_id=view.buyer_identity_id,
            created_at=view.created_at,
            updated_at=view.updated_at,
            items=[
                CartLineOut(
                    offer_id=item.offer_id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
                for item in view.items
            ],
        )

    def _order_out(view: Any) -> OrderOut:
        return OrderOut(
            order_id=view.order_id,
            buyer_identity_id=view.buyer_identity_id,
            payment_state=view.payment_state,
            created_at=view.created_at,
            updated_at=view.updated_at,
            lines=[
                OrderLineOut(
                    order_line_id=line.order_line_id,
                    offer_id=line.offer_id,
                    product_id=line.product_id,
                    amount=line.amount,
                    currency=line.currency,
                    quantity=line.quantity,
                )
                for line in view.lines
            ],
        )

    @app.post(
        "/api/v1/commerce/products",
        response_model=ProductOut,
        status_code=201,
    )
    def create_product(
        payload: ProductCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Create one owned Product — the command ``commerce.products.create``.

        ``Idempotency-Key`` is mandatory: the command is a state-changing
        command delivered through IS-005. The effective tenant comes from the
        verified identity through the published chain — never from the
        payload and never from ``X-Tenant-Id``, which is a cross-check only
        (LAW-16a). A new Product is created ``ACTIVE``.
        """
        try:
            view, _, _ = engine.create_product(
                _token(authorization),
                name=payload.name,
                description=payload.description,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return ProductOut(
            product_id=view.product_id,
            name=view.name,
            description=view.description,
            status=view.status,
            created_by=view.created_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )

    @app.get(
        "/api/v1/commerce/products/{product_id}",
        response_model=ProductOut,
    )
    def read_product(
        product_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve one owned Product — the read operation
        ``commerce.products.read``."""
        try:
            view, _, _ = engine.read_product(
                _token(authorization),
                product_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return ProductOut(
            product_id=view.product_id,
            name=view.name,
            description=view.description,
            status=view.status,
            created_by=view.created_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )

    # ------------------------------------------------------------------ catalog
    @app.post(
        "/api/v1/commerce/offers",
        response_model=OfferOut,
        status_code=201,
    )
    def create_offer(
        payload: OfferCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Define one sellable proposition of a Product — the command
        ``commerce.offers.create``.

        The parent Product must exist here; the Offer belongs to the
        Product's Tenant by construction and is created ``ACTIVE``.
        ``Idempotency-Key`` is mandatory (IS-005).
        """
        try:
            view, _, _ = engine.create_offer(
                _token(authorization),
                product_id=payload.product_id,
                name=payload.name,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return OfferOut(
            offer_id=view.offer_id,
            product_id=view.product_id,
            name=view.name,
            status=view.status,
            created_by=view.created_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )

    @app.post(
        "/api/v1/commerce/prices",
        response_model=PriceOut,
        status_code=201,
    )
    def create_price(
        payload: PriceCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Set the current price of one Offer — the command
        ``commerce.prices.create``.

        The parent Offer must exist here; the Price belongs to the Offer's
        Tenant by construction. Prices are append-only history: a new
        record never rewrites a past one. ``Idempotency-Key`` is mandatory
        (IS-005).
        """
        try:
            view, _, _ = engine.create_price(
                _token(authorization),
                offer_id=payload.offer_id,
                amount=payload.amount,
                currency=payload.currency,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return PriceOut(
            price_id=view.price_id,
            offer_id=view.offer_id,
            amount=view.amount,
            currency=view.currency,
            created_at=view.created_at,
        )

    # -------------------------------------------------------------------- cart
    @app.post(
        "/api/v1/commerce/carts",
        response_model=CartOut,
        status_code=201,
    )
    def create_cart(
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Open one cart for the verified subject — the command
        ``commerce.carts.create``.

        The command carries no body: the buyer is the verified subject and
        the Tenant is the effective tenant of the chain, so a subject can
        only ever open its own cart. A new cart holds no lines.
        ``Idempotency-Key`` is mandatory (IS-005).
        """
        try:
            view, _, _ = engine.create_cart(
                _token(authorization),
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _cart_out(view)

    @app.get(
        "/api/v1/commerce/carts/{cart_id}",
        response_model=CartOut,
    )
    def read_cart(
        cart_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve one owned cart — the read operation ``commerce.carts.read``.

        A subject reads only its own carts: a cart of another buyer is
        refused even within the same Tenant.
        """
        try:
            view, _, _ = engine.read_cart(
                _token(authorization),
                cart_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _cart_out(view)

    @app.post(
        "/api/v1/commerce/carts/{cart_id}/items",
        response_model=CartOut,
    )
    def add_cart_item(
        cart_id: str,
        payload: CartItemAddIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Add one offer to a cart — the command ``commerce.carts.update``.

        The cart must be the subject's own; the offer must exist here and
        belong to the cart's Tenant. Adding an offer that is already
        selected increments its line. ``Idempotency-Key`` is mandatory
        (IS-005).
        """
        try:
            view, _, _ = engine.add_cart_item(
                _token(authorization),
                cart_id,
                offer_id=payload.offer_id,
                quantity=payload.quantity,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _cart_out(view)

    @app.post(
        "/api/v1/commerce/carts/{cart_id}/items/{offer_id}/quantity",
        response_model=CartOut,
    )
    def set_cart_item_quantity(
        cart_id: str,
        offer_id: str,
        payload: CartItemQuantityIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Set the quantity of one cart line — the command
        ``commerce.carts.update``.

        The line must exist; the quantity is absolute and positive.
        ``Idempotency-Key`` is mandatory (IS-005).
        """
        try:
            view, _, _ = engine.set_cart_item_quantity(
                _token(authorization),
                cart_id,
                offer_id,
                quantity=payload.quantity,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _cart_out(view)

    @app.post(
        "/api/v1/commerce/carts/{cart_id}/items/{offer_id}/remove",
        response_model=CartOut,
    )
    def remove_cart_item(
        cart_id: str,
        offer_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Remove one offer from a cart — the command ``commerce.carts.update``.

        The line must exist. ``Idempotency-Key`` is mandatory (IS-005).
        """
        try:
            view, _, _ = engine.remove_cart_item(
                _token(authorization),
                cart_id,
                offer_id,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _cart_out(view)

    # ---------------------------------------------------------------- checkout
    @app.post(
        "/api/v1/commerce/checkout",
        response_model=OrderOut,
        status_code=201,
    )
    def checkout(
        payload: CheckoutIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Convert one owned cart into an order — the command
        ``commerce.checkout.create``.

        Checkout is a business command, not an entity: it validates the
        selected offers and their current prices, creates the Order with
        immutable purchase facts and price snapshots, consumes the cart
        lines, and sets the initial ``PENDING_PAYMENT`` state.
        ``Idempotency-Key`` is mandatory (IS-005): an exact replay returns
        the recorded order without a second effect.
        """
        try:
            view, _, _ = engine.checkout(
                _token(authorization),
                cart_id=payload.cart_id,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _order_out(view)

    # ------------------------------------------------------------------- order
    @app.get(
        "/api/v1/commerce/orders/{order_id}",
        response_model=OrderOut,
    )
    def read_order(
        order_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve one owned order — the read operation ``commerce.orders.read``.

        A subject reads only its own orders: an order of another buyer is
        refused even within the same Tenant. The purchase facts shown here
        are immutable; only the payment state moves.
        """
        try:
            view, _, _ = engine.read_order(
                _token(authorization),
                order_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _order_out(view)

    @app.post(
        "/api/v1/commerce/orders/{order_id}/pay",
        response_model=OrderOut,
    )
    def pay_order(
        order_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Record one owned order as paid — the command ``commerce.orders.pay``.

        Only a ``PENDING_PAYMENT`` order may be recorded as paid, and only
        by its own buyer. Recording the payment moves the mutable payment
        state/result and never touches the immutable purchase facts.
        ``Idempotency-Key`` is mandatory (IS-005).
        """
        try:
            view, _, _ = engine.pay_order(
                _token(authorization),
                order_id,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _order_out(view)

    return app
