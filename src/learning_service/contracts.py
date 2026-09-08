"""Published data contract of the Learning component.

This module publishes the whole vocabulary a consumer needs: the closed set
of machine-readable error codes with their HTTP statuses, the immutable
representations an allowed operation returns, and the error classes.
Internal modules (``engine``, ``store``, ``models``, ``api``, ``deployment``,
``transport``, ``ports``, ``adapters``) are not part of the contract and must
not be imported by another component (ARCHITECTURE.md §1.1, LAW-04).

The machine-readable API contract is
``components/learning/contract/openapi.yaml``; this module is its code-side
mirror, and the contract tests keep the two from drifting.

Three facts about the model are deliberate:

* every representation is exactly the published fields and nothing more —
  lifecycle state, ownership and timestamps are facts of this owner, never
  client-supplied values echoed back;
* the error vocabulary is closed: fourteen codes, each mapped to exactly one
  HTTP status, and every refusal carries one of them;
* the authorization operation vocabulary is the only way this component asks
  IS-003 — roles such as Teacher or Student exist only as grants of these
  operations inside IS-003, not as a second mechanism here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from learning_service.errors import AccessRefused, ConfigurationError, ContractViolation

#: This component: the single data owner of everything it serves.
OWNER_COMPONENT = "learning"

#: The published API base path of this slice.
API_BASE_PATH = "/api/v1/learning"

# --------------------------------------------------------------- operations
#
# The authorization vocabulary asked of IS-003. Teacher and Student are not
# roles implemented here: they are grants of these operations inside IS-003
# (invariant 15 — no second authorization mechanism).
OPERATION_COURSE_WRITE = "learning.course.write"
OPERATION_COURSE_READ = "learning.course.read"
OPERATION_ASSIGNMENT_READ = "learning.assignment.read"
OPERATION_ENROLLMENT_WRITE = "learning.enrollment.write"
OPERATION_ENROLLMENT_READ = "learning.enrollment.read"
OPERATION_SUBMISSION_WRITE = "learning.submission.write"
OPERATION_SUBMISSION_READ = "learning.submission.read"

#: A Teacher of a Tenant: authors and publishes content, reads submissions.
TEACHER_OPERATIONS: tuple[str, ...] = (
    OPERATION_COURSE_WRITE,
    OPERATION_COURSE_READ,
    OPERATION_ASSIGNMENT_READ,
    OPERATION_SUBMISSION_READ,
)

#: A Student of a Tenant: enrolls into published courses and submits work.
STUDENT_OPERATIONS: tuple[str, ...] = (
    OPERATION_COURSE_READ,
    OPERATION_ASSIGNMENT_READ,
    OPERATION_ENROLLMENT_WRITE,
    OPERATION_ENROLLMENT_READ,
    OPERATION_SUBMISSION_WRITE,
    OPERATION_SUBMISSION_READ,
)

# ------------------------------------------------------------- error codes
class ErrorCode(StrEnum):
    """Machine-readable codes of every refusal — a closed, published set.

    Each code maps to exactly one HTTP status
    (:data:`ERROR_STATUS`); the mapping is part of this contract.
    """

    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    RESOURCE_OWNER_MISMATCH = "RESOURCE_OWNER_MISMATCH"

    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    COURSE_NOT_PUBLISHED = "COURSE_NOT_PUBLISHED"
    ASSIGNMENT_NOT_PUBLISHED = "ASSIGNMENT_NOT_PUBLISHED"
    ENROLLMENT_REQUIRED = "ENROLLMENT_REQUIRED"
    DUPLICATE_ENROLLMENT = "DUPLICATE_ENROLLMENT"

    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    VALIDATION_ERROR = "VALIDATION_ERROR"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


#: HTTP status of each published error code. One code — one status, always.
ERROR_STATUS: dict[str, int] = {
    ErrorCode.AUTHENTICATION_REQUIRED.value: 401,
    ErrorCode.AUTHORIZATION_DENIED.value: 403,
    ErrorCode.TENANT_MISMATCH.value: 403,
    ErrorCode.RESOURCE_NOT_FOUND.value: 404,
    ErrorCode.RESOURCE_OWNER_MISMATCH.value: 403,
    ErrorCode.INVALID_STATE_TRANSITION.value: 409,
    ErrorCode.COURSE_NOT_PUBLISHED.value: 409,
    ErrorCode.ASSIGNMENT_NOT_PUBLISHED.value: 409,
    ErrorCode.ENROLLMENT_REQUIRED.value: 409,
    ErrorCode.DUPLICATE_ENROLLMENT.value: 409,
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED.value: 400,
    ErrorCode.IDEMPOTENCY_CONFLICT.value: 409,
    ErrorCode.VALIDATION_ERROR.value: 422,
    ErrorCode.DEPENDENCY_UNAVAILABLE.value: 503,
}

#: Static, safe refusal messages. A message never depends on the payload, a
#: stack trace, a dependency answer or another tenant's data.
ERROR_MESSAGES: dict[str, str] = {
    ErrorCode.AUTHENTICATION_REQUIRED.value: "A verified identity is required.",
    ErrorCode.AUTHORIZATION_DENIED.value: "Operation is not permitted.",
    ErrorCode.TENANT_MISMATCH.value: "The resource belongs to another tenant.",
    ErrorCode.RESOURCE_NOT_FOUND.value: "The resource is not available to the caller.",
    ErrorCode.RESOURCE_OWNER_MISMATCH.value: "The resource is owned by another component.",
    ErrorCode.INVALID_STATE_TRANSITION.value: "The operation does not apply to the current state.",
    ErrorCode.COURSE_NOT_PUBLISHED.value: "The course is not published.",
    ErrorCode.ASSIGNMENT_NOT_PUBLISHED.value: "The assignment is not published.",
    ErrorCode.ENROLLMENT_REQUIRED.value: "An active enrollment in the course is required.",
    ErrorCode.DUPLICATE_ENROLLMENT.value: "The student is already enrolled.",
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED.value: "An Idempotency-Key header is required for this command.",
    ErrorCode.IDEMPOTENCY_CONFLICT.value: "The Idempotency-Key was already used for a different command.",
    ErrorCode.VALIDATION_ERROR.value: "The request does not satisfy the published schema.",
    ErrorCode.DEPENDENCY_UNAVAILABLE.value: "A required dependency did not answer; the operation is refused.",
}

#: The closed set of published codes, as plain values.
PUBLISHED_ERROR_CODES: frozenset[str] = frozenset(ErrorCode)


# ------------------------------------------------------------ representations
@dataclass(frozen=True, slots=True)
class CourseView:
    """The published representation of one course — the contract fields only."""

    course_id: str
    tenant_id: str
    title: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ModuleView:
    """The published representation of one module of a course."""

    module_id: str
    course_id: str
    tenant_id: str
    title: str
    position: int
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class LessonView:
    """The published representation of one lesson of a module."""

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
class AssignmentView:
    """The published representation of one assignment of a lesson.

    Exactly the fields of the published contract: identity, ownership, the
    two content fields and the lifecycle state.
    """

    assignment_id: str
    lesson_id: str
    tenant_id: str
    title: str
    instructions: str
    status: str


@dataclass(frozen=True, slots=True)
class EnrollmentView:
    """The published representation of one enrollment."""

    enrollment_id: str
    tenant_id: str
    course_id: str
    student_identity_id: str
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class SubmissionView:
    """The published representation of one submission.

    No grading or evaluation result is part of this slice.
    """

    submission_id: str
    tenant_id: str
    assignment_id: str
    student_identity_id: str
    attempt: int
    content: str
    status: str
    created_at: str
    updated_at: str


__all__ = [
    "API_BASE_PATH",
    "ERROR_MESSAGES",
    "ERROR_STATUS",
    "ErrorCode",
    "OPERATION_ASSIGNMENT_READ",
    "OPERATION_COURSE_READ",
    "OPERATION_COURSE_WRITE",
    "OPERATION_ENROLLMENT_READ",
    "OPERATION_ENROLLMENT_WRITE",
    "OPERATION_SUBMISSION_READ",
    "OPERATION_SUBMISSION_WRITE",
    "OWNER_COMPONENT",
    "PUBLISHED_ERROR_CODES",
    "STUDENT_OPERATIONS",
    "TEACHER_OPERATIONS",
    "AccessRefused",
    "AssignmentView",
    "ConfigurationError",
    "ContractViolation",
    "CourseView",
    "EnrollmentView",
    "LessonView",
    "ModuleView",
    "SubmissionView",
]
