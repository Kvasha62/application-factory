"""Published data contract of the Learning component (SCS-001).

This module publishes the whole vocabulary a consumer needs: the immutable
value an allowed access returns (:class:`SubmissionView`), the closed
reason sets a refusal may carry, and the error classes. Internal modules
(``engine``, ``store``, ``models``, ``api``, ``deployment``, ``transport``,
``ports``, ``adapters``) are not part of the contract and must not be
imported or reached through the published surface (ARCHITECTURE.md §1.1,
LAW-04).

The object a consumer receives is ``learning_service.reader.LearningClient``
— the published operations over the published API (the Slice 1 single read,
the Slice 2 teacher-discovery list and the Slice 3 review command), values
in and values out.

Two facts about the model are deliberate:

* :class:`SubmissionView` is the whole submission and nothing more: the
  ten fields of the business representation. A view is produced only by
  the owned-data operation at the end of the enforcement chain, so holding
  a view means the access was allowed — and a refusal can never be mistaken
  for one. No student profile/account data is carried: ``student_identity_id``
  is an opaque reference owned outside Learning;
* the published reason vocabulary is closed: the reasons of this
  component's own boundary (see :class:`OwnDenyReason`) plus the published
  denial reasons of IS-003 passed through unchanged. A refusal outside that
  vocabulary cannot happen: a non-authoritative answer of the dependency is
  reported as ``authorization_unavailable`` and denied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from learning_service.consumed import DENY_REASONS as _DECISION_DENIALS
from learning_service.errors import AccessRefused, ConfigurationError, ContractViolation

#: This component: the single data owner of every record it serves.
OWNER_COMPONENT = "learning"

#: Resource types this component owns in this slice.
RESOURCE_TYPE_ASSIGNMENT = "assignment"
RESOURCE_TYPE_SUBMISSION = "submission"
RESOURCE_TYPE_COURSE = "course"
RESOURCE_TYPE_MODULE = "module"
RESOURCE_TYPE_LESSON = "lesson"

#: The grant vocabulary the data owner asks IS-003 about. The two read
#: operations change no state beyond the append-only audit journal; the review
#: command is the state-changing operation of Slice 3, guarded by IS-005.
#: The teacher discovery operation is the Slice 2 addition; the single read is
#: the Slice 1 prerequisite it builds on.
OPERATION_READ = "learning.submissions.read"
OPERATION_LIST = "learning.submissions.list"
OPERATION_REVIEW = "learning.submissions.review"

#: The Content Authoring grant vocabulary (Issue #31). One operation, one
#: grant, one question per access — no policy logic lives inside Learning:
#: a Teacher is the subject that holds the authoring grants, a Student the
#: subject that holds the published-content read.
OPERATION_COURSE_CREATE = "learning.courses.create"
OPERATION_MODULE_CREATE = "learning.modules.create"
OPERATION_LESSON_CREATE = "learning.lessons.create"
OPERATION_ASSIGNMENT_CREATE = "learning.assignments.create"
OPERATION_COURSE_PUBLISH = "learning.courses.publish"
OPERATION_COURSE_ARCHIVE = "learning.courses.archive"

#: The hierarchy-read questions. A PUBLISHED Course is read through
#: ``learning.courses.read`` (granted to Teachers and Students). A hierarchy
#: that is not published — ``DRAFT`` or ``ARCHIVED`` — is read through
#: ``learning.courses.read_unpublished`` (a Teacher grant): the state of the
#: course, a fact of this component's store, selects which question is asked,
#: exactly as the resource's own Tenant does.
OPERATION_COURSE_READ = "learning.courses.read"
OPERATION_COURSE_READ_UNPUBLISHED = "learning.courses.read_unpublished"


class OwnDenyReason:
    """Reasons of this component's own enforcement boundary (closed set).

    ``assignment_unknown`` — no such assignment is owned here.
    ``submission_unknown`` — no such submission is owned here.
    ``course_unknown`` / ``module_unknown`` / ``lesson_unknown`` — no such
    course, module or lesson is owned here.
    ``owner_mismatch`` — a record whose single owner is another component
    can never be served through this boundary.
    ``authorization_unavailable`` — the decision dependency did not answer,
    or answered outside its published contract: fail closed.
    ``invalid_state_transition`` — a domain refusal after an ALLOW: only a
    ``SUBMITTED`` submission may be reviewed and review never changes the
    lifecycle state; a child is created only under a ``DRAFT`` parent;
    publication applies only to a structurally valid ``DRAFT`` hierarchy;
    archive applies only to a ``PUBLISHED`` hierarchy.
    ``already_reviewed`` — a domain refusal after an ALLOW: the review fact
    is immutable, so a review command against an already reviewed submission
    (a different command, i.e. a different ``Idempotency-Key``) can never
    overwrite ``reviewed_by`` / ``reviewed_at``.
    ``idempotency_key_required`` — a state-changing command was sent without
    the mandatory ``Idempotency-Key`` header.
    ``idempotency_conflict`` — the ``Idempotency-Key`` was already used with
    a different binding (identity, tenant, operation, target or command).
    ``validation_error`` — the command payload is schema-valid but violates
    the content rules of the capability (empty title, oversized text,
    negative position): a refusal before any access decision.
    """

    ASSIGNMENT_UNKNOWN = "assignment_unknown"
    SUBMISSION_UNKNOWN = "submission_unknown"
    COURSE_UNKNOWN = "course_unknown"
    MODULE_UNKNOWN = "module_unknown"
    LESSON_UNKNOWN = "lesson_unknown"
    OWNER_MISMATCH = "owner_mismatch"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    ALREADY_REVIEWED = "already_reviewed"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    VALIDATION_ERROR = "validation_error"


#: The closed set of own reasons, as values.
OWN_DENY_REASONS: frozenset[str] = frozenset(
    {
        OwnDenyReason.ASSIGNMENT_UNKNOWN,
        OwnDenyReason.SUBMISSION_UNKNOWN,
        OwnDenyReason.COURSE_UNKNOWN,
        OwnDenyReason.MODULE_UNKNOWN,
        OwnDenyReason.LESSON_UNKNOWN,
        OwnDenyReason.OWNER_MISMATCH,
        OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.ALREADY_REVIEWED,
        OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
        OwnDenyReason.IDEMPOTENCY_CONFLICT,
        OwnDenyReason.VALIDATION_ERROR,
    }
)

#: Published denial reasons of IS-003 this boundary passes through unchanged
#: when the decision denies: the fact itself belongs to the authority.
PASSED_THROUGH_DENIALS: frozenset[str] = _DECISION_DENIALS

#: Every reason a published refusal may carry.
PUBLISHED_DENY_REASONS: frozenset[str] = OWN_DENY_REASONS | PASSED_THROUGH_DENIALS


@dataclass(frozen=True, slots=True)
class SubmissionView:
    """The published representation of one owned submission.

    Exactly the ten business fields — ``submission_id``,
    ``assignment_id``, ``student_identity_id``, ``attempt``, ``content``,
    ``status``, ``created_at``, ``updated_at``, ``reviewed_by``,
    ``reviewed_at`` — and nothing else. No tenant, owner or profile data is
    exposed: the view is the value an allowed access returns, immutable and
    granting nothing by existing. ``reviewed_by`` / ``reviewed_at`` are
    ``None`` until the first successful review and are the only review fact
    published.
    """

    submission_id: str
    assignment_id: str
    student_identity_id: str
    attempt: int
    content: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentView:
    """The published representation of one authored Assignment.

    Exactly the hierarchy fields — ``assignment_id``, ``lesson_id``,
    ``title``, ``instructions``, ``status`` — and nothing else: no tenant,
    owner or submission data. This is the same Assignment identity the
    submission flow serves; no second Assignment model exists.
    """

    assignment_id: str
    lesson_id: str
    title: str
    instructions: str
    status: str


@dataclass(frozen=True, slots=True)
class LessonView:
    """The published representation of one authored Lesson and its assignments."""

    lesson_id: str
    module_id: str
    title: str
    content: str
    position: int
    status: str
    assignments: tuple[AssignmentView, ...] = ()


@dataclass(frozen=True, slots=True)
class ModuleView:
    """The published representation of one authored Module and its lessons."""

    module_id: str
    course_id: str
    title: str
    position: int
    status: str
    lessons: tuple[LessonView, ...] = ()


@dataclass(frozen=True, slots=True)
class CourseView:
    """The published representation of one Course hierarchy.

    ``modules`` nests the full ``Course → Module → Lesson → Assignment``
    chain in deterministic order, which is what makes the single hierarchy
    read sufficient for navigation. No tenant or owner data is exposed: the
    view is the value an allowed access returns, immutable and granting
    nothing by existing.
    """

    course_id: str
    title: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str
    modules: tuple[ModuleView, ...] = ()


__all__ = [
    "OPERATION_ASSIGNMENT_CREATE",
    "OPERATION_COURSE_ARCHIVE",
    "OPERATION_COURSE_CREATE",
    "OPERATION_COURSE_PUBLISH",
    "OPERATION_COURSE_READ",
    "OPERATION_COURSE_READ_UNPUBLISHED",
    "OPERATION_LESSON_CREATE",
    "OPERATION_LIST",
    "OPERATION_MODULE_CREATE",
    "OPERATION_READ",
    "OPERATION_REVIEW",
    "OWN_DENY_REASONS",
    "OwnDenyReason",
    "OWNER_COMPONENT",
    "PASSED_THROUGH_DENIALS",
    "PUBLISHED_DENY_REASONS",
    "RESOURCE_TYPE_ASSIGNMENT",
    "RESOURCE_TYPE_COURSE",
    "RESOURCE_TYPE_LESSON",
    "RESOURCE_TYPE_MODULE",
    "RESOURCE_TYPE_SUBMISSION",
    "AccessRefused",
    "AssignmentView",
    "ConfigurationError",
    "ContractViolation",
    "CourseView",
    "LessonView",
    "ModuleView",
    "SubmissionView",
]
