"""Internal records of the Learning component.

These types describe the owned data of the component (logical schema
``learning``). They are not part of the public contract; consumers receive
:mod:`learning_service.contracts` values.

Lifecycle facts:

* a Course moves along ``DRAFT → PUBLISHED`` (``ARCHIVED`` is reserved for a
  later slice — no command reaches it yet);
* Modules, Lessons and Assignments are created ``DRAFT`` and become
  ``PUBLISHED`` only as a consequence of the Publish Course command: no other
  command in this slice changes their state;
* an Enrollment is created ``ACTIVE`` — there is no cancellation command in
  this slice;
* a Submission is created ``SUBMITTED`` — no grading exists in this slice.

States are never written by a caller: every state is set by a command of this
component after the enforcement chain allowed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------- lifecycle
COURSE_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED", "ARCHIVED")
CONTENT_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED")
ENROLLMENT_STATES: tuple[str, ...] = ("ACTIVE",)
SUBMISSION_STATES: tuple[str, ...] = ("SUBMITTED",)

#: The closed transition map of the Course state machine. A command names a
#: business operation; the map names the only state it may start from.
COURSE_TRANSITIONS: dict[str, tuple[str, str]] = {
    "publish": ("DRAFT", "PUBLISHED"),
}


@dataclass(frozen=True, slots=True)
class Course:
    """One course of one Tenant. ``tenant_id`` is a fact of this store."""

    course_id: str
    tenant_id: str
    title: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Module:
    """One module of a course; the parent chain is stored, never derived."""

    module_id: str
    course_id: str
    tenant_id: str
    title: str
    position: int
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Lesson:
    """One lesson of a module."""

    lesson_id: str
    module_id: str
    tenant_id: str
    title: str
    content: str
    position: int
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Assignment:
    """One assignment of a lesson."""

    assignment_id: str
    lesson_id: str
    tenant_id: str
    title: str
    instructions: str
    status: str


@dataclass(frozen=True, slots=True)
class Enrollment:
    """One student enrolled in one course. Unique per (course, student)."""

    enrollment_id: str
    tenant_id: str
    course_id: str
    student_identity_id: str
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Submission:
    """One submission of one student for one assignment."""

    submission_id: str
    tenant_id: str
    assignment_id: str
    student_identity_id: str
    attempt: int
    content: str
    status: str
    created_at: str
    updated_at: str


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
    """Append-only audit record of one operation attempt — served or refused.

    ``identity_id`` and ``tenant_id`` are what the verification of IS-001
    established for the attempt; ``resource_type``/``resource_id`` name the
    question. Refusals are audited exactly like served operations
    (ARCHITECTURE.md §27).
    """

    event_id: str
    timestamp: str
    action: str
    decision: str
    reason: str | None
    identity_id: str | None
    tenant_id: str | None
    resource_type: str | None
    resource_id: str | None
    platform_id: str | None
    request_id: str | None
    correlation_id: str | None
    details: dict[str, Any]
