from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from identity_service import COMPONENT_ID, COMPONENT_VERSION
from identity_service.engine import IdentityEngine
from identity_service.errors import AccessDenied
from identity_service.store import IdentityStore

store = IdentityStore()
store.seed_demo()
engine = IdentityEngine(store)

app = FastAPI(title="IS-001 Identity / Tenant Context", version=COMPONENT_VERSION)


class RecordOut(BaseModel):
    record_id: str
    tenant_id: str
    body: str


class WriteIn(BaseModel):
    record_id: str
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component_id": COMPONENT_ID, "version": COMPONENT_VERSION}


@app.get("/ready")
def ready() -> dict:
    return {"status": "ready", "component_id": COMPONENT_ID}


@app.get("/api/v1/me", response_model=IdentityOut)
def me(authorization: str | None = Header(default=None)) -> IdentityOut:
    try:
        identity = engine.verify_identity(_token(authorization))
    except AccessDenied as exc:
        raise HTTPException(status_code=401, detail=exc.reason.value) from exc
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
) -> RecordOut:
    try:
        record, obs, _ = engine.read_record(
            _token(authorization),
            record_id,
            x_tenant_id,
            request_id=x_request_id,
        )
    except AccessDenied as exc:
        status = 401 if exc.reason.value.endswith("identity") or "identity" in exc.reason.value else 403
        raise HTTPException(
            status_code=status,
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
            idempotency_key=idempotency_key,
        )
    except AccessDenied as exc:
        status = 401 if "identity" in exc.reason.value else 403
        raise HTTPException(
            status_code=status,
            detail={"decision": "DENY", "reason": exc.reason.value},
        ) from exc
    return RecordOut(record_id=record.record_id, tenant_id=record.tenant_id, body=record.body)
