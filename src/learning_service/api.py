"""HTTP surface of Learning (SCS-001) — the published contract.

Everything else (engine, stores, audit journal, ports, adapters,
deployment, transport) is internal: no other component may reach it
directly (ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this
contract in-process through ``learning_service.transport`` and receive the
value-only client from ``learning_service.reader``.

The surface is deliberately narrow: the single-submission read and the
single-submission review behind the same enforcement chain, plus health and
readiness. There is no bulk export, no search, no pagination, no grading,
no feedback, no comments and no direct store path — no access path that
bypasses the enforcement boundary exists to bypass with.

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
    "ErrorBody",
    "ErrorEnvelope",
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

_NOT_FOUND_REASONS = frozenset({"submission_unknown"})


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

    return app
