"""Tenant Authority engine: registry, lifecycle state machine, audit and observability.

Authoritative source of truth for Tenant state (invariant T-004). Consumers
reach this component only through the operations published in the Component
Contract; the store is internal (T-009).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.config import TenantAuthorityConfig
from tenant_authority.contracts import (
    LifecycleDecision,
    TenantSnapshot,
    TenantState,
)
from tenant_authority.errors import (
    AuthenticationDenied,
    AuthorizationDenied,
    DenyReason,
    InvalidTransition,
    OwnershipDenied,
    TenantAuthorityError,
    TenantConflict,
    TenantNotFound,
)
from tenant_authority.lifecycle import (
    assert_transition_allowed,
    operation_decision,
)
from tenant_authority.models import (
    AuditEvent,
    Decision,
    IdempotencyRecord,
    LifecycleTransition,
    ObservabilityContext,
    ServiceAccess,
    TenantRecord,
)
from tenant_authority.store import (
    PERM_CREATE,
    PERM_LIFECYCLE_LOOKUP,
    PERM_LIST,
    PERM_READ,
    PERM_TRANSITION,
    TenantAuthorityStore,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fingerprint(operation: str, **fields: Any) -> str:
    payload = "\n".join(
        [operation, *(f"{key}={value}" for key, value in sorted(fields.items()))]
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _snapshot(record: TenantRecord) -> TenantSnapshot:
    return TenantSnapshot(
        tenant_id=record.tenant_id,
        platform_id=record.platform_id,
        state=record.state,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@dataclass
class TenantAuthorityEngine:
    """Tenant Registry + lifecycle authority for exactly one Platform Instance."""

    store: TenantAuthorityStore
    config: TenantAuthorityConfig
    clock: Callable[[], str] = field(default=_utc_now)

    # ------------------------------------------------------------------ surface
    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (T-003 input)."""
        return self.config.platform_id

    # -- published contract reads: no second mechanism, authoritative lookup ----
    def lookup(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        consumer_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantSnapshot:
        """Authoritative Tenant state for a trusted in-process consumer.

        ``expected_platform_id`` is the consumer's current Platform Instance:
        a Tenant of another instance is never returned (T-003).
        """
        return _snapshot(
            self._authoritative_read(
                tenant_id,
                expected_platform_id,
                consumer_id,
                request_id,
                correlation_id,
            )
        )

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        consumer_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleDecision:
        """Permitted-ness of an ordinary tenant-scoped operation for this Tenant.

        Security-sensitive denials on this path (unknown Tenant, Tenant of another
        Platform Instance) are audited here as well as at the consuming boundary.
        """
        record = self._authoritative_read(
            tenant_id,
            expected_platform_id,
            consumer_id,
            request_id,
            correlation_id,
        )
        return operation_decision(record.tenant_id, record.platform_id, record.state)

    def _authoritative_read(
        self,
        tenant_id: str,
        expected_platform_id: str | None,
        consumer_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
    ) -> TenantRecord:
        try:
            return self._require_tenant(tenant_id, expected_platform_id)
        except TenantAuthorityError as exc:
            obs = self.observability(
                access=None,
                platform_id=expected_platform_id,
                tenant_id=tenant_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
            self.audit(
                action="tenant.lookup",
                decision=Decision.DENY,
                reason=exc.reason.value,
                access=None,
                obs=obs,
                tenant_id=tenant_id,
                details={} if consumer_id is None else {"consumer_id": consumer_id},
            )
            raise

    # -- registry operations ---------------------------------------------------
    def create_tenant(
        self,
        token: str | None,
        *,
        claimed_platform_id: str | None = None,
        tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[TenantSnapshot, ObservabilityContext, AuditEvent]:
        access, platform_id, _, obs = self._gate(
            token,
            permission=PERM_CREATE,
            action="tenant.create",
            claimed_platform_id=claimed_platform_id,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            load_tenant=False,
        )
        with self._audited_denial(
            action="tenant.create", access=access, obs=obs, tenant_id=tenant_id
        ):
            return self._create_tenant(
                access=access,
                platform_id=platform_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                obs=obs,
            )

    def _create_tenant(
        self,
        *,
        access: ServiceAccess,
        platform_id: str,
        tenant_id: str | None,
        idempotency_key: str | None,
        obs: ObservabilityContext,
    ) -> tuple[TenantSnapshot, ObservabilityContext, AuditEvent]:
        fingerprint = _fingerprint("tenant.create", platform_id=platform_id, tenant_id=tenant_id)
        if idempotency_key:
            replay = self._replay(
                idempotency_key,
                access=access,
                platform_id=platform_id,
                operation="tenant.create",
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                action="tenant.create",
                obs=obs,
            )
            if replay is not None:
                record = self.store.tenants[replay[0].result_ref]
                return _snapshot(record), obs, replay[1]

        if tenant_id is not None and tenant_id in self.store.tenants:
            raise TenantConflict(
                DenyReason.TENANT_EXISTS,
                f"tenant {tenant_id} already exists",
                details={"tenant_id": tenant_id},
            )

        new_id = tenant_id or _new_id("ten")
        timestamp = self.clock()
        record = TenantRecord(
            tenant_id=new_id,
            platform_id=platform_id,
            state=TenantState.PROVISIONING,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.store.tenants[new_id] = record
        if idempotency_key:
            self._store_idempotency(
                idempotency_key,
                access=access,
                platform_id=platform_id,
                operation="tenant.create",
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                result_ref=new_id,
            )
        event = self.audit(
            action="tenant.create",
            decision=Decision.ALLOW,
            reason=None,
            access=access,
            obs=obs,
            tenant_id=new_id,
            details={"state": record.state.value, "platform_id": platform_id},
        )
        return _snapshot(record), obs, event

    def get_tenant(
        self,
        token: str | None,
        tenant_id: str,
        *,
        claimed_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[TenantSnapshot, ObservabilityContext, AuditEvent]:
        access, _, record, obs = self._gate(
            token,
            permission=PERM_READ,
            action="tenant.read",
            claimed_platform_id=claimed_platform_id,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        event = self.audit(
            action="tenant.read",
            decision=Decision.ALLOW,
            reason=None,
            access=access,
            obs=obs,
            tenant_id=record.tenant_id,
            details={"state": record.state.value},
        )
        return _snapshot(record), obs, event

    def list_tenants(
        self,
        token: str | None,
        *,
        claimed_platform_id: str | None = None,
        state: TenantState | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[list[TenantSnapshot], ObservabilityContext, AuditEvent]:
        """Cross-tenant listing is authorized over `platform-scoped` data only and audited."""
        access, platform_id, _, obs = self._gate(
            token,
            permission=PERM_LIST,
            action="tenant.list",
            claimed_platform_id=claimed_platform_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        wanted = None if state is None else TenantState(state)
        records = [
            record
            for record in self.store.tenants.values()
            if record.platform_id == platform_id and (wanted is None or record.state is wanted)
        ]
        records.sort(key=lambda record: record.tenant_id)
        event = self.audit(
            action="tenant.list",
            decision=Decision.ALLOW,
            reason=None,
            access=access,
            obs=obs,
            details={"count": len(records), "state_filter": None if wanted is None else wanted.value},
        )
        return [_snapshot(record) for record in records], obs, event

    def transition_tenant(
        self,
        token: str | None,
        tenant_id: str,
        to_state: TenantState | str,
        *,
        claimed_platform_id: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[LifecycleTransition, ObservabilityContext, AuditEvent]:
        access, platform_id, record, obs = self._gate(
            token,
            permission=PERM_TRANSITION,
            action="tenant.transition",
            claimed_platform_id=claimed_platform_id,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        with self._audited_denial(
            action="tenant.transition", access=access, obs=obs, tenant_id=tenant_id
        ):
            return self._transition_tenant(
                access=access,
                platform_id=platform_id,
                record=record,
                to_state=to_state,
                reason=reason,
                idempotency_key=idempotency_key,
                obs=obs,
            )

    def _transition_tenant(
        self,
        *,
        access: ServiceAccess,
        platform_id: str,
        record: TenantRecord,
        to_state: TenantState | str,
        reason: str | None,
        idempotency_key: str | None,
        obs: ObservabilityContext,
    ) -> tuple[LifecycleTransition, ObservabilityContext, AuditEvent]:
        tenant_id = record.tenant_id
        requested = self._requested_state(to_state)
        fingerprint = _fingerprint(
            "tenant.transition",
            platform_id=platform_id,
            tenant_id=tenant_id,
            to_state=requested.value,
            reason=reason,
        )
        if idempotency_key:
            replay = self._replay(
                idempotency_key,
                access=access,
                platform_id=platform_id,
                operation="tenant.transition",
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                action="tenant.transition",
                obs=obs,
            )
            if replay is not None:
                transition = next(
                    item
                    for item in self.store.transitions
                    if item.transition_id == replay[0].result_ref
                )
                return transition, obs, replay[1]

        # T-005 / T-006: the state machine is the only source of permitted moves.
        assert_transition_allowed(record.state, requested)

        timestamp = self.clock()
        self.store.tenants[record.tenant_id] = replace(
            record, state=requested, updated_at=timestamp
        )
        transition = LifecycleTransition(
            transition_id=_new_id("trn"),
            tenant_id=record.tenant_id,
            platform_id=platform_id,
            previous_state=record.state,
            new_state=requested,
            actor_id=access.service_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            timestamp=timestamp,
            reason=reason,
        )
        self.store.transitions.append(transition)
        if idempotency_key:
            self._store_idempotency(
                idempotency_key,
                access=access,
                platform_id=platform_id,
                operation="tenant.transition",
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                result_ref=transition.transition_id,
            )
        # T-010: every accepted transition is auditable.
        event = self.audit(
            action="tenant.transition",
            decision=Decision.ALLOW,
            reason=None,
            access=access,
            obs=obs,
            tenant_id=tenant_id,
            details={
                "transition_id": transition.transition_id,
                "previous_state": transition.previous_state.value,
                "new_state": transition.new_state.value,
            },
        )
        return transition, obs, event

    def lifecycle_status(
        self,
        token: str | None,
        tenant_id: str,
        *,
        claimed_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[LifecycleDecision, ObservabilityContext, AuditEvent]:
        """Authorized variant of the lifecycle read for service-identity consumers."""
        access, _, record, obs = self._gate(
            token,
            permission=PERM_LIFECYCLE_LOOKUP,
            action="tenant.lifecycle",
            claimed_platform_id=claimed_platform_id,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        decision = operation_decision(record.tenant_id, record.platform_id, record.state)
        event = self.audit(
            action="tenant.lifecycle",
            decision=Decision.ALLOW,
            reason=None,
            access=access,
            obs=obs,
            tenant_id=record.tenant_id,
            details={"state": record.state.value, "permitted": decision.permitted,
                     "lifecycle_reason": decision.reason},
        )
        return decision, obs, event

    def transition_history(
        self,
        token: str | None,
        tenant_id: str,
        *,
        claimed_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[list[LifecycleTransition], ObservabilityContext, AuditEvent]:
        access, _, record, obs = self._gate(
            token,
            permission=PERM_READ,
            action="tenant.history",
            claimed_platform_id=claimed_platform_id,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        history = [
            item for item in self.store.transitions if item.tenant_id == record.tenant_id
        ]
        event = self.audit(
            action="tenant.history",
            decision=Decision.ALLOW,
            reason=None,
            access=access,
            obs=obs,
            tenant_id=record.tenant_id,
            details={"count": len(history)},
        )
        return history, obs, event

    # ------------------------------------------------------------------ internals
    @contextmanager
    def _audited_denial(
        self,
        *,
        action: str,
        access: ServiceAccess,
        obs: ObservabilityContext,
        tenant_id: str | None,
    ) -> Iterator[None]:
        """Every rejection after the gate is recorded in the audit journal.

        A denied lifecycle transition is as security-relevant as a denied read:
        the caller must not be able to probe the state machine silently.
        """
        try:
            yield
        except TenantAuthorityError as exc:
            self.audit(
                action=action,
                decision=Decision.DENY,
                reason=exc.reason.value,
                access=access,
                obs=obs,
                tenant_id=tenant_id,
                details=exc.details,
            )
            raise

    def verify_service_identity(self, token: str | None) -> ServiceAccess:
        """Service, background job and agent use their own checked identity (§6.2)."""
        if token is None or token.strip() == "":
            raise AuthenticationDenied(DenyReason.MISSING_SERVICE_IDENTITY)
        if not token.startswith(self.config.service_token_prefix):
            raise AuthenticationDenied(DenyReason.INVALID_SERVICE_IDENTITY)
        service_id = self.store.service_tokens.get(token)
        if service_id is None:
            raise AuthenticationDenied(DenyReason.INVALID_SERVICE_IDENTITY)
        access = self.store.services.get(service_id)
        if access is None:
            raise AuthenticationDenied(DenyReason.UNKNOWN_SERVICE)
        return access

    def _resolve_platform(self, access: ServiceAccess, claimed_platform_id: str | None) -> str:
        current = self.config.platform_id
        if claimed_platform_id is not None and claimed_platform_id != current:
            raise AuthorizationDenied(
                DenyReason.PLATFORM_MISMATCH,
                "claimed platform does not match the current Platform Instance",
                details={"claimed_platform_id": claimed_platform_id},
            )
        if access.platform_id != current:
            raise AuthorizationDenied(
                DenyReason.PLATFORM_MISMATCH,
                "service identity belongs to another Platform Instance",
                details={"actor_platform_id": access.platform_id},
            )
        return current

    @staticmethod
    def _requested_state(to_state: TenantState | str) -> TenantState:
        """A state outside the published vocabulary is a rejection, not a crash.

        It is reported through the same reason code as an invalid transition, so
        every refused attempt stays auditable and the reason set stays closed.
        """
        try:
            return TenantState(to_state)
        except ValueError as exc:
            raise InvalidTransition(
                DenyReason.INVALID_TRANSITION,
                f"unknown tenant lifecycle state {to_state!r}",
                details={"requested_state": str(to_state)},
            ) from exc

    def _require_permission(self, access: ServiceAccess, permission: str) -> None:
        if permission not in access.permissions:
            raise AuthorizationDenied(
                DenyReason.INSUFFICIENT_AUTHORIZATION,
                f"service {access.service_id} lacks {permission}",
                details={"required_permission": permission},
            )

    def _require_tenant(
        self,
        tenant_id: str,
        expected_platform_id: str | None = None,
    ) -> TenantRecord:
        """T-001, T-003: exactly one Tenant per id, and only within its own platform."""
        record = self.store.tenants.get(tenant_id)
        if record is None:
            raise TenantNotFound(
                DenyReason.TENANT_NOT_FOUND,
                f"tenant {tenant_id} does not exist",
                details={"tenant_id": tenant_id},
            )
        if expected_platform_id is not None and record.platform_id != expected_platform_id:
            # Same status as "not found": no existence disclosure across platforms.
            raise OwnershipDenied(
                DenyReason.FOREIGN_TENANT,
                f"tenant {tenant_id} does not exist in this Platform Instance",
                details={"tenant_id": tenant_id},
            )
        return record

    def _gate(
        self,
        token: str | None,
        *,
        permission: str,
        action: str,
        claimed_platform_id: str | None = None,
        tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        load_tenant: bool = True,
    ) -> tuple[ServiceAccess, str, TenantRecord | None, ObservabilityContext]:
        access: ServiceAccess | None = None
        # The observability context is always bound to this instance's platform:
        # a caller-claimed platform id is untrusted input and never lands in the
        # context (it is recorded only as an audited detail of the denial).
        obs = self.observability(
            access=None,
            platform_id=self.config.platform_id,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        try:
            access = self.verify_service_identity(token)
            platform_id = self._resolve_platform(access, claimed_platform_id)
            self._require_permission(access, permission)
            record = (
                self._require_tenant(tenant_id, platform_id) if load_tenant and tenant_id else None
            )
        except TenantAuthorityError as exc:
            obs = self.observability(
                access=access,
                platform_id=obs.platform_id,
                tenant_id=tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
            self.audit(
                action=action,
                decision=Decision.DENY,
                reason=exc.reason.value,
                access=access,
                obs=obs,
                tenant_id=tenant_id,
                details=exc.details,
            )
            raise
        obs = self.observability(
            access=access,
            platform_id=platform_id,
            tenant_id=None if record is None else record.tenant_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        return access, platform_id, record, obs

    def _replay(
        self,
        idempotency_key: str,
        *,
        access: ServiceAccess,
        platform_id: str,
        operation: str,
        tenant_id: str | None,
        fingerprint: str,
        action: str,
        obs: ObservabilityContext,
    ) -> tuple[IdempotencyRecord, AuditEvent] | None:
        """Replay of the same logical request only; any other context is a conflict.

        A replay does not create a second effect: the caller receives the result of
        the original accepted operation (ARCHITECTURE.md §6.5, AMD-05).
        """
        existing = self.store.idempotency.get(idempotency_key)
        if existing is None:
            return None
        same_request = (
            existing.actor_id == access.service_id
            and existing.platform_id == platform_id
            and existing.operation == operation
            and existing.tenant_id == tenant_id
            and existing.fingerprint == fingerprint
        )
        if not same_request:
            raise TenantConflict(
                DenyReason.IDEMPOTENCY_CONFLICT,
                "idempotency key was already used for another request",
                details={"idempotency_key": idempotency_key},
            )
        event = self.audit(
            action=action,
            decision=Decision.ALLOW,
            reason="idempotent_replay",
            access=access,
            obs=obs,
            tenant_id=tenant_id,
            details={"idempotency_key": idempotency_key, "result_ref": existing.result_ref},
        )
        return existing, event

    def _store_idempotency(
        self,
        idempotency_key: str,
        *,
        access: ServiceAccess,
        platform_id: str,
        operation: str,
        tenant_id: str | None,
        fingerprint: str,
        result_ref: str,
    ) -> None:
        self.store.idempotency[idempotency_key] = IdempotencyRecord(
            key=idempotency_key,
            actor_id=access.service_id,
            platform_id=platform_id,
            operation=operation,
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            result_ref=result_ref,
        )

    # ------------------------------------------------------------ observability
    def observability(
        self,
        *,
        access: ServiceAccess | None,
        platform_id: str | None,
        tenant_id: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ObservabilityContext:
        rid = request_id or _new_id("req")
        cid = correlation_id or rid
        return ObservabilityContext(
            timestamp=self.clock(),
            environment=self.config.environment,
            platform_id=platform_id,
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            request_id=rid,
            trace_id=rid,
            correlation_id=cid,
            tenant_id=tenant_id,
            service_id=None if access is None else access.service_id,
        )

    def audit(
        self,
        *,
        action: str,
        decision: Decision,
        reason: str | None,
        access: ServiceAccess | None,
        obs: ObservabilityContext,
        tenant_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=_new_id("aud"),
            action=action,
            decision=decision,
            reason=reason,
            actor_id=None if access is None else access.service_id,
            platform_id=obs.platform_id,
            tenant_id=tenant_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            timestamp=self.clock(),
            details=details or {},
        )
        self.store.audit.append(event)
        return event
