"""HTTP surface of Learning (SCS-001) — the published contract.

Everything else (engine, stores, audit journal, ports, adapters,
deployment, transport) is internal: no other component may reach it
directly (ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this
contract in-process through ``learning_service.transport`` and receive the
value-only client from ``learning_service.reader``.

The surface is deliberately narrow: the single-submission read, the
teacher submission-discovery list and the single-submission review behind
the same enforcement chain, plus health and readiness. There is no bulk
export, no search, no pagination, no grading, no feedback, no comments and
no direct store path — no access path that bypasses the enforcement
boundary exists to bypass with.

``Authorization`` carries the credential presented by the subject; this
component verifies no credential itself — the decision dependency has it
verified by IS-001 inside the decision. ``X-Tenant-Id`` is a
caller-supplied cross-check only and is forwarded as such; it can never
select the effective tenant (LAW-16a).

Errors use the approved SCS-001 envelope: ``error.code`` /
``error.message`` / ``error.details`` plus top-level ``request_id`` /
``correlation_id``. The stable internal reason is preserved in
``error.details.reason``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from learning_service import COMPONENT_ID, COMPONENT_VERSION
from learning_service.deployment import LearningDeployment
from learning_service.errors import AccessRefused

__all__ = [
    "ERROR_CODES",
    "AssignmentCreateIn",
    "AssignmentOut",
    "CourseCreateIn",
    "CourseOut",
    "ErrorBody",
    "ErrorEnvelope",
    "LessonCreateIn",
    "LessonOut",
    "ModuleCreateIn",
    "ModuleOut",
    "SubmissionListOut",
    "SubmissionOut",
    "create_app",
    "error_code_for",
]


class SubmissionOut(BaseModel):
    """The published submission representation: the ten fields, nothing else.

    ``reviewed_by`` / ``reviewed_at`` are ``None`` until the first successful
    review and are the only review fact published.
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


class SubmissionListOut(BaseModel):
    """The published teacher-discovery representation: exactly ``{items: [...]}``."""

    items: list[SubmissionOut]


class AssignmentOut(BaseModel):
    """The published representation of one authored Assignment."""

    assignment_id: str
    lesson_id: str
    title: str
    instructions: str
    status: str


class LessonOut(BaseModel):
    """The published representation of one authored Lesson and its assignments."""

    lesson_id: str
    module_id: str
    title: str
    content: str
    position: int
    status: str
    assignments: list[AssignmentOut]


class ModuleOut(BaseModel):
    """The published representation of one authored Module and its lessons."""

    module_id: str
    course_id: str
    title: str
    position: int
    status: str
    lessons: list[LessonOut]


class CourseOut(BaseModel):
    """The published representation of one Course hierarchy.

    ``modules`` nests the full navigation chain
    ``Course → Module → Lesson → Assignment`` in deterministic order.
    """

    course_id: str
    title: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str
    modules: list[ModuleOut]


class CourseCreateIn(BaseModel):
    """The create-course command payload."""

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str


class ModuleCreateIn(BaseModel):
    """The create-module command payload."""

    model_config = ConfigDict(extra="forbid")

    title: str
    position: int


class LessonCreateIn(BaseModel):
    """The create-lesson command payload."""

    model_config = ConfigDict(extra="forbid")

    title: str
    content: str
    position: int


class AssignmentCreateIn(BaseModel):
    """The create-assignment command payload."""

    model_config = ConfigDict(extra="forbid")

    title: str
    instructions: str


class ErrorBody(BaseModel):
    """The ``error`` object of the approved SCS-001 envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any]


class ErrorEnvelope(BaseModel):
    """The approved SCS-001 error envelope of every refusal."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str
    correlation_id: str


#: The minimal published error-code vocabulary of SCS-001. The HTTP status
#: selects the code; the stable internal reason is preserved in
#: ``error.details.reason``.
ERROR_CODES: dict[str, dict[str, Any]] = {
    "AUTHENTICATION_REQUIRED": {
        "status": 401,
        "message": "Subject authentication is required.",
    },
    "AUTHORIZATION_DENIED": {
        "status": 403,
        "message": "Operation is not permitted.",
    },
    "NOT_FOUND": {
        "status": 404,
        "message": "Requested resource was not found.",
    },
    "INVALID_REQUEST": {
        "status": 422,
        "message": "Request does not match the published contract.",
    },
    "VALIDATION_ERROR": {
        "status": 422,
        "message": "Request payload is not valid.",
    },
    "INVALID_STATE_TRANSITION": {
        "status": 409,
        "message": "Operation is not valid for the current submission state.",
    },
    "ALREADY_REVIEWED": {
        "status": 409,
        "message": "The submission has already been reviewed.",
    },
    "IDEMPOTENCY_KEY_REQUIRED": {
        "status": 400,
        "message": "An Idempotency-Key header is required for this operation.",
    },
    "IDEMPOTENCY_CONFLICT": {
        "status": 409,
        "message": "The Idempotency-Key was already used with a different request.",
    },
    "DEPENDENCY_UNAVAILABLE": {
        "status": 503,
        "message": "Authorization dependency is unavailable.",
    },
}

_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)

_NOT_FOUND_REASONS = frozenset(
    {
        "assignment_unknown",
        "submission_unknown",
        "course_unknown",
        "module_unknown",
        "lesson_unknown",
    }
)


def error_code_for(reason: str) -> str:
    """The published error code for one internal refusal reason.

    The mapping follows the HTTP status of the refusal; every reason that
    denies with 403 shares ``AUTHORIZATION_DENIED`` while its own stable
    value is preserved in ``error.details.reason``.
    """
    if reason in _AUTHENTICATION_DENIALS:
        return "AUTHENTICATION_REQUIRED"
    if reason in _NOT_FOUND_REASONS:
        return "NOT_FOUND"
    if reason == "malformed_request":
        return "INVALID_REQUEST"
    if reason == "validation_error":
        return "VALIDATION_ERROR"
    if reason == "invalid_state_transition":
        return "INVALID_STATE_TRANSITION"
    if reason == "already_reviewed":
        return "ALREADY_REVIEWED"
    if reason == "idempotency_key_required":
        return "IDEMPOTENCY_KEY_REQUIRED"
    if reason == "idempotency_conflict":
        return "IDEMPOTENCY_CONFLICT"
    if reason == "authorization_unavailable":
        return "DEPENDENCY_UNAVAILABLE"
    return "AUTHORIZATION_DENIED"


def _token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def _refused(exc: AccessRefused) -> JSONResponse:
    """Render one audited refusal in the approved SCS-001 envelope."""
    code = error_code_for(exc.reason)
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=str(ERROR_CODES[code]["message"]),
            details={"reason": exc.reason},
        ),
        request_id=exc.request_id or "",
        correlation_id=exc.correlation_id or exc.request_id or "",
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope.model_dump(),
    )


def _schema_problems(exc: RequestValidationError) -> list[str]:
    """Describe what the contract could not read — never the values it was sent."""
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location or 'body'}: {error.get('type', 'invalid')}")
    return sorted(problems)


def create_app(deployment: LearningDeployment) -> FastAPI:
    """Bind the published HTTP contract to one Learning deployment."""
    engine = deployment.engine
    app = FastAPI(
        title="SCS-001 Learning",
        version=COMPONENT_VERSION,
    )

    @app.exception_handler(RequestValidationError)
    async def unreadable_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Refuse — and audit — a request the contract schema rejected."""
        refusal = engine.refuse_unreadable_request(
            path=request.url.path,
            schema_problems=_schema_problems(exc),
            request_id=request.headers.get("x-request-id"),
            correlation_id=request.headers.get("x-correlation-id"),
        )
        return _refused(refusal)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "component_id": COMPONENT_ID,
            "version": COMPONENT_VERSION,
            "platform_id": engine.current_platform_id,
        }

    @app.get("/ready")
    def ready() -> dict:
        return {"status": "ready", "component_id": COMPONENT_ID}

    @app.get("/api/v1/learning/submissions/{submission_id}", response_model=SubmissionOut)
    def read_submission(
        submission_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve one owned submission — the read operation ``learning.submissions.read``."""
        try:
            view, _, _ = engine.read_submission(
                _token(authorization),
                submission_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return SubmissionOut(
            submission_id=view.submission_id,
            assignment_id=view.assignment_id,
            student_identity_id=view.student_identity_id,
            attempt=view.attempt,
            content=dict(view.content),
            status=view.status,
            created_at=view.created_at,
            updated_at=view.updated_at,
            reviewed_by=view.reviewed_by,
            reviewed_at=view.reviewed_at,
        )

    @app.post(
        "/api/v1/learning/submissions/{submission_id}/review",
        response_model=SubmissionOut,
    )
    def review_submission(
        submission_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Record the review of one owned submission — the state-changing command
        ``learning.submissions.review``.

        ``Idempotency-Key`` is mandatory: the review is a state-changing
        command delivered through IS-005, so an exact replay returns the same
        result without a second effect and a changed binding is refused.
        """
        try:
            view, _, _ = engine.review_submission(
                _token(authorization),
                submission_id,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return SubmissionOut(
            submission_id=view.submission_id,
            assignment_id=view.assignment_id,
            student_identity_id=view.student_identity_id,
            attempt=view.attempt,
            content=dict(view.content),
            status=view.status,
            created_at=view.created_at,
            updated_at=view.updated_at,
            reviewed_by=view.reviewed_by,
            reviewed_at=view.reviewed_at,
        )

    @app.get(
        "/api/v1/learning/assignments/{assignment_id}/submissions",
        response_model=SubmissionListOut,
    )
    def list_submissions(
        assignment_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """List one Assignment's submissions — the read operation
        ``learning.submissions.list`` (teacher discovery). An empty
        Assignment answers 200 with ``{"items": []}``."""
        try:
            views, _, _ = engine.list_submissions(
                _token(authorization),
                assignment_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return SubmissionListOut(
            items=[
                SubmissionOut(
                    submission_id=view.submission_id,
                    assignment_id=view.assignment_id,
                    student_identity_id=view.student_identity_id,
                    attempt=view.attempt,
                    content=dict(view.content),
                    status=view.status,
                    created_at=view.created_at,
                    updated_at=view.updated_at,
                    reviewed_by=view.reviewed_by,
                    reviewed_at=view.reviewed_at,
                )
                for view in views
            ]
        )

    # ------------------------------------------------- content authoring API
    def _assignment_out(view: Any) -> AssignmentOut:
        return AssignmentOut(
            assignment_id=view.assignment_id,
            lesson_id=view.lesson_id,
            title=view.title,
            instructions=view.instructions,
            status=view.status,
        )

    def _lesson_out(view: Any) -> LessonOut:
        return LessonOut(
            lesson_id=view.lesson_id,
            module_id=view.module_id,
            title=view.title,
            content=view.content,
            position=view.position,
            status=view.status,
            assignments=[_assignment_out(a) for a in view.assignments],
        )

    def _module_out(view: Any) -> ModuleOut:
        return ModuleOut(
            module_id=view.module_id,
            course_id=view.course_id,
            title=view.title,
            position=view.position,
            status=view.status,
            lessons=[_lesson_out(l) for l in view.lessons],
        )

    def _course_out(view: Any) -> CourseOut:
        return CourseOut(
            course_id=view.course_id,
            title=view.title,
            description=view.description,
            status=view.status,
            created_by=view.created_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
            modules=[_module_out(m) for m in view.modules],
        )

    @app.post(
        "/api/v1/learning/courses",
        response_model=CourseOut,
        status_code=201,
    )
    def create_course(
        payload: CourseCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Create one owned Course — the command ``learning.courses.create``.

        ``Idempotency-Key`` is mandatory: the command is a state-changing
        command delivered through IS-005. The effective tenant comes from the
        verified identity through the published chain — never from the
        payload and never from ``X-Tenant-Id``, which is a cross-check only
        (LAW-16a). A new Course is created in ``DRAFT``.
        """
        try:
            view, _, _ = engine.create_course(
                _token(authorization),
                title=payload.title,
                description=payload.description,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _course_out(view)

    @app.get(
        "/api/v1/learning/courses/{course_id}",
        response_model=CourseOut,
    )
    def read_course(
        course_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Serve one owned Course hierarchy — the read operation
        ``learning.courses.read`` for a published Course (Teachers and
        Students) or ``learning.courses.read_unpublished`` otherwise
        (Teachers). The question asked depends on the Course's own stored
        state; the answer nests the full navigation chain."""
        try:
            view, _, _ = engine.read_course(
                _token(authorization),
                course_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _course_out(view)

    @app.post(
        "/api/v1/learning/courses/{course_id}/modules",
        response_model=ModuleOut,
        status_code=201,
    )
    def create_module(
        course_id: str,
        payload: ModuleCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Create one Module inside a Course — the command
        ``learning.modules.create``. Only a ``DRAFT`` Course accepts modules;
        the Module belongs to the Course's Tenant and is created in
        ``DRAFT``."""
        try:
            view, _, _ = engine.create_module(
                _token(authorization),
                course_id,
                title=payload.title,
                position=payload.position,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _module_out(view)

    @app.post(
        "/api/v1/learning/modules/{module_id}/lessons",
        response_model=LessonOut,
        status_code=201,
    )
    def create_lesson(
        module_id: str,
        payload: LessonCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Create one Lesson inside a Module — the command
        ``learning.lessons.create``. Only a ``DRAFT`` Module accepts lessons;
        the Lesson belongs to the Module's Tenant and is created in
        ``DRAFT``."""
        try:
            view, _, _ = engine.create_lesson(
                _token(authorization),
                module_id,
                title=payload.title,
                content=payload.content,
                position=payload.position,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _lesson_out(view)

    @app.post(
        "/api/v1/learning/lessons/{lesson_id}/assignments",
        response_model=AssignmentOut,
        status_code=201,
    )
    def create_assignment(
        lesson_id: str,
        payload: AssignmentCreateIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Create one Assignment inside a Lesson — the command
        ``learning.assignments.create``. The existing Assignment model is
        extended, not replaced: the created record carries its parent Lesson,
        the Lesson's Tenant, title/instructions and the ``DRAFT`` status, and
        stays the Assignment identity the submission flow serves."""
        try:
            view, _, _ = engine.create_assignment(
                _token(authorization),
                lesson_id,
                title=payload.title,
                instructions=payload.instructions,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _assignment_out(view)

    @app.post(
        "/api/v1/learning/courses/{course_id}/publish",
        response_model=CourseOut,
    )
    def publish_course(
        course_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Publish one owned Course and its whole hierarchy atomically — the
        command ``learning.courses.publish``.

        The structurally valid ``DRAFT`` hierarchy flips to ``PUBLISHED`` or
        nothing does: a validation or dependency failure leaves every
        descendant unpublished. There is no unpublish; the published
        hierarchy is immutable."""
        try:
            view, _, _ = engine.publish_course(
                _token(authorization),
                course_id,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _course_out(view)

    @app.post(
        "/api/v1/learning/courses/{course_id}/archive",
        response_model=CourseOut,
    )
    def archive_course(
        course_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Any:
        """Archive one owned Course and its whole hierarchy atomically — the
        command ``learning.courses.archive``. Only a ``PUBLISHED`` Course can
        be archived; no unarchive exists."""
        try:
            view, _, _ = engine.archive_course(
                _token(authorization),
                course_id,
                claimed_tenant_id=x_tenant_id,
                idempotency_key=idempotency_key,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            return _refused(exc)
        return _course_out(view)

    return app
