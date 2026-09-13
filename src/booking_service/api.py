"""HTTP surface of Booking (SCS-003) — the published contract.

Everything else (engine, store, audit journal, ports, adapters,
deployment, transport) is internal: no other component may reach it
directly (ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this
contract in-process through ``booking_service.transport`` and receive the
value-only client from ``booking_service.reader``.

The surface is exactly the seven business operations of ADR-0014 §14 —
create/read one Resource, declare/list the Availability Windows of a
Resource, create/read/cancel one Reservation — behind the same enforcement
chain, plus health and readiness. There is no listing of resources or
reservations, no search, no delete, no update and no direct store path —
no access path that bypasses the enforcement boundary exists to bypass
with.

``Authorization`` carries the credential presented by the subject; this
component verifies no credential itself — the decision dependency has it
verified by IS-001 inside the decision. ``X-Tenant-Id`` is a
caller-supplied cross-check only and is forwarded as such; it can never
select the effective tenant (LAW-16a).

Timestamps: request bodies carry ISO 8601 timestamps **with an explicit
UTC offset** (``Z`` or ``±hh:mm``); a naive timestamp is refused with
``422 VALIDATION_ERROR``. Responses carry the persisted UTC form with
``+00:00``.

Errors use the approved SCS envelope: ``error.code`` / ``error.message``
/ ``error.details`` plus top-level ``request_id`` / ``correlation_id``.
The stable internal reason is preserved in ``error.details.reason``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from booking_service import COMPONENT_ID, COMPONENT_VERSION
from booking_service.deployment import BookingDeployment
from booking_service.errors import AccessRefused

__all__ = [
    "ERROR_CODES",
    "AvailabilityCreateIn",
    "AvailabilityListOut",
    "AvailabilityOut",
    "ErrorBody",
    "ErrorEnvelope",
    "ReservationCreateIn",
    "ReservationOut",
    "ResourceCreateIn",
    "ResourceOut",
    "create_app",
    "error_code_for",
]


class ResourceOut(BaseModel):
    """The published resource representation: the six fields, nothing else."""

    resource_id: str
    name: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class ResourceCreateIn(BaseModel):
    """The create-resource command payload.

    ``status`` defaults to ``ACTIVE``; ``INACTIVE`` declares a Resource
    that receives no Reservations.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str = "ACTIVE"


class AvailabilityCreateIn(BaseModel):
    """The declare-availability command payload: one half-open window.

    Both bounds are ISO 8601 timestamps with an explicit UTC offset.
    """

    model_config = ConfigDict(extra="forbid")

    start_at: str
    end_at: str


class AvailabilityOut(BaseModel):
    """The published availability window representation (UTC bounds)."""

    availability_id: str
    resource_id: str
    start_at: str
    end_at: str
    created_at: str


class AvailabilityListOut(BaseModel):
    """The Availability Windows of one Resource in deterministic order."""

    resource_id: str
    items: list[AvailabilityOut]


class ReservationCreateIn(BaseModel):
    """The create-reservation command payload.

    The booker is the verified subject — there is no ``booker_identity_id``
    field by construction. Both bounds are ISO 8601 timestamps with an
    explicit UTC offset.
    """

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    start_at: str
    end_at: str


class ReservationOut(BaseModel):
    """The published reservation representation (UTC bounds)."""

    reservation_id: str
    resource_id: str
    booker_identity_id: str
    start_at: str
    end_at: str
    status: str
    created_at: str
    cancelled_at: str | None


class ErrorBody(BaseModel):
    """The ``error`` object of the approved envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any]


class ErrorEnvelope(BaseModel):
    """The approved error envelope of every refusal."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str
    correlation_id: str


#: The minimal published error-code vocabulary of SCS-003. The HTTP status
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
    "RESOURCE_INACTIVE": {
        "status": 409,
        "message": "Resource is inactive and accepts no new reservations.",
    },
    "OUTSIDE_AVAILABILITY": {
        "status": 409,
        "message": "Requested interval is not within an availability window.",
    },
    "RESERVATION_CONFLICT": {
        "status": 409,
        "message": "Requested interval conflicts with an active reservation.",
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

_NOT_FOUND_REASONS = frozenset({"resource_not_found", "reservation_not_found"})


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
    if reason in ("outside_availability", "availability_not_found"):
        return "OUTSIDE_AVAILABILITY"
    if reason == "reservation_conflict":
        return "RESERVATION_CONFLICT"
    if reason == "resource_inactive":
        return "RESOURCE_INACTIVE"
    if reason == "invalid_state_transition":
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
    """Render one audited refusal in the approved envelope."""
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
    return JSONResponse(status_code=exc.status_code, content=envelope.model_dump())


def _schema_problems(exc: RequestValidationError) -> list[str]:
    """Describe what the contract could not read — never the values it was sent."""
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location or 'body'}: {error.get('type', 'invalid')}")
    return sorted(problems)


def _resource_out(view: Any) -> ResourceOut:
    return ResourceOut(
        resource_id=view.resource_id,
        name=view.name,
        status=view.status,
        created_by=view.created_by,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _availability_out(view: Any) -> AvailabilityOut:
    return AvailabilityOut(
        availability_id=view.availability_id,
        resource_id=view.resource_id,
        start_at=view.start_at,
        end_at=view.end_at,
        created_at=view.created_at,
    )


def _reservation_out(view: Any) -> ReservationOut:
    return ReservationOut(
        reservation_id=view.reservation_id,
        resource_id=view.resource_id,
        booker_identity_id=view.booker_identity_id,
        start_at=view.start_at,
        end_at=view.end_at,
        status=view.status,
        created_at=view.created_at,
        cancelled_at=view.cancelled_at,
    )


def create_app(deployment: BookingDeployment) -> FastAPI:
    """Bind the published HTTP contract to one Booking deployment."""
    engine = deployment.engine
    app = FastAPI(title="SCS-003 Booking", version=COMPONENT_VERSION)

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

    # ---------------------------------------------------------------- resource
    @app.post(
        "/api/v1/booking/resources",
        response_model=ResourceOut,
        status_code=201,
    )
    def create_resource(
        payload: ResourceCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Create one owned Resource — the command ``booking.resources.create``.

        ``Idempotency-Key`` is mandatory (IS-005). The effective tenant
        comes from the verified identity through the published chain —
        never from the payload and never from ``X-Tenant-Id`` (LAW-16a).
        """
        try:
            view, _, _ = engine.create_resource(
                _token(authorization),
                name=payload.name,
                status=payload.status,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _resource_out(view)

    @app.get(
        "/api/v1/booking/resources/{resource_id}",
        response_model=ResourceOut,
    )
    def read_resource(
        resource_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve one owned Resource — ``booking.resources.read``."""
        try:
            view, _, _ = engine.read_resource(
                _token(authorization),
                resource_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _resource_out(view)

    # ------------------------------------------------------------ availability
    @app.post(
        "/api/v1/booking/resources/{resource_id}/availability",
        response_model=AvailabilityOut,
        status_code=201,
    )
    def create_availability(
        resource_id: str,
        payload: AvailabilityCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Declare one Availability Window — ``booking.availability.create``.

        ``[start_at, end_at)`` with ``start_at < end_at``, timezone-aware,
        persisted in UTC. ``Idempotency-Key`` is mandatory (IS-005).
        """
        try:
            view, _, _ = engine.create_availability(
                _token(authorization),
                resource_id,
                start_at=payload.start_at,
                end_at=payload.end_at,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _availability_out(view)

    @app.get(
        "/api/v1/booking/resources/{resource_id}/availability",
        response_model=AvailabilityListOut,
    )
    def list_availability(
        resource_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve the Availability Windows of one Resource —
        ``booking.availability.read``."""
        try:
            views, _, _ = engine.list_availability(
                _token(authorization),
                resource_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return AvailabilityListOut(
            resource_id=resource_id,
            items=[_availability_out(view) for view in views],
        )

    # ------------------------------------------------------------- reservation
    @app.post(
        "/api/v1/booking/reservations",
        response_model=ReservationOut,
        status_code=201,
    )
    def create_reservation(
        payload: ReservationCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Create one Reservation — ``booking.reservations.create``.

        The booker is the verified subject. The five creation conditions of
        ADR-0014 §8 are decided atomically; a conflicting ``ACTIVE``
        Reservation answers ``409 RESERVATION_CONFLICT``.
        ``Idempotency-Key`` is mandatory (IS-005).
        """
        try:
            view, _, _ = engine.create_reservation(
                _token(authorization),
                resource_id=payload.resource_id,
                start_at=payload.start_at,
                end_at=payload.end_at,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _reservation_out(view)

    @app.get(
        "/api/v1/booking/reservations/{reservation_id}",
        response_model=ReservationOut,
    )
    def read_reservation(
        reservation_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve one owned Reservation — ``booking.reservations.read``."""
        try:
            view, _, _ = engine.read_reservation(
                _token(authorization),
                reservation_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _reservation_out(view)

    @app.post(
        "/api/v1/booking/reservations/{reservation_id}/cancel",
        response_model=ReservationOut,
    )
    def cancel_reservation(
        reservation_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Cancel one owned Reservation — ``booking.reservations.cancel``.

        Only the booker cancels, only an ``ACTIVE`` Reservation moves, and
        the record is preserved as ``CANCELLED``. ``Idempotency-Key`` is
        mandatory (IS-005).
        """
        try:
            view, _, _ = engine.cancel_reservation(
                _token(authorization),
                reservation_id,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _reservation_out(view)

    return app
