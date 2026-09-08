from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class IdentityKind(StrEnum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"


class TenantStatus(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETION_REQUESTED = "deletion_requested"
    DELETED = "deleted"


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class DenyReason(StrEnum):
    MISSING_IDENTITY = "missing_identity"
    INVALID_IDENTITY = "invalid_identity"
    UNKNOWN_IDENTITY = "unknown_identity"
    MISSING_TENANT_CONTEXT = "missing_tenant_context"
    TENANT_MISMATCH = "tenant_mismatch"
    TENANT_SUSPENDED = "tenant_suspended"
    TENANT_DELETED = "tenant_deleted"
    TENANT_NOT_ACTIVE = "tenant_not_active"
    INSUFFICIENT_AUTHORIZATION = "insufficient_authorization"
    UNKNOWN_RESOURCE = "unknown_resource"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    identity_id: str
    kind: IdentityKind
    subject: str
    home_tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class TenantRecord:
    tenant_id: str
    status: TenantStatus


@dataclass(frozen=True, slots=True)
class TenantAssociation:
    identity_id: str
    tenant_id: str
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    status: TenantStatus
    source: str = "verified_identity"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    identity: VerifiedIdentity
    tenant: TenantContext
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    request_id: str
    correlation_id: str
    trace_id: str
    component_id: str
    component_version: str
    actor_id: str | None
    tenant_id: str | None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    action: str
    decision: Decision
    reason: str | None
    actor_id: str | None
    tenant_id: str | None
    request_id: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProtectedRecord:
    record_id: str
    tenant_id: str
    body: str


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    key: str
    identity_id: str
    tenant_id: str
    operation: str
    record_id: str
    fingerprint: str
    stored_record_id: str
