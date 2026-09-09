"""Learning engine: the enforcement chain of the data owner.

This component is the final enforcement boundary for the Learning data it
owns (ARCHITECTURE.md §5.3, §6.2): IS-003 decides, and this component
applies the decision at its own boundary — including refusing when the
decision cannot be obtained at all. The chain is fixed, published, and
every step can only deny:

```text
Request (subject credential, record id, operation, [claimed tenant])
      ↓
ownership_boundary       the record exists here and its single owner is
                         this component                  else DENY
      ↓
authorization_decision   IS-003 decides through the port; the default
                         outcome is DENY; a dependency that does not answer
                         or answers outside its contract fails closed
      ↓
owned_data_operation     only now is the owned data read or reviewed
```

An ``ALLOW`` is not data access: the owned-data operation runs only here,
at the end, and a request denied earlier never reaches it. Both the served
accesses and every refusal are audited with ``request_id`` and
``correlation_id``.

The review command runs the same chain and then applies, inside the
owned-data step, exactly the ratified review semantics: state rule
(``SUBMITTED`` only) before the IS-005 guard, then the review effect —
``reviewed_by`` / ``reviewed_at`` are recorded and the lifecycle stays
``SUBMITTED``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from idempotency.errors import IdempotencyConflict
from idempotency.guard import IdempotencyGuard
from learning_service import COMPONENT_ID, COMPONENT_VERSION
from learning_service.config import LearningConfig
from learning_service.consumed import (
    ALLOW,
    DENY,
    DENY_REASONS,
    PERMITTED,
    DependencyRefusal,
    TenantContextRefusal,
)
from learning_service.contracts import (
    OPERATION_ASSIGNMENT_CREATE,
    OPERATION_COURSE_ARCHIVE,
    OPERATION_COURSE_CREATE,
    OPERATION_COURSE_PUBLISH,
    OPERATION_COURSE_READ,
    OPERATION_COURSE_READ_UNPUBLISHED,
    OPERATION_LESSON_CREATE,
    OPERATION_LIST,
    OPERATION_MODULE_CREATE,
    OPERATION_READ,
    OPERATION_REVIEW,
    AssignmentView,
    CourseView,
    LessonView,
    ModuleView,
    OwnDenyReason,
    SubmissionView,
)
from learning_service.errors import AccessRefused
from learning_service.models import (
    AccessAuditEvent,
    ObservabilityContext,
    OwnedAssignment,
    OwnedCourse,
    OwnedLesson,
    OwnedModule,
    OwnedSubmission,
)
from learning_service.ports import (
    AuthorizationPort,
    CommandSafetyPort,
    TenantContextPort,
)
from learning_service.store import DomainRefusal, LearningStore

#: The published order of enforcement. Also used by the contract tests: a
#: change of order is a change of behaviour and must be visible.
ENFORCEMENT_CHAIN: tuple[str, ...] = (
    "ownership_boundary",
    "authorization_decision",
    "owned_data_operation",
)

#: The audit action of a request the published schema rejected before any
#: handler ran: an access attempt that could not even be read.
ACTION_UNREADABLE = "learning.access"

#: Identity-family denial reasons of IS-003 map to 401; every other denial
#: is an authorization refusal (403). An unmapped reason never falls through
#: to a permissive status.
_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)

_NOT_FOUND_REASONS = frozenset(
    {
        OwnDenyReason.ASSIGNMENT_UNKNOWN,
        OwnDenyReason.SUBMISSION_UNKNOWN,
        OwnDenyReason.COURSE_UNKNOWN,
        OwnDenyReason.MODULE_UNKNOWN,
        OwnDenyReason.LESSON_UNKNOWN,
    }
)

#: Payload content rules of the authoring commands. A payload that violates
#: them is schema-valid but refused as ``validation_error`` before any access
#: decision: an unreadable command is not an access attempt.
TITLE_MAX = 512
DESCRIPTION_MAX = 4096
CONTENT_MAX = 65536
INSTRUCTIONS_MAX = 4096


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _status_for(reason: str) -> int:
    """HTTP status of a refusal: 401 authentication, 404 unknown, 409 state,
    immutability or idempotency conflict, 400 missing idempotency key, 422
    invalid payload, 503 fail-closed dependency, 403 everything else."""
    if reason in _AUTHENTICATION_DENIALS:
        return 401
    if reason in _NOT_FOUND_REASONS:
        return 404
    if reason in (
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.ALREADY_REVIEWED,
        OwnDenyReason.IDEMPOTENCY_CONFLICT,
    ):
        return 409
    if reason == OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED:
        return 400
    if reason == OwnDenyReason.VALIDATION_ERROR:
        return 422
    if reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE:
        return 503
    return 403


def _review_fingerprint(submission_id: str) -> str:
    """The command identity of one review: operation plus target. The review
    command carries no body, so the payload component is empty by contract."""
    return hashlib.sha256(f"{OPERATION_REVIEW}\n{submission_id}\n".encode()).hexdigest()


def _payload_fingerprint(operation: str, payload: dict[str, Any]) -> str:
    """The command identity of one authoring command: operation plus payload.

    The target is a separate binding of the IS-005 guard (the parent record
    for a creation, the Course for publish/archive); the fingerprint carries
    the payload, so a changed payload with the same key is a conflict.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{operation}\n{canonical}\n".encode()).hexdigest()


def _validation_problem(
    title: Any, texts: dict[str, tuple[Any, int, bool]]
) -> str | None:
    """The first content rule a command payload violates, or ``None``.

    ``title`` must be a non-empty string of at most :data:`TITLE_MAX` chars;
    ``texts`` maps a field name to its (value, max length, required) triple.
    """
    if not isinstance(title, str) or not title.strip():
        return "title must be a non-empty string"
    if len(title) > TITLE_MAX:
        return f"title must be at most {TITLE_MAX} characters"
    for name, (value, maximum, required) in texts.items():
        if not isinstance(value, str):
            return f"{name} must be a string"
        if required and not value.strip():
            return f"{name} must not be empty"
        if len(value) > maximum:
            return f"{name} must be at most {maximum} characters"
    return None


def _position_problem(position: Any) -> str | None:
    if isinstance(position, bool) or not isinstance(position, int):
        return "position must be an integer"
    if position < 0:
        return "position must not be negative"
    return None


def _course_view_of(
    course: OwnedCourse,
    modules: list[OwnedModule],
    lessons: list[OwnedLesson],
    assignments: list[OwnedAssignment],
) -> CourseView:
    """Build the published hierarchy view in deterministic order."""
    by_module: dict[str, list[OwnedLesson]] = {m.module_id: [] for m in modules}
    for lesson in lessons:
        by_module.setdefault(lesson.module_id, []).append(lesson)
    by_lesson: dict[str, list[OwnedAssignment]] = {l.lesson_id: [] for l in lessons}
    for assignment in assignments:
        by_lesson.setdefault(assignment.lesson_id or "", []).append(assignment)
    lesson_views = {
        lesson.lesson_id: LessonView(
            lesson_id=lesson.lesson_id,
            module_id=lesson.module_id,
            title=lesson.title,
            content=lesson.content,
            position=lesson.position,
            status=lesson.status,
            assignments=tuple(
                AssignmentView(
                    assignment_id=item.assignment_id,
                    lesson_id=item.lesson_id or "",
                    title=item.title or "",
                    instructions=item.instructions or "",
                    status=item.status,
                )
                for item in sorted(
                    by_lesson.get(lesson.lesson_id, []),
                    key=lambda a: a.assignment_id,
                )
            ),
        )
        for lesson in lessons
    }
    module_views = [
        ModuleView(
            module_id=module.module_id,
            course_id=module.course_id,
            title=module.title,
            position=module.position,
            status=module.status,
            lessons=tuple(
                lesson_views[lesson.lesson_id]
                for lesson in sorted(
                    by_module.get(module.module_id, []),
                    key=lambda l: (l.position, l.lesson_id),
                )
            ),
        )
        for module in sorted(modules, key=lambda m: (m.position, m.module_id))
    ]
    return CourseView(
        course_id=course.course_id,
        title=course.title,
        description=course.description,
        status=course.status,
        created_by=course.created_by,
        created_at=course.created_at,
        updated_at=course.updated_at,
        modules=tuple(module_views),
    )


def _view_of(submission: OwnedSubmission) -> SubmissionView:
    return SubmissionView(
        submission_id=submission.submission_id,
        assignment_id=submission.assignment_id,
        student_identity_id=submission.student_identity_id,
        attempt=submission.attempt,
        content=dict(submission.content),
        status=submission.status,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
        reviewed_by=submission.reviewed_by,
        reviewed_at=submission.reviewed_at,
    )


@dataclass
class LearningEngine:
    """The enforcement boundary of Learning in one Platform Instance.

    It owns assignments, submissions and an access audit journal. It owns no
    identity, no permission, no tenant registry and no tenant state: the
    decision is read, per access, from the published contract of IS-003
    through the port. It owns no second idempotency mechanism either: the
    review command is delivered to the IS-005 guard through the port.
    """

    store: LearningStore
    config: LearningConfig
    authorization: AuthorizationPort
    clock: Callable[[], str] = field(default=_utc_now)
    idempotency: CommandSafetyPort | None = field(default=None)
    tenant_context: TenantContextPort | None = field(default=None)

    def __post_init__(self) -> None:
        if self.idempotency is None:
            # No second idempotency mechanism: the default is the IS-005 guard
            # itself, reporting its replays and conflicts into this component's
            # audit journal.
            self.idempotency = IdempotencyGuard(audit_sink=self._idempotency_audit)

    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (never a caller claim)."""
        return self.config.platform_id

    # ------------------------------------------------------------- operations
    def read_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[SubmissionView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned submission — and only after the full chain allowed it."""
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        submission = self.store.ownership_of_submission(submission_id)
        if submission is None:
            raise self._refuse(
                OwnDenyReason.SUBMISSION_UNKNOWN,
                action=OPERATION_READ,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=submission_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if submission.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=OPERATION_READ,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": submission.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=OPERATION_READ,
            resource_type="submission",
            resource_id=submission.submission_id,
            resource_tenant_id=submission.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=OPERATION_READ,
            assignment_id=submission.assignment_id,
            submission_id=submission.submission_id,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served = self.store.serve_submission(submission.submission_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.SUBMISSION_UNKNOWN,
                action=OPERATION_READ,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        event = self.audit(
            action=OPERATION_READ,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=submission_id,
            resource_tenant_id=submission.tenant_id,
            assignment_id=submission.assignment_id,
            submission_id=submission.submission_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_view_of(served), obs, event)

    def list_submissions(
        self,
        subject_credential: str | None,
        assignment_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[list[SubmissionView], ObservabilityContext, AccessAuditEvent]:
        """List the submissions of one Assignment — teacher discovery.

        The same enforcement chain as the single read, applied to the
        Assignment: the record must be owned here before IS-003 is asked
        (an unknown assignment fails closed without a decision), and the
        grant question is ``learning.submissions.list`` against the
        Assignment's own tenant.
        """
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        assignment = self.store.ownership_of_assignment(assignment_id)
        if assignment is None:
            raise self._refuse(
                OwnDenyReason.ASSIGNMENT_UNKNOWN,
                action=OPERATION_LIST,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=assignment_id,
                resource_tenant_id=None,
                assignment_id=assignment_id,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if assignment.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=OPERATION_LIST,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=assignment_id,
                resource_tenant_id=assignment.tenant_id,
                assignment_id=assignment_id,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": assignment.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=OPERATION_LIST,
            resource_type="assignment",
            resource_id=assignment.assignment_id,
            resource_tenant_id=assignment.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=OPERATION_LIST,
            assignment_id=assignment.assignment_id,
            submission_id=None,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        served = self.store.list_submissions_for_assignment(assignment.assignment_id)

        event = self.audit(
            action=OPERATION_LIST,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=assignment_id,
            resource_tenant_id=assignment.tenant_id,
            assignment_id=assignment.assignment_id,
            submission_id=None,
            claimed_tenant_id=claimed_tenant_id,
        )
        return ([_view_of(item) for item in served], obs, event)

    def review_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[SubmissionView, ObservabilityContext, AccessAuditEvent]:
        """Record that one owned submission was reviewed — and only after the
        full chain allowed it and the IS-005 guard executed the effect.

        The enforced order is fixed: ownership boundary, the IS-003 decision
        (verified identity, effective tenant, authorization, resource-tenant
        match), then inside the owned-data step the state rule (``SUBMITTED``
        only), then the IS-005 guard (mandatory key, replay, conflict), then
        the review effect. The review effect records ``reviewed_by`` /
        ``reviewed_at`` and leaves the lifecycle ``SUBMITTED``. The review
        fact is immutable: a second review command with a different
        ``Idempotency-Key`` cannot overwrite the first review — only an exact
        replay of the first command returns the recorded result.
        """
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        submission = self.store.ownership_of_submission(submission_id)
        if submission is None:
            raise self._refuse(
                OwnDenyReason.SUBMISSION_UNKNOWN,
                action=OPERATION_REVIEW,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=submission_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if submission.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=OPERATION_REVIEW,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": submission.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=OPERATION_REVIEW,
            resource_type="submission",
            resource_id=submission.submission_id,
            resource_tenant_id=submission.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=OPERATION_REVIEW,
            assignment_id=submission.assignment_id,
            submission_id=submission.submission_id,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        # 3a. state rule before idempotency: only a SUBMITTED submission may be
        # reviewed, and review never changes the lifecycle state.
        try:
            self.store.assert_reviewable(submission.submission_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.SUBMISSION_UNKNOWN,
                action=OPERATION_REVIEW,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                exc.reason,
                action=OPERATION_REVIEW,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "kind": "domain_refusal",
                    "submission_state": submission.status,
                },
            )

        # 3b. idempotency: a state-changing command requires the mandatory key.
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=OPERATION_REVIEW,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
            )

        reviewed_at = self.clock()
        fingerprint = _review_fingerprint(submission.submission_id)
        try:
            reviewed = self.idempotency.execute(
                idempotency_key,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=OPERATION_REVIEW,
                resource=submission.submission_id,
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=lambda: self.store.apply_review(
                    submission.submission_id, subject_id, reviewed_at
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=OPERATION_REVIEW,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except DomainRefusal as exc:
            # A second review command (different Idempotency-Key) reached an
            # already reviewed submission: the review fact is immutable, so the
            # owned-data operation refused and no record was saved by IS-005.
            raise self._refuse(
                exc.reason,
                action=OPERATION_REVIEW,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "kind": "domain_refusal",
                    "submission_state": submission.status,
                },
            )

        # 3c. the review effect ran exactly once; the result is the submission
        # representation with the review fact.
        event = self.audit(
            action=OPERATION_REVIEW,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=submission_id,
            resource_tenant_id=submission.tenant_id,
            assignment_id=submission.assignment_id,
            submission_id=submission.submission_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_view_of(reviewed), obs, event)

    # -------------------------------------------------- content authoring
    def _effective_tenant(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None,
        obs: ObservabilityContext,
        action: str,
        resource_id: str,
    ) -> tuple[ObservabilityContext, str, str]:
        """Resolve the effective tenant of the verified subject (IS-001).

        Consumed by create-course only: its target does not exist yet, so the
        resource Tenant of the authorization question can only be the
        effective tenant of the verified identity. The tenant-context port is
        the published identity contract adapted by the composition root; a
        port that is absent, does not answer or answers outside its contract
        fails closed. The caller-supplied tenant identifier is forwarded as
        the cross-check only — it can never select the effective tenant.
        """
        if self.tenant_context is None:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_port_not_wired"},
            )
        try:
            answer = self.tenant_context.resolve(
                subject_credential,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception:
            answer = TenantContextRefusal(None)
        if isinstance(answer, TenantContextRefusal):
            code = answer.reason_code
            if code in _AUTHENTICATION_DENIALS or code in DENY_REASONS:
                reason, subject_id, tenant_id = code, None, None
            else:
                reason = OwnDenyReason.AUTHORIZATION_UNAVAILABLE
                subject_id, tenant_id = None, None
            raise self._refuse(
                reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details=(
                    None
                    if reason != OwnDenyReason.AUTHORIZATION_UNAVAILABLE
                    else {"stated_code": code}
                ),
            )
        return (
            self.observability(
                tenant_id=answer.tenant_id,
                subject_id=answer.identity_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            ),
            answer.identity_id,
            answer.tenant_id,
        )

    def create_course(
        self,
        subject_credential: str | None,
        *,
        title: str,
        description: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CourseView, ObservabilityContext, AccessAuditEvent]:
        """Create one owned Course — the command ``learning.courses.create``.

        The enforced chain is the published one, with the effective tenant
        resolved first because the target does not exist yet: the tenant-
        context port (IS-001) derives it from the verified identity, then
        IS-003 decides ``learning.courses.create`` for a course-to-be in that
        tenant, then the IS-005 guard executes the creation effect exactly
        once. The Course's Tenant is the effective tenant of the chain —
        never a caller claim — and a new Course is created in ``DRAFT``.
        """
        action = OPERATION_COURSE_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _validation_problem(
            title,
            {"description": (description, DESCRIPTION_MAX, False)},
        )
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=None,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )

        # --- step 1: the effective tenant of the verified subject (IS-001) ---
        obs, _, effective_tenant = self._effective_tenant(
            subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            resource_id=None,
        )
        course_id = _new_id("crs")

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="course",
            resource_id=course_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            assignment_id=None,
            submission_id=None,
        )
        obs = answer_obs
        if tenant_id != effective_tenant:
            # The decision and the identity context must agree about the
            # effective tenant; a disagreement is a non-authoritative answer.
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=effective_tenant,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_disagreement"},
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=effective_tenant,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action, {"title": title, "description": description}
        )
        try:
            created = self.idempotency.execute(
                idempotency_key,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=None,
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=lambda: self.store.create_course(
                    tenant_id=tenant_id,
                    title=title,
                    description=description,
                    created_by=subject_id or "",
                    now=self.clock(),
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=effective_tenant,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=created.course_id,
            resource_tenant_id=created.tenant_id,
            assignment_id=None,
            submission_id=None,
            claimed_tenant_id=claimed_tenant_id,
        )
        view = CourseView(
            course_id=created.course_id,
            title=created.title,
            description=created.description,
            status=created.status,
            created_by=created.created_by,
            created_at=created.created_at,
            updated_at=created.updated_at,
            modules=(),
        )
        return (view, obs, event)

    def _create_child(
        self,
        subject_credential: str | None,
        *,
        action: str,
        parent_kind: str,
        parent_id: str,
        unknown_reason: str,
        ownership_of: Callable[[str], Any],
        payload: dict[str, Any],
        claimed_tenant_id: str | None,
        idempotency_key: str | None,
        request_id: str | None,
        correlation_id: str | None,
        effect: Callable[[], Any],
    ) -> tuple[Any, ObservabilityContext, AccessAuditEvent]:
        """One child-creation command: Course → Module → Lesson → Assignment.

        The same fixed chain for every level: the parent must exist here and
        be owned by this component, IS-003 decides the command against the
        parent (the parent's own Tenant from this store), the IS-005 guard
        executes the creation effect exactly once, and the state rule — only
        a ``DRAFT`` parent accepts children — lives inside the effect. The
        child's Tenant is the parent's Tenant by construction.
        """
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        parent = ownership_of(parent_id)
        if parent is None:
            raise self._refuse(
                unknown_reason,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=parent_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if parent.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=parent_id,
                resource_tenant_id=parent.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": parent.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type=parent_kind,
            resource_id=parent_id,
            resource_tenant_id=parent.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            assignment_id=None,
            submission_id=None,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=parent_id,
                resource_tenant_id=parent.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(action, payload)
        try:
            created = self.idempotency.execute(
                idempotency_key,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=parent_id,
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=effect,
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=parent_id,
                resource_tenant_id=parent.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except KeyError:
            raise self._refuse(
                unknown_reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=parent_id,
                resource_tenant_id=parent.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                exc.reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=parent_id,
                resource_tenant_id=parent.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "domain_refusal", "parent_status": parent.status},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=parent_id,
            resource_tenant_id=parent.tenant_id,
            assignment_id=None,
            submission_id=None,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (created, obs, event)

    def create_module(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        title: str,
        position: int,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ModuleView, ObservabilityContext, AccessAuditEvent]:
        """Create one Module inside a Course — ``learning.modules.create``."""
        action = OPERATION_MODULE_CREATE
        obs0 = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        problem = _validation_problem(title, {}) or _position_problem(position)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs0,
                subject_id=None,
                tenant_id=None,
                resource_id=course_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )
        created, obs, event = self._create_child(
            subject_credential,
            action=action,
            parent_kind="course",
            parent_id=course_id,
            unknown_reason=OwnDenyReason.COURSE_UNKNOWN,
            ownership_of=self.store.ownership_of_course,
            payload={"title": title, "position": position},
            claimed_tenant_id=claimed_tenant_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
            effect=lambda: self.store.create_module(
                course_id, title=title, position=position, now=self.clock()
            ),
        )
        view = ModuleView(
            module_id=created.module_id,
            course_id=created.course_id,
            title=created.title,
            position=created.position,
            status=created.status,
            lessons=(),
        )
        return (view, obs, event)

    def create_lesson(
        self,
        subject_credential: str | None,
        module_id: str,
        *,
        title: str,
        content: str,
        position: int,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[LessonView, ObservabilityContext, AccessAuditEvent]:
        """Create one Lesson inside a Module — ``learning.lessons.create``."""
        action = OPERATION_LESSON_CREATE
        obs0 = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        problem = _validation_problem(
            title, {"content": (content, CONTENT_MAX, False)}
        ) or _position_problem(position)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs0,
                subject_id=None,
                tenant_id=None,
                resource_id=module_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )
        created, obs, event = self._create_child(
            subject_credential,
            action=action,
            parent_kind="module",
            parent_id=module_id,
            unknown_reason=OwnDenyReason.MODULE_UNKNOWN,
            ownership_of=self.store.ownership_of_module,
            payload={"title": title, "content": content, "position": position},
            claimed_tenant_id=claimed_tenant_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
            effect=lambda: self.store.create_lesson(
                module_id,
                title=title,
                content=content,
                position=position,
                now=self.clock(),
            ),
        )
        view = LessonView(
            lesson_id=created.lesson_id,
            module_id=created.module_id,
            title=created.title,
            content=created.content,
            position=created.position,
            status=created.status,
            assignments=(),
        )
        return (view, obs, event)

    def create_assignment(
        self,
        subject_credential: str | None,
        lesson_id: str,
        *,
        title: str,
        instructions: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[AssignmentView, ObservabilityContext, AccessAuditEvent]:
        """Create one Assignment inside a Lesson — ``learning.assignments.create``.

        The existing Assignment model is extended, not replaced: the created
        record is the same Assignment identity the submission flow serves,
        now anchored to its parent Lesson.
        """
        action = OPERATION_ASSIGNMENT_CREATE
        obs0 = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        problem = _validation_problem(
            title, {"instructions": (instructions, INSTRUCTIONS_MAX, False)}
        )
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs0,
                subject_id=None,
                tenant_id=None,
                resource_id=lesson_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )
        created, obs, event = self._create_child(
            subject_credential,
            action=action,
            parent_kind="lesson",
            parent_id=lesson_id,
            unknown_reason=OwnDenyReason.LESSON_UNKNOWN,
            ownership_of=self.store.ownership_of_lesson,
            payload={"title": title, "instructions": instructions},
            claimed_tenant_id=claimed_tenant_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
            effect=lambda: self.store.create_assignment(
                lesson_id, title=title, instructions=instructions, now=self.clock()
            ),
        )
        view = AssignmentView(
            assignment_id=created.assignment_id,
            lesson_id=created.lesson_id or "",
            title=created.title or "",
            instructions=created.instructions or "",
            status=created.status,
        )
        return (view, obs, event)

    def _course_command(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        action: str,
        claimed_tenant_id: str | None,
        idempotency_key: str | None,
        request_id: str | None,
        correlation_id: str | None,
        effect: Callable[[str, str], OwnedCourse],
    ) -> tuple[CourseView, ObservabilityContext, AccessAuditEvent]:
        """One Course-level command: publish or archive.

        The fixed chain: the Course must exist here and be owned by this
        component, IS-003 decides the command against the Course's own
        Tenant, the IS-005 guard executes the effect exactly once, and the
        state rule (``DRAFT`` publishes, ``PUBLISHED`` archives; the whole
        hierarchy flips or nothing does) lives inside the effect — so an
        exact replay returns the recorded result even though the Course has
        meanwhile changed state.
        """
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        course = self.store.ownership_of_course(course_id)
        if course is None:
            raise self._refuse(
                OwnDenyReason.COURSE_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=course_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if course.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": course.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="course",
            resource_id=course_id,
            resource_tenant_id=course.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            assignment_id=None,
            submission_id=None,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(action, {})

        def run_effect() -> CourseView:
            # The whole critical sequence — validation, the transition of the
            # Course and of every descendant, and the recorded snapshot —
            # inside the Course's domain critical section: no child creation
            # and no archive can interleave between the publication's
            # validation and its writes, and the recorded result describes
            # exactly the tree as this command transitioned it (Issue #31).
            # The section is reentrant: the store re-acquires it inside.
            with self.store.hierarchy_section(course_id):
                updated = effect(course_id, self.clock())
                _, modules, lessons, assignments = self.store.course_hierarchy(
                    course_id
                )
            return _course_view_of(updated, modules, lessons, assignments)

        try:
            view = self.idempotency.execute(
                idempotency_key,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=course_id,
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=run_effect,
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )
        except KeyError:
            raise self._refuse(
                OwnDenyReason.COURSE_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )
        except DomainRefusal as exc:
            raise self._refuse(
                exc.reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "domain_refusal", "course_status": course.status},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=course_id,
            resource_tenant_id=course.tenant_id,
            assignment_id=None,
            submission_id=None,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (view, obs, event)

    def publish_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CourseView, ObservabilityContext, AccessAuditEvent]:
        """Publish one owned Course and its whole hierarchy atomically —
        the command ``learning.courses.publish``.

        Publication is a single Course-level command: the structurally valid
        ``DRAFT`` hierarchy flips ``DRAFT → PUBLISHED`` or nothing does. A
        validation or dependency failure leaves every descendant unpub-
        lished; there is no partial publication and no unpublish.
        """
        return self._course_command(
            subject_credential,
            course_id,
            action=OPERATION_COURSE_PUBLISH,
            claimed_tenant_id=claimed_tenant_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
            effect=self.store.publish_course,
        )

    def archive_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CourseView, ObservabilityContext, AccessAuditEvent]:
        """Archive one owned Course and its whole hierarchy atomically —
        the command ``learning.courses.archive``.

        Only a ``PUBLISHED`` Course can be archived; the whole hierarchy
        flips ``PUBLISHED → ARCHIVED`` or nothing does. No unarchive exists.
        """
        return self._course_command(
            subject_credential,
            course_id,
            action=OPERATION_COURSE_ARCHIVE,
            claimed_tenant_id=claimed_tenant_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
            effect=self.store.archive_course,
        )

    def read_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[CourseView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned Course hierarchy — and only after the chain
        allowed it.

        The question depends on the Course's own state, a fact of this
        store: a ``PUBLISHED`` Course is read through
        ``learning.courses.read`` (Teachers and Students); a hierarchy that
        is not published — ``DRAFT`` or ``ARCHIVED`` — is read through
        ``learning.courses.read_unpublished`` (Teachers). The published-
        content read of a Student therefore covers exactly the published
        content, without any policy logic inside this component.
        """
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        course = self.store.ownership_of_course(course_id)
        if course is None:
            raise self._refuse(
                OwnDenyReason.COURSE_UNKNOWN,
                action=OPERATION_COURSE_READ,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=course_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if course.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=OPERATION_COURSE_READ,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": course.owner_component},
            )
        operation = (
            OPERATION_COURSE_READ
            if course.status == "PUBLISHED"
            else OPERATION_COURSE_READ_UNPUBLISHED
        )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=operation,
            resource_type="course",
            resource_id=course.course_id,
            resource_tenant_id=course.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=operation,
            assignment_id=None,
            submission_id=None,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served, modules, lessons, assignments = self.store.course_hierarchy(
                course.course_id
            )
        except KeyError:
            raise self._refuse(
                OwnDenyReason.COURSE_UNKNOWN,
                action=operation,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=course_id,
                resource_tenant_id=course.tenant_id,
                assignment_id=None,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        event = self.audit(
            action=operation,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=course_id,
            resource_tenant_id=course.tenant_id,
            assignment_id=None,
            submission_id=None,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_course_view_of(served, modules, lessons, assignments), obs, event)

    def refuse_unreadable_request(
        self,
        *,
        path: str,
        schema_problems: list[str],
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AccessRefused:
        """Refuse — and audit — a request the published schema rejected."""
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        exc = AccessRefused(
            "malformed_request",
            status_code=422,
            details={
                "refused": "malformed_request",
                "schema_problems": list(schema_problems),
                "path": path,
            },
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        self.audit(
            action=ACTION_UNREADABLE,
            decision=DENY,
            reason="malformed_request",
            obs=obs,
            subject_id=None,
            tenant_id=None,
            resource_id=None,
            resource_tenant_id=None,
            assignment_id=None,
            submission_id=None,
            claimed_tenant_id=None,
            details=dict(exc.details),
        )
        return exc

    # ------------------------------------------------------------------ chain
    def _decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        obs: ObservabilityContext,
        action: str,
        assignment_id: str | None,
        submission_id: str | None,
    ) -> tuple[ObservabilityContext, str | None, str | None]:
        """Ask the port and enforce the answer; returns the decided context.

        Raises the audited refusal for every outcome that is not an explicit
        authoritative ALLOW. The owned-data operation runs only after this
        returns.
        """
        try:
            answer = self.authorization.decide(
                subject_credential,
                operation=operation,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:
            code = getattr(exc, "reason", None)
            code = getattr(code, "value", code)
            answer = DependencyRefusal(code if isinstance(code, str) and code else None)
        if isinstance(answer, DependencyRefusal):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                assignment_id=assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_code": answer.reason_code},
            )
        if answer.decision == DENY and answer.reason in DENY_REASONS:
            raise self._refuse(
                answer.reason,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                assignment_id=assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if not (answer.decision == ALLOW and answer.reason == PERMITTED):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                assignment_id=assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "nonauthoritative_answer": {
                        "decision": answer.decision,
                        "reason": answer.reason,
                    }
                },
            )
        decided_obs = self.observability(
            tenant_id=answer.tenant_id,
            subject_id=answer.subject_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        return (decided_obs, answer.subject_id, answer.tenant_id)

    # -------------------------------------------------------------- outcomes
    def _refuse(
        self,
        reason: str,
        *,
        action: str,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        assignment_id: str | None,
        submission_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessRefused:
        """Refuse one access attempt and audit the refusal on the way out."""
        self.audit(
            action=action,
            decision=DENY,
            reason=reason,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            assignment_id=assignment_id,
            submission_id=submission_id,
            claimed_tenant_id=claimed_tenant_id,
            details=details,
        )
        return AccessRefused(
            reason,
            status_code=_status_for(reason),
            details=details,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )

    # --------------------------------------------------------- observability
    def observability(
        self,
        *,
        tenant_id: str | None,
        subject_id: str | None,
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
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        assignment_id: str | None,
        submission_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessAuditEvent:
        event = AccessAuditEvent(
            event_id=_new_id("aud"),
            action=action,
            decision=decision,
            reason=reason,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            assignment_id=assignment_id,
            submission_id=submission_id,
            claimed_tenant_id=claimed_tenant_id,
            platform_id=obs.platform_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            timestamp=self.clock(),
            details=details or {},
        )
        self.store.audit.append(event)
        return event

    # ------------------------------------------------------ IS-005 observability
    def _idempotency_audit(self, action: str, details: dict[str, Any]) -> None:
        """Map a report of the IS-005 guard into this component's audit journal.

        The guard reports ``idempotency_replay`` and ``idempotency_conflict``:
        both are security-relevant (a replayed command produced no second
        effect, a differing binding was refused) and are recorded in the same
        journal as every other access outcome.
        """
        request_id = details.get("request_id")
        correlation_id = details.get("correlation_id") or request_id
        obs = self.observability(
            tenant_id=details.get("tenant_id"),
            subject_id=details.get("identity"),
            request_id=request_id,
            correlation_id=correlation_id,
        )
        self.audit(
            action=action,
            decision=ALLOW if action == "idempotency_replay" else DENY,
            reason=action,
            obs=obs,
            subject_id=details.get("identity"),
            tenant_id=details.get("tenant_id"),
            resource_id=details.get("resource"),
            resource_tenant_id=None,
            assignment_id=None,
            submission_id=details.get("resource"),
            claimed_tenant_id=None,
            details={key: value for key, value in details.items() if key != "resource"},
        )


__all__ = ["ACTION_UNREADABLE", "ENFORCEMENT_CHAIN", "LearningEngine"]
