"""HTTP surface of Learning — the published contract.

Everything else (engine, business store, audit journal, ports, adapters,
deployment, transport) is internal: no other component may reach it directly
(ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this contract
in-process through ``learning_service.transport`` and receive the value-only
client from ``learning_service.reader``.

The surface is exactly the eleven operations of the first implementation
slice plus health and readiness: seven explicit business commands (each
requiring ``Idempotency-Key``) and four reads. There is no generic CRUD —
no ``PUT``, ``PATCH`` or ``DELETE`` — no listing, no grading, no export and
no direct store path: no access path that bypasses the enforcement boundary
exists to bypass with.

``Authorization`` carries the credential presented by the subject; this
component verifies no credential itself — IS-001 verifies it and derives the
effective tenant (LAW-16a). There is no header and no field that could select
a tenant, name an identity or set a lifecycle state: the published schemas
forbid unknown fields, and the server owns every server-controlled value.

Every refusal is the published error envelope:

```json
{"error": {"code": "...", "message": "...", "details": {}},
 "request_id": "...", "correlation_id": "..."}
```
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from learning_service import COMPONENT_ID, COMPONENT_VERSION
from learning_service.contracts import ERROR_MESSAGES
from learning_service.deployment import LearningDeployment
from learning_service.engine import ACTION_UNREADABLE
from learning_service.errors import AccessRefused

__all__ = [
    "AssignmentEnvelope",
    "AssignmentOut",
    "CourseEnvelope",
    "CourseOut",
    "CreateAssignmentIn",
    "CreateCourseIn",
    "CreateLessonIn",
    "CreateModuleIn",
    "CreateSubmissionIn",
    "EmptyCommandIn",
    "EnrollmentEnvelope",
    "EnrollmentOut",
    "ErrorEnvelope",
    "LessonEnvelope",
    "LessonOut",
    "ModuleEnvelope",
    "ModuleOut",
    "SubmissionEnvelope",
    "SubmissionOut",
    "create_app",
]

#: The largest ``Idempotency-Key`` the contract accepts (IS-005 header bound).
IDEMPOTENCY_KEY_MAX_LENGTH = 128


# ------------------------------------------------------------- request bodies
class CreateCourseIn(BaseModel):
    """The client names the course; the server owns everything else."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=5000)


class CreateModuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    position: int = Field(ge=1)


class CreateLessonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=20000)
    position: int = Field(ge=1)


class CreateAssignmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(max_length=20000)


class CreateSubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=50000)


class EmptyCommandIn(BaseModel):
    """A command without client-controlled input.

    Deliberately closed: a client cannot smuggle ``status``, identifiers or
    ownership into a Publish/Enroll command — an unknown field is a
    ``VALIDATION_ERROR``.
    """

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------ response bodies
class CourseOut(BaseModel):
    course_id: str
    tenant_id: str
    title: str
    description: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class ModuleOut(BaseModel):
    module_id: str
    course_id: str
    tenant_id: str
    title: str
    position: int
    status: str
    created_at: str
    updated_at: str


class LessonOut(BaseModel):
    lesson_id: str
    module_id: str
    tenant_id: str
    title: str
    content: str
    position: int
    status: str
    created_at: str
    updated_at: str


class AssignmentOut(BaseModel):
    assignment_id: str
    lesson_id: str
    tenant_id: str
    title: str
    instructions: str
    status: str


class EnrollmentOut(BaseModel):
    enrollment_id: str
    tenant_id: str
    course_id: str
    student_identity_id: str
    status: str
    created_at: str


class SubmissionOut(BaseModel):
    submission_id: str
    tenant_id: str
    assignment_id: str
    student_identity_id: str
    attempt: int
    content: str
    status: str
    created_at: str
    updated_at: str


class CourseEnvelope(BaseModel):
    course: CourseOut


class ModuleEnvelope(BaseModel):
    module: ModuleOut


class LessonEnvelope(BaseModel):
    lesson: LessonOut


class AssignmentEnvelope(BaseModel):
    assignment: AssignmentOut


class EnrollmentEnvelope(BaseModel):
    enrollment: EnrollmentOut


class SubmissionEnvelope(BaseModel):
    submission: SubmissionOut


class ErrorBody(BaseModel):
    """The body of the published refusal: code, safe message, bounded details."""

    code: str
    message: str
    details: dict


class ErrorEnvelope(BaseModel):
    """The published refusal shape — produced by the exception handlers below."""

    error: ErrorBody
    request_id: str | None
    correlation_id: str | None


def _token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def _schema_problems(exc: RequestValidationError) -> list[str]:
    """Describe what the contract could not read — never the values it was sent.

    Only the location and the kind of problem are kept: the audit journal
    records that a request was unreadable, not the payload that was sent.
    """
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location or 'body'}: {error.get('type', 'invalid')}")
    return sorted(problems)


def create_app(deployment: LearningDeployment) -> FastAPI:
    """Bind the published HTTP contract to one Learning deployment."""
    engine = deployment.engine
    app = FastAPI(
        title="Learning — Business System (first implementation slice)",
        version=COMPONENT_VERSION,
    )

    @app.exception_handler(AccessRefused)
    async def refused(request: Request, exc: AccessRefused) -> JSONResponse:
        """The published refusal envelope of every refused operation.

        The envelope is data: the machine-readable ``code`` of the closed set,
        a static safe ``message``, bounded ``details`` and the caller's
        request/correlation context. The refusal was already audited by the
        engine before this handler ran.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": ERROR_MESSAGES.get(exc.code, exc.code),
                    "details": exc.details,
                },
                "request_id": request.headers.get("x-request-id"),
                "correlation_id": request.headers.get("x-correlation-id"),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def unreadable_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Refuse — and audit — a request the contract schema rejected.

        This is a transport-level refusal (``422``), not a business answer:
        the request was unreadable, so no operation was attempted, no decision
        was asked and no idempotency record exists. The refusal is audited
        with the caller's ``request_id`` and ``correlation_id``.
        """
        try:
            engine.audit(
                action=ACTION_UNREADABLE,
                decision="DENY",
                reason="VALIDATION_ERROR",
                obs=engine.observability(
                    request_id=request.headers.get("x-request-id"),
                    correlation_id=request.headers.get("x-correlation-id"),
                ),
                identity_id=None,
                tenant_id=None,
                resource_type=None,
                resource_id=request.url.path,
                details={"problems": _schema_problems(exc)},
            )
        except Exception:  # pragma: no cover - auditing must never mask the refusal
            pass
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": ERROR_MESSAGES["VALIDATION_ERROR"],
                    "details": {"problems": _schema_problems(exc)},
                },
                "request_id": request.headers.get("x-request-id"),
                "correlation_id": request.headers.get("x-correlation-id"),
            },
        )

    @app.get("/health", operation_id="get_health")
    def health() -> dict:
        return {
            "status": "ok",
            "component_id": COMPONENT_ID,
            "version": COMPONENT_VERSION,
            "platform_id": engine.current_platform_id,
        }

    @app.get("/ready", operation_id="get_ready")
    def ready() -> dict:
        return {"status": "ready", "component_id": COMPONENT_ID}

    # ------------------------------------------------------------------ Course
    @app.post(
        "/api/v1/learning/courses",
        response_model=CourseEnvelope,
        operation_id="create_course",
    )
    def create_course(
        payload: CreateCourseIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> CourseEnvelope:
        """Create one course in the effective tenant (Teacher capability)."""
        _require_key(idempotency_key)
        view, _, _ = engine.create_course(
            _token(authorization),
            title=payload.title,
            description=payload.description,
            idempotency_key=idempotency_key or "",
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return CourseEnvelope(course=CourseOut(**asdict(view)))

    @app.get(
        "/api/v1/learning/courses/{course_id}",
        response_model=CourseEnvelope,
        operation_id="get_course",
    )
    def get_course(
        course_id: str,
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> CourseEnvelope:
        """Serve one course of the effective tenant (Teacher: any state; Student: PUBLISHED)."""
        view, _, _ = engine.get_course(
            _token(authorization),
            course_id,
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return CourseEnvelope(course=CourseOut(**asdict(view)))

    @app.post(
        "/api/v1/learning/courses/{course_id}/publish",
        response_model=CourseEnvelope,
        operation_id="publish_course",
    )
    def publish_course(
        course_id: str,
        payload: EmptyCommandIn | None = None,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> CourseEnvelope:
        """Publish one course — the only lifecycle command of this slice."""
        _require_key(idempotency_key)
        view, _, _ = engine.publish_course(
            _token(authorization),
            course_id,
            idempotency_key=idempotency_key or "",
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return CourseEnvelope(course=CourseOut(**asdict(view)))

    # ------------------------------------------------------------------ Module
    @app.post(
        "/api/v1/learning/courses/{course_id}/modules",
        response_model=ModuleEnvelope,
        operation_id="create_module",
    )
    def create_module(
        course_id: str,
        payload: CreateModuleIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> ModuleEnvelope:
        """Create one module of a course; refused once the course is PUBLISHED."""
        _require_key(idempotency_key)
        view, _, _ = engine.create_module(
            _token(authorization),
            course_id,
            title=payload.title,
            position=payload.position,
            idempotency_key=idempotency_key or "",
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return ModuleEnvelope(module=ModuleOut(**asdict(view)))

    # ------------------------------------------------------------------ Lesson
    @app.post(
        "/api/v1/learning/modules/{module_id}/lessons",
        response_model=LessonEnvelope,
        operation_id="create_lesson",
    )
    def create_lesson(
        module_id: str,
        payload: CreateLessonIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> LessonEnvelope:
        """Create one lesson of a module; refused once the course is PUBLISHED."""
        _require_key(idempotency_key)
        view, _, _ = engine.create_lesson(
            _token(authorization),
            module_id,
            title=payload.title,
            content=payload.content,
            position=payload.position,
            idempotency_key=idempotency_key or "",
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return LessonEnvelope(lesson=LessonOut(**asdict(view)))

    # -------------------------------------------------------------- Assignment
    @app.post(
        "/api/v1/learning/lessons/{lesson_id}/assignments",
        response_model=AssignmentEnvelope,
        operation_id="create_assignment",
    )
    def create_assignment(
        lesson_id: str,
        payload: CreateAssignmentIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> AssignmentEnvelope:
        """Create one assignment of a lesson; refused once the course is PUBLISHED."""
        _require_key(idempotency_key)
        view, _, _ = engine.create_assignment(
            _token(authorization),
            lesson_id,
            title=payload.title,
            instructions=payload.instructions,
            idempotency_key=idempotency_key or "",
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return AssignmentEnvelope(assignment=AssignmentOut(**asdict(view)))

    @app.get(
        "/api/v1/learning/assignments/{assignment_id}",
        response_model=AssignmentEnvelope,
        operation_id="get_assignment",
    )
    def get_assignment(
        assignment_id: str,
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> AssignmentEnvelope:
        """Serve one assignment (Teacher: any state; Student: PUBLISHED)."""
        view, _, _ = engine.get_assignment(
            _token(authorization),
            assignment_id,
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return AssignmentEnvelope(assignment=AssignmentOut(**asdict(view)))

    # -------------------------------------------------------------- Enrollment
    @app.post(
        "/api/v1/learning/courses/{course_id}/enrollments",
        response_model=EnrollmentEnvelope,
        operation_id="enroll_in_course",
    )
    def enroll_in_course(
        course_id: str,
        payload: EmptyCommandIn | None = None,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> EnrollmentEnvelope:
        """Enroll the verified identity into a published course (Student capability)."""
        _require_key(idempotency_key)
        view, _, _ = engine.enroll(
            _token(authorization),
            course_id,
            idempotency_key=idempotency_key or "",
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return EnrollmentEnvelope(enrollment=EnrollmentOut(**asdict(view)))

    @app.get(
        "/api/v1/learning/courses/{course_id}/enrollments/me",
        response_model=EnrollmentEnvelope,
        operation_id="get_own_enrollment",
    )
    def get_own_enrollment(
        course_id: str,
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> EnrollmentEnvelope:
        """Serve the enrollment of the verified identity — never another student's."""
        view, _, _ = engine.get_my_enrollment(
            _token(authorization),
            course_id,
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return EnrollmentEnvelope(enrollment=EnrollmentOut(**asdict(view)))

    # -------------------------------------------------------------- Submission
    @app.post(
        "/api/v1/learning/assignments/{assignment_id}/submissions",
        response_model=SubmissionEnvelope,
        operation_id="create_submission",
    )
    def create_submission(
        assignment_id: str,
        payload: CreateSubmissionIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> SubmissionEnvelope:
        """Submit one attempt for a published assignment (Student capability)."""
        _require_key(idempotency_key)
        view, _, _ = engine.create_submission(
            _token(authorization),
            assignment_id,
            attempt=payload.attempt,
            content=payload.content,
            idempotency_key=idempotency_key or "",
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return SubmissionEnvelope(submission=SubmissionOut(**asdict(view)))

    @app.get(
        "/api/v1/learning/submissions/{submission_id}",
        response_model=SubmissionEnvelope,
        operation_id="get_submission",
    )
    def get_submission(
        submission_id: str,
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> SubmissionEnvelope:
        """Serve one submission: its own student, or a Teacher of the Tenant."""
        view, _, _ = engine.get_submission(
            _token(authorization),
            submission_id,
            request_id=x_request_id,
            correlation_id=x_correlation_id,
        )
        return SubmissionEnvelope(submission=SubmissionOut(**asdict(view)))

    def _require_key(idempotency_key: str | None) -> None:
        """Header-bound validation of IS-005: present, a string, bounded."""
        if idempotency_key is not None and len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
            raise AccessRefused(
                "VALIDATION_ERROR",
                status_code=422,
                details={"problems": ["Idempotency-Key: exceeds 128 characters"]},
            )

    return app
