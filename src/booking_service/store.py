"""Owned data of the Booking component (logical schema ``booking``).

This module is internal. No other component has direct access to it:
resources, availability windows, reservations and the audit journal are
reachable only through the published contract of this component
(ARCHITECTURE.md §1.1, LAW-04).

The store separates the two ways its data is touched, and the separation
is the enforcement story of the component:

* :meth:`BookingStore.ownership_of_resource` and
  :meth:`BookingStore.ownership_of_reservation` — enforcement metadata:
  whether a record exists here and who its single owner is. The engine
  reads them to form the question it asks IS-003; they serve nothing to a
  caller;
* :meth:`BookingStore.create_resource`, :meth:`BookingStore.serve_resource`,
  :meth:`BookingStore.create_availability`,
  :meth:`BookingStore.availability_of`,
  :meth:`BookingStore.create_reservation`,
  :meth:`BookingStore.serve_reservation` and
  :meth:`BookingStore.cancel_reservation` — the owned-data operations
  themselves. The engine calls them only after the enforcement chain
  produced an ``ALLOW``.

**The double-booking invariant lives here (ADR-0014 §7).** For one
Resource, two ``ACTIVE`` Reservations never overlap. The guarantee is not
a check-then-insert in the engine: the whole reservation decision — the
Resource is ``ACTIVE``, the interval is contained in one Availability
Window, no ``ACTIVE`` Reservation intersects it — and the insert run as
one critical sequence inside the Resource's domain critical section
(:meth:`BookingStore.resource_section`). Two concurrent creations for the
same Resource are therefore serialized: the second one observes the first
one's record and is refused with ``reservation_conflict``. Cancellation
runs in the same section, so a cancellation and a creation can never
interleave between the read and the write either. This is Level-0 domain
serialization inside this component, not a second idempotency mechanism:
IS-005 stays exactly-once per key, while the section makes the state
change exclusive across different keys.

Every persisted ``start_at`` / ``end_at`` is a UTC-normalized aware
:class:`datetime`; registration refuses anything else (ADR-0014 §5).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from booking_service import OWNER_COMPONENT
from booking_service.contracts import (
    RESERVATION_STATES,
    RESOURCE_STATES,
    OwnDenyReason,
)
from booking_service.intervals import Interval, contains, overlaps
from booking_service.models import (
    AccessAuditEvent,
    OwnedAvailabilityWindow,
    OwnedReservation,
    OwnedResource,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


#: The only reservation lifecycle move that exists (ADR-0014 §6).
_RESERVATION_TRANSITIONS: dict[str, tuple[str, ...]] = {"ACTIVE": ("CANCELLED",)}

#: Payload content rule shared by the resource record.
NAME_MAX = 512


class DomainRefusal(Exception):
    """A domain-level refusal of an otherwise allowed operation.

    Raised only inside the owned-data step of the chain: an unknown or
    inactive Resource, an interval outside every Availability Window, a
    conflicting ``ACTIVE`` Reservation, an invalid lifecycle move, or a
    structural violation at registration. ``reason`` is one of the
    published Booking domain reasons where the rule is a published one;
    the engine maps anything else to a fail-closed refusal.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _utc_aware(value: datetime, *, what: str) -> datetime:
    """Refuse a naive or non-UTC instant at the persistence boundary."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DomainRefusal(f"naive_{what}")
    if value.utcoffset() != UTC.utcoffset(value):
        raise DomainRefusal(f"non_utc_{what}")
    return value


def _interval_of(record: OwnedAvailabilityWindow | OwnedReservation) -> Interval:
    return Interval(start_at=record.start_at, end_at=record.end_at)


@dataclass
class BookingStore:
    """In-memory owned storage of the Booking data and the audit journal.

    The store owns every record of the Booking logical schema: resources,
    availability windows, reservations and the access audit journal. The
    same-tenant rule of every child, the UTC-aware shape of every
    interval, the ``start_at < end_at`` invariant and the no-overlap
    invariant of ``ACTIVE`` Reservations are facts of this store, not
    claims of a caller.
    """

    resources: dict[str, OwnedResource] = field(default_factory=dict)
    availability: dict[str, OwnedAvailabilityWindow] = field(default_factory=dict)
    reservations: dict[str, OwnedReservation] = field(default_factory=dict)
    audit: list[AccessAuditEvent] = field(default_factory=list)

    #: One domain critical section per Resource: every mutation of the
    #: booking state of one Resource — declaring a window, creating a
    #: Reservation, cancelling one — is serialized against every other
    #: mutation of the same Resource. Two concurrent reservation commands
    #: with different Idempotency-Keys can never interleave between the
    #: conflict check and the insert: this is what makes the no-overlap
    #: invariant hold under concurrency (ADR-0014 §7).
    _resource_locks: dict[str, threading.RLock] = field(
        default_factory=dict, repr=False, compare=False
    )
    _resource_locks_guard: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # ------------------------------------------------------- critical section
    def _resource_lock(self, resource_id: str) -> threading.RLock:
        with self._resource_locks_guard:
            lock = self._resource_locks.get(resource_id)
            if lock is None:
                lock = threading.RLock()
                self._resource_locks[resource_id] = lock
            return lock

    @contextmanager
    def resource_section(self, resource_id: str) -> Iterator[None]:
        """The exclusive critical section of one Resource's booking state."""
        with self._resource_lock(resource_id):
            yield

    # ------------------------------------------------------------ registration
    @staticmethod
    def _text(value: object, *, what: str, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise DomainRefusal(f"invalid_{what}")
        if len(value) > max_length:
            raise DomainRefusal(f"invalid_{what}")
        return value

    def register_resource(self, resource: OwnedResource) -> OwnedResource:
        """Register one Resource; the structural invariants are enforced here."""
        if resource.resource_id in self.resources:
            raise DomainRefusal("duplicate_resource")
        if resource.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("foreign_owner")
        if not resource.tenant_id:
            raise DomainRefusal("missing_tenant")
        if resource.status not in RESOURCE_STATES:
            raise DomainRefusal("unknown_resource_status")
        self._text(resource.name, what="name", max_length=NAME_MAX)
        self.resources[resource.resource_id] = resource
        return resource

    def register_availability(
        self, window: OwnedAvailabilityWindow
    ) -> OwnedAvailabilityWindow:
        """Register one Availability Window under an existing same-tenant Resource.

        Refuses an orphan, a cross-tenant child, a naive or non-UTC bound
        and an empty or inverted interval.
        """
        if window.availability_id in self.availability:
            raise DomainRefusal("duplicate_availability")
        if window.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("foreign_owner")
        parent = self.resources.get(window.resource_id)
        if parent is None or parent.tenant_id != window.tenant_id:
            raise DomainRefusal(OwnDenyReason.RESOURCE_NOT_FOUND)
        start = _utc_aware(window.start_at, what="start_at")
        end = _utc_aware(window.end_at, what="end_at")
        if not start < end:
            raise DomainRefusal("invalid_interval")
        self.availability[window.availability_id] = window
        return window

    def register_reservation(self, reservation: OwnedReservation) -> OwnedReservation:
        """Register one Reservation under the no-overlap invariant.

        Runs inside the Resource's critical section. Refuses an orphan, a
        cross-tenant child, a naive or non-UTC bound, an empty or inverted
        interval, an unknown status, and — for an ``ACTIVE`` record — any
        intersection with another ``ACTIVE`` Reservation of the Resource.
        """
        if reservation.reservation_id in self.reservations:
            raise DomainRefusal("duplicate_reservation")
        if reservation.owner_component != OWNER_COMPONENT:
            raise DomainRefusal("foreign_owner")
        if reservation.status not in RESERVATION_STATES:
            raise DomainRefusal("unknown_reservation_status")
        if not reservation.booker_identity_id:
            raise DomainRefusal("missing_booker")
        with self.resource_section(reservation.resource_id):
            parent = self.resources.get(reservation.resource_id)
            if parent is None or parent.tenant_id != reservation.tenant_id:
                raise DomainRefusal(OwnDenyReason.RESOURCE_NOT_FOUND)
            start = _utc_aware(reservation.start_at, what="start_at")
            end = _utc_aware(reservation.end_at, what="end_at")
            if not start < end:
                raise DomainRefusal("invalid_interval")
            if reservation.status == "ACTIVE" and self._conflicting(
                reservation.resource_id, _interval_of(reservation)
            ):
                raise DomainRefusal(OwnDenyReason.RESERVATION_CONFLICT)
            self.reservations[reservation.reservation_id] = reservation
            return reservation

    # ------------------------------------------------------------- enforcement
    def ownership_of_resource(self, resource_id: str) -> OwnedResource | None:
        """Enforcement metadata of one Resource, or ``None`` if unknown here."""
        return self.resources.get(resource_id)

    def ownership_of_reservation(self, reservation_id: str) -> OwnedReservation | None:
        """Enforcement metadata of one Reservation, or ``None`` if unknown here."""
        return self.reservations.get(reservation_id)

    # ------------------------------------------------------ owned-data: resource
    def create_resource(
        self,
        *,
        tenant_id: str,
        name: str,
        status: str,
        created_by: str,
        now: str,
    ) -> OwnedResource:
        """Create one Resource in ``tenant_id`` — the owned-data operation."""
        return self.register_resource(
            OwnedResource(
                resource_id=_new_id("res"),
                tenant_id=tenant_id,
                owner_component=OWNER_COMPONENT,
                name=name,
                status=status,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
        )

    def serve_resource(self, resource_id: str) -> OwnedResource:
        """Serve one Resource — the owned-data operation of a read."""
        return self.resources[resource_id]

    # -------------------------------------------------- owned-data: availability
    def create_availability(
        self,
        *,
        resource_id: str,
        tenant_id: str,
        interval: Interval,
        created_by: str,
        now: str,
    ) -> OwnedAvailabilityWindow:
        """Declare one Availability Window of a Resource.

        Runs inside the Resource's critical section so that a window and a
        reservation of the same Resource never interleave. Raises
        ``DomainRefusal(resource_not_found)`` for a missing or foreign
        Resource.
        """
        with self.resource_section(resource_id):
            return self.register_availability(
                OwnedAvailabilityWindow(
                    availability_id=_new_id("avl"),
                    resource_id=resource_id,
                    tenant_id=tenant_id,
                    owner_component=OWNER_COMPONENT,
                    start_at=interval.start_at,
                    end_at=interval.end_at,
                    created_by=created_by,
                    created_at=now,
                )
            )

    def availability_of(self, resource_id: str) -> list[OwnedAvailabilityWindow]:
        """The Availability Windows of one Resource in deterministic order."""
        windows = [
            window
            for window in self.availability.values()
            if window.resource_id == resource_id
        ]
        windows.sort(key=lambda w: (w.start_at, w.end_at, w.availability_id))
        return windows

    # --------------------------------------------------- owned-data: reservation
    def _conflicting(self, resource_id: str, interval: Interval) -> bool:
        """Whether an ``ACTIVE`` Reservation of the Resource intersects ``interval``.

        Occupancy is exactly the set of ``ACTIVE`` Reservations: a
        ``CANCELLED`` record frees its interval. Half-open semantics —
        touching bounds do not intersect.
        """
        return any(
            existing.status == "ACTIVE" and overlaps(_interval_of(existing), interval)
            for existing in self.reservations.values()
            if existing.resource_id == resource_id
        )

    def _covering_window(
        self, resource_id: str, interval: Interval
    ) -> OwnedAvailabilityWindow | None:
        """The one Availability Window that completely contains ``interval``."""
        for window in self.availability_of(resource_id):
            if contains(_interval_of(window), interval):
                return window
        return None

    def create_reservation(
        self,
        *,
        resource_id: str,
        tenant_id: str,
        booker_identity_id: str,
        interval: Interval,
        now: str,
    ) -> OwnedReservation:
        """Create one ``ACTIVE`` Reservation — the atomic booking decision.

        The five creation conditions of ADR-0014 §8 are evaluated and the
        insert performed as one critical sequence inside the Resource's
        critical section:

        1. the Resource exists here and in ``tenant_id`` — else
           ``resource_not_found``;
        2. the Resource is ``ACTIVE`` — else ``resource_inactive``;
        3. ``start_at < end_at`` — guaranteed by the :class:`Interval`
           value and re-checked at registration;
        4. the interval is completely contained in one Availability Window
           — else ``availability_not_found`` when the Resource declares no
           window at all, ``outside_availability`` otherwise;
        5. no ``ACTIVE`` Reservation of the Resource intersects the
           interval — else ``reservation_conflict``.

        Because the section is exclusive, two concurrent creations for one
        Resource cannot both observe the same free interval: the second
        one runs after the first one's insert and is refused.
        """
        with self.resource_section(resource_id):
            resource = self.resources.get(resource_id)
            if resource is None or resource.tenant_id != tenant_id:
                raise DomainRefusal(OwnDenyReason.RESOURCE_NOT_FOUND)
            if resource.status != "ACTIVE":
                raise DomainRefusal(OwnDenyReason.RESOURCE_INACTIVE)
            windows = self.availability_of(resource_id)
            if not windows:
                raise DomainRefusal(OwnDenyReason.AVAILABILITY_NOT_FOUND)
            if self._covering_window(resource_id, interval) is None:
                raise DomainRefusal(OwnDenyReason.OUTSIDE_AVAILABILITY)
            if self._conflicting(resource_id, interval):
                raise DomainRefusal(OwnDenyReason.RESERVATION_CONFLICT)
            return self.register_reservation(
                OwnedReservation(
                    reservation_id=_new_id("rsv"),
                    tenant_id=tenant_id,
                    resource_id=resource_id,
                    owner_component=OWNER_COMPONENT,
                    booker_identity_id=booker_identity_id,
                    start_at=interval.start_at,
                    end_at=interval.end_at,
                    status="ACTIVE",
                    created_at=now,
                )
            )

    def serve_reservation(self, reservation_id: str) -> OwnedReservation:
        """Serve one Reservation — the owned-data operation of a read."""
        return self.reservations[reservation_id]

    def cancel_reservation(self, reservation_id: str, *, now: str) -> OwnedReservation:
        """Move one Reservation ``ACTIVE → CANCELLED`` — the only lifecycle move.

        Runs inside the Resource's critical section. Any other move —
        including cancelling an already cancelled Reservation — is
        ``invalid_state_transition``. The record is preserved: only
        ``status`` and ``cancelled_at`` change; the interval, the booker
        and the timestamps of creation stay as the historical fact. Raises
        ``KeyError`` when the Reservation is gone.
        """
        current = self.reservations[reservation_id]
        with self.resource_section(current.resource_id):
            current = self.reservations[reservation_id]
            allowed = _RESERVATION_TRANSITIONS.get(current.status, ())
            if "CANCELLED" not in allowed:
                raise DomainRefusal(OwnDenyReason.INVALID_STATE_TRANSITION)
            updated = replace(current, status="CANCELLED", cancelled_at=now)
            self.reservations[reservation_id] = updated
            return updated

    def active_reservations_of(self, resource_id: str) -> list[OwnedReservation]:
        """The occupancy of one Resource: its ``ACTIVE`` Reservations, ordered."""
        active = [
            r
            for r in self.reservations.values()
            if r.resource_id == resource_id and r.status == "ACTIVE"
        ]
        active.sort(key=lambda r: (r.start_at, r.end_at, r.reservation_id))
        return active

    # ------------------------------------------------------------------- demo
    def seed_demo(self) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        Tenant identifiers are the demo ones of IS-002: this component does
        not define Tenants, it only states which Tenant each record belongs
        to. Identity references are the demo ones of IS-001 (opaque ids —
        no profile data is stored here). Resources: an ``ACTIVE`` and an
        ``INACTIVE`` one in Tenant A, an ``ACTIVE`` one in Tenant B, each
        with one demo Availability Window on 2026-10-01 (UTC). No demo
        Reservation exists: occupancy is a business fact the demo does not
        assert.
        """
        self.resources = {}
        self.availability = {}
        self.reservations = {}
        self.audit = []

        when = "2026-09-13T00:00:00+00:00"
        window_start = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
        window_end = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
        for resource_id, tenant_id, status, created_by in (
            ("res_a1", "ten_a", "ACTIVE", "idn_human_a"),
            ("res_a2", "ten_a", "INACTIVE", "idn_human_a"),
            ("res_b1", "ten_b", "ACTIVE", "idn_human_b"),
        ):
            self.register_resource(
                OwnedResource(
                    resource_id=resource_id,
                    tenant_id=tenant_id,
                    owner_component=OWNER_COMPONENT,
                    name=f"Demo resource {resource_id}",
                    status=status,
                    created_by=created_by,
                    created_at=when,
                    updated_at=when,
                )
            )
            self.register_availability(
                OwnedAvailabilityWindow(
                    availability_id=f"avl_{resource_id}",
                    resource_id=resource_id,
                    tenant_id=tenant_id,
                    owner_component=OWNER_COMPONENT,
                    start_at=window_start,
                    end_at=window_end,
                    created_by=created_by,
                    created_at=when,
                )
            )
