"""Learning engine: the enforcement chain of the business data owner.

This component owns its business data and is the final enforcement boundary
for it (ARCHITECTURE.md §5.3, §6.2). Every published operation — read or
command — runs one fixed chain, and every step can only refuse:

```text
Request (subject credential, operation, payload)
      ↓
identity_context       IS-001 verifies the credential and answers the
                       verified identity + effective tenant; no answer,
                       no operation                      else refuse
      ↓
effective_tenant       the Tenant of the request is the one IS-001 derived;
                       a caller cannot name one (LAW-16a)
      ↓
ownership_boundary     the addressed entity exists in this store and belongs
                       to the effective tenant; a command that creates data
                       binds the new entity to the effective tenant
      ↓
authorization_decision IS-003 decides through the port; the default outcome
                       is DENY; a dependency that does not answer, or answers
                       outside its published vocabulary, fails closed
      ↓
owned_data_operation   the business effect, and only here — inside the
                       IS-005 command-safety guard for every state-changing
                       command, so an exact replay replays and never repeats
```

An ``ALLOW`` from IS-003 is not data access: the owned-data operation runs
only at the end of the chain. Every refusal and every served operation is
audited with ``request_id`` and ``correlation_id`` (ARCHITECTURE.md §27).

There is no path that executes a required command without IS-005: every
command method takes ``idempotency_key`` as a required argument, refuses an
empty one, and hands the effect to the guard. An authorization denial or a
failed precondition raises inside the guarded effect, so no idempotency
record and no business effect is created (I-006 of IS-005).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from idempotency.errors import IdempotencyConflict
from idempotency.guard import IdempotencyGuard

from learning_service import COMPONENT_ID, COMPONENT_VERSION
from learning_service.config import LearningConfig
from learning_service.consumed import (
    ALLOW,
    DENY,
    KNOWN_IDENTITY_REASONS,
    KNOWN_REASONS,
    PERMITTED,
    TENANT_CONTEXT_SOURCE,
    DecisionAnswer,
    DependencyRefusal,
    SubjectContext,
)
from learning_service.contracts import (
    ERROR_STATUS,
    OPERATION_ASSIGNMENT_READ,
    OPERATION_COURSE_READ,
    OPERATION_COURSE_WRITE,
    OPERATION_ENROLLMENT_READ,
    OPERATION_ENROLLMENT_WRITE,
    OPERATION_SUBMISSION_READ,
    OPERATION_SUBMISSION_WRITE,
    ErrorCode,
)
from learning_service.errors import AccessRefused
from learning_service.models import (
    AccessAuditEvent,
    Assignment,
    Course,
    Enrollment,
    Lesson,
    Module,
    ObservabilityContext,
    Submission,
)
from learning_service.ports import AuthorizationPort, IdentityContextPort
from learning_service.store import DomainRefusal, LearningStore

#: The published order of enforcement. Also used by the contract tests: a
#: change of order is a change of behaviour and must be visible.
ENFORCEMENT_CHAIN: tuple[str, ...] = (
    "identity_context",
    "effective_tenant",
    "ownership_boundary",
    "authorization_decision",
    "owned_data_operation",
)

#: Resource types this component states in its questions to IS-003.
RESOURCE_COURSE = "learning.course"
RESOURCE_MODULE = "learning.module"
RESOURCE_LESSON = "learning.lesson"
RESOURCE_ASSIGNMENT = "learning.assignment"
RESOURCE_SUBMISSION = "learning.submission"

#: The audit action of every command (the business command, not an HTTP route).
COMMAND_CREATE_COURSE = "learning.create_course"
COMMAND_PUBLISH_COURSE = "learning.publish_course"
COMMAND_CREATE_MODULE = "learning.create_module"
COMMAND_CREATE_LESSON = "learning.create_lesson"
COMMAND_CREATE_ASSIGNMENT = "learning.create_assignment"
COMMAND_ENROLL = "learning.enroll"
COMMAND_CREATE_SUBMISSION = "learning.create_submission"

#: The audit action of every read.
ACTION_READ_COURSE = "learning.read_course"
ACTION_READ_ASSIGNMENT = "learning.read_assignment"
ACTION_READ_ENROLLMENT = "learning.read_enrollment"
ACTION_READ_SUBMISSION = "learning.read_submission"

#: The audit action of a request the published schema rejected before any
#: handler ran: an operation attempt that could not even be read.
ACTION_UNREADABLE = "learning.access"

#: Identity-family denial reasons of IS-001/IS-003: the subject was not
#: established or no tenant context exists — the request cannot enter the
#: business operation at all.
_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity", "missing_tenant_context"}
)

#: Cross-tenant denials: the fact belongs to data ownership at this boundary.
_TENANT_MISMATCH_DENIALS = frozenset({"tenant_mismatch", "resource_tenant_mismatch"})

#: Any other published denial of the authorities: authenticated, but refused.
#: (tenant lifecycle, missing permission, …)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fingerprint(command: str, target: str | None, payload: Mapping[str, Any]) -> str:
    """The request fingerprint IS-005 binds the key to.

    Canonical JSON over command, target and payload: the same bytes for the
    same request, different bytes for any different one.
    """
    canonical = json.dumps(
        {"command": command, "target": target, "payload": dict(payload)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class LearningEngine:
    """The enforcement boundary of one Learning deployment in one Platform Instance.

    It owns the business data and the audit journal, and nothing else: no
    identity verification, no tenant derivation, no permission logic and no
    idempotency mechanism of its own — each is consumed through its port.
    """

    store: LearningStore
    config: LearningConfig
    identity: IdentityContextPort
    authorization: AuthorizationPort
    clock: Callable[[], str] = field(default=_utc_now)
    guard: IdempotencyGuard | None = field(default=None)

    def __post_init__(self) -> None:
        # No second idempotency mechanism: the default is the IS-005 guard,
        # auditing into this component's own journal.
        if self.guard is None:
            self.guard = IdempotencyGuard(audit_sink=self._guard_audit)

    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (never a caller claim)."""
        return self.config.platform_id

    # --------------------------------------------------------------- commands
    def create_course(
        self,
        subject_credential: str | None,
        *,
        title: str,
        description: str,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Create one course in the effective tenant: ``status = DRAFT``."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            course_id = _new_id("crs")
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_COURSE_WRITE,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
                resource_tenant_id=subject.tenant_id,
                action=COMMAND_CREATE_COURSE,
                obs=obs,
            )
            now = self.clock()
            course = self.store.save_course(
                Course(
                    course_id=course_id,
                    tenant_id=subject.tenant_id,
                    title=title,
                    description=description,
                    status="DRAFT",
                    created_by=subject.identity_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            return _course_view(course)

        return self._command(
            command=COMMAND_CREATE_COURSE,
            target=None,
            payload={"title": title, "description": description},
            subject_credential=subject_credential,
            idempotency_key=idempotency_key,
            resource_type=RESOURCE_COURSE,
            resource_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def publish_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Publish one course: the only allowed transition is ``DRAFT → PUBLISHED``."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            course = self.store.course(course_id)
            if course is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=COMMAND_PUBLISH_COURSE,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            if course.tenant_id != subject.tenant_id:
                raise self._refuse(
                    ErrorCode.TENANT_MISMATCH,
                    action=COMMAND_PUBLISH_COURSE,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_COURSE_WRITE,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                action=COMMAND_PUBLISH_COURSE,
                obs=obs,
            )
            try:
                published, _cascade = self.store.apply_publish(course_id, timestamp=self.clock())
            except KeyError:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=COMMAND_PUBLISH_COURSE,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                    details={"refused": "vanished_after_decision"},
                ) from None
            except DomainRefusal as exc:
                raise self._refuse(
                    exc.code,
                    action=COMMAND_PUBLISH_COURSE,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                    details={"course_status": course.status},
                ) from None
            return _course_view(published)

        return self._command(
            command=COMMAND_PUBLISH_COURSE,
            target=course_id,
            payload={},
            subject_credential=subject_credential,
            idempotency_key=idempotency_key,
            resource_type=RESOURCE_COURSE,
            resource_id=course_id,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def create_module(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        title: str,
        position: int,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Create one module of a course; refused once the course is PUBLISHED."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            course = self._owned_course(
                course_id,
                action=COMMAND_CREATE_MODULE,
                subject=subject,
                obs=obs,
            )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_COURSE_WRITE,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                action=COMMAND_CREATE_MODULE,
                obs=obs,
            )
            self._require_structurable_course(course, COMMAND_CREATE_MODULE, subject, obs)
            now = self.clock()
            module = self.store.save_module(
                Module(
                    module_id=_new_id("mod"),
                    course_id=course_id,
                    tenant_id=subject.tenant_id,
                    title=title,
                    position=position,
                    status="DRAFT",
                    created_at=now,
                    updated_at=now,
                )
            )
            return _module_view(module)

        return self._command(
            command=COMMAND_CREATE_MODULE,
            target=course_id,
            payload={"title": title, "position": position},
            subject_credential=subject_credential,
            idempotency_key=idempotency_key,
            resource_type=RESOURCE_COURSE,
            resource_id=course_id,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def create_lesson(
        self,
        subject_credential: str | None,
        module_id: str,
        *,
        title: str,
        content: str,
        position: int,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Create one lesson of a module; refused once the course is PUBLISHED."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            module = self._owned_module(module_id, COMMAND_CREATE_LESSON, subject, obs)
            course = self._course_of_module(module, COMMAND_CREATE_LESSON, subject, obs)
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_COURSE_WRITE,
                resource_type=RESOURCE_MODULE,
                resource_id=module_id,
                resource_tenant_id=module.tenant_id,
                action=COMMAND_CREATE_LESSON,
                obs=obs,
            )
            self._require_structurable_course(course, COMMAND_CREATE_LESSON, subject, obs)
            now = self.clock()
            lesson = self.store.save_lesson(
                Lesson(
                    lesson_id=_new_id("les"),
                    module_id=module_id,
                    tenant_id=subject.tenant_id,
                    title=title,
                    content=content,
                    position=position,
                    status="DRAFT",
                    created_at=now,
                    updated_at=now,
                )
            )
            return _lesson_view(lesson)

        return self._command(
            command=COMMAND_CREATE_LESSON,
            target=module_id,
            payload={"title": title, "content": content, "position": position},
            subject_credential=subject_credential,
            idempotency_key=idempotency_key,
            resource_type=RESOURCE_MODULE,
            resource_id=module_id,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def create_assignment(
        self,
        subject_credential: str | None,
        lesson_id: str,
        *,
        title: str,
        instructions: str,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Create one assignment of a lesson; refused once the course is PUBLISHED."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            lesson = self._owned_lesson(lesson_id, COMMAND_CREATE_ASSIGNMENT, subject, obs)
            course = self._course_of_lesson(lesson, COMMAND_CREATE_ASSIGNMENT, subject, obs)
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_COURSE_WRITE,
                resource_type=RESOURCE_LESSON,
                resource_id=lesson_id,
                resource_tenant_id=lesson.tenant_id,
                action=COMMAND_CREATE_ASSIGNMENT,
                obs=obs,
            )
            self._require_structurable_course(course, COMMAND_CREATE_ASSIGNMENT, subject, obs)
            assignment = self.store.save_assignment(
                Assignment(
                    assignment_id=_new_id("asg"),
                    lesson_id=lesson_id,
                    tenant_id=subject.tenant_id,
                    title=title,
                    instructions=instructions,
                    status="DRAFT",
                )
            )
            return _assignment_view(assignment)

        return self._command(
            command=COMMAND_CREATE_ASSIGNMENT,
            target=lesson_id,
            payload={"title": title, "instructions": instructions},
            subject_credential=subject_credential,
            idempotency_key=idempotency_key,
            resource_type=RESOURCE_LESSON,
            resource_id=lesson_id,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def enroll(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Enroll the verified identity into a published course: ``status = ACTIVE``."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            course = self.store.course(course_id)
            if course is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=COMMAND_ENROLL,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            if course.tenant_id != subject.tenant_id:
                raise self._refuse(
                    ErrorCode.TENANT_MISMATCH,
                    action=COMMAND_ENROLL,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_ENROLLMENT_WRITE,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                action=COMMAND_ENROLL,
                obs=obs,
            )
            if course.status != "PUBLISHED":
                raise self._refuse(
                    ErrorCode.COURSE_NOT_PUBLISHED,
                    action=COMMAND_ENROLL,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                    details={"course_status": course.status},
                )
            if self.store.enrollment_of(course_id, subject.identity_id) is not None:
                raise self._refuse(
                    ErrorCode.DUPLICATE_ENROLLMENT,
                    action=COMMAND_ENROLL,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            enrollment = self.store.save_enrollment(
                Enrollment(
                    enrollment_id=_new_id("enr"),
                    tenant_id=subject.tenant_id,
                    course_id=course_id,
                    student_identity_id=subject.identity_id,
                    status="ACTIVE",
                    created_at=self.clock(),
                )
            )
            return _enrollment_view(enrollment)

        return self._command(
            command=COMMAND_ENROLL,
            target=course_id,
            payload={},
            subject_credential=subject_credential,
            idempotency_key=idempotency_key,
            resource_type=RESOURCE_COURSE,
            resource_id=course_id,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def create_submission(
        self,
        subject_credential: str | None,
        assignment_id: str,
        *,
        attempt: int,
        content: str,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Submit one attempt of a student for a published assignment."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            assignment = self.store.assignment(assignment_id)
            if assignment is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=COMMAND_CREATE_SUBMISSION,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_ASSIGNMENT,
                    resource_id=assignment_id,
                )
            if assignment.tenant_id != subject.tenant_id:
                raise self._refuse(
                    ErrorCode.TENANT_MISMATCH,
                    action=COMMAND_CREATE_SUBMISSION,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_ASSIGNMENT,
                    resource_id=assignment_id,
                )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_SUBMISSION_WRITE,
                resource_type=RESOURCE_ASSIGNMENT,
                resource_id=assignment_id,
                resource_tenant_id=assignment.tenant_id,
                action=COMMAND_CREATE_SUBMISSION,
                obs=obs,
            )
            if assignment.status != "PUBLISHED":
                raise self._refuse(
                    ErrorCode.ASSIGNMENT_NOT_PUBLISHED,
                    action=COMMAND_CREATE_SUBMISSION,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_ASSIGNMENT,
                    resource_id=assignment_id,
                    details={"assignment_status": assignment.status},
                )
            course = self._course_of_assignment(
                assignment, subject, obs, COMMAND_CREATE_SUBMISSION
            )
            enrollment = self.store.enrollment_of(course.course_id, subject.identity_id)
            if enrollment is None or enrollment.status != "ACTIVE":
                raise self._refuse(
                    ErrorCode.ENROLLMENT_REQUIRED,
                    action=COMMAND_CREATE_SUBMISSION,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_ASSIGNMENT,
                    resource_id=assignment_id,
                    details={"course_id": course.course_id},
                )
            now = self.clock()
            submission = self.store.save_submission(
                Submission(
                    submission_id=_new_id("sub"),
                    tenant_id=subject.tenant_id,
                    assignment_id=assignment_id,
                    student_identity_id=subject.identity_id,
                    attempt=attempt,
                    content=content,
                    status="SUBMITTED",
                    created_at=now,
                    updated_at=now,
                )
            )
            return _submission_view(submission)

        return self._command(
            command=COMMAND_CREATE_SUBMISSION,
            target=assignment_id,
            payload={"attempt": attempt, "content": content},
            subject_credential=subject_credential,
            idempotency_key=idempotency_key,
            resource_type=RESOURCE_ASSIGNMENT,
            resource_id=assignment_id,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    # ------------------------------------------------------------------ reads
    def get_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Serve one course: a Student only when it is PUBLISHED, a Teacher always."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            course = self.store.course(course_id)
            if course is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=ACTION_READ_COURSE,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            if course.tenant_id != subject.tenant_id:
                raise self._refuse(
                    ErrorCode.TENANT_MISMATCH,
                    action=ACTION_READ_COURSE,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_COURSE_READ,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                action=ACTION_READ_COURSE,
                obs=obs,
            )
            if course.status != "PUBLISHED":
                # Not published: only the content-author capability may read it.
                # The question is asked live; its denial is the refusal.
                self._require_teacher(
                    subject_credential, subject, course, ACTION_READ_COURSE, obs
                )
            return _course_view(course)

        return self._read(
            action=ACTION_READ_COURSE,
            subject_credential=subject_credential,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def get_assignment(
        self,
        subject_credential: str | None,
        assignment_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Serve one assignment: a Student only when it is PUBLISHED."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            assignment = self.store.assignment(assignment_id)
            if assignment is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=ACTION_READ_ASSIGNMENT,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_ASSIGNMENT,
                    resource_id=assignment_id,
                )
            if assignment.tenant_id != subject.tenant_id:
                raise self._refuse(
                    ErrorCode.TENANT_MISMATCH,
                    action=ACTION_READ_ASSIGNMENT,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_ASSIGNMENT,
                    resource_id=assignment_id,
                )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_ASSIGNMENT_READ,
                resource_type=RESOURCE_ASSIGNMENT,
                resource_id=assignment_id,
                resource_tenant_id=assignment.tenant_id,
                action=ACTION_READ_ASSIGNMENT,
                obs=obs,
            )
            if assignment.status != "PUBLISHED":
                self._require_teacher(
                    subject_credential,
                    subject,
                    self._course_of_assignment(assignment, subject, obs, ACTION_READ_ASSIGNMENT),
                    ACTION_READ_ASSIGNMENT,
                    obs,
                )
            return _assignment_view(assignment)

        return self._read(
            action=ACTION_READ_ASSIGNMENT,
            subject_credential=subject_credential,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def get_my_enrollment(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Serve the enrollment of the verified identity in one course — and only that one."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            course = self.store.course(course_id)
            if course is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=ACTION_READ_ENROLLMENT,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            if course.tenant_id != subject.tenant_id:
                raise self._refuse(
                    ErrorCode.TENANT_MISMATCH,
                    action=ACTION_READ_ENROLLMENT,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_ENROLLMENT_READ,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                action=ACTION_READ_ENROLLMENT,
                obs=obs,
            )
            enrollment = self.store.enrollment_of(course_id, subject.identity_id)
            if enrollment is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=ACTION_READ_ENROLLMENT,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_COURSE,
                    resource_id=course_id,
                    details={"refused": "no_own_enrollment"},
                )
            return _enrollment_view(enrollment)

        return self._read(
            action=ACTION_READ_ENROLLMENT,
            subject_credential=subject_credential,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    def get_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Serve one submission: its own student, or a Teacher of the Tenant."""

        def run(subject: SubjectContext, obs: ObservabilityContext) -> Any:
            submission = self.store.submission(submission_id)
            if submission is None:
                raise self._refuse(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    action=ACTION_READ_SUBMISSION,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_SUBMISSION,
                    resource_id=submission_id,
                )
            if submission.tenant_id != subject.tenant_id:
                raise self._refuse(
                    ErrorCode.TENANT_MISMATCH,
                    action=ACTION_READ_SUBMISSION,
                    obs=obs,
                    identity_id=subject.identity_id,
                    tenant_id=subject.tenant_id,
                    resource_type=RESOURCE_SUBMISSION,
                    resource_id=submission_id,
                )
            self._authorize(
                subject_credential,
                subject=subject,
                operation=OPERATION_SUBMISSION_READ,
                resource_type=RESOURCE_SUBMISSION,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                action=ACTION_READ_SUBMISSION,
                obs=obs,
            )
            if submission.student_identity_id != subject.identity_id:
                # Not the submission's own student: only the content-author
                # capability may read it. The question is asked live.
                self._require_teacher(
                    subject_credential,
                    subject,
                    self._course_of_submission(submission, subject, obs),
                    ACTION_READ_SUBMISSION,
                    obs,
                )
            return _submission_view(submission)

        return self._read(
            action=ACTION_READ_SUBMISSION,
            subject_credential=subject_credential,
            request_id=request_id,
            correlation_id=correlation_id,
            run=run,
        )

    # ------------------------------------------------------- the chain itself
    def _command(
        self,
        *,
        command: str,
        target: str | None,
        payload: Mapping[str, Any],
        subject_credential: str | None,
        idempotency_key: str,
        resource_type: str,
        resource_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
        run: Callable[[SubjectContext, ObservabilityContext], Any],
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Run one state-changing command through identity, IS-005 and the chain.

        ``idempotency_key`` is a required argument of every command method and
        is refused when empty *before* anything else happens: there is no
        silent path that executes the command without idempotency. The whole
        business effect — authorization included — runs inside the guard, so
        a denial creates neither a record nor an effect, and an exact replay
        returns the stored result without executing anything.
        """
        obs = self.observability(request_id=request_id, correlation_id=correlation_id)
        key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if not key:
            raise self._refuse(
                ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
                action=command,
                obs=obs,
                identity_id=None,
                tenant_id=None,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        subject = self._authenticate(
            subject_credential,
            action=command,
            obs=obs,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        obs = self.observability(
            tenant_id=subject.tenant_id,
            subject_id=subject.identity_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        assert self.guard is not None  # set in __post_init__
        try:
            served = self.guard.execute(
                key,
                identity=subject.identity_id,
                tenant_id=subject.tenant_id,
                operation=command,
                resource=target,
                fingerprint=_fingerprint(command, target, payload),
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=lambda: run(subject, obs),
            )
        except IdempotencyConflict as exc:
            raise self._refuse(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                action=command,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details={"stated_code": "idempotency_conflict"},
            ) from exc
        event = self.audit(
            action=command,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            identity_id=subject.identity_id,
            tenant_id=subject.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return served, obs, event

    def _read(
        self,
        *,
        action: str,
        subject_credential: str | None,
        request_id: str | None,
        correlation_id: str | None,
        run: Callable[[SubjectContext, ObservabilityContext], Any],
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """Run one read through the chain. A read changes no business state."""
        obs = self.observability(request_id=request_id, correlation_id=correlation_id)
        subject = self._authenticate(
            subject_credential,
            action=action,
            obs=obs,
            resource_type=None,
            resource_id=None,
        )
        obs = self.observability(
            tenant_id=subject.tenant_id,
            subject_id=subject.identity_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        served = run(subject, obs)
        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            identity_id=subject.identity_id,
            tenant_id=subject.tenant_id,
            resource_type=None,
            resource_id=None,
        )
        return served, obs, event

    def _authenticate(
        self,
        subject_credential: str | None,
        *,
        action: str,
        obs: ObservabilityContext,
        resource_type: str | None,
        resource_id: str | None,
    ) -> SubjectContext:
        """Step 1: IS-001 verifies the credential and derives the effective tenant.

        The effective tenant is whatever IS-001 answers — never a caller
        claim, and there is no argument here that could carry one (LAW-16a).
        """
        try:
            answer = self.identity.resolve_context(
                subject_credential,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:
            # The port is declared total, but the boundary does not trust the
            # declaration: a port that raises instead of answering is a port
            # that did not answer — fail closed (only the machine-readable
            # code it stated, if any, is kept as context).
            code = getattr(exc, "reason", None)
            code = getattr(code, "value", code)
            answer = DependencyRefusal(code if isinstance(code, str) and code else None)
        if isinstance(answer, DependencyRefusal):
            code = answer.reason_code
            if code in KNOWN_IDENTITY_REASONS:
                raise self._refuse(
                    _identity_refusal_code(code),
                    action=action,
                    obs=obs,
                    identity_id=None,
                    tenant_id=None,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details={"stated_code": code},
                )
            # No answer, or an answer outside the published vocabulary: fail
            # closed — the request cannot enter the business operation.
            raise self._refuse(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                action=action,
                obs=obs,
                identity_id=None,
                tenant_id=None,
                resource_type=resource_type,
                resource_id=resource_id,
                details={"stated_code": code},
            )
        if answer.source != TENANT_CONTEXT_SOURCE:
            # A context that does not prove where its tenant came from is not
            # a context this boundary may act on.
            raise self._refuse(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                action=action,
                obs=obs,
                identity_id=None,
                tenant_id=None,
                resource_type=resource_type,
                resource_id=resource_id,
                details={"nonauthoritative_answer": {"source": answer.source}},
            )
        return answer

    def _authorize(
        self,
        subject_credential: str | None,
        *,
        subject: SubjectContext,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str,
        action: str,
        obs: ObservabilityContext,
    ) -> DecisionAnswer:
        """Step 3: the decision of IS-003 through the port.

        The resource's owning Tenant is stated from this component's own
        store, never from a caller claim. A DENY with a published reason is
        mapped onto the contract's error code; a dependency that does not
        answer, or answers outside the published vocabulary, fails closed.
        """
        try:
            answer = self.authorization.decide(
                subject_credential,
                operation=operation,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:
            # The port is declared total, but the boundary does not trust the
            # declaration: a port that raises instead of answering is a port
            # that did not answer — fail closed.
            code = getattr(exc, "reason", None)
            code = getattr(code, "value", code)
            answer = DependencyRefusal(code if isinstance(code, str) and code else None)
        if isinstance(answer, DependencyRefusal):
            raise self._refuse(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details={"stated_code": answer.reason_code},
            )
        decision = getattr(answer, "decision", None)
        reason = getattr(answer, "reason", None)
        decision = getattr(decision, "value", decision)
        reason = getattr(reason, "value", reason)
        if not (isinstance(decision, str) and isinstance(reason, str)):
            # An answer that is not even readable is not an answer.
            raise self._refuse(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details={"nonauthoritative_answer": {"answer_type": type(answer).__name__}},
            )
        if decision == DENY and reason in KNOWN_REASONS:
            raise self._refuse(
                _decision_refusal_code(reason),
                action=action,
                obs=obs,
                identity_id=getattr(answer, "subject_id", None) or subject.identity_id,
                tenant_id=getattr(answer, "tenant_id", None) or subject.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details={"authority_reason": reason},
            )
        if not (decision == ALLOW and reason == PERMITTED):
            # ALLOW with a wrong reason, a DENY with an unknown one, an unknown
            # decision value: a non-authoritative answer is not an access
            # grant, whatever direction it points in — fail closed.
            raise self._refuse(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                action=action,
                obs=obs,
                identity_id=getattr(answer, "subject_id", None) or subject.identity_id,
                tenant_id=getattr(answer, "tenant_id", None) or subject.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details={
                    "nonauthoritative_answer": {
                        "decision": decision,
                        "reason": reason,
                    }
                },
            )
        return answer

    def _require_teacher(
        self,
        subject_credential: str | None,
        subject: SubjectContext,
        course: Course,
        action: str,
        obs: ObservabilityContext,
    ) -> None:
        """Require the content-author capability, decided by IS-003 — never inferred here.

        Used for the two visibility rules of the contract: a Teacher may read
        entities that are not PUBLISHED, and a Teacher may read submissions
        that are not the caller's own. The question is asked live, per
        access, and its denial is the refusal — nothing is cached and
        nothing is inferred from the kind of the identity.
        """
        self._authorize(
            subject_credential,
            subject=subject,
            operation=OPERATION_COURSE_WRITE,
            resource_type=RESOURCE_COURSE,
            resource_id=course.course_id,
            resource_tenant_id=course.tenant_id,
            action=action,
            obs=obs,
        )

    # --------------------------------------------------- ownership helpers
    def _owned_course(
        self, course_id: str, action: str, subject: SubjectContext, obs: ObservabilityContext
    ) -> Course:
        course = self.store.course(course_id)
        if course is None:
            raise self._refuse(
                ErrorCode.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
            )
        if course.tenant_id != subject.tenant_id:
            raise self._refuse(
                ErrorCode.TENANT_MISMATCH,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_COURSE,
                resource_id=course_id,
            )
        return course

    def _owned_module(
        self, module_id: str, action: str, subject: SubjectContext, obs: ObservabilityContext
    ) -> Module:
        module = self.store.module(module_id)
        if module is None:
            raise self._refuse(
                ErrorCode.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_MODULE,
                resource_id=module_id,
            )
        if module.tenant_id != subject.tenant_id:
            raise self._refuse(
                ErrorCode.TENANT_MISMATCH,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_MODULE,
                resource_id=module_id,
            )
        return module

    def _owned_lesson(
        self, lesson_id: str, action: str, subject: SubjectContext, obs: ObservabilityContext
    ) -> Lesson:
        lesson = self.store.lesson(lesson_id)
        if lesson is None:
            raise self._refuse(
                ErrorCode.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_LESSON,
                resource_id=lesson_id,
            )
        if lesson.tenant_id != subject.tenant_id:
            raise self._refuse(
                ErrorCode.TENANT_MISMATCH,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_LESSON,
                resource_id=lesson_id,
            )
        return lesson

    def _course_of_module(
        self, module: Module, action: str, subject: SubjectContext, obs: ObservabilityContext
    ) -> Course:
        course = self.store.course(module.course_id)
        if course is None or course.tenant_id != subject.tenant_id:
            # The parent chain is complete by construction; a broken chain is
            # reported as an unavailable resource, never guessed around.
            raise self._refuse(
                ErrorCode.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_COURSE,
                resource_id=module.course_id,
            )
        return course

    def _course_of_lesson(
        self, lesson: Lesson, action: str, subject: SubjectContext, obs: ObservabilityContext
    ) -> Course:
        module = self.store.module(lesson.module_id)
        if module is None:
            raise self._refuse(
                ErrorCode.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_MODULE,
                resource_id=lesson.module_id,
            )
        return self._course_of_module(module, action, subject, obs)

    def _course_of_assignment(
        self,
        assignment: Assignment,
        subject: SubjectContext,
        obs: ObservabilityContext,
        action: str,
    ) -> Course:
        lesson = self.store.lesson(assignment.lesson_id)
        if lesson is None:
            raise self._refuse(
                ErrorCode.RESOURCE_NOT_FOUND,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_LESSON,
                resource_id=assignment.lesson_id,
            )
        return self._course_of_lesson(lesson, action, subject, obs)

    def _course_of_submission(
        self, submission: Submission, subject: SubjectContext, obs: ObservabilityContext
    ) -> Course:
        assignment = self.store.assignment(submission.assignment_id)
        if assignment is None:
            raise self._refuse(
                ErrorCode.RESOURCE_NOT_FOUND,
                action=ACTION_READ_SUBMISSION,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_ASSIGNMENT,
                resource_id=submission.assignment_id,
            )
        return self._course_of_assignment(
            assignment, subject, obs, ACTION_READ_SUBMISSION
        )

    def _require_structurable_course(
        self, course: Course, action: str, subject: SubjectContext, obs: ObservabilityContext
    ) -> None:
        """A structural mutation is accepted only while the course is DRAFT."""
        if course.status != "DRAFT":
            raise self._refuse(
                ErrorCode.INVALID_STATE_TRANSITION,
                action=action,
                obs=obs,
                identity_id=subject.identity_id,
                tenant_id=subject.tenant_id,
                resource_type=RESOURCE_COURSE,
                resource_id=course.course_id,
                details={"course_status": course.status},
            )

    # -------------------------------------------------------------- outcomes
    def _refuse(
        self,
        code: str,
        *,
        action: str,
        obs: ObservabilityContext,
        identity_id: str | None,
        tenant_id: str | None,
        resource_type: str | None,
        resource_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessRefused:
        """Refuse one operation attempt and audit the refusal on the way out."""
        self.audit(
            action=action,
            decision=DENY,
            reason=code,
            obs=obs,
            identity_id=identity_id,
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )
        return AccessRefused(
            code, status_code=ERROR_STATUS[code], details=details
        )

    # ---------------------------------------------------------- observability
    def observability(
        self,
        *,
        tenant_id: str | None = None,
        subject_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ObservabilityContext:
        rid = request_id or _new_id("req")
        cid = correlation_id or rid
        return ObservabilityContext(
            timestamp=self.clock(),
            environment=self.config.environment,
            platform_id=self.config.platform_id,
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            request_id=rid,
            trace_id=rid,
            correlation_id=cid,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    def audit(
        self,
        *,
        action: str,
        decision: str,
        reason: str | None,
        obs: ObservabilityContext,
        identity_id: str | None,
        tenant_id: str | None,
        resource_type: str | None,
        resource_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessAuditEvent:
        event = AccessAuditEvent(
            event_id=_new_id("aud"),
            timestamp=self.clock(),
            action=action,
            decision=decision,
            reason=reason,
            identity_id=identity_id,
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            platform_id=obs.platform_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            details=details or {},
        )
        self.store.append_audit(event)
        return event

    def _guard_audit(self, action: str, context: dict[str, Any]) -> None:
        """The audit sink of IS-005: its replay/conflict events, in this journal."""
        obs = self.observability(
            tenant_id=context.get("tenant_id"),
            subject_id=context.get("identity"),
            request_id=context.get("request_id"),
            correlation_id=context.get("correlation_id"),
        )
        self.audit(
            action=action,
            decision=DENY if action == "idempotency_conflict" else ALLOW,
            reason=action,
            obs=obs,
            identity_id=context.get("identity"),
            tenant_id=context.get("tenant_id"),
            resource_type=None,
            resource_id=context.get("resource"),
            details={"key": context.get("key")},
        )


def _identity_refusal_code(reason: str) -> str:
    """Map a published denial of IS-001 onto this contract's error codes."""
    if reason in _AUTHENTICATION_DENIALS:
        return ErrorCode.AUTHENTICATION_REQUIRED.value
    if reason in _TENANT_MISMATCH_DENIALS:
        return ErrorCode.TENANT_MISMATCH.value
    return ErrorCode.AUTHORIZATION_DENIED.value


def _decision_refusal_code(reason: str) -> str:
    """Map a published DENY of IS-003 onto this contract's error codes."""
    if reason in _AUTHENTICATION_DENIALS:
        return ErrorCode.AUTHENTICATION_REQUIRED.value
    if reason in _TENANT_MISMATCH_DENIALS:
        return ErrorCode.TENANT_MISMATCH.value
    return ErrorCode.AUTHORIZATION_DENIED.value


# ----------------------------------------------------------------- the views
def _course_view(course: Course) -> Any:
    from learning_service.contracts import CourseView

    return CourseView(
        course_id=course.course_id,
        tenant_id=course.tenant_id,
        title=course.title,
        description=course.description,
        status=course.status,
        created_by=course.created_by,
        created_at=course.created_at,
        updated_at=course.updated_at,
    )


def _module_view(module: Module) -> Any:
    from learning_service.contracts import ModuleView

    return ModuleView(
        module_id=module.module_id,
        course_id=module.course_id,
        tenant_id=module.tenant_id,
        title=module.title,
        position=module.position,
        status=module.status,
        created_at=module.created_at,
        updated_at=module.updated_at,
    )


def _lesson_view(lesson: Lesson) -> Any:
    from learning_service.contracts import LessonView

    return LessonView(
        lesson_id=lesson.lesson_id,
        module_id=lesson.module_id,
        tenant_id=lesson.tenant_id,
        title=lesson.title,
        content=lesson.content,
        position=lesson.position,
        status=lesson.status,
        created_at=lesson.created_at,
        updated_at=lesson.updated_at,
    )


def _assignment_view(assignment: Assignment) -> Any:
    from learning_service.contracts import AssignmentView

    return AssignmentView(
        assignment_id=assignment.assignment_id,
        lesson_id=assignment.lesson_id,
        tenant_id=assignment.tenant_id,
        title=assignment.title,
        instructions=assignment.instructions,
        status=assignment.status,
    )


def _enrollment_view(enrollment: Enrollment) -> Any:
    from learning_service.contracts import EnrollmentView

    return EnrollmentView(
        enrollment_id=enrollment.enrollment_id,
        tenant_id=enrollment.tenant_id,
        course_id=enrollment.course_id,
        student_identity_id=enrollment.student_identity_id,
        status=enrollment.status,
        created_at=enrollment.created_at,
    )


def _submission_view(submission: Submission) -> Any:
    from learning_service.contracts import SubmissionView

    return SubmissionView(
        submission_id=submission.submission_id,
        tenant_id=submission.tenant_id,
        assignment_id=submission.assignment_id,
        student_identity_id=submission.student_identity_id,
        attempt=submission.attempt,
        content=submission.content,
        status=submission.status,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
    )


__all__ = [
    "ACTION_UNREADABLE",
    "COMMAND_CREATE_ASSIGNMENT",
    "COMMAND_CREATE_COURSE",
    "COMMAND_CREATE_LESSON",
    "COMMAND_CREATE_MODULE",
    "COMMAND_CREATE_SUBMISSION",
    "COMMAND_ENROLL",
    "COMMAND_PUBLISH_COURSE",
    "ENFORCEMENT_CHAIN",
    "LearningEngine",
]
