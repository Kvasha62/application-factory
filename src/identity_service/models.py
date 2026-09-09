from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from identity_service.contracts import TENANT_CONTEXT_SOURCE, DenyReason, IdentityKind
from tenant_authority.contracts import TenantState

# `IdentityKind` and `DenyReason` are defined by the published contract of this
# component (`identity_service.contracts`) and re-exported here for the internal
# modules that already use them: one vocabulary, one definition.
__all__ = [
    "AuditEvent",
    "AuthorizationContext",
    "Decision",
    "DenyReason",
    "IdempotencyRecord",
    "IdentityKind",
    "ObservabilityContext",
    "ProtectedRecord",
    "TenantAssociation",
    "TenantContext",
    "VerifiedIdentity",
]


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


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
    source: str = TENANT_CONTEXT_SOURCE
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
