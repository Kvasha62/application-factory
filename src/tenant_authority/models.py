"""Internal records of the Tenant Authority component.

These types describe the owned data of the component (logical schema
``tenant_authority``). They are not part of the public contract; consumers use
:mod:`tenant_authority.contracts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from tenant_authority.contracts import TenantState


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class TenantRecord:
    """Row of the Tenant Registry.

    ``tenant_id`` and ``platform_id`` are immutable after creation (T-002, T-003);
    only ``state`` and ``updated_at`` change, and a Tenant has exactly one current
    state (T-001).
    """

    tenant_id: str
    platform_id: str
    state: TenantState
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ServiceAccess:
    """Checked service identity of a component allowed to operate the registry.

    Platform-scoped by nature: a service identity belongs to exactly one
    Platform Instance, which is the ``current_platform_id`` used for ownership
    checks (ARCHITECTURE.md §6.2, §11 identifiers of §6.x).
    """

    service_id: str
    platform_id: str
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    """One accepted lifecycle transition: auditable evidence for T-010."""

    transition_id: str
    tenant_id: str
    platform_id: str
    previous_state: TenantState
    new_state: TenantState
    actor_id: str
    request_id: str
    correlation_id: str
    timestamp: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    """Standard observability context (ARCHITECTURE.md §26 / ADR-0010 AMD-10)."""

    timestamp: str
    environment: str
    platform_id: str | None
    component_id: str
    component_version: str
    request_id: str
    trace_id: str
    correlation_id: str
    tenant_id: str | None
    service_id: str | None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Append-only audit record of a security- or state-relevant operation."""

    event_id: str
    action: str
    decision: Decision
    reason: str | None
    actor_id: str | None
    platform_id: str | None
    tenant_id: str | None
    request_id: str
    correlation_id: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Bound replay key for state-changing operations (ARCHITECTURE.md §6.5)."""

    key: str
    actor_id: str
    platform_id: str
    operation: str
    tenant_id: str | None
    fingerprint: str
    result_ref: str
