"""Published data contract of the Booking component (SCS-003).

This module publishes the whole vocabulary a consumer needs: the immutable
values an allowed access returns, the closed reason sets a refusal may
carry, and the error classes. Internal modules (``engine``, ``store``,
``models``, ``api``, ``deployment``, ``transport``, ``ports``,
``adapters``) are not part of the contract and must not be imported or
reached through the published surface (ARCHITECTURE.md §1.1, LAW-04).

The object a consumer receives is ``booking_service.reader.BookingClient``
— the seven published operations of ADR-0014 §14 over the published API
(create and read one owned Resource; declare and list the Availability
Windows of a Resource; create, read and cancel one owned Reservation),
values in and values out.

Two facts about the model are deliberate:

* views are the whole record and nothing more: the business fields of the
  representation. A view is produced only by the owned-data operation at
  the end of the enforcement chain, so holding a view means the access was
  allowed — and a refusal can never be mistaken for one. No tenant data is
  carried: identity references are opaque and owned outside Booking;
* the published reason vocabulary is closed: the reasons of this
  component's own boundary (see :class:`OwnDenyReason`) plus the published
  denial reasons of IS-003 passed through unchanged. A refusal outside that
  vocabulary cannot happen: a non-authoritative answer of the dependency is
  reported as ``authorization_unavailable`` and denied.

Timestamps in views (``start_at`` / ``end_at``) are ISO 8601 strings in
UTC with an explicit ``+00:00`` offset: the persisted, normalized form of
the timezone-aware input (ADR-0014 §5).
"""

from __future__ import annotations

from dataclasses import dataclass

from booking_service.consumed import DENY_REASONS as _DECISION_DENIALS
from booking_service.errors import AccessRefused, ConfigurationError, ContractViolation

#: This component: the single data owner of every record it serves.
OWNER_COMPONENT = "booking"

#: Resource types this component owns (the vocabulary of its IS-003 questions).
RESOURCE_TYPE_RESOURCE = "resource"
RESOURCE_TYPE_RESERVATION = "reservation"

#: The grant vocabulary the data owner asks IS-003 about. Reads change no
#: state beyond the append-only audit journal; every other operation is a
#: state-changing command guarded by IS-005. One operation, one grant, one
#: question per access — no policy logic lives inside Booking.
OPERATION_RESOURCE_CREATE = "booking.resources.create"
OPERATION_RESOURCE_READ = "booking.resources.read"
OPERATION_AVAILABILITY_CREATE = "booking.availability.create"
OPERATION_AVAILABILITY_READ = "booking.availability.read"
OPERATION_RESERVATION_CREATE = "booking.reservations.create"
OPERATION_RESERVATION_READ = "booking.reservations.read"
OPERATION_RESERVATION_CANCEL = "booking.reservations.cancel"

#: Resource lifecycle (ADR-0014 §3): exactly these two states.
RESOURCE_STATES: tuple[str, ...] = ("ACTIVE", "INACTIVE")

#: Reservation lifecycle (ADR-0014 §6): exactly these two states and the
#: single move ``ACTIVE → CANCELLED``.
RESERVATION_STATES: tuple[str, ...] = ("ACTIVE", "CANCELLED")


class OwnDenyReason:
    """Reasons of this component's own enforcement boundary (closed set).

    The Booking domain vocabulary of ADR-0014 §15:

    ``resource_not_found`` — no such Resource is owned here (a Resource of
    another Tenant named by a command is also ``resource_not_found``: the
    caller learns nothing about records of other Tenants).
    ``resource_inactive`` — the Resource is ``INACTIVE`` and receives no
    new Reservations.
    ``availability_not_found`` — the Resource declares no Availability
    Window at all, so no interval is bookable.
    ``outside_availability`` — the requested interval is not completely
    contained in one Availability Window of the Resource.
    ``reservation_conflict`` — an ACTIVE Reservation of the same Resource
    intersects the requested half-open interval (the double-booking
    invariant, enforced atomically in the store).
    ``reservation_not_found`` — no such Reservation is owned here.
    ``invalid_state_transition`` — the command does not apply to the
    record's current state: only an ``ACTIVE`` Reservation may be cancelled.
    ``booker_mismatch`` — the Reservation belongs to another booker of the
    same Tenant: a subject cancels only its own Reservations.

    Boundary reasons shared with the other data owners of the project:

    ``owner_mismatch`` — a record whose single owner is another component
    can never be served through this boundary.
    ``authorization_unavailable`` — the decision dependency did not answer,
    or answered outside its published contract: fail closed.
    ``idempotency_key_required`` — a state-changing command was sent without
    the mandatory ``Idempotency-Key`` header.
    ``idempotency_conflict`` — the ``Idempotency-Key`` was already used with
    a different binding (identity, tenant, operation, target or command).
    ``validation_error`` — the command payload is schema-valid but violates
    the content rules of the capability (empty name, unknown status, naive
    or unparsable timestamp, ``start_at >= end_at``): a refusal before any
    access decision.
    """

    RESOURCE_NOT_FOUND = "resource_not_found"
    RESOURCE_INACTIVE = "resource_inactive"
    AVAILABILITY_NOT_FOUND = "availability_not_found"
    OUTSIDE_AVAILABILITY = "outside_availability"
    RESERVATION_CONFLICT = "reservation_conflict"
    RESERVATION_NOT_FOUND = "reservation_not_found"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    BOOKER_MISMATCH = "booker_mismatch"
    OWNER_MISMATCH = "owner_mismatch"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    VALIDATION_ERROR = "validation_error"


#: The closed set of own reasons, as values.
OWN_DENY_REASONS: frozenset[str] = frozenset(
    {
        OwnDenyReason.RESOURCE_NOT_FOUND,
        OwnDenyReason.RESOURCE_INACTIVE,
        OwnDenyReason.AVAILABILITY_NOT_FOUND,
        OwnDenyReason.OUTSIDE_AVAILABILITY,
        OwnDenyReason.RESERVATION_CONFLICT,
        OwnDenyReason.RESERVATION_NOT_FOUND,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.BOOKER_MISMATCH,
        OwnDenyReason.OWNER_MISMATCH,
        OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
        OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
        OwnDenyReason.IDEMPOTENCY_CONFLICT,
        OwnDenyReason.VALIDATION_ERROR,
    }
)

#: The Booking domain vocabulary of ADR-0014 §15, as values.
DOMAIN_DENY_REASONS: frozenset[str] = frozenset(
    {
        OwnDenyReason.RESOURCE_NOT_FOUND,
        OwnDenyReason.RESOURCE_INACTIVE,
        OwnDenyReason.AVAILABILITY_NOT_FOUND,
        OwnDenyReason.OUTSIDE_AVAILABILITY,
        OwnDenyReason.RESERVATION_CONFLICT,
        OwnDenyReason.RESERVATION_NOT_FOUND,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.BOOKER_MISMATCH,
    }
)

#: Published denial reasons of IS-003 this boundary passes through unchanged
#: when the decision denies: the fact itself belongs to the authority.
PASSED_THROUGH_DENIALS: frozenset[str] = _DECISION_DENIALS

#: Every reason a published refusal may carry.
PUBLISHED_DENY_REASONS: frozenset[str] = OWN_DENY_REASONS | PASSED_THROUGH_DENIALS


@dataclass(frozen=True, slots=True)
class ResourceView:
    """The published representation of one owned Resource.

    Exactly the six business fields — ``resource_id``, ``name``,
    ``status``, ``created_by``, ``created_at``, ``updated_at`` — and
    nothing else. No tenant or owner data is exposed: the view is the value
    an allowed access returns, immutable and granting nothing by existing.
    ``status`` is ``ACTIVE`` or ``INACTIVE``.
    """

    resource_id: str
    name: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AvailabilityWindowView:
    """The published representation of one Availability Window.

    ``start_at`` / ``end_at`` are the UTC-normalized bounds of the half-open
    window ``[start_at, end_at)`` as ISO 8601 strings with ``+00:00``.
    """

    availability_id: str
    resource_id: str
    start_at: str
    end_at: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReservationView:
    """The published representation of one owned Reservation.

    ``booker_identity_id`` is the opaque reference to the verified subject
    that holds the reservation. ``start_at`` / ``end_at`` are the
    UTC-normalized bounds of ``[start_at, end_at)``. ``status`` is
    ``ACTIVE`` or ``CANCELLED``; ``cancelled_at`` is ``None`` until the
    single ``ACTIVE → CANCELLED`` move.
    """

    reservation_id: str
    resource_id: str
    booker_identity_id: str
    start_at: str
    end_at: str
    status: str
    created_at: str
    cancelled_at: str | None = None


__all__ = [
    "DOMAIN_DENY_REASONS",
    "OPERATION_AVAILABILITY_CREATE",
    "OPERATION_AVAILABILITY_READ",
    "OPERATION_RESERVATION_CANCEL",
    "OPERATION_RESERVATION_CREATE",
    "OPERATION_RESERVATION_READ",
    "OPERATION_RESOURCE_CREATE",
    "OPERATION_RESOURCE_READ",
    "OWNER_COMPONENT",
    "OWN_DENY_REASONS",
    "PASSED_THROUGH_DENIALS",
    "PUBLISHED_DENY_REASONS",
    "RESERVATION_STATES",
    "RESOURCE_STATES",
    "RESOURCE_TYPE_RESERVATION",
    "RESOURCE_TYPE_RESOURCE",
    "AccessRefused",
    "AvailabilityWindowView",
    "ConfigurationError",
    "ContractViolation",
    "OwnDenyReason",
    "ReservationView",
    "ResourceView",
]
