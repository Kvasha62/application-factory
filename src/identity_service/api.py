"""HTTP surface of Identity / Tenant Context (IS-001) — this API and nothing else.

The module owns identity's own published routes and a factory that binds them to
an engine. It publishes no object of any other component: not the Tenant
Authority deployment, not its contract application, not its engine or store. A
component that needs Tenant Authority talks to the component itself through
``tenant_authority.reader.TenantAuthorityClient``; wiring both components into
one process is the job of a composition root or a test fixture, not of this API.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from identity_service import COMPONENT_ID, COMPONENT_VERSION
from identity_service.engine import IdentityEngine
from identity_service.errors import AccessDenied
from identity_service.models import DenyReason

#: The published surface of this component: its own models and its own factory.
#: Names imported for use are not part of it, and nothing of another component is.
__all__ = [
    "AUTHENTICATION_DENIALS",
    "ContextOut",
    "ErrorOut",
    "IdentityOut",
    "RecordOut",
    "WriteIn",
    "create_app",
]

AUTHENTICATION_DENIALS = frozenset(
    {
        DenyReason.MISSING_IDENTITY,
        DenyReason.INVALID_IDENTITY,
        DenyReason.UNKNOWN_IDENTITY,
    }
)


class RecordOut(BaseModel):
    record_id: str
    tenant_id: str
    body: str


class WriteIn(BaseModel):
    body: str
    tenant_id: str | None = Field(default=None)


class IdentityOut(BaseModel):
    identity_id: str
    kind: str
    subject: str


class ContextOut(BaseModel):
    """Verified subject plus the effective Tenant of one request.

    ``source`` is part of the answer: it states that the effective tenant was
    derived from the verified identity, not from a caller claim. Tenant
    lifecycle state is not published here — it belongs to Tenant Authority and
    is read there, live, at the authorization boundary.
    """

    identity_id: str
    kind: str
    subject: str
    tenant_id: str
    platform_id: str
    source: str


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


def _status_for(reason: str) -> int:
    """401 for authentication failures, 403 for every other denial.

    An unmapped reason never falls through to a permissive status.
    """
    return 401 if reason in {denial.value for denial in AUTHENTICATION_DENIALS} else 403


def create_app(engine: IdentityEngine) -> FastAPI:
    """Bind this component's published API to one identity engine.

    The engine is supplied by whoever composes the deployment, so the HTTP
    surface neither assembles another component nor leaks it to a caller.
    """
    app = FastAPI(title="IS-001 Identity / Tenant Context", version=COMPONENT_VERSION)

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

    @app.get("/api/v1/me", response_model=IdentityOut)
    def me(
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> IdentityOut:
        try:
            identity, _, _ = engine.authenticate(
                _token(authorization),
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessDenied as exc:
            raise HTTPException(
                status_code=401,
                detail={
                    "decision": "DENY",
                    "reason": exc.reason.value,
                    "request_id": x_request_id,
                },
            ) from exc
        return IdentityOut(
            identity_id=identity.identity_id,
            kind=identity.kind.value,
            subject=identity.subject,
        )

    @app.get("/api/v1/context", response_model=ContextOut)
    def context(
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> ContextOut:
        """Verified identity and effective tenant of the presented credential.

        This is the operation a platform service consumes when it must not
        invent its own identity verification or its own tenant derivation
        (ARCHITECTURE.md §6.2, LAW-16a). It grants nothing: the answer is a
        context, and authorization stays with the data owner.
        """
        try:
            identity, tenant, _, _ = engine.verified_context(
                _token(authorization),
                x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessDenied as exc:
            raise HTTPException(
                status_code=_status_for(exc.reason.value),
                detail={
                    "decision": "DENY",
                    "reason": exc.reason.value,
                    "request_id": x_request_id,
                },
            ) from exc
        return ContextOut(
            identity_id=identity.identity_id,
            kind=identity.kind.value,
            subject=identity.subject,
            tenant_id=tenant.tenant_id,
            platform_id=tenant.platform_id,
            source=tenant.source,
        )

    @app.get("/api/v1/records/{record_id}", response_model=RecordOut)
    def get_record(
        record_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> RecordOut:
        try:
            record, obs, _ = engine.read_record(
                _token(authorization),
                record_id,
                x_tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except AccessDenied as exc:
            raise HTTPException(
                status_code=_status_for(exc.reason.value),
                detail={
                    "decision": "DENY",
                    "reason": exc.reason.value,
                    "request_id": x_request_id,
                },
            ) from exc
        request.state.obs = obs
        return RecordOut(
            record_id=record.record_id, tenant_id=record.tenant_id, body=record.body
        )

    @app.put("/api/v1/records/{record_id}", response_model=RecordOut)
    def put_record(
        record_id: str,
        payload: WriteIn,
        authorization: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> RecordOut:
        claimed = payload.tenant_id or x_tenant_id
        try:
            record, _, _ = engine.write_record(
                _token(authorization),
                record_id,
                payload.body,
                claimed,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
                idempotency_key=idempotency_key,
            )
        except AccessDenied as exc:
            raise HTTPException(
                status_code=_status_for(exc.reason.value),
                detail={"decision": "DENY", "reason": exc.reason.value},
            ) from exc
        return RecordOut(
            record_id=record.record_id, tenant_id=record.tenant_id, body=record.body
        )

    return app
