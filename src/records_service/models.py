"""Internal records of the Resource Boundary component (IS-004).

These types describe the owned data of the component (logical schema
``records``). They are not part of the public contract; consumers receive
:mod:`records_service.contracts` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OwnedResource:
    """One proof resource of this component.

    Exactly the five fields of the minimal resource model. ``owner_component``
    is singular by construction: one value, one owner — a resource cannot have
    two owners and ownership is never derived from a request (invariant 1).
    ``tenant_id`` is the Tenant the resource belongs to, as registered by the
    composition root; it is a fact of this store, never a caller claim
    (LAW-16a).
    """

    resource_id: str
    resource_type: str
    owner_component: str
    tenant_id: str
    state: str


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


@dataclass(frozen=True, slots=True)
class AccessAuditEvent:
    """Append-only audit record of one access attempt — served or refused.

    ``subject_id`` and ``tenant_id`` are what the decision stated where known:
    before the decision this component knows nothing about the caller, because
    it verifies no credential itself (invariant 5). ``claimed_tenant_id`` is
    recorded as security-relevant context of the attempt — the value the caller
    supplied as a cross-check — and never as tenant identity.
    """

    event_id: str
    action: str
    decision: str
    reason: str | None
    subject_id: str | None
    tenant_id: str | None
    resource_id: str | None
    resource_tenant_id: str | None
    claimed_tenant_id: str | None
    platform_id: str | None
    request_id: str
    correlation_id: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)
