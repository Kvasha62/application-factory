from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timezone

from identity_service import COMPONENT_ID, COMPONENT_VERSION
from identity_service.config import IdentityConfig
from identity_service.errors import AccessDenied
from identity_service.models import (
    AuditEvent,
    AuthorizationContext,
    Decision,
    DenyReason,
    IdempotencyRecord,
    IdentityKind,
    ObservabilityContext,
    ProtectedRecord,
    TenantContext,
    VerifiedIdentity,
)
from identity_service.ports import TenantAuthorityPort
from identity_service.store import IdentityStore
from tenant_authority.contracts import TenantSnapshot
from tenant_authority.errors import ContractViolation, TenantAuthorityError
from tenant_authority.errors import DenyReason as TenantAuthorityDenial

# Lifecycle state -> identity deny reason. The decision itself is made by
# Tenant Authority (IS-002); identity only names the outcome at its own
# authorization boundary, so the vocabulary stays a single source of truth.
TENANT_LIFECYCLE_DENIALS: dict[str, DenyReason] = {
    "provisioning_not_served": DenyReason.TENANT_NOT_ACTIVE,
    "tenant_suspended": DenyReason.TENANT_SUSPENDED,
    "tenant_deletion_requested": DenyReason.TENANT_DELETION_REQUESTED,
    "tenant_deleted": DenyReason.TENANT_DELETED,
    "unsupported_state": DenyReason.TENANT_NOT_ACTIVE,
}

#: Tenant Authority answers a Tenant of another Platform Instance exactly like a
#: missing one, so the contract-level reason for both is `tenant_not_found`; a
#: platform cross-check failure (`platform_mismatch`) means this component is
#: wired to the wrong Platform Instance and is reported distinctly.
TENANT_AUTHORITY_DENIALS: dict[str, DenyReason] = {
    TenantAuthorityDenial.TENANT_NOT_FOUND.value: DenyReason.TENANT_UNKNOWN,
    TenantAuthorityDenial.FOREIGN_TENANT.value: DenyReason.PLATFORM_OWNERSHIP_MISMATCH,
    TenantAuthorityDenial.PLATFORM_MISMATCH.value: DenyReason.PLATFORM_OWNERSHIP_MISMATCH,
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _fingerprint(operation: str, record_id: str, body: str) -> str:
    payload = f"{operation}\n{record_id}\n{body}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass
class IdentityEngine:
    """Identity verification, effective tenant derivation and authorization.

    Tenant state is not owned here: it is read through the published Tenant
    Authority port (IS-002), which is the authoritative source for lifecycle
    state and Platform Instance ownership.
    """

    store: IdentityStore
    tenant_authority: TenantAuthorityPort
    config: IdentityConfig

    @property
    def current_platform_id(self) -> str:
        """The Platform Instance this boundary belongs to (never a caller claim)."""
        return self.config.current_platform_id

    # ------------------------------------------------------------------ identity
    def verify_identity(self, token: str | None) -> VerifiedIdentity:
        if token is None or token.strip() == "":
            raise AccessDenied(DenyReason.MISSING_IDENTITY)
        if token == "token-invalid" or not token.startswith(self.config.token_prefix):
            raise AccessDenied(DenyReason.INVALID_IDENTITY)
        identity_id = self.store.tokens.get(token)
        if identity_id is None:
            raise AccessDenied(DenyReason.INVALID_IDENTITY)
        identity = self.store.identities.get(identity_id)
        if identity is None:
            raise AccessDenied(DenyReason.UNKNOWN_IDENTITY)
        return identity

    def identity_kind(self, identity: VerifiedIdentity) -> IdentityKind:
        return identity.kind

    # -------------------------------------------------------- tenant context
    def resolve_tenant_context(
        self,
        identity: VerifiedIdentity,
        claimed_tenant_id: str | None,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantContext:
        """Derive the effective tenant from verified identity, then look it up.

        The caller-supplied ``tenant_id`` is only a cross-check (LAW-16a): the
        effective tenant can never be changed by rewriting it. State and
        ownership come from Tenant Authority (T-004), and a Tenant of another
        Platform Instance is never resolved here (T-003, T-007).
        """
        associations = [
            a for a in self.store.associations.values() if a.identity_id == identity.identity_id
        ]
        if not associations:
            raise AccessDenied(DenyReason.MISSING_TENANT_CONTEXT)

        bound_ids = {a.tenant_id for a in associations}
        if claimed_tenant_id:
            if claimed_tenant_id not in bound_ids:
                raise AccessDenied(DenyReason.TENANT_MISMATCH)
            tenant_id = claimed_tenant_id
        else:
            home = identity.home_tenant_id
            if home and home in bound_ids:
                tenant_id = home
            elif len(bound_ids) == 1:
                tenant_id = next(iter(bound_ids))
            else:
                raise AccessDenied(DenyReason.MISSING_TENANT_CONTEXT)

        snapshot = self._tenant_snapshot(tenant_id, request_id, correlation_id)
        return TenantContext(
            tenant_id=snapshot.tenant_id,
            status=snapshot.state,
            platform_id=snapshot.platform_id,
        )

    def _tenant_snapshot(
        self,
        tenant_id: str,
        request_id: str | None,
        correlation_id: str | None,
    ) -> TenantSnapshot:
        try:
            return self.tenant_authority.lookup(
                tenant_id,
                expected_platform_id=self.current_platform_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        except (TenantAuthorityError, ContractViolation) as exc:
            # A transport fault is handled like an unknown Tenant: no answer from
            # the authority is never read as "this Tenant may be served".
            raise AccessDenied(self._authority_denial(exc)) from exc

    @staticmethod
    def _authority_denial(exc: BaseException) -> DenyReason:
        reason = getattr(exc, "reason", None)
        if reason is None:
            return DenyReason.TENANT_UNKNOWN
        return TENANT_AUTHORITY_DENIALS.get(reason.value, DenyReason.TENANT_UNKNOWN)

    # ------------------------------------------------------------------ authz
    def authorize(
        self,
        identity: VerifiedIdentity,
        tenant: TenantContext,
        permission: str,
        *,
        tenant_scoped: bool = True,
    ) -> AuthorizationContext:
        """Authorization boundary of the data owner: lifecycle + permission check."""
        if tenant_scoped:
            # T-008: a Tenant in a lifecycle state that must not be served
            # blocks ordinary tenant-scoped operations. The state machine and
            # the operational policy belong to Tenant Authority, not to us.
            try:
                decision = self.tenant_authority.lifecycle_decision(
                    tenant.tenant_id,
                    expected_platform_id=self.current_platform_id,
                )
            except (TenantAuthorityError, ContractViolation) as exc:
                raise AccessDenied(self._authority_denial(exc)) from exc
            if not decision.permitted:
                raise AccessDenied(TENANT_LIFECYCLE_DENIALS[decision.reason])

        assoc = self.store.associations.get((identity.identity_id, tenant.tenant_id))
        if assoc is None:
            raise AccessDenied(DenyReason.INSUFFICIENT_AUTHORIZATION)
        if permission not in assoc.permissions:
            raise AccessDenied(DenyReason.INSUFFICIENT_AUTHORIZATION)
        return AuthorizationContext(
            identity=identity,
            tenant=tenant,
            permissions=assoc.permissions,
        )

    # ------------------------------------------------------- observability
    def observability(
        self,
        *,
        identity: VerifiedIdentity | None,
        tenant: TenantContext | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ObservabilityContext:
        rid = request_id or _new_id("req")
        cid = correlation_id or rid
        return ObservabilityContext(
            request_id=rid,
            correlation_id=cid,
            trace_id=rid,
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            platform_id=self.current_platform_id,
            environment=self.config.environment,
            tenant_id=None if tenant is None else tenant.tenant_id,
            actor_id=None if identity is None else identity.identity_id,
            timestamp=_utc_now(),
        )

    def audit(
        self,
        *,
        action: str,
        decision: Decision,
        reason: str | None,
        identity: VerifiedIdentity | None,
        tenant: TenantContext | None,
        obs: ObservabilityContext,
        details: dict | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=_new_id("aud"),
            action=action,
            decision=decision,
            reason=reason,
            actor_id=None if identity is None else identity.identity_id,
            tenant_id=None if tenant is None else tenant.tenant_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            details=details or {},
        )
        self.store.audit.append(event)
        return event

    # ------------------------------------------------------------ operations
    def authenticate(
        self,
        token: str | None,
        *,
        action: str = "identity.authenticate",
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[VerifiedIdentity, ObservabilityContext, AuditEvent]:
        identity: VerifiedIdentity | None = None
        obs = self.observability(
            identity=None, tenant=None, request_id=request_id, correlation_id=correlation_id
        )
        try:
            identity = self.verify_identity(token)
            obs = self.observability(
                identity=identity,
                tenant=None,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except AccessDenied as exc:
            self.audit(
                action=action,
                decision=Decision.DENY,
                reason=exc.reason.value,
                identity=identity,
                tenant=None,
                obs=obs,
            )
            raise
        event = self.audit(
            action=action,
            decision=Decision.ALLOW,
            reason=None,
            identity=identity,
            tenant=None,
            obs=obs,
        )
        return identity, obs, event

    def verified_context(
        self,
        token: str | None,
        claimed_tenant_id: str | None = None,
        *,
        action: str = "identity.context",
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[VerifiedIdentity, TenantContext, ObservabilityContext, AuditEvent]:
        """Verified subject plus the effective Tenant of this request.

        This is the operation another component consumes (through the published
        contract) when it needs a verified subject and an effective tenant: the
        tenant is derived here, from the verified identity, so no consumer has to
        invent a second tenant-context mechanism. A caller-supplied
        ``claimed_tenant_id`` stays a cross-check (LAW-16a) and is rejected on
        mismatch.

        It is deliberately **not** an authorization decision: no permission is
        checked and no lifecycle verdict is applied here. The data owner — and,
        for the platform, IS-003 — decides ALLOW/DENY at its own boundary
        (ARCHITECTURE.md §6.2).
        """
        identity: VerifiedIdentity | None = None
        tenant: TenantContext | None = None
        obs = self.observability(
            identity=None, tenant=None, request_id=request_id, correlation_id=correlation_id
        )
        try:
            identity = self.verify_identity(token)
            obs = self.observability(
                identity=identity,
                tenant=None,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
            tenant = self.resolve_tenant_context(
                identity,
                claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except AccessDenied as exc:
            self.audit(
                action=action,
                decision=Decision.DENY,
                reason=exc.reason.value,
                identity=identity,
                tenant=tenant,
                obs=obs,
            )
            raise
        obs = self.observability(
            identity=identity,
            tenant=tenant,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        event = self.audit(
            action=action,
            decision=Decision.ALLOW,
            reason=None,
            identity=identity,
            tenant=tenant,
            obs=obs,
            details={"source": tenant.source},
        )
        return identity, tenant, obs, event

    def read_record(
        self,
        token: str | None,
        record_id: str,
        claimed_tenant_id: str | None,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ProtectedRecord, ObservabilityContext, AuditEvent]:
        return self._access_record(
            token,
            record_id,
            claimed_tenant_id,
            permission="records.read",
            action="records.read",
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def write_record(
        self,
        token: str | None,
        record_id: str,
        body: str,
        claimed_tenant_id: str | None,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ProtectedRecord, ObservabilityContext, AuditEvent]:
        identity, tenant, authz, obs = self._gate(
            token,
            claimed_tenant_id,
            "records.write",
            "records.write",
            request_id,
            correlation_id,
        )
        fingerprint = _fingerprint("records.write", record_id, body)
        if idempotency_key:
            existing_key = self.store.idempotency.get(idempotency_key)
            if existing_key is not None:
                same_request = (
                    existing_key.identity_id == identity.identity_id
                    and existing_key.tenant_id == tenant.tenant_id
                    and existing_key.operation == "records.write"
                    and existing_key.record_id == record_id
                    and existing_key.fingerprint == fingerprint
                )
                if not same_request:
                    self.audit(
                        action="records.write",
                        decision=Decision.DENY,
                        reason=DenyReason.IDEMPOTENCY_CONFLICT.value,
                        identity=identity,
                        tenant=tenant,
                        obs=obs,
                        details={"idempotency_key": idempotency_key},
                    )
                    raise AccessDenied(DenyReason.IDEMPOTENCY_CONFLICT)
                existing = self.store.records[existing_key.stored_record_id]
                event = self.audit(
                    action="records.write",
                    decision=Decision.ALLOW,
                    reason="idempotent_replay",
                    identity=identity,
                    tenant=tenant,
                    obs=obs,
                    details={"record_id": existing.record_id, "kind": authz.identity.kind.value},
                )
                return existing, obs, event

        record = ProtectedRecord(record_id, tenant.tenant_id, body)
        self.store.records[record_id] = record
        if idempotency_key:
            self.store.idempotency[idempotency_key] = IdempotencyRecord(
                key=idempotency_key,
                identity_id=identity.identity_id,
                tenant_id=tenant.tenant_id,
                operation="records.write",
                record_id=record_id,
                fingerprint=fingerprint,
                stored_record_id=record_id,
            )
        event = self.audit(
            action="records.write",
            decision=Decision.ALLOW,
            reason=None,
            identity=identity,
            tenant=tenant,
            obs=obs,
            details={"record_id": record_id, "kind": authz.identity.kind.value},
        )
        return record, obs, event

    def _access_record(
        self,
        token: str | None,
        record_id: str,
        claimed_tenant_id: str | None,
        *,
        permission: str,
        action: str,
        request_id: str | None,
        correlation_id: str | None = None,
    ) -> tuple[ProtectedRecord, ObservabilityContext, AuditEvent]:
        identity, tenant, authz, obs = self._gate(
            token,
            claimed_tenant_id,
            permission,
            action,
            request_id,
            correlation_id,
        )
        record = self.store.records.get(record_id)
        if record is None:
            self.audit(
                action=action,
                decision=Decision.DENY,
                reason=DenyReason.UNKNOWN_RESOURCE.value,
                identity=identity,
                tenant=tenant,
                obs=obs,
            )
            raise AccessDenied(DenyReason.UNKNOWN_RESOURCE)
        if record.tenant_id != tenant.tenant_id:
            # Isolation: do not leak existence of foreign tenant data.
            self.audit(
                action=action,
                decision=Decision.DENY,
                reason=DenyReason.UNKNOWN_RESOURCE.value,
                identity=identity,
                tenant=tenant,
                obs=obs,
            )
            raise AccessDenied(DenyReason.UNKNOWN_RESOURCE)
        event = self.audit(
            action=action,
            decision=Decision.ALLOW,
            reason=None,
            identity=identity,
            tenant=tenant,
            obs=obs,
            details={"record_id": record_id, "kind": authz.identity.kind.value},
        )
        return record, obs, event

    def _gate(
        self,
        token: str | None,
        claimed_tenant_id: str | None,
        permission: str,
        action: str,
        request_id: str | None,
        correlation_id: str | None = None,
    ) -> tuple[VerifiedIdentity, TenantContext, AuthorizationContext, ObservabilityContext]:
        identity: VerifiedIdentity | None = None
        tenant: TenantContext | None = None
        obs = self.observability(
            identity=None, tenant=None, request_id=request_id, correlation_id=correlation_id
        )
        try:
            identity = self.verify_identity(token)
            obs = self.observability(
                identity=identity,
                tenant=None,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
            tenant = self.resolve_tenant_context(
                identity,
                claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
            obs = self.observability(
                identity=identity,
                tenant=tenant,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
            authz = self.authorize(identity, tenant, permission)
            return identity, tenant, authz, obs
        except AccessDenied as exc:
            self.audit(
                action=action,
                decision=Decision.DENY,
                reason=exc.reason.value,
                identity=identity,
                tenant=tenant,
                obs=obs,
            )
            raise
