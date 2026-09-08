"""Published consumer surface of the Learning component.

A consumer receives exactly one object: :class:`LearningClient`. It offers
the eleven operations of the published contract — seven business commands
(each carrying an ``Idempotency-Key``) and four reads — and returns an
immutable value of :mod:`learning_service.contracts` or raises
:class:`learning_service.errors.AccessRefused` with the published error code.

Its state is one value: an opaque contract-channel handle. No credential is
bound to the client — the subject credential is supplied per operation. No
callable, closure or cell is stored, so the component's ASGI application,
deployment, engine, business store and audit journal are unreachable through
any attribute of the client. This module keeps no reference to the internal
transport either — the executor is resolved at call time — so importing the
published surface does not open a door to the channel table or to the
application behind it.

What the client cannot do is as important as what it can: it cannot list
courses, read the store, read the audit journal, choose a tenant or set a
lifecycle state. Every operation it can perform crosses the same enforcement
chain as the identical HTTP request.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from learning_service.contracts import (
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
    "build_client",
]


def _require_mapping(payload: Mapping[str, Any], key: str, what: str) -> Mapping[str, Any]:
    inner = payload.get(key)
    if not isinstance(inner, Mapping):
        raise ContractViolation(f"the learning contract answered outside its published shape: {what}")
    return inner


def _course_from(payload: Mapping[str, Any]) -> CourseView:
    try:
        course = _require_mapping(payload, "course", "course envelope")
        return CourseView(
            course_id=str(course["course_id"]),
            tenant_id=str(course["tenant_id"]),
            title=str(course["title"]),
            description=str(course["description"]),
            status=str(course["status"]),
            created_by=str(course["created_by"]),
            created_at=str(course["created_at"]),
            updated_at=str(course["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published course model"
        ) from exc


def _module_from(payload: Mapping[str, Any]) -> ModuleView:
    try:
        module = _require_mapping(payload, "module", "module envelope")
        return ModuleView(
            module_id=str(module["module_id"]),
            course_id=str(module["course_id"]),
            tenant_id=str(module["tenant_id"]),
            title=str(module["title"]),
            position=int(module["position"]),
            status=str(module["status"]),
            created_at=str(module["created_at"]),
            updated_at=str(module["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published module model"
        ) from exc


def _lesson_from(payload: Mapping[str, Any]) -> LessonView:
    try:
        lesson = _require_mapping(payload, "lesson", "lesson envelope")
        return LessonView(
            lesson_id=str(lesson["lesson_id"]),
            module_id=str(lesson["module_id"]),
            tenant_id=str(lesson["tenant_id"]),
            title=str(lesson["title"]),
            content=str(lesson["content"]),
            position=int(lesson["position"]),
            status=str(lesson["status"]),
            created_at=str(lesson["created_at"]),
            updated_at=str(lesson["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published lesson model"
        ) from exc


def _assignment_from(payload: Mapping[str, Any]) -> AssignmentView:
    try:
        assignment = _require_mapping(payload, "assignment", "assignment envelope")
        return AssignmentView(
            assignment_id=str(assignment["assignment_id"]),
            lesson_id=str(assignment["lesson_id"]),
            tenant_id=str(assignment["tenant_id"]),
            title=str(assignment["title"]),
            instructions=str(assignment["instructions"]),
            status=str(assignment["status"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published assignment model"
        ) from exc


def _enrollment_from(payload: Mapping[str, Any]) -> EnrollmentView:
    try:
        enrollment = _require_mapping(payload, "enrollment", "enrollment envelope")
        return EnrollmentView(
            enrollment_id=str(enrollment["enrollment_id"]),
            tenant_id=str(enrollment["tenant_id"]),
            course_id=str(enrollment["course_id"]),
            student_identity_id=str(enrollment["student_identity_id"]),
            status=str(enrollment["status"]),
            created_at=str(enrollment["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published enrollment model"
        ) from exc


def _submission_from(payload: Mapping[str, Any]) -> SubmissionView:
    try:
        submission = _require_mapping(payload, "submission", "submission envelope")
        return SubmissionView(
            submission_id=str(submission["submission_id"]),
            tenant_id=str(submission["tenant_id"]),
            assignment_id=str(submission["assignment_id"]),
            student_identity_id=str(submission["student_identity_id"]),
            attempt=int(submission["attempt"]),
            content=str(submission["content"]),
            status=str(submission["status"]),
            created_at=str(submission["created_at"]),
            updated_at=str(submission["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published submission model"
        ) from exc


class LearningClient:
    """Value-only consumer over the published Learning contract.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that issued
    them.
    """

    __slots__ = ("_channel", "__weakref__")

    def __init__(self, channel: str) -> None:
        if not isinstance(channel, str) or not channel:
            raise ValueError("a learning contract channel handle is required")
        self._channel = channel

    # ------------------------------------------------------------- commands
    def create_course(
        self,
        subject_credential: str | None,
        *,
        title: str,
        description: str,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CourseView:
        """``POST /api/v1/learning/courses`` — creates the course as ``DRAFT``."""
        return _course_from(
            self._request(
                "POST",
                "/api/v1/learning/courses",
                subject_credential=subject_credential,
                idempotency_key=idempotency_key,
                request_id=request_id,
                correlation_id=correlation_id,
                body={"title": title, "description": description},
            )
        )

    def publish_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CourseView:
        """``POST /api/v1/learning/courses/{course_id}/publish`` — ``DRAFT → PUBLISHED``."""
        return _course_from(
            self._request(
                "POST",
                f"/api/v1/learning/courses/{quote(course_id, safe='')}/publish",
                subject_credential=subject_credential,
                idempotency_key=idempotency_key,
                request_id=request_id,
                correlation_id=correlation_id,
                body={},
            )
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
    ) -> ModuleView:
        """``POST /api/v1/learning/courses/{course_id}/modules``."""
        return _module_from(
            self._request(
                "POST",
                f"/api/v1/learning/courses/{quote(course_id, safe='')}/modules",
                subject_credential=subject_credential,
                idempotency_key=idempotency_key,
                request_id=request_id,
                correlation_id=correlation_id,
                body={"title": title, "position": position},
            )
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
    ) -> LessonView:
        """``POST /api/v1/learning/modules/{module_id}/lessons``."""
        return _lesson_from(
            self._request(
                "POST",
                f"/api/v1/learning/modules/{quote(module_id, safe='')}/lessons",
                subject_credential=subject_credential,
                idempotency_key=idempotency_key,
                request_id=request_id,
                correlation_id=correlation_id,
                body={"title": title, "content": content, "position": position},
            )
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
    ) -> AssignmentView:
        """``POST /api/v1/learning/lessons/{lesson_id}/assignments``."""
        return _assignment_from(
            self._request(
                "POST",
                f"/api/v1/learning/lessons/{quote(lesson_id, safe='')}/assignments",
                subject_credential=subject_credential,
                idempotency_key=idempotency_key,
                request_id=request_id,
                correlation_id=correlation_id,
                body={"title": title, "instructions": instructions},
            )
        )

    def enroll(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> EnrollmentView:
        """``POST /api/v1/learning/courses/{course_id}/enrollments`` — the verified identity."""
        return _enrollment_from(
            self._request(
                "POST",
                f"/api/v1/learning/courses/{quote(course_id, safe='')}/enrollments",
                subject_credential=subject_credential,
                idempotency_key=idempotency_key,
                request_id=request_id,
                correlation_id=correlation_id,
                body={},
            )
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
    ) -> SubmissionView:
        """``POST /api/v1/learning/assignments/{assignment_id}/submissions``."""
        return _submission_from(
            self._request(
                "POST",
                f"/api/v1/learning/assignments/{quote(assignment_id, safe='')}/submissions",
                subject_credential=subject_credential,
                idempotency_key=idempotency_key,
                request_id=request_id,
                correlation_id=correlation_id,
                body={"attempt": attempt, "content": content},
            )
        )

    # ---------------------------------------------------------------- reads
    def get_course(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CourseView:
        """``GET /api/v1/learning/courses/{course_id}``."""
        return _course_from(
            self._request(
                "GET",
                f"/api/v1/learning/courses/{quote(course_id, safe='')}",
                subject_credential=subject_credential,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        )

    def get_assignment(
        self,
        subject_credential: str | None,
        assignment_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AssignmentView:
        """``GET /api/v1/learning/assignments/{assignment_id}``."""
        return _assignment_from(
            self._request(
                "GET",
                f"/api/v1/learning/assignments/{quote(assignment_id, safe='')}",
                subject_credential=subject_credential,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        )

    def get_my_enrollment(
        self,
        subject_credential: str | None,
        course_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> EnrollmentView:
        """``GET /api/v1/learning/courses/{course_id}/enrollments/me``."""
        return _enrollment_from(
            self._request(
                "GET",
                f"/api/v1/learning/courses/{quote(course_id, safe='')}/enrollments/me",
                subject_credential=subject_credential,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        )

    def get_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SubmissionView:
        """``GET /api/v1/learning/submissions/{submission_id}``."""
        return _submission_from(
            self._request(
                "GET",
                f"/api/v1/learning/submissions/{quote(submission_id, safe='')}",
                subject_credential=subject_credential,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        )

    # ------------------------------------------------------------- internals
    def _request(
        self,
        method: str,
        path: str,
        *,
        subject_credential: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        headers: list[tuple[str, str]] = []
        if subject_credential is not None:
            headers.append(("authorization", f"Bearer {subject_credential}"))
        if idempotency_key is not None:
            headers.append(("idempotency-key", idempotency_key))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))
        status, payload = _call_contract()(
            self._channel, method, path, headers, body
        )
        if 200 <= status < 300:
            return payload
        if status >= 500:
            raise ContractViolation(f"learning contract failed with status {status}")
        raise self._refused(status, payload)

    @staticmethod
    def _refused(status: int, payload: Mapping[str, Any]) -> AccessRefused:
        """Rebuild the published refusal; an answer outside the envelope fails closed."""
        error = payload.get("error")
        if not isinstance(error, Mapping) or not isinstance(error.get("code"), str):
            raise ContractViolation(
                "the learning contract answered outside its published error envelope"
            )
        details = error.get("details")
        return AccessRefused(
            error["code"],
            status_code=status,
            details=dict(details) if isinstance(details, Mapping) else {},
        )


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the boundary:
    after ``import learning_service.reader`` this module's namespace holds no
    module object, no channel table, no transport class and no application.
    """
    from learning_service.transport import call_contract

    return call_contract


def build_client(app: Any) -> LearningClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — this component's published ASGI contract — is handed to the
    provider-side channel table and stays there: the client receives the
    handle back, not the application. A client dropped by its consumer
    revokes that handle, so the table is not a place where applications
    accumulate.
    """
    from learning_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = LearningClient(channel)
    revoke_on_death(client, channel)
    return client
