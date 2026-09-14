"""Published consumer surface of Booking (SCS-003) — values in, values out.

A consumer is handed exactly one object — :class:`BookingClient` — and it
receives immutable values back. The client performs exactly the seven
published operations of ADR-0014 §14 (create and read one owned Resource;
declare and list the Availability Windows of a Resource; create, read and
cancel one owned Reservation) and nothing else; the exact same API is
available to a remote consumer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from booking_service.contracts import (
    PUBLISHED_DENY_REASONS,
    AvailabilityWindowView,
    ReservationView,
    ResourceView,
)
from booking_service.errors import AccessRefused, ContractViolation

__all__ = ["BookingClient", "build_client"]

_BASE = "/api/v1/booking"


class BookingClient:
    """Value-only access reader over the published booking contract.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that
    issued them.
    """

    __slots__ = ("__weakref__", "_channel")

    def __init__(self, channel: str) -> None:
        if not isinstance(channel, str) or not channel:
            raise ValueError("a booking contract channel handle is required")
        self._channel = channel

    # ---------------------------------------------------------------- resource
    def create_resource(
        self,
        subject_credential: str | None,
        *,
        name: str,
        status: str = "ACTIVE",
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ResourceView:
        """Create one owned Resource through the enforcement chain.

        ``subject_credential`` is verified by IS-001 inside the IS-003
        decision, so a consumer cannot assert who the subject is.
        ``claimed_tenant_id`` is forwarded as a cross-check only.
        ``idempotency_key`` is mandatory (IS-005).
        """
        status_code, payload = self._call(
            "POST",
            f"{_BASE}/resources",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"name": name, "status": status},
        )
        if not 200 <= status_code < 300:
            raise self._error(status_code, payload)
        return _resource_from(payload)

    def read_resource(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ResourceView:
        """Read one owned Resource through the enforcement chain."""
        status_code, payload = self._call(
            "GET",
            f"{_BASE}/resources/{quote(resource_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status_code < 300:
            raise self._error(status_code, payload)
        return _resource_from(payload)

    # ------------------------------------------------------------ availability
    def create_availability(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        start_at: str,
        end_at: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AvailabilityWindowView:
        """Declare one Availability Window ``[start_at, end_at)`` of a Resource.

        Both bounds are ISO 8601 timestamps with an explicit UTC offset.
        ``idempotency_key`` is mandatory (IS-005).
        """
        status_code, payload = self._call(
            "POST",
            f"{_BASE}/resources/{quote(resource_id, safe='')}/availability",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"start_at": start_at, "end_at": end_at},
        )
        if not 200 <= status_code < 300:
            raise self._error(status_code, payload)
        return _availability_from(payload)

    def list_availability(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[AvailabilityWindowView, ...]:
        """Read the Availability Windows of one Resource in deterministic order."""
        status_code, payload = self._call(
            "GET",
            f"{_BASE}/resources/{quote(resource_id, safe='')}/availability",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status_code < 300:
            raise self._error(status_code, payload)
        try:
            items = payload["items"]
            if not isinstance(items, list):
                raise TypeError("items must be a list")
            return tuple(_availability_from(item) for item in items)
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolation(
                "the booking contract answered outside its published availability model"
            ) from exc

    # ------------------------------------------------------------- reservation
    def create_reservation(
        self,
        subject_credential: str | None,
        *,
        resource_id: str,
        start_at: str,
        end_at: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ReservationView:
        """Create one Reservation for the verified subject.

        A conflicting ``ACTIVE`` Reservation raises ``AccessRefused`` with
        reason ``reservation_conflict`` (409). ``idempotency_key`` is
        mandatory (IS-005).
        """
        status_code, payload = self._call(
            "POST",
            f"{_BASE}/reservations",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"resource_id": resource_id, "start_at": start_at, "end_at": end_at},
        )
        if not 200 <= status_code < 300:
            raise self._error(status_code, payload)
        return _reservation_from(payload)

    def read_reservation(
        self,
        subject_credential: str | None,
        reservation_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ReservationView:
        """Read one owned Reservation through the enforcement chain."""
        status_code, payload = self._call(
            "GET",
            f"{_BASE}/reservations/{quote(reservation_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status_code < 300:
            raise self._error(status_code, payload)
        return _reservation_from(payload)

    def cancel_reservation(
        self,
        subject_credential: str | None,
        reservation_id: str,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ReservationView:
        """Cancel one owned Reservation (``ACTIVE → CANCELLED``).

        ``idempotency_key`` is mandatory (IS-005).
        """
        status_code, payload = self._call(
            "POST",
            f"{_BASE}/reservations/{quote(reservation_id, safe='')}/cancel",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        if not 200 <= status_code < 300:
            raise self._error(status_code, payload)
        return _reservation_from(payload)

    # --------------------------------------------------------------- internals
    def _call(
        self,
        method: str,
        path: str,
        *,
        subject_credential: str | None,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
        idempotency_key: str | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        headers: list[tuple[str, str]] = []
        if subject_credential is not None:
            headers.append(("authorization", f"Bearer {subject_credential}"))
        if claimed_tenant_id:
            headers.append(("x-tenant-id", claimed_tenant_id))
        if idempotency_key:
            headers.append(("idempotency-key", idempotency_key))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))
        return _call_contract()(self._channel, method, path, headers, body)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        refusal = _refusal_of(payload)
        if refusal is None:
            if status >= 500:
                return ContractViolation(
                    f"booking contract failed with status {status}"
                )
            return ContractViolation(
                "the booking contract refused without a published reason"
            )
        reason, request_id, correlation_id = refusal
        return AccessRefused(
            reason,
            status_code=status,
            request_id=request_id,
            correlation_id=correlation_id,
        )


def _resource_from(payload: Mapping[str, Any]) -> ResourceView:
    """Rebuild the published resource value; an outside answer fails closed."""
    try:
        return ResourceView(
            resource_id=_text_field(payload, "resource_id"),
            name=_text_field(payload, "name"),
            status=_text_field(payload, "status"),
            created_by=_text_field(payload, "created_by"),
            created_at=_text_field(payload, "created_at"),
            updated_at=_text_field(payload, "updated_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the booking contract answered outside its published resource model"
        ) from exc


def _availability_from(payload: Any) -> AvailabilityWindowView:
    """Rebuild one published availability window; an outside answer fails closed."""
    if not isinstance(payload, Mapping):
        raise TypeError("an availability window must be a mapping")
    try:
        return AvailabilityWindowView(
            availability_id=_text_field(payload, "availability_id"),
            resource_id=_text_field(payload, "resource_id"),
            start_at=_text_field(payload, "start_at"),
            end_at=_text_field(payload, "end_at"),
            created_at=_text_field(payload, "created_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the booking contract answered outside its published availability model"
        ) from exc


def _reservation_from(payload: Mapping[str, Any]) -> ReservationView:
    """Rebuild the published reservation value; an outside answer fails closed."""
    try:
        cancelled_at = payload.get("cancelled_at")
        if cancelled_at is not None and not isinstance(cancelled_at, str):
            raise TypeError("cancelled_at must be a string or null")
        return ReservationView(
            reservation_id=_text_field(payload, "reservation_id"),
            resource_id=_text_field(payload, "resource_id"),
            booker_identity_id=_text_field(payload, "booker_identity_id"),
            start_at=_text_field(payload, "start_at"),
            end_at=_text_field(payload, "end_at"),
            status=_text_field(payload, "status"),
            created_at=_text_field(payload, "created_at"),
            cancelled_at=cancelled_at,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the booking contract answered outside its published reservation model"
        ) from exc


def _text_field(payload: Mapping[str, Any], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _refusal_of(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Read the approved error envelope: (reason, request_id, correlation_id).

    The envelope shape is strict: ``error.code`` must be present, the
    internal reason must come from ``error.details.reason`` and belong to
    the published vocabulary, and top-level ``request_id`` /
    ``correlation_id`` must be present. Anything else fails closed.
    """
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code:
        return None
    details = error.get("details")
    if not isinstance(details, Mapping):
        return None
    reason = details.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    if reason != "malformed_request" and reason not in PUBLISHED_DENY_REASONS:
        return None
    request_id = payload.get("request_id")
    correlation_id = payload.get("correlation_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(correlation_id, str) or not correlation_id:
        return None
    return (reason, request_id, correlation_id)


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the
    boundary: after ``import booking_service.reader`` this module's
    namespace holds no module object, no channel table, no transport class
    and no application.
    """
    from booking_service.transport import call_contract

    return call_contract


def build_client(app: Any) -> BookingClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — the component's published ASGI contract — is handed to the
    provider's own channel table and stays there: the client receives the
    handle back, not the application. A client dropped by its consumer
    revokes that handle, so the table is not a place where applications
    accumulate.
    """
    from booking_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = BookingClient(channel)
    revoke_on_death(client, channel)
    return client
