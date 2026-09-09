"""Internal records of the Learning component (SCS-001).

These types describe the owned data of the component (logical schema
``learning``). They are not part of the public contract; consumers receive
:mod:`learning_service.contracts` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OwnedAssignment:
    """One assignment owned by this component.

    ``owner_component`` is singular by construction: one value, one owner.
    ``tenant_id`` is the Tenant the assignment belongs to, as registered by
    the composition root; it is a fact of this store, never a caller claim
    (LAW-16a). ``status`` is the Assignment lifecycle state; the list
    operation does not filter on it — existence and tenant are the boundary
    facts, and any lifecycle gating would be an explicit future slice.
    """

    assignment_id: str
    tenant_id: str
    owner_component: str
    status: str
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class OwnedSubmission:
    """One submission owned by this component.

    ``tenant_id`` is denormalized from the parent Assignment at registration
    and must equal it: a submission cannot belong to another Tenant than its
    Assignment. ``student_identity_id`` is an opaque identity reference owned
    outside Learning (Identity owns the profile); Learning stores only the
    reference, never profile/account data. ``status`` is exactly
    ``DRAFT`` or ``SUBMITTED`` — no grading/review states exist in this
    slice.
    """

    submission_id: str
    assignment_id: str
    tenant_id: str
    student_identity_id: str
    attempt: int
    content: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    owner_component: str = "learning"


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

    ``subject_id`` and ``tenant_id`` are what the decision stated where
    known: before the decision this component knows nothing about the
    caller, because it verifies no credential itself. ``claimed_tenant_id``
    is recorded as security-relevant context of the attempt — the value the
    caller supplied as a cross-check — and never as tenant identity.
    ``resource_id`` is the URL target (assignment for a list, submission for
    a single read); ``assignment_id`` is always the Assignment in scope.
    """

    event_id: str
    action: str
    decision: str
    reason: str | None
    subject_id: str | None
    tenant_id: str | None
    resource_id: str | None
    resource_tenant_id: str | None
    assignment_id: str | None
    submission_id: str | None
    claimed_tenant_id: str | None
    platform_id: str | None
    request_id: str
    correlation_id: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)
