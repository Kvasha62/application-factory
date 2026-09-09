"""Authorization engine: the decision chain, the grant lookup and the audit journal.

This component decides; it never enforces. The answer it produces is a value
(``ALLOW``/``DENY`` plus a stable reason) handed back to the component that owns
the resource, and that component performs the enforcement at its own boundary
(ARCHITECTURE.md §6.2, invariant 8).

Two levels of refusal are kept apart on purpose:

* the *caller* — the component asking — is authenticated and permission-checked
  like any consumer of a platform service; failing that is an error, not a
  decision, because no decision was ever made;
* the *subject* of the question is never refused with an error: the answer is a
  decision, ``DENY`` with a reason.

Both are audited with ``request_id`` and ``correlation_id`` (invariant 9).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from typing import Any

from authorization_service import COMPONENT_ID, COMPONENT_VERSION
from authorization_service.config import AuthorizationConfig
from authorization_service.consumed import (
    DependencyRefusal,
    SubjectContext,
    TenantVerdict,
)
from authorization_service.contracts import (
    AuthorizationDecision,
    Decision,
    Reason,
    ResourceRef,
)
from authorization_service.errors import (
    AuthorizationServiceError,
    CallerDenyReason,
    CallerNotAuthenticated,
    CallerNotAuthorized,
    MalformedDecisionRequest,
)
from authorization_service.models import AuditEvent, ObservabilityContext, ServiceAccess
from authorization_service.policy import (
    identity_denial,
    lifecycle_denial,
    tenant_authority_denial,
)
from authorization_service.ports import IdentityContextPort, TenantAuthorityPort
from authorization_service.store import PERM_DECIDE, AuthorizationStore

DECISION_ACTION = "authorization.decide"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass
class AuthorizationEngine:
    """Permission authority of exactly one Platform Instance.

    It owns permission grants and an audit journal. It owns no identity, no
    tenant registry and no tenant state: those are read, per decision, from the
    published contracts of IS-001 and IS-002 through the ports.
    """

    store: AuthorizationStore
    config: AuthorizationConfig
    identity: IdentityContextPort
    tenant_authority: TenantAuthorityPort
    clock: Callable[[], str] = field(default=_utc_now)

    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (never a caller claim)."""
        return self.config.platform_id

    # ------------------------------------------------------------- operations
    def decide(
        self,
        token: str | None,
        *,
        operation: str,
        resource: ResourceRef,
        subject_credential: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[AuthorizationDecision, ObservabilityContext, AuditEvent]:
        """Decide one access question and audit the outcome.

        ``token`` is the credential of the *asking component*; it authorizes the
        question, never the subject. ``subject_credential`` is the credential
        presented by the subject: IS-003 accepts no claim about who the subject
        is, it has the credential verified by IS-001 (invariants 1, 3, 5).
        """
        access, obs = self._gate(
            token,
            permission=PERM_DECIDE,
            action=DECISION_ACTION,
            request_id=request_id,
            correlation_id=correlation_id,
            operation=operation,
            resource=resource,
        )
        if not isinstance(operation, str) or not operation.strip():
            raise self._refuse(
                MalformedDecisionRequest(
                    CallerDenyReason.MALFORMED_REQUEST,
                    "an operation is required to decide anything",
                ),
                action=DECISION_ACTION,
                access=access,
                obs=obs,
                operation=operation,
                resource=resource,
            )

        # --- step 1 and 2: verified subject and effective tenant (IS-001) ----
        context = self._subject_context(
            subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        if isinstance(context, Reason):
            return self._decided(
                Decision.DENY,
                context,
                access=access,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                operation=operation,
                resource=resource,
            )

        obs = self.observability(
            access=access,
            tenant_id=context.tenant_id,
            subject_id=context.identity_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )

        # --- step 3: the resource belongs to the subject's Tenant ------------
        resource_reason = self._resource_check(context, resource)
        if resource_reason is not None:
            return self._decided(
                Decision.DENY,
                resource_reason,
                access=access,
                obs=obs,
                subject_id=context.identity_id,
                tenant_id=context.tenant_id,
                operation=operation,
                resource=resource,
            )

        # --- step 4: the Tenant may be served right now (IS-002) -------------
        lifecycle_reason = self._lifecycle_check(context, obs)
        if lifecycle_reason is not None:
            return self._decided(
                Decision.DENY,
                lifecycle_reason,
                access=access,
                obs=obs,
                subject_id=context.identity_id,
                tenant_id=context.tenant_id,
                operation=operation,
                resource=resource,
            )

        # --- step 5: an explicit grant, or nothing ---------------------------
        if not self.store.is_granted(context.tenant_id, context.identity_id, operation):
            return self._decided(
                Decision.DENY,
                Reason.PERMISSION_NOT_GRANTED,
                access=access,
                obs=obs,
                subject_id=context.identity_id,
                tenant_id=context.tenant_id,
                operation=operation,
                resource=resource,
            )

        return self._decided(
            Decision.ALLOW,
            Reason.PERMITTED,
            access=access,
            obs=obs,
            subject_id=context.identity_id,
            tenant_id=context.tenant_id,
            operation=operation,
            resource=resource,
        )

    # ----------------------------------------------------------------- steps
    def _subject_context(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None,
        request_id: str,
        correlation_id: str,
    ) -> SubjectContext | Reason:
        """Ask the identity port who the subject is and which Tenant is effective.

        No fallback exists: if the authority does not answer, the decision is a
        denial. This is also why no second tenant-context mechanism can appear
        here — the tenant is not derivable in this component at all.

        Only values of this component cross the port (see
        :mod:`authorization_service.consumed`), so nothing of the provider's
        implementation is known, caught or stored here. A port that misbehaves
        instead of answering is a port that did not answer: it denies.
        """
        try:
            answer = self.identity.resolve_context(
                subject_credential,
                claimed_tenant_id=claimed_tenant_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        except Exception:
            return Reason.AUTHORITY_UNAVAILABLE
        if isinstance(answer, SubjectContext):
            return answer
        if isinstance(answer, DependencyRefusal):
            return identity_denial(answer.reason_code)
        return Reason.AUTHORITY_UNAVAILABLE

    @staticmethod
    def _resource_check(context: SubjectContext, resource: ResourceRef) -> Reason | None:
        """Invariant 4: the resource of another Tenant is never accessible.

        A tenant-scoped resource whose owning Tenant the data owner did not
        state cannot be checked, and an uncheckable resource is denied — a
        missing tenant is not a wildcard.
        """
        if resource.tenant_id is None or not str(resource.tenant_id).strip():
            return Reason.RESOURCE_TENANT_UNKNOWN
        if resource.tenant_id != context.tenant_id:
            return Reason.RESOURCE_TENANT_MISMATCH
        return None

    def _lifecycle_check(
        self, context: SubjectContext, obs: ObservabilityContext
    ) -> Reason | None:
        """Invariant 7: read the Tenant verdict through the port at decision time.

        The verdict is a value of this component; a Tenant the authority refuses
        to answer about, for whatever published reason, is a Tenant this
        boundary does not serve.
        """
        try:
            answer = self.tenant_authority.lifecycle_decision(
                context.tenant_id,
                expected_platform_id=self.current_platform_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception:
            return Reason.AUTHORITY_UNAVAILABLE
        if isinstance(answer, DependencyRefusal):
            return tenant_authority_denial(answer.reason_code)
        if not isinstance(answer, TenantVerdict):
            return Reason.AUTHORITY_UNAVAILABLE
        if answer.permitted:
            return None
        return lifecycle_denial(answer.reason_code)

    # ------------------------------------------------------------ caller gate
    def verify_service_identity(self, token: str | None) -> ServiceAccess:
        """A service, background job or agent uses its own checked identity (§6.2)."""
        if token is None or token.strip() == "":
            raise CallerNotAuthenticated(CallerDenyReason.MISSING_SERVICE_IDENTITY)
        if not token.startswith(self.config.service_token_prefix):
            raise CallerNotAuthenticated(CallerDenyReason.INVALID_SERVICE_IDENTITY)
        service_id = self.store.service_tokens.get(token)
        if service_id is None:
            raise CallerNotAuthenticated(CallerDenyReason.INVALID_SERVICE_IDENTITY)
        access = self.store.services.get(service_id)
        if access is None:
            raise CallerNotAuthenticated(CallerDenyReason.UNKNOWN_SERVICE)
        return access

    def _gate(
        self,
        token: str | None,
        *,
        permission: str,
        action: str,
        request_id: str | None,
        correlation_id: str | None,
        operation: str | None,
        resource: ResourceRef | None,
    ) -> tuple[ServiceAccess, ObservabilityContext]:
        access: ServiceAccess | None = None
        obs = self.observability(
            access=None,
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        try:
            access = self.verify_service_identity(token)
            if access.platform_id != self.config.platform_id:
                raise CallerNotAuthorized(
                    CallerDenyReason.PLATFORM_MISMATCH,
                    "service identity belongs to another Platform Instance",
                    details={"actor_platform_id": access.platform_id},
                )
            if permission not in access.permissions:
                raise CallerNotAuthorized(
                    CallerDenyReason.INSUFFICIENT_AUTHORIZATION,
                    f"service {access.service_id} lacks {permission}",
                    details={"required_permission": permission},
                )
        except AuthorizationServiceError as exc:
            raise self._refuse(
                exc,
                action=action,
                access=access,
                obs=obs,
                operation=operation,
                resource=resource,
            )
        return access, self.observability(
            access=access,
            tenant_id=None,
            subject_id=None,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )

    def _refuse(
        self,
        exc: AuthorizationServiceError,
        *,
        action: str,
        access: ServiceAccess | None,
        obs: ObservabilityContext,
        operation: str | None,
        resource: ResourceRef | None,
    ) -> AuthorizationServiceError:
        """Audit a refusal to answer and hand the error back to the caller.

        A refused question is security-relevant too: it is recorded as a DENY of
        the *request*, with the caller, the request id and the correlation id.
        """
        self.audit(
            action=action,
            decision=Decision.DENY,
            reason=None,
            access=access,
            obs=obs,
            subject_id=None,
            tenant_id=None,
            operation=operation,
            resource=resource,
            details={"refused": exc.reason.value, **exc.details},
        )
        # The refusal carries the ids it was audited with, so the caller can be
        # pointed at its own record in the journal — including when the request
        # arrived without any request context of its own.
        exc.details.setdefault("request_id", obs.request_id)
        exc.details.setdefault("correlation_id", obs.correlation_id)
        return exc

    def refuse_unreadable_request(
        self,
        token: str | None,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> MalformedDecisionRequest:
        """Audit a request the published contract could not even read.

        A request rejected by the contract schema — a missing field, a field the
        contract does not declare, a value of the wrong type — never reaches
        :meth:`decide`, so without this path the refusal would be the only
        security-relevant event of the component that leaves no trace. It is
        audited exactly like every other refusal to answer: as a ``DENY`` of the
        *request*, attributed to the calling service where one could be
        verified, with ``request_id`` and ``correlation_id``.

        It stays a refusal, not a decision: an unreadable question has no
        authorization meaning, and answering ``DENY`` would hide a defect of the
        consuming component behind a security-looking answer.
        """
        obs = self.observability(
            access=None,
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        try:
            access: ServiceAccess | None = self.verify_service_identity(token)
        except AuthorizationServiceError:
            # An unreadable request from an unidentified caller is still audited;
            # the caller is simply unknown, which is itself worth recording.
            access = None
        if access is not None and access.platform_id != self.config.platform_id:
            access = None
        obs = self.observability(
            access=access,
            tenant_id=None,
            subject_id=None,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        exc = MalformedDecisionRequest(
            CallerDenyReason.MALFORMED_REQUEST,
            "the request does not match the published decision contract",
            details=dict(details or {}),
        )
        self._refuse(
            exc,
            action=DECISION_ACTION,
            access=access,
            obs=obs,
            operation=None,
            resource=None,
        )
        return exc

    # -------------------------------------------------------------- outcomes
    def _decided(
        self,
        decision: Decision,
        reason: Reason,
        *,
        access: ServiceAccess,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        operation: str,
        resource: ResourceRef,
    ) -> tuple[AuthorizationDecision, ObservabilityContext, AuditEvent]:
        event = self.audit(
            action=DECISION_ACTION,
            decision=decision,
            reason=reason,
            access=access,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            operation=operation,
            resource=resource,
        )
        answer = AuthorizationDecision(
            decision=decision,
            reason=reason,
            operation=operation,
            resource=resource,
            subject_id=subject_id,
            tenant_id=tenant_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        return answer, obs, event

    # --------------------------------------------------------- observability
    def observability(
        self,
        *,
        access: ServiceAccess | None,
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
            service_id=None if access is None else access.service_id,
        )

    def audit(
        self,
        *,
        action: str,
        decision: Decision,
        reason: Reason | None,
        access: ServiceAccess | None,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        operation: str | None,
        resource: ResourceRef | None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=_new_id("aud"),
            action=action,
            decision=decision,
            reason=reason,
            actor_id=None if access is None else access.service_id,
            subject_id=subject_id,
            platform_id=obs.platform_id,
            tenant_id=tenant_id,
            operation=None if operation is None else str(operation),
            resource_type=None if resource is None else resource.resource_type,
            resource_id=None if resource is None else resource.resource_id,
            resource_tenant_id=None if resource is None else resource.tenant_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            timestamp=self.clock(),
            details=details or {},
        )
        self.store.audit.append(event)
        return event


__all__ = ["DECISION_ACTION", "AuthorizationEngine"]
