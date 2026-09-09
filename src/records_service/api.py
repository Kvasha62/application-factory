"""HTTP surface of the Resource Boundary (IS-004) — the published contract.

Everything else (engine, resource store, audit journal, ports, adapters,
deployment, transport) is internal: no other component may reach it directly
(invariant 8, ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this
contract in-process through ``records_service.transport`` and receive the
value-only client from ``records_service.reader``.

The surface is deliberately narrow: two resource operations, both behind the
same enforcement chain, plus health and readiness. There is no bulk read, no
listing, no export and no direct store path — no access path that bypasses
the enforcement boundary exists to bypass with (invariant 6).

``Authorization`` carries the credential presented by the subject; this
component verifies no credential itself — the decision dependency has it
verified by IS-001 inside the decision. ``X-Tenant-Id`` is a caller-supplied
cross-check only and is forwarded as such; it can never select the effective
tenant (LAW-16a).
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from records_service import COMPONENT_ID, COMPONENT_VERSION
from records_service.deployment import RecordsDeployment
from records_service.errors import AccessRefused

__all__ = [
    "ErrorOut",
    "ResourceOut",
    "TransitionIn",
    "create_app",
]


class ResourceOut(BaseModel):
    """The published resource representation: the five fields, nothing else."""

    resource_id: str
    resource_type: str
    owner_component: str
    tenant_id: str
    state: str


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transition: str


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


def _refused(exc: AccessRefused, request_id: str | None) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "decision": exc.decision,
            "reason": exc.reason,
            "request_id": request_id,
        },
    )


def _schema_problems(exc: RequestValidationError) -> list[str]:
    """Describe what the contract could not read — never the values it was sent.

    Only the location and the kind of problem are kept: an audit journal
    records that a request was unreadable, not the payload that was sent.
    """
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        problems.append(f"{location or 'body'}: {error.get('type', 'invalid')}")
    return sorted(problems)


def create_app(deployment: RecordsDeployment) -> FastAPI:
    """Bind the published HTTP contract to one Resource Boundary deployment."""
    engine = deployment.engine
    app = FastAPI(
        title="IS-004 Data Ownership / Resource Boundary",
        version=COMPONENT_VERSION,
    )

    @app.exception_handler(RequestValidationError)
    async def unreadable_request(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Refuse — and audit — a request the contract schema rejected.

        This is a transport-level refusal (``422``), not an access answer:
        the request was unreadable, so no access attempt was made and no
        decision was asked. The refusal is audited with the caller's
        ``request_id`` and ``correlation_id`` (invariant 7).
        """
        refusal = engine.refuse_unreadable_request(
            path=request.url.path,
            schema_problems=_schema_problems(exc),
            request_id=request.headers.get("x-request-id"),
            correlation_id=request.headers.get("x-correlation-id"),
        )
        return JSONResponse(
            status_code=refusal.status_code,
            content={
                "detail": {
                    "decision": refusal.decision,
                    "reason": refusal.reason,
                    "request_id": request.headers.get("x-request-id"),
                }
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

    @app.get("/api/v1/resources/{resource_id}", response_model=ResourceOut)
    def read_resource(
        resource_id: str,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> ResourceOut:
        """Serve one owned resource — the read operation ``records.read``.

        The resource is returned only after the full enforcement chain
        allowed the access; every other outcome is a refusal with a stable
        reason, audited with ``request_id`` and ``correlation_id``.
        """
        try:
            view, _, _ = engine.read_resource(
                _token(authorization),
                resource_id,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            raise _refused(exc, x_request_id) from exc
        return ResourceOut(
            resource_id=view.resource_id,
            resource_type=view.resource_type,
            owner_component=view.owner_component,
            tenant_id=view.tenant_id,
            state=view.state,
        )

    @app.post(
        "/api/v1/resources/{resource_id}/transitions",
        response_model=ResourceOut,
    )
    def transition_resource(
        resource_id: str,
        payload: TransitionIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> ResourceOut:
        """Change the state of one owned resource — the write operation ``records.write``.

        The state machine is guarded by the owner: a repeated or
        non-applicable transition is refused with ``invalid_transition``
        after the authorization decision, so the operation needs no
        ``Idempotency-Key`` to stay safe (ARCHITECTURE.md §6.5 covers
        operations that could produce a second effect; this one cannot).
        """
        try:
            view, _, _ = engine.transition_resource(
                _token(authorization),
                resource_id,
                payload.transition,
                claimed_tenant_id=x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessRefused as exc:
            raise _refused(exc, x_request_id) from exc
        return ResourceOut(
            resource_id=view.resource_id,
            resource_type=view.resource_type,
            owner_component=view.owner_component,
            tenant_id=view.tenant_id,
            state=view.state,
        )

    return app
