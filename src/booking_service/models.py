"""Internal records of the Booking component (SCS-003).

These types describe the owned data of the component (logical schema
``booking``). They are not part of the public contract; consumers receive
:mod:`booking_service.contracts` values.

The model implements the ratified ADR-0014 contour:

* ``Resource`` — what can be booked; lifecycle ``ACTIVE`` / ``INACTIVE``;
  only an ACTIVE Resource receives new Reservations;
* ``Availability Window`` — an explicitly allowed booking interval of one
  Resource: *when booking is allowed*. It is a permission window, never
  occupancy and never per-minute slot inventory;
* ``Reservation`` — *what is booked*: one half-open interval of one
  Resource held by one booker; lifecycle ``ACTIVE → CANCELLED``. Occupancy
  of a Resource is determined by its ACTIVE Reservations and nothing else.

Every interval field (``start_at`` / ``end_at``) of a persisted record is a
timezone-aware :class:`datetime` already normalized to UTC by the store:
a naive timestamp is never persisted (ADR-0014 §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class OwnedResource:
    """One bookable Resource owned by this component.

    ``owner_component`` is singular by construction: one value, one owner.
    ``tenant_id`` is the Tenant the resource belongs to — a fact of this
    store derived from the verified identity through the published chain,
    never a caller claim (LAW-16a). ``created_by`` is an opaque reference
    to the verified identity that created the resource; the profile is
    owned outside Booking.

    A Resource carries no price, product, course, lesson, enrollment,
    analytics or notification data (ADR-0014 §3, §19): it is the object a
    Reservation refers to and nothing more. Capacity is exactly one.
    """

    resource_id: str
    tenant_id: str
    owner_component: str
    name: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OwnedAvailabilityWindow:
    """One explicitly allowed booking interval of a Resource.

    ``[start_at, end_at)`` in UTC with ``start_at < end_at``. The window
    belongs to exactly one Resource of the same Tenant. It states when a
    Reservation *may* be placed; it says nothing about whether the interval
    is currently booked — occupancy lives in :class:`OwnedReservation`.
    """

    availability_id: str
    resource_id: str
    tenant_id: str
    owner_component: str
    start_at: datetime
    end_at: datetime
    created_by: str
    created_at: str


@dataclass(frozen=True, slots=True)
class OwnedReservation:
    """One booked half-open interval of a Resource, owned by this component.

    ``booker_identity_id`` is an opaque reference to the verified identity
    that holds the reservation — Identity owns the profile, Booking stores
    the reference only and never interprets it. ``status`` is exactly
    ``ACTIVE`` or ``CANCELLED``; the only move is ``ACTIVE → CANCELLED``,
    and a cancelled record stays as a historical fact (never deleted).
    ``cancelled_at`` is set by that single move and is ``None`` before it.
    """

    reservation_id: str
    tenant_id: str
    resource_id: str
    owner_component: str
    booker_identity_id: str
    start_at: datetime
    end_at: datetime
    status: str
    created_at: str
    cancelled_at: str | None = None


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
    the booking record identifiers in scope where applicable.
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
