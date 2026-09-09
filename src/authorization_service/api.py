"""HTTP surface of the Authorization Boundary (IS-003) — the published contract.

Everything else (engine, grant store, audit journal, policy chain, deployment,
transport) is internal: no other component may reach it directly (invariant 10,
ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this contract
in-process through ``authorization_service.transport`` and receive the
value-only client from ``authorization_service.reader``.

Two status families, on purpose:

* ``401`` / ``403`` / ``422`` — the *question* was refused: the asking component
  is not authenticated, not permitted to ask, or asked something unanswerable.
  No decision exists in that case, and none is invented;
* ``200`` with ``decision: DENY`` — the question was answered, and the answer is
  no. A denial is data, not a transport failure, and the consumer must enforce it.

This module publishes no application of any other component and composes
nothing: the deployment it is bound to is built by a composition root.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from authorization_service import COMPONENT_ID, COMPONENT_VERSION
from authorization_service.contracts import AuthorizationDecision, ResourceRef
from authorization_service.deployment import AuthorizationDeployment
from authorization_service.errors import AuthorizationServiceError

__all__ = [
    "DecisionIn",
    "DecisionOut",
    "ErrorOut",
    "ResourceIn",
    "ResourceOut",
    "create_app",
]


class ResourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: str
    resource_id: str
    tenant_id: str | None = Field(
        default=None,
        description=(
            "Tenant that owns the resource, stated by its data owner; it is never "
            "a statement about the identity of the caller"
        ),
    )


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    resource: ResourceIn


class ResourceOut(BaseModel):
    resource_type: str
    resource_id: str
    tenant_id: str | None = None


class DecisionOut(BaseModel):
    decision: str
    reason: str
    operation: str
    resource: ResourceOut
    subject_id: str | None = None
    tenant_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    decided_by: str


class ErrorOut(BaseModel):
    decision: str = "DENY"
    reason: str
    request_id: str | None = None


def _token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def _decision_out(decision: AuthorizationDecision) -> DecisionOut:
    return DecisionOut(
        decision=decision.decision.value,
        reason=decision.reason.value,
        operation=decision.operation,
        resource=ResourceOut(
            resource_type=decision.resource.resource_type,
            resource_id=decision.resource.resource_id,
            tenant_id=decision.resource.tenant_id,
        ),
        subject_id=decision.subject_id,
        tenant_id=decision.tenant_id,
        request_id=decision.request_id,
        correlation_id=decision.correlation_id,
        decided_by=decision.decided_by,
    )


def _refusal_detail(exc: AuthorizationServiceError, request_id: str | None) -> dict:
    """The published body of a refusal: the reason, and the id it was audited with."""
    return {
        "decision": "DENY",
        "reason": exc.reason.value,
        "request_id": exc.details.get("request_id") or request_id,
    }


def _refused(exc: AuthorizationServiceError, request_id: str | None) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=_refusal_detail(exc, request_id),
    )


def _schema_problems(exc: RequestValidationError) -> list[str]:
    """Describe what the contract could not read — never the values it was sent.

    Only the location and the kind of problem are kept: an audit journal records
    that a request was unreadable, not the payload of the component that sent it.
    """
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location or 'body'}: {error.get('type', 'invalid')}")
    return sorted(problems)


def create_app(deployment: AuthorizationDeployment) -> FastAPI:
    """Bind the published HTTP contract to one Authorization Boundary deployment."""
    engine = deployment.engine
    app = FastAPI(
        title="IS-003 Authorization Boundary / Permission Authority",
        version=COMPONENT_VERSION,
    )

    @app.exception_handler(RequestValidationError)
    async def unreadable_request(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Refuse — and audit — a request the contract schema rejected.

        The schema rejects a request before any handler of this component runs,
        so the refusal is produced here explicitly instead of silently: a
        malformed request to the decision contract is security-relevant, and
        every refusal of this component appears in its audit journal with the
        caller's ``request_id`` and ``correlation_id`` (invariant 9).

        This is a transport-level refusal (``422``), not an authorization
        answer: no decision was made, so none is reported.
        """
        refusal = engine.refuse_unreadable_request(
            _token(request.headers.get("authorization")),
            request_id=request.headers.get("x-request-id"),
            correlation_id=request.headers.get("x-correlation-id"),
            details={
                "schema_problems": _schema_problems(exc),
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=refusal.status_code,
            content={
                "detail": _refusal_detail(refusal, request.headers.get("x-request-id"))
            },
        )

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

    @app.post("/api/v1/decisions", response_model=DecisionOut)
    def decide(
        payload: DecisionIn,
        authorization: str | None = Header(default=None),
        x_subject_authorization: str | None = Header(
            default=None, alias="X-Subject-Authorization"
        ),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> DecisionOut:
        """Decide one access question for a verified subject.

        ``Authorization`` carries the credential of the *asking component*;
        ``X-Subject-Authorization`` carries the credential presented by the
        subject, which this component has verified by IS-001. ``X-Tenant-Id`` is
        a cross-check only and can never select the effective tenant. The
        operation changes no state of this component beyond its append-only
        audit journal, so it needs no ``Idempotency-Key`` (§6.5 covers
        state-changing operations).
        """
        try:
            decision, _, _ = engine.decide(
                _token(authorization),
                operation=payload.operation,
                resource=ResourceRef(
                    resource_type=payload.resource.resource_type,
                    resource_id=payload.resource.resource_id,
                    tenant_id=payload.resource.tenant_id,
                ),
                subject_credential=_token(x_subject_authorization),
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AuthorizationServiceError as exc:
            raise _refused(exc, x_request_id) from exc
        return _decision_out(decision)

    return app
