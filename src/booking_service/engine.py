"""Booking engine: the enforcement chain of the data owner.

This component is the final enforcement boundary for the Booking data it
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

The seven operations of ADR-0014 §14 run on this chain. The create-resource
command derives the effective tenant from the verified identity (its
target does not exist yet); every other command names an existing owned
record whose Tenant the store states. The reservation decision itself —
the five conditions of ADR-0014 §8 and the insert — is one atomic step of
the store inside the Resource's critical section: the engine never
performs a check-then-insert of its own.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from booking_service import COMPONENT_ID, COMPONENT_VERSION
from booking_service.config import BookingConfig
from booking_service.consumed import (
    ALLOW,
    DENY,
    DENY_REASONS,
    PERMITTED,
    DependencyRefusal,
    TenantContextRefusal,
)
from booking_service.contracts import (
    OPERATION_AVAILABILITY_CREATE,
    OPERATION_AVAILABILITY_READ,
    OPERATION_RESERVATION_CANCEL,
    OPERATION_RESERVATION_CREATE,
    OPERATION_RESERVATION_READ,
    OPERATION_RESOURCE_CREATE,
    OPERATION_RESOURCE_READ,
    RESOURCE_STATES,
    RESOURCE_TYPE_RESERVATION,
    RESOURCE_TYPE_RESOURCE,
    AvailabilityWindowView,
    OwnDenyReason,
    ReservationView,
    ResourceView,
)
from booking_service.errors import AccessRefused
from booking_service.intervals import Interval, TimeProblem, iso_utc, parse_interval
from booking_service.models import (
    AccessAuditEvent,
    ObservabilityContext,
    OwnedAvailabilityWindow,
    OwnedReservation,
    OwnedResource,
)
from booking_service.ports import (
    AuthorizationPort,
    CommandSafetyPort,
    TenantContextPort,
)
from booking_service.store import BookingStore, DomainRefusal
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
ACTION_UNREADABLE = "booking.access"

#: Identity-family denial reasons of IS-003 map to 401; every other denial
#: is an authorization refusal (403). An unmapped reason never falls through
#: to a permissive status.
_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)

_NOT_FOUND_REASONS = frozenset(
    {OwnDenyReason.RESOURCE_NOT_FOUND, OwnDenyReason.RESERVATION_NOT_FOUND}
)

#: Business refusals that answer 409: the request is well-formed and
#: allowed, but the current booking state of the target does not admit it —
#: an inactive Resource, an interval outside every Availability Window, an
#: intersecting ACTIVE Reservation, a lifecycle move that does not exist —
#: plus the IS-005 binding conflict.
_STATE_CONFLICT_REASONS = frozenset(
    {
        OwnDenyReason.RESOURCE_INACTIVE,
        OwnDenyReason.AVAILABILITY_NOT_FOUND,
        OwnDenyReason.OUTSIDE_AVAILABILITY,
        OwnDenyReason.RESERVATION_CONFLICT,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.IDEMPOTENCY_CONFLICT,
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
        OwnDenyReason.RESOURCE_NOT_FOUND,
        OwnDenyReason.RESOURCE_INACTIVE,
        OwnDenyReason.AVAILABILITY_NOT_FOUND,
        OwnDenyReason.OUTSIDE_AVAILABILITY,
        OwnDenyReason.RESERVATION_CONFLICT,
        OwnDenyReason.RESERVATION_NOT_FOUND,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.BOOKER_MISMATCH,
        OwnDenyReason.OWNER_MISMATCH,
    }
)

#: Payload content rules of the resource command.
NAME_MAX = 512


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _status_for(reason: str) -> int:
    """HTTP status of a refusal: 401 authentication, 404 unknown, 409 booking
    state conflict (including the idempotency conflict), 400 missing
    idempotency key, 422 invalid payload, 503 fail-closed dependency, 403
    everything else."""
    if reason in _AUTHENTICATION_DENIALS:
        return 401
    if reason in _NOT_FOUND_REASONS:
        return 404
    if reason in _STATE_CONFLICT_REASONS:
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
    key is a conflict. Interval bounds enter the fingerprint in their
    UTC-normalized form: the command identity is the instant, not the
    spelling of the offset.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{operation}\n{canonical}\n".encode()).hexdigest()


def _domain_reason(reason: str) -> str:
    """Map an internal store refusal to the published reason vocabulary."""
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


def _resource_status_problem(status: Any) -> str | None:
    """The content rule a resource status violates, or ``None``."""
    if status not in RESOURCE_STATES:
        return "status must be one of " + ", ".join(RESOURCE_STATES)
    return None


def _interval_or_problem(start_at: Any, end_at: Any) -> tuple[Interval | None, str]:
    """Parse the interval, or describe the time rule it violates."""
    try:
        return parse_interval(start_at, end_at), ""
    except TimeProblem as exc:
        return None, exc.problem


def _resource_view_of(resource: OwnedResource) -> ResourceView:
    return ResourceView(
        resource_id=resource.resource_id,
        name=resource.name,
        status=resource.status,
        created_by=resource.created_by,
        created_at=resource.created_at,
        updated_at=resource.updated_at,
    )


def _availability_view_of(window: OwnedAvailabilityWindow) -> AvailabilityWindowView:
    return AvailabilityWindowView(
        availability_id=window.availability_id,
        resource_id=window.resource_id,
        start_at=iso_utc(window.start_at),
        end_at=iso_utc(window.end_at),
        created_at=window.created_at,
    )


def _reservation_view_of(reservation: OwnedReservation) -> ReservationView:
    return ReservationView(
        reservation_id=reservation.reservation_id,
        resource_id=reservation.resource_id,
        booker_identity_id=reservation.booker_identity_id,
        start_at=iso_utc(reservation.start_at),
        end_at=iso_utc(reservation.end_at),
        status=reservation.status,
        created_at=reservation.created_at,
        cancelled_at=reservation.cancelled_at,
    )


@dataclass
class BookingEngine:
    """The enforcement boundary of Booking in one Platform Instance.

    It owns the booking records and an access audit journal. It owns no
    identity, no permission, no tenant registry and no tenant state: the
    decision is read, per access, from the published contract of IS-003
    through the port. It owns no second idempotency mechanism either: every
    state-changing command is delivered to the IS-005 guard through the
    port.
    """

    store: BookingStore
    config: BookingConfig
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

    # --------------------------------------------------------------- resource
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
    ) -> tuple[ResourceView, ObservabilityContext, AccessAuditEvent]:
        """Create one owned Resource — the command ``booking.resources.create``.

        The effective tenant is resolved first because the target does not
        exist yet: the tenant-context port (IS-001) derives it from the
        verified identity, then IS-003 decides for a resource-to-be in that
        tenant, then the IS-005 guard executes the creation effect exactly
        once. The Resource's Tenant is the effective tenant of the chain —
        never a caller claim. ``status`` is ``ACTIVE`` by default;
        ``INACTIVE`` declares a Resource that receives no Reservations.
        """
        action = OPERATION_RESOURCE_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _name_problem(name) or _resource_status_problem(status)
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
        resource_id = _new_id("res")

        # --- step 2: the decision of IS-003 through the port ------------------
        obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=RESOURCE_TYPE_RESOURCE,
            resource_id=resource_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        if tenant_id != effective_tenant:
            # The decision and the identity context must agree about the
            # effective tenant; a disagreement is a non-authoritative answer.
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_disagreement"},
            )

        # --- step 3: the owned-data operation, and only here ------------------
        created = self._command(
            idempotency_key,
            action=action,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            resource=None,
            payload={"name": name, "status": status},
            effect=lambda: self.store.create_resource(
                tenant_id=tenant_id,
                name=name,
                status=status,
                created_by=subject_id or "",
                now=self.clock(),
            ),
        )
        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=created.resource_id,
            resource_tenant_id=created.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_resource_view_of(created), obs, event)

    def read_resource(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ResourceView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned Resource — and only after the chain allowed it."""
        action = OPERATION_RESOURCE_READ
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        resource = self._resource_boundary(
            resource_id, action=action, obs=obs, claimed_tenant_id=claimed_tenant_id
        )
        obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=RESOURCE_TYPE_RESOURCE,
            resource_id=resource.resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        try:
            served = self.store.serve_resource(resource.resource_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
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
            resource_id=resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_resource_view_of(served), obs, event)

    # ----------------------------------------------------------- availability
    def create_availability(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        start_at: Any,
        end_at: Any,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[AvailabilityWindowView, ObservabilityContext, AccessAuditEvent]:
        """Declare one Availability Window — ``booking.availability.create``.

        The interval must be timezone-aware with ``start_at < end_at``; it
        is normalized to UTC before anything else happens. The Resource
        must exist here; IS-003 decides for that Resource in its own
        Tenant; the IS-005 guard executes the declaration exactly once,
        bound to the Resource.
        """
        action = OPERATION_AVAILABILITY_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        interval = self._interval_boundary(
            start_at,
            end_at,
            action=action,
            obs=obs,
            resource_id=resource_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        resource = self._resource_boundary(
            resource_id, action=action, obs=obs, claimed_tenant_id=claimed_tenant_id
        )
        obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=RESOURCE_TYPE_RESOURCE,
            resource_id=resource.resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        created = self._command(
            idempotency_key,
            action=action,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            resource=resource_id,
            payload={
                "resource_id": resource_id,
                "start_at": iso_utc(interval.start_at),
                "end_at": iso_utc(interval.end_at),
            },
            effect=lambda: self.store.create_availability(
                resource_id=resource_id,
                tenant_id=resource.tenant_id,
                interval=interval,
                created_by=subject_id or "",
                now=self.clock(),
            ),
        )
        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            details={"availability_id": created.availability_id},
        )
        return (_availability_view_of(created), obs, event)

    def list_availability(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[
        tuple[AvailabilityWindowView, ...], ObservabilityContext, AccessAuditEvent
    ]:
        """Serve the Availability Windows of one Resource — ``booking.availability.read``."""
        action = OPERATION_AVAILABILITY_READ
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        resource = self._resource_boundary(
            resource_id, action=action, obs=obs, claimed_tenant_id=claimed_tenant_id
        )
        obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=RESOURCE_TYPE_RESOURCE,
            resource_id=resource.resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        windows = self.store.availability_of(resource.resource_id)
        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (tuple(_availability_view_of(w) for w in windows), obs, event)

    # ------------------------------------------------------------ reservation
    def create_reservation(
        self,
        subject_credential: str | None,
        *,
        resource_id: str,
        start_at: Any,
        end_at: Any,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ReservationView, ObservabilityContext, AccessAuditEvent]:
        """Create one Reservation — the command ``booking.reservations.create``.

        The booker is the verified subject: ``booker_identity_id`` is the
        opaque identity reference the decision stated, never a payload
        field. The interval must be timezone-aware with ``start_at <
        end_at`` and is normalized to UTC first. The Resource must exist
        here; IS-003 decides for that Resource in its own Tenant; then the
        IS-005 guard executes the store's atomic booking decision exactly
        once, bound to the Resource. The five conditions of ADR-0014 §8 and
        the insert run inside the Resource's critical section in the store:
        a concurrent conflicting creation is refused with
        ``reservation_conflict`` (409).
        """
        action = OPERATION_RESERVATION_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        interval = self._interval_boundary(
            start_at,
            end_at,
            action=action,
            obs=obs,
            resource_id=resource_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        resource = self._resource_boundary(
            resource_id, action=action, obs=obs, claimed_tenant_id=claimed_tenant_id
        )
        obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=RESOURCE_TYPE_RESOURCE,
            resource_id=resource.resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        if not subject_id:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "decision_without_subject"},
            )
        created = self._command(
            idempotency_key,
            action=action,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            resource=resource_id,
            payload={
                "resource_id": resource_id,
                "start_at": iso_utc(interval.start_at),
                "end_at": iso_utc(interval.end_at),
            },
            effect=lambda: self.store.create_reservation(
                resource_id=resource_id,
                tenant_id=resource.tenant_id,
                booker_identity_id=subject_id,
                interval=interval,
                now=self.clock(),
            ),
        )
        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            details={"reservation_id": created.reservation_id},
        )
        return (_reservation_view_of(created), obs, event)

    def read_reservation(
        self,
        subject_credential: str | None,
        reservation_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ReservationView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned Reservation — ``booking.reservations.read``."""
        action = OPERATION_RESERVATION_READ
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        reservation = self._reservation_boundary(
            reservation_id,
            action=action,
            obs=obs,
            claimed_tenant_id=claimed_tenant_id,
        )
        obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=RESOURCE_TYPE_RESERVATION,
            resource_id=reservation.reservation_id,
            resource_tenant_id=reservation.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        try:
            served = self.store.serve_reservation(reservation.reservation_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.RESERVATION_NOT_FOUND,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=reservation_id,
                resource_tenant_id=reservation.tenant_id,
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
            resource_id=reservation_id,
            resource_tenant_id=reservation.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_reservation_view_of(served), obs, event)

    def cancel_reservation(
        self,
        subject_credential: str | None,
        reservation_id: str,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ReservationView, ObservabilityContext, AccessAuditEvent]:
        """Cancel one owned Reservation — ``booking.reservations.cancel``.

        The Reservation must exist here; IS-003 decides for it in its own
        Tenant; the Reservation must be the subject's own — another
        booker's Reservation is ``booker_mismatch`` even inside the same
        Tenant (``booker_identity_id`` is an ownership reference, not an
        authorization bypass: the IS-003 grant is still required). Only an
        ``ACTIVE`` Reservation may be cancelled — any other move is
        ``invalid_state_transition``. The move preserves the record.
        """
        action = OPERATION_RESERVATION_CANCEL
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        reservation = self._reservation_boundary(
            reservation_id,
            action=action,
            obs=obs,
            claimed_tenant_id=claimed_tenant_id,
        )
        obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=RESOURCE_TYPE_RESERVATION,
            resource_id=reservation.reservation_id,
            resource_tenant_id=reservation.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        if not subject_id or reservation.booker_identity_id != subject_id:
            raise self._refuse(
                OwnDenyReason.BOOKER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=reservation_id,
                resource_tenant_id=reservation.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        cancelled = self._command(
            idempotency_key,
            action=action,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=reservation_id,
            resource_tenant_id=reservation.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            resource=reservation_id,
            payload={"reservation_id": reservation_id},
            effect=lambda: self.store.cancel_reservation(
                reservation_id, now=self.clock()
            ),
        )
        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=reservation_id,
            resource_tenant_id=reservation.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_reservation_view_of(cancelled), obs, event)

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
    def _interval_boundary(
        self,
        start_at: Any,
        end_at: Any,
        *,
        action: str,
        obs: ObservabilityContext,
        resource_id: str | None,
        claimed_tenant_id: str | None,
    ) -> Interval:
        """Step 0 of the interval commands: the time rules of ADR-0014 §5.

        A naive, unparsable, empty or inverted interval is refused as
        ``validation_error`` before any access decision — nothing naive
        ever reaches the store.
        """
        interval, problem = _interval_or_problem(start_at, end_at)
        if interval is None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error", "rule": problem},
            )
        return interval

    def _resource_boundary(
        self,
        resource_id: str,
        *,
        action: str,
        obs: ObservabilityContext,
        claimed_tenant_id: str | None,
    ) -> OwnedResource:
        """Step 1: the Resource exists here and its single owner is this component."""
        resource = self.store.ownership_of_resource(resource_id)
        if resource is None:
            raise self._refuse(
                OwnDenyReason.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if resource.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": resource.owner_component},
            )
        return resource

    def _reservation_boundary(
        self,
        reservation_id: str,
        *,
        action: str,
        obs: ObservabilityContext,
        claimed_tenant_id: str | None,
    ) -> OwnedReservation:
        """Step 1: the Reservation exists here and its single owner is this component."""
        reservation = self.store.ownership_of_reservation(reservation_id)
        if reservation is None:
            raise self._refuse(
                OwnDenyReason.RESERVATION_NOT_FOUND,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=reservation_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if reservation.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=reservation_id,
                resource_tenant_id=reservation.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": reservation.owner_component},
            )
        return reservation

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

        Consumed by create-resource only: its target does not exist yet, so
        the resource Tenant of the authorization question can only be the
        effective tenant of the verified identity. A port that is absent,
        does not answer or answers outside its contract fails closed. The
        caller-supplied tenant identifier is forwarded as the cross-check
        only — it can never select the effective tenant.
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
                reason = code
            else:
                reason = OwnDenyReason.AUTHORIZATION_UNAVAILABLE
            raise self._refuse(
                reason,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
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
        decided_obs = self.observability(
            tenant_id=answer.tenant_id,
            subject_id=answer.subject_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        if answer.decision == DENY and answer.reason in DENY_REASONS:
            raise self._refuse(
                answer.reason,
                action=action,
                obs=decided_obs,
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
                obs=decided_obs,
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
        return (decided_obs, answer.subject_id, answer.tenant_id)

    # -------------------------------------------------------------- outcomes
    def _command(
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
        resource: str | None,
        payload: dict[str, Any],
        effect: Callable[[], Any],
    ) -> Any:
        """Step 3 of a state-changing command: the IS-005 guard, then the effect.

        The ``Idempotency-Key`` is mandatory. The command identity is
        (operation, payload fingerprint) bound to identity, tenant and the
        target ``resource``. ``IdempotencyConflict`` maps to
        ``idempotency_conflict``; a ``DomainRefusal`` of the store maps to
        its published reason; a vanished record maps to its not-found
        reason; anything else means the safety dependency itself failed and
        the command is refused fail-closed without a business effect.
        """
        if not key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        fingerprint = _payload_fingerprint(action, payload)

        def refuse(reason: str, details: dict[str, Any] | None = None) -> AccessRefused:
            return self._refuse(
                reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details=details,
            )

        try:
            return self.idempotency.execute(
                key,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=resource,
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=effect,
            )
        except IdempotencyConflict:
            raise refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT, {"idempotency_key": key}
            ) from None
        except DomainRefusal as exc:
            raise refuse(
                _domain_reason(exc.reason), {"stated_rule": exc.reason}
            ) from None
        except KeyError:
            not_found = (
                OwnDenyReason.RESERVATION_NOT_FOUND
                if action == OPERATION_RESERVATION_CANCEL
                else OwnDenyReason.RESOURCE_NOT_FOUND
            )
            raise refuse(not_found, {"refused": "vanished_after_decision"}) from None
        except AccessRefused:
            raise
        except Exception as exc:
            raise refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                {
                    "failing_dependency": "idempotency",
                    "stated_error": type(exc).__name__,
                },
            ) from None

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


__all__ = ["ACTION_UNREADABLE", "ENFORCEMENT_CHAIN", "BookingEngine"]
