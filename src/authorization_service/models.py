"""Internal records of the Authorization Boundary component (IS-003).

These types describe the owned data of the component (logical schema
``authorization``). They are not part of the public contract; consumers use
:mod:`authorization_service.contracts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from authorization_service.contracts import Decision, Reason


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """An explicit permission of one subject inside one Tenant.

    Tenant-scoped by nature: a grant exists inside exactly one Tenant, and the
    same subject in another Tenant is another grant. There is no inheritance,
    no role hierarchy and no wildcard — absence of a grant is a denial
    (invariant 6), so nothing has to be excluded, only granted.
    """

    tenant_id: str
    subject_id: str
    operations: frozenset[str]


@dataclass(frozen=True, slots=True)
class ServiceAccess:
    """Checked service identity of a component allowed to ask for decisions.

    Platform-scoped by nature: a service identity belongs to exactly one
    Platform Instance (ARCHITECTURE.md §6.2, §11).
    """

    service_id: str
    platform_id: str
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    """Standard observability context (ARCHITECTURE.md §26 / ADR-0010 AMD-10).

    ``saga_id`` is absent on purpose: this component runs no inter-component
    sagas.
    """

    timestamp: str
    environment: str
    platform_id: str | None
    component_id: str
    component_version: str
    request_id: str
    trace_id: str
    correlation_id: str
    tenant_id: str | None
    subject_id: str | None
    service_id: str | None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Append-only audit record of one decision or of one refused request.

    ``actor_id`` is the asking component's service identity; ``subject_id`` is
    the verified subject the decision was about. Both are recorded: a denial
    must be attributable to a caller and to a subject.
    """

    event_id: str
    action: str
    decision: Decision
    reason: Reason | None
    actor_id: str | None
    subject_id: str | None
    platform_id: str | None
    tenant_id: str | None
    operation: str | None
    resource_type: str | None
    resource_id: str | None
    resource_tenant_id: str | None
    request_id: str
    correlation_id: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)
