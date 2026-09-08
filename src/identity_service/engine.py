from __future__ import annotations

import uuid
from dataclasses import dataclass

from identity_service import COMPONENT_ID, COMPONENT_VERSION
from identity_service.errors import AccessDenied
from identity_service.models import (
    AuditEvent,
    AuthorizationContext,
    Decision,
    DenyReason,
    IdentityKind,
    ObservabilityContext,
    ProtectedRecord,
    TenantContext,
    TenantStatus,
    VerifiedIdentity,
)
from identity_service.store import IdentityStore


PROTECTED_TENANT_STATUSES = {TenantStatus.SUSPENDED}
DELETED_STATUSES = {TenantStatus.DELETED}
ACTIVE_ONLY = {TenantStatus.ACTIVE}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass
class IdentityEngine:
    store: IdentityStore

    def verify_identity(self, token: str | None) -> VerifiedIdentity:
        if token is None or token.strip() == "":
            raise AccessDenied(DenyReason.MISSING_IDENTITY)
        if token == "token-invalid" or not token.startswith("token-"):
            raise AccessDenied(DenyReason.INVALID_IDENTITY)
        identity_id = self.store.tokens.get(token)
        if identity_id is None:
            raise AccessDenied(DenyReason.INVALID_IDENTITY)
        identity = self.store.identities.get(identity_id)
        if identity is None:
            raise AccessDenied(DenyReason.UNKNOWN_IDENTITY)
        return identity

    def resolve_tenant_context(
        self,
        identity: VerifiedIdentity,
        claimed_tenant_id: str | None,
    ) -> TenantContext:
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

        tenant = self.store.tenants.get(tenant_id)
        if tenant is None:
            raise AccessDenied(DenyReason.MISSING_TENANT_CONTEXT)
        return TenantContext(tenant_id=tenant.tenant_id, status=tenant.status)

    def authorize(
        self,
        identity: VerifiedIdentity,
        tenant: TenantContext,
        permission: str,
        *,
        tenant_scoped: bool = True,
    ) -> AuthorizationContext:
        if tenant_scoped:
            if tenant.status in DELETED_STATUSES:
                raise AccessDenied(DenyReason.TENANT_DELETED)
            if tenant.status in PROTECTED_TENANT_STATUSES:
                raise AccessDenied(DenyReason.TENANT_SUSPENDED)
            if tenant.status not in ACTIVE_ONLY:
                raise AccessDenied(DenyReason.TENANT_NOT_ACTIVE)

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
            actor_id=None if identity is None else identity.identity_id,
            tenant_id=None if tenant is None else tenant.tenant_id,
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
            details=details or {},
        )
        self.store.audit.append(event)
        return event

    def read_record(
        self,
        token: str | None,
        record_id: str,
        claimed_tenant_id: str | None,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ProtectedRecord, ObservabilityContext, AuditEvent]:
        return self._access_record(
            token,
            record_id,
            claimed_tenant_id,
            permission="records.read",
            action="records.read",
            request_id=request_id,
        )

    def authenticate(
        self,
        token: str | None,
        *,
        action: str = "identity.authenticate",
        request_id: str | None = None,
    ) -> tuple[VerifiedIdentity, ObservabilityContext, AuditEvent]:
        identity: VerifiedIdentity | None = None
        obs = self.observability(identity=None, tenant=None, request_id=request_id)
        try:
            identity = self.verify_identity(token)
            obs = self.observability(identity=identity, tenant=None, request_id=obs.request_id)
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

    def write_record(
        self,
        token: str | None,
        record_id: str,
        body: str,
        claimed_tenant_id: str | None,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ProtectedRecord, ObservabilityContext, AuditEvent]:
        identity, tenant, authz, obs = self._gate(
            token, claimed_tenant_id, "records.write", "records.write", request_id
        )
        if idempotency_key and idempotency_key in self.store.idempotency:
            existing_id = self.store.idempotency[idempotency_key]
            existing = self.store.records[existing_id]
            if existing.tenant_id != tenant.tenant_id:
                self.audit(
                    action="records.write",
                    decision=Decision.DENY,
                    reason=DenyReason.UNKNOWN_RESOURCE.value,
                    identity=identity,
                    tenant=tenant,
                    obs=obs,
                )
                raise AccessDenied(DenyReason.UNKNOWN_RESOURCE)
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
            self.store.idempotency[idempotency_key] = record_id
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
    ) -> tuple[ProtectedRecord, ObservabilityContext, AuditEvent]:
        identity, tenant, authz, obs = self._gate(
            token, claimed_tenant_id, permission, action, request_id
        )
        record = self.store.records.get(record_id)
        if record is None:
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
    ) -> tuple[VerifiedIdentity, TenantContext, AuthorizationContext, ObservabilityContext]:
        identity: VerifiedIdentity | None = None
        tenant: TenantContext | None = None
        obs = self.observability(identity=None, tenant=None, request_id=request_id)
        try:
            identity = self.verify_identity(token)
            obs = self.observability(identity=identity, tenant=None, request_id=obs.request_id)
            tenant = self.resolve_tenant_context(identity, claimed_tenant_id)
            obs = self.observability(identity=identity, tenant=tenant, request_id=obs.request_id)
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

    def identity_kind(self, identity: VerifiedIdentity) -> IdentityKind:
        return identity.kind
