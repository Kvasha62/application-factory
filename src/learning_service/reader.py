"""Published consumer surface of the Learning component (SCS-001).

A consumer receives exactly one object: :class:`LearningClient`. It offers
the read and review operations of the published contract — read one
submission, list one Assignment's submissions and record the review of one
submission — and returns an immutable
:class:`learning_service.contracts.SubmissionView` (or a list of them) or
raises :class:`learning_service.errors.AccessRefused`.

Its state is one value: an opaque contract-channel handle. No credential
is bound to the client — the subject credential is supplied per access,
because it is the subject, not the consumer, whom this boundary
authenticates through the decision. No callable, closure or cell is
stored, so the component's ASGI application, deployment, engine, stores
and audit journal are unreachable through any attribute of the client.
This module keeps no reference to the internal transport either — the
executor is resolved at call time — so importing the published surface
does not open a door to the channel table or to the application behind it.

What the client cannot do is as important as what it can: it cannot read
the store, read the audit journal, register records or reach submissions
except through the published operations — which is to say, without going
through the enforcement chain.

Refusals arrive in the approved SCS-001 envelope (``error.code`` /
``error.message`` / ``error.details`` plus top-level ``request_id`` /
``correlation_id``). Any other shape — including the legacy ``detail``
shape — is a contract violation and fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from learning_service.contracts import (
    PUBLISHED_DENY_REASONS,
    AssignmentView,
    CourseView,
    EnrollmentView,
    LessonView,
    ModuleView,
    SubmissionView,
)
from learning_service.errors import AccessRefused, ContractViolation

__all__ = [
    "LearningClient",
    "SubmissionView",
    "build_client",
]


def _view_from(payload: Mapping[str, Any]) -> SubmissionView:
    """Rebuild the published value; an answer outside the contract fails closed."""
    try:
        content = payload["content"]
        if not isinstance(content, dict):
            raise TypeError("content must be an object")
        reviewed_by = payload.get("reviewed_by")
        reviewed_at = payload.get("reviewed_at")
        if reviewed_by is not None and not isinstance(reviewed_by, str):
            raise ValueError("reviewed_by must be a string")
        if reviewed_at is not None and not isinstance(reviewed_at, str):
            raise ValueError("reviewed_at must be a string")
        return SubmissionView(
            submission_id=str(payload["submission_id"]),
            assignment_id=str(payload["assignment_id"]),
            student_identity_id=str(payload["student_identity_id"]),
            attempt=int(payload["attempt"]),
            content=dict(content),
            status=str(payload["status"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published submission model"
        ) from exc


class LearningClient:
    """Value-only access reader over the published learning contract.

    It performs exactly the published operations — the single-submission
    read, the teacher submission-discovery list, the single-submission
    review and the Content Authoring commands and hierarchy read — and
    nothing else; the exact same API is available to a remote consumer.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that
    issued them.
    """

    __slots__ = ("__weakref__", "_channel")

    def __init__(self, channel: str) -> None:
        if not isinstance(channel, str) or not channel:
            raise ValueError("a learning contract channel handle is required")
        self._channel = channel

    # -------------------------------------------------------------- operations
    def read_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SubmissionView:
        """Read one owned submission through the enforcement chain.

        ``subject_credential`` is the credential presented by the subject;
        it is verified by IS-001 inside the IS-003 decision, so a consumer
        cannot assert who the subject is. ``claimed_tenant_id`` is forwarded
        as a cross-check only: it can never select the effective tenant.
        """
        status, payload = self._call(
            "GET",
            f"/api/v1/learning/submissions/{quote(submission_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _view_from(payload)

    def list_submissions(
        self,
        subject_credential: str | None,
        assignment_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[SubmissionView]:
        """List one Assignment's submissions through the enforcement chain.

        The teacher discovery operation of the published contract: an empty
        Assignment answers an empty list, and a caller outside the
        Assignment's tenant — or without the list grant — receives a
        refusal, never a partial set.
        """
        status, payload = self._call(
            "GET",
            f"/api/v1/learning/assignments/{quote(assignment_id, safe='')}/submissions",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _views_from(payload)

    def review_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SubmissionView:
        """Record the review of one owned submission through the enforcement chain.

        ``idempotency_key`` is mandatory and delivered through IS-005: an exact
        replay returns the same result without a second effect, and a changed
        binding is refused. ``subject_credential`` is verified by IS-001 inside
        the IS-003 decision, so a consumer cannot assert who the subject is.
        """
        status, payload = self._call(
            "POST",
            f"/api/v1/learning/submissions/{quote(submission_id, safe='')}/review",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _view_from(payload)

    # ------------------------------------------------- content authoring
    def create_course(
        self,
        subject_credential: str | None,
        *,
        title: str,
        description: str,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CourseView:
        """Create one Course through the enforcement chain.

        ``idempotency_key`` is mandatory and delivered through IS-005. The
        effective tenant is resolved inside the enforcement chain from the
        verified identity — never from ``claimed_tenant_id``, which stays a
        cross-check.
        """
        status, payload = self._call(
            "POST",
            "/api/v1/learning/courses",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"title": title, "description": description},
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _course_from(payload)

    def read_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CourseView:
        """Read one Course hierarchy through the enforcement chain.

        Returns the nested navigation chain
        ``Course → Module → Lesson → Assignment``; a caller outside the
        Course's Tenant — or, for a hierarchy that is not published, a
        caller without the authoring-side read grant — receives a refusal,
        never a partial hierarchy.
        """
        status, payload = self._call(
            "GET",
            f"/api/v1/learning/courses/{quote(course_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _course_from(payload)

    def create_module(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        title: str,
        position: int,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ModuleView:
        """Create one Module inside a Course through the enforcement chain."""
        status, payload = self._call(
            "POST",
            f"/api/v1/learning/courses/{quote(course_id, safe='')}/modules",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"title": title, "position": position},
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _module_from(payload)

    def create_lesson(
        self,
        subject_credential: str | None,
        module_id: str,
        *,
        title: str,
        content: str,
        position: int,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LessonView:
        """Create one Lesson inside a Module through the enforcement chain."""
        status, payload = self._call(
            "POST",
            f"/api/v1/learning/modules/{quote(module_id, safe='')}/lessons",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"title": title, "content": content, "position": position},
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _lesson_from(payload)

    def create_assignment(
        self,
        subject_credential: str | None,
        lesson_id: str,
        *,
        title: str,
        instructions: str,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AssignmentView:
        """Create one Assignment inside a Lesson through the enforcement chain."""
        status, payload = self._call(
            "POST",
            f"/api/v1/learning/lessons/{quote(lesson_id, safe='')}/assignments",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"title": title, "instructions": instructions},
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _assignment_from(payload)

    def publish_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CourseView:
        """Publish one Course and its hierarchy atomically through the chain.

        An exact replay with the same ``Idempotency-Key`` returns the
        recorded published hierarchy without a second effect.
        """
        status, payload = self._call(
            "POST",
            f"/api/v1/learning/courses/{quote(course_id, safe='')}/publish",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _course_from(payload)

    def archive_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CourseView:
        """Archive one Course and its hierarchy atomically through the chain.

        An exact replay with the same ``Idempotency-Key`` returns the
        recorded archived hierarchy without a second effect.
        """
        status, payload = self._call(
            "POST",
            f"/api/v1/learning/courses/{quote(course_id, safe='')}/archive",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _course_from(payload)

    # ---------------------------------------------------- student enrollment
    def enroll(
        self,
        subject_credential: str | None,
        *,
        course_id: str,
        idempotency_key: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> EnrollmentView:
        """Enroll the verified subject in one Course through the enforcement chain.

        ``idempotency_key`` is mandatory and delivered through IS-005: an exact
        replay returns the recorded Enrollment without a second effect, and a
        changed binding is refused. The command takes a Course and no identity:
        the student of the Enrollment is the verified subject of the chain, so
        a consumer cannot enroll another identity — ``claimed_tenant_id`` stays
        a cross-check and can never select the effective tenant.
        """
        status, payload = self._call(
            "POST",
            "/api/v1/learning/enrollments",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"course_id": course_id},
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _enrollment_from(payload)

    def read_enrollment(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> EnrollmentView:
        """Read the verified subject's own Enrollment in one Course.

        The Course identifies the record: the operation accepts no enrollment
        identifier, so another student's Enrollment is unreachable through it
        and its existence is never disclosed — "never enrolled" and "not yours"
        are the same refusal. A caller outside the Course's Tenant, or without
        the read grant, receives a refusal, never a record.
        """
        status, payload = self._call(
            "GET",
            f"/api/v1/learning/enrollments/{quote(course_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _enrollment_from(payload)

    # --------------------------------------------------------------- internals
    def _call(
        self,
        method: str,
        path: str,
        *,
        subject_credential: str | None,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
        idempotency_key: str | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        headers: list[tuple[str, str]] = []
        if subject_credential is not None:
            headers.append(("authorization", f"Bearer {subject_credential}"))
        if claimed_tenant_id:
            headers.append(("x-tenant-id", claimed_tenant_id))
        if idempotency_key:
            headers.append(("idempotency-key", idempotency_key))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))
        return _call_contract()(self._channel, method, path, headers, body)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        refusal = _refusal_of(payload)
        if refusal is None:
            if status >= 500:
                return ContractViolation(
                    f"learning contract failed with status {status}"
                )
            return ContractViolation(
                "the learning contract refused without a published reason"
            )
        reason, request_id, correlation_id = refusal
        return AccessRefused(
            reason,
            status_code=status,
            request_id=request_id,
            correlation_id=correlation_id,
        )


def _views_from(payload: Mapping[str, Any]) -> list[SubmissionView]:
    """Rebuild the published ``{items: [...]}`` list; anything else fails closed."""
    try:
        items = payload["items"]
        if not isinstance(items, list):
            raise TypeError("items must be a list")
        return [_view_from(item) for item in items]
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published list model"
        ) from exc


def _text_field(payload: Mapping[str, Any], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _assignment_from(payload: Mapping[str, Any]) -> AssignmentView:
    """Rebuild the published assignment value; an outside answer fails closed."""
    try:
        return AssignmentView(
            assignment_id=_text_field(payload, "assignment_id"),
            lesson_id=_text_field(payload, "lesson_id"),
            title=_text_field(payload, "title"),
            instructions=_text_field(payload, "instructions"),
            status=_text_field(payload, "status"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published assignment model"
        ) from exc


def _lesson_from(payload: Mapping[str, Any]) -> LessonView:
    """Rebuild the published lesson value; an outside answer fails closed."""
    try:
        position = payload["position"]
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError("position must be an integer")
        assignments = payload["assignments"]
        if not isinstance(assignments, list):
            raise TypeError("assignments must be a list")
        return LessonView(
            lesson_id=_text_field(payload, "lesson_id"),
            module_id=_text_field(payload, "module_id"),
            title=_text_field(payload, "title"),
            content=_text_field(payload, "content"),
            position=position,
            status=_text_field(payload, "status"),
            assignments=tuple(_assignment_from(item) for item in assignments),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published lesson model"
        ) from exc


def _module_from(payload: Mapping[str, Any]) -> ModuleView:
    """Rebuild the published module value; an outside answer fails closed."""
    try:
        position = payload["position"]
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError("position must be an integer")
        lessons = payload["lessons"]
        if not isinstance(lessons, list):
            raise TypeError("lessons must be a list")
        return ModuleView(
            module_id=_text_field(payload, "module_id"),
            course_id=_text_field(payload, "course_id"),
            title=_text_field(payload, "title"),
            position=position,
            status=_text_field(payload, "status"),
            lessons=tuple(_lesson_from(item) for item in lessons),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published module model"
        ) from exc


def _course_from(payload: Mapping[str, Any]) -> CourseView:
    """Rebuild the published course-hierarchy value; an outside answer fails closed."""
    try:
        modules = payload["modules"]
        if not isinstance(modules, list):
            raise TypeError("modules must be a list")
        return CourseView(
            course_id=_text_field(payload, "course_id"),
            title=_text_field(payload, "title"),
            description=_text_field(payload, "description"),
            status=_text_field(payload, "status"),
            created_by=_text_field(payload, "created_by"),
            created_at=_text_field(payload, "created_at"),
            updated_at=_text_field(payload, "updated_at"),
            modules=tuple(_module_from(item) for item in modules),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published course model"
        ) from exc


def _enrollment_from(payload: Mapping[str, Any]) -> EnrollmentView:
    """Rebuild the published enrollment value; an outside answer fails closed."""
    try:
        return EnrollmentView(
            enrollment_id=_text_field(payload, "enrollment_id"),
            course_id=_text_field(payload, "course_id"),
            student_identity_id=_text_field(payload, "student_identity_id"),
            status=_text_field(payload, "status"),
            created_at=_text_field(payload, "created_at"),
            updated_at=_text_field(payload, "updated_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published enrollment model"
        ) from exc


def _refusal_of(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Read the approved error envelope: (reason, request_id, correlation_id).

    The envelope shape is strict: ``error.code`` must be present, the
    internal reason must come from ``error.details.reason`` and belong to
    the published vocabulary, and top-level ``request_id`` /
    ``correlation_id`` must be present. Anything else fails closed.
    """
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code:
        return None
    details = error.get("details")
    if not isinstance(details, Mapping):
        return None
    reason = details.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    if reason != "malformed_request" and reason not in PUBLISHED_DENY_REASONS:
        return None
    request_id = payload.get("request_id")
    correlation_id = payload.get("correlation_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(correlation_id, str) or not correlation_id:
        return None
    return (reason, request_id, correlation_id)


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the
    boundary: after ``import learning_service.reader`` this module's
    namespace holds no module object, no channel table, no transport class
    and no application.
    """
    from learning_service.transport import call_contract

    return call_contract


def build_client(app: Any) -> LearningClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — the component's published ASGI contract — is handed to the
    provider's own channel table and stays there: the client receives the
    handle back, not the application. A client dropped by its consumer
    revokes that handle, so the table is not a place where applications
    accumulate.
    """
    from learning_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = LearningClient(channel)
    revoke_on_death(client, channel)
    return client
