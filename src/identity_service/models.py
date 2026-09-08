from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from tenant_authority.contracts import TenantState


class IdentityKind(StrEnum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"


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
    TENANT_DELETION_REQUESTED = "tenant_deletion_requested"
    TENANT_NOT_ACTIVE = "tenant_not_active"
    TENANT_UNKNOWN = "tenant_unknown"
    PLATFORM_OWNERSHIP_MISMATCH = "platform_ownership_mismatch"
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
class TenantAssociation:
    identity_id: str
    tenant_id: str
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Effective Tenant for this request.

    ``source`` proves where the *effective tenant id* came from (IS-001 rule:
    verified identity, never a caller claim). ``state_source`` proves where the
    *lifecycle state* came from (IS-002 rule: Tenant Authority is the only
    source of Tenant state — invariant T-004).
    """

    tenant_id: str
    status: TenantState
    platform_id: str
    source: str = "verified_identity"
    state_source: str = "tenant_authority"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    identity: VerifiedIdentity
    tenant: TenantContext
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    """Standard observability context (ARCHITECTURE.md §26 / ADR-0010 AMD-10).

    ``platform_id`` is bound by IS-002 (Tenant Authority integration) and is
    never taken from a caller claim. ``saga_id`` is absent on purpose: this
    component does not run inter-component sagas.
    """

    request_id: str
    correlation_id: str
    trace_id: str
    component_id: str
    component_version: str
    platform_id: str | None = None
    environment: str = "standalone"
    actor_id: str | None = None
    tenant_id: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Append-only audit record of a security- or state-relevant operation."""

    event_id: str
    action: str
    decision: Decision
    reason: str | None
    actor_id: str | None
    tenant_id: str | None
    request_id: str
    correlation_id: str
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
