"""Internal records of the Learning component (SCS-001).

These types describe the owned data of the component (logical schema
``learning``). They are not part of the public contract; consumers receive
:mod:`learning_service.contracts` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OwnedCourse:
    """One course owned by this component (Content Authoring, Issue #31).

    ``owner_component`` is singular by construction: one value, one owner.
    ``tenant_id`` is the Tenant the course belongs to — a fact of this store
    derived from the verified identity through the published chain, never a
    caller claim (LAW-16a). ``created_by`` is an opaque reference to the
    verified Teacher identity that created the course; the profile is owned
    outside Learning. Lifecycle: exactly ``DRAFT → PUBLISHED → ARCHIVED`` —
    no unpublish and no unarchive exist.
    """

    course_id: str
    tenant_id: str
    owner_component: str
    title: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OwnedModule:
    """One module owned by this component.

    A module belongs to exactly one Course and to the same Tenant as that
    Course: the hierarchy is registered, never declared by a caller. A module
    is created only in ``DRAFT``; publication and archive are Course-level
    commands that move the whole hierarchy atomically.
    """

    module_id: str
    course_id: str
    tenant_id: str
    owner_component: str
    title: str
    position: int
    status: str
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class OwnedLesson:
    """One lesson owned by this component.

    A lesson belongs to exactly one Module and to the same Tenant as that
    Module (and therefore as the Course above it). Created only in ``DRAFT``.
    ``content`` is the lesson body text; no media, attachments or revisions
    exist in this slice.
    """

    lesson_id: str
    module_id: str
    tenant_id: str
    owner_component: str
    title: str
    content: str
    position: int
    status: str
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class OwnedAssignment:
    """One assignment owned by this component.

    ``owner_component`` is singular by construction: one value, one owner.
    ``tenant_id`` is the Tenant the assignment belongs to, as registered by
    the composition root; it is a fact of this store, never a caller claim
    (LAW-16a). ``status`` is the Assignment lifecycle state; the list
    operation does not filter on it — existence and tenant are the boundary
    facts, and any lifecycle gating would be an explicit future slice.

    Content Authoring (Issue #31) extends the same model — no second
    Assignment model exists — with exactly the fields needed to anchor the
    hierarchy: ``lesson_id`` is the parent Lesson (``None`` only for records
    registered before authoring existed, e.g. the demo data), ``title`` and
    ``instructions`` are the assignment content. A new assignment is created
    only under a Lesson of the same Tenant and only in ``DRAFT``.
    """

    assignment_id: str
    tenant_id: str
    owner_component: str
    status: str
    created_at: str = ""
    updated_at: str = ""
    lesson_id: str | None = None
    title: str | None = None
    instructions: str | None = None


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

    ``reviewed_by`` is an opaque reference to the verified Teacher identity
    that performed the review, owned outside Learning; ``reviewed_at`` is the
    timestamp of that successful review. Both stay ``None`` until the first
    review and are the only review fact recorded — there is no Review entity,
    no grade, no feedback and no comment.
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
    reviewed_by: str | None = None
    reviewed_at: str | None = None


@dataclass(frozen=True, slots=True)
class OwnedEnrollment:
    """One student enrollment owned by this component (ADR-0012, Slice 1).

    The business fact of enrollment is the participation of one student
    identity in one Course within one Tenant. ``student_identity_id`` is an
    opaque reference to the identity owned outside Learning (Identity owns
    the profile); Learning stores the reference and never interprets it.
    ``status`` is exactly ``ACTIVE`` — no other enrollment lifecycle state
    exists in this slice. Within one Tenant at most one ``ACTIVE``
    enrollment may exist for one student in one Course: the duplicate
    protection of ADR-0012 is an invariant of this store's registration,
    never a caller declaration. ``owner_component`` is singular by
    construction: one value, one owner — ``learning``. Enrollment is not
    part of the authored Course hierarchy: a Course does not contain its
    enrollments; the two are related only by ``course_id``.
    """

    enrollment_id: str
    course_id: str
    tenant_id: str
    student_identity_id: str
    status: str
    created_at: str
    updated_at: str
    owner_component: str


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
