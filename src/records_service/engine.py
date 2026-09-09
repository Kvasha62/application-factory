"""Records engine: the enforcement chain of the data owner.

This component is the final enforcement boundary for the resources it owns
(ARCHITECTURE.md §5.3, §6.2): IS-003 decides, and this component applies the
decision at its own boundary — including refusing when the decision cannot be
obtained at all. The chain is fixed, published, and every step can only deny:

```text
Request (subject credential, resource_id, operation, [claimed tenant])
      ↓
ownership_boundary       the resource exists here and its single owner is
                         this component                  else DENY
      ↓
authorization_decision   IS-003 decides through the port; the default
                         outcome is DENY; a dependency that does not answer
                         or answers outside its contract  fails closed
      ↓
owned_data_operation     only now is the resource read or changed
```

An ``ALLOW`` is not data access: the owned-data operation runs only here, at
the end, and a request denied earlier never reaches it (invariant 3). Both
the served accesses and every refusal are audited with ``request_id`` and
``correlation_id`` (invariant 7).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from records_service import COMPONENT_ID, COMPONENT_VERSION
from records_service.config import RecordsConfig
from records_service.consumed import (
    ALLOW,
    DENY,
    DENY_REASONS,
    PERMITTED,
    DependencyRefusal,
)
from records_service.contracts import (
    OPERATION_READ,
    OPERATION_WRITE,
    OwnDenyReason,
    ResourceView,
)
from records_service.errors import AccessRefused
from records_service.models import AccessAuditEvent, ObservabilityContext, OwnedResource
from records_service.ports import AuthorizationPort
from records_service.store import TRANSITIONS, DomainRefusal, RecordsStore

#: The published order of enforcement. Also used by the contract tests: a
#: change of order is a change of behaviour and must be visible.
ENFORCEMENT_CHAIN: tuple[str, ...] = (
    "ownership_boundary",
    "authorization_decision",
    "owned_data_operation",
)

#: The audit action of a request the published schema rejected before any
#: handler ran: an access attempt that could not even be read.
ACTION_UNREADABLE = "records.access"

#: Identity-family denial reasons of IS-003 map to 401; every other denial is
#: an authorization refusal (403). An unmapped reason never falls through to
#: a permissive status.
_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _status_for(reason: str) -> int:
    """HTTP status of a refusal: 401 authentication, 404 unknown, 409 domain,
    503 fail-closed dependency, 403 everything else."""
    if reason in _AUTHENTICATION_DENIALS:
        return 401
    if reason == OwnDenyReason.RESOURCE_UNKNOWN:
        return 404
    if reason == OwnDenyReason.INVALID_TRANSITION:
        return 409
    if reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE:
        return 503
    return 403


@dataclass
class RecordsEngine:
    """The enforcement boundary of one data owner in one Platform Instance.

    It owns proof resources and an access audit journal. It owns no identity,
    no permission, no tenant registry and no tenant state: the decision is
    read, per access, from the published contract of IS-003 through the port.
    """

    store: RecordsStore
    config: RecordsConfig
    authorization: AuthorizationPort
    clock: Callable[[], str] = field(default=_utc_now)

    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (never a caller claim)."""
        return self.config.platform_id

    # ------------------------------------------------------------- operations
    def read_resource(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ResourceView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned resource — and only after the full chain allowed it."""
        return self._access(
            subject_credential,
            resource_id,
            operation=OPERATION_READ,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            perform=lambda resource: self.store.serve_resource(resource.resource_id),
        )

    def transition_resource(
        self,
        subject_credential: str | None,
        resource_id: str,
        transition: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ResourceView, ObservabilityContext, AccessAuditEvent]:
        """Change the state of one owned resource — only after the chain allowed it.

        The transition must be one of the published state-machine operations
        and must apply to the resource's current state; both are checked by
        the owner. A non-applicable transition is refused after the ALLOW as a
        domain refusal: the authorization boundary and the state machine are
        different facts, and both are enforced here.
        """
        if not isinstance(transition, str) or not transition.strip():
            transition = ""

        def perform(resource: OwnedResource) -> OwnedResource:
            if transition not in TRANSITIONS:
                raise DomainRefusal(OwnDenyReason.INVALID_TRANSITION)
            return self.store.apply_transition(resource.resource_id, transition)

        return self._access(
            subject_credential,
            resource_id,
            operation=OPERATION_WRITE,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            perform=perform,
        )

    def refuse_unreadable_request(
        self,
        *,
        path: str,
        schema_problems: list[str],
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AccessRefused:
        """Refuse — and audit — a request the published schema rejected.

        The schema rejects a request before any handler of this component
        runs, so the refusal is produced here explicitly instead of silently:
        an unreadable access attempt is security-relevant, and every refusal
        appears in the audit journal with the caller's ``request_id`` and
        ``correlation_id`` (invariant 7). Values of the payload are never
        recorded — only the location and kind of the problem.
        """
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
    def _access(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        operation: str,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
        perform: Callable[[OwnedResource], OwnedResource],
    ) -> tuple[ResourceView, ObservabilityContext, AccessAuditEvent]:
        """Run the fixed enforcement chain for one access attempt.

        The owned-data operation ``perform`` is called exactly once, and only
        at the end of a chain that produced an ALLOW: every earlier outcome
        returns a refusal without touching the resource data (invariant 3).
        """
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        resource = self.store.ownership_of(resource_id)
        if resource is None:
            # Unknown resources are denied here: this component asks IS-003 no
            # questions about resources it does not own, and invents nothing.
            raise self._refuse(
                OwnDenyReason.RESOURCE_UNKNOWN,
                action=operation,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if resource.owner_component != COMPONENT_ID:
            # A resource whose single owner is another component is never
            # served through this boundary (invariant 1): ALLOW or not.
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=operation,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": resource.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        try:
            answer = self.authorization.decide(
                subject_credential,
                operation=operation,
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:
            # The port is declared total, but the boundary does not trust the
            # declaration: a port that misbehaves instead of answering is a
            # port that did not answer — fail closed (invariant 4). Only the
            # machine-readable code of the refusal, if one is stated, is kept
            # as audit context — never the exception itself.
            code = getattr(exc, "reason", None)
            code = getattr(code, "value", code)
            answer = DependencyRefusal(code if isinstance(code, str) and code else None)
        if isinstance(answer, DependencyRefusal):
            # The dependency did not answer: fail closed, owned data untouched.
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=operation,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_code": answer.reason_code},
            )
        if answer.decision == DENY and answer.reason in DENY_REASONS:
            # The authority answered no with a published reason: the reason is
            # passed through unchanged — the fact belongs to IS-003.
            raise self._refuse(
                answer.reason,
                action=operation,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if not (answer.decision == ALLOW and answer.reason == PERMITTED):
            # ALLOW with a wrong reason, a DENY with an unknown one, an unknown
            # decision value: a non-authoritative answer is not an access
            # grant, whatever direction it points in — fail closed.
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=operation,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "nonauthoritative_answer": {
                        "decision": answer.decision,
                        "reason": answer.reason,
                    }
                },
            )

        obs = self.observability(
            tenant_id=answer.tenant_id,
            subject_id=answer.subject_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served = perform(resource)
        except KeyError:
            # The resource vanished between the decision and the serving step:
            # reported as a failure, never as a served resource.
            raise self._refuse(
                OwnDenyReason.RESOURCE_UNKNOWN,
                action=operation,
                obs=obs,
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                exc.reason,
                action=operation,
                obs=obs,
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "domain_refusal", "resource_state": resource.state},
            )

        event = self.audit(
            action=operation,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=answer.subject_id,
            tenant_id=answer.tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (
            ResourceView(
                resource_id=served.resource_id,
                resource_type=served.resource_type,
                owner_component=served.owner_component,
                tenant_id=served.tenant_id,
                state=served.state,
            ),
            obs,
            event,
        )

    # -------------------------------------------------------------- outcomes
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
        return AccessRefused(reason, status_code=_status_for(reason), details=details)

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


__all__ = ["ACTION_UNREADABLE", "ENFORCEMENT_CHAIN", "RecordsEngine"]
