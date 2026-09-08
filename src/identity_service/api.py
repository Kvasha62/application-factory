from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from identity_service import COMPONENT_ID, COMPONENT_VERSION
from identity_service.config import IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.errors import AccessDenied
from identity_service.models import DenyReason
from identity_service.store import IdentityStore
from tenant_authority.deployment import build_deployment

# Composition root of the Level 0 modular monolith. Tenant Authority (IS-002) is
# published to identity through its contract client only: identity never receives
# the deployment, the engine, the store, the audit journal or any mutation
# operation (invariant T-009, ARCHITECTURE.md §1.1). The credential below is a
# deployment secret in production; the demo wiring uses the component's demo
# service identity, whose permissions are exactly the two published reads.
TENANT_AUTHORITY_CREDENTIAL = "svc-token-identity"
_tenant_authority_deployment = build_deployment(
    {"platform_id": "plt_demo"}, seed_demo=True, with_http=True
)

store = IdentityStore()
store.seed_demo()
identity_config = IdentityConfig.from_mapping(
    {
        # The composition root binds both components to the same Platform Instance;
        # a mismatch would be denied by the contract cross-check, not ignored.
        "current_platform_id": _tenant_authority_deployment.current_platform_id,
        "environment": _tenant_authority_deployment.config.environment,
    }
)
tenant_authority = _tenant_authority_deployment.publish(
    credential=TENANT_AUTHORITY_CREDENTIAL,
    expected_platform_id=identity_config.current_platform_id,
)
engine = IdentityEngine(
    store=store,
    tenant_authority=tenant_authority,
    config=identity_config,
)

def tenant_authority_contract_app():
    """The published contract application of the Tenant Authority in use.

    Deliberately the only handle this module exposes: the deployment (engine,
    store, configuration) stays composition-internal, so nothing in this module
    is a path to another component's internals.
    """
    return _tenant_authority_deployment.contract_app()


app = FastAPI(title="IS-001 Identity / Tenant Context", version=COMPONENT_VERSION)

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
            detail={"decision": "DENY", "reason": exc.reason.value, "request_id": x_request_id},
        ) from exc
    return IdentityOut(
        identity_id=identity.identity_id,
        kind=identity.kind.value,
        subject=identity.subject,
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
            detail={"decision": "DENY", "reason": exc.reason.value, "request_id": x_request_id},
        ) from exc
    request.state.obs = obs
    return RecordOut(record_id=record.record_id, tenant_id=record.tenant_id, body=record.body)


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
    return RecordOut(record_id=record.record_id, tenant_id=record.tenant_id, body=record.body)
