"""HTTP surface of the Tenant Authority component — the published contract.

Everything else (engine, store, audit journal, idempotency table, mutation
operations) is internal: no other component may reach it directly
(invariant T-009, ARCHITECTURE.md §1.1, LAW-04). Level 0 consumers talk to this
contract in-process through ``tenant_authority.transport`` and receive the
value-only client from ``tenant_authority.reader``.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.contracts import LifecycleDecision, TenantSnapshot, TenantState
from tenant_authority.errors import DenyReason, TenantAuthorityError
from tenant_authority.lifecycle import ALLOWED_TRANSITIONS
from tenant_authority.models import LifecycleTransition
from tenant_authority.deployment import TenantAuthorityDeployment, build_deployment


class TenantOut(BaseModel):
    tenant_id: str
    platform_id: str
    state: TenantState
    created_at: str
    updated_at: str
    state_source: str


class TransitionOut(BaseModel):
    transition_id: str
    tenant_id: str
    previous_state: TenantState
    new_state: TenantState
    actor_id: str
    request_id: str
    correlation_id: str
    timestamp: str
    reason: str | None = None


class LifecycleOut(BaseModel):
    tenant_id: str
    platform_id: str
    state: TenantState
    permitted: bool
    reason: str


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_id: str | None = Field(
        default=None, description="caller-supplied cross-check only; never trusted"
    )
    tenant_id: str | None = None


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_state: TenantState
    reason: str | None = None


class ErrorOut(BaseModel):
    decision: str = "DENY"
    reason: str
    request_id: str | None = None


# A Tenant owned by another Platform Instance is answered exactly like a missing
# one: the transport surface never confirms that an identifier resolves
# somewhere else. The precise reason stays in the audit journal.
PUBLIC_REASONS = {DenyReason.FOREIGN_TENANT: DenyReason.TENANT_NOT_FOUND}


def _deny(exc: TenantAuthorityError, request_id: str | None) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "decision": "DENY",
            "reason": PUBLIC_REASONS.get(exc.reason, exc.reason).value,
            "request_id": request_id,
        },
    )


def _tenant_out(snapshot: TenantSnapshot) -> TenantOut:
    return TenantOut(
        tenant_id=snapshot.tenant_id,
        platform_id=snapshot.platform_id,
        state=snapshot.state,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        state_source=snapshot.state_source,
    )


def _transition_out(transition: LifecycleTransition) -> TransitionOut:
    return TransitionOut(
        transition_id=transition.transition_id,
        tenant_id=transition.tenant_id,
        previous_state=transition.previous_state,
        new_state=transition.new_state,
        actor_id=transition.actor_id,
        request_id=transition.request_id,
        correlation_id=transition.correlation_id,
        timestamp=transition.timestamp,
        reason=transition.reason,
    )


def _lifecycle_out(decision: LifecycleDecision) -> LifecycleOut:
    return LifecycleOut(
        tenant_id=decision.tenant_id,
        platform_id=decision.platform_id,
        state=decision.state,
        permitted=decision.permitted,
        reason=decision.reason,
    )


def _token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def create_app(deployment: TenantAuthorityDeployment) -> FastAPI:
    """Bind the published HTTP contract to one Tenant Authority deployment."""
    engine = deployment.engine
    app = FastAPI(
        title="IS-002 Tenant Authority / Tenant Lifecycle",
        version=COMPONENT_VERSION,
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

    @app.get("/api/v1/lifecycle")
    def lifecycle_machine() -> dict:
        """Published state machine: which transitions this authority accepts."""
        return {
            "lifecycle": ["provisioning", "active", "suspended", "deletion_requested", "deleted"],
            "allowed_transitions": {
                state.value: sorted(target.value for target in targets)
                for state, targets in ALLOWED_TRANSITIONS.items()
            },
            "terminal_states": ["deleted"],
        }

    @app.post("/api/v1/tenants", response_model=TenantOut, status_code=201)
    def create_tenant(
        payload: CreateIn,
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TenantOut:
        try:
            snapshot, _, _ = engine.create_tenant(
                _token(authorization),
                claimed_platform_id=payload.platform_id,
                tenant_id=payload.tenant_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
                idempotency_key=idempotency_key,
            )
        except TenantAuthorityError as exc:
            raise _deny(exc, x_request_id) from exc
        return _tenant_out(snapshot)

    @app.get("/api/v1/tenants", response_model=list[TenantOut])
    def list_tenants(
        authorization: str | None = Header(default=None),
        x_platform_id: str | None = Header(default=None, alias="X-Platform-Id"),
        state: TenantState | None = Query(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> list[TenantOut]:
        try:
            records, _, _ = engine.list_tenants(
                _token(authorization),
                claimed_platform_id=x_platform_id,
                state=state,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except TenantAuthorityError as exc:
            raise _deny(exc, x_request_id) from exc
        return [_tenant_out(record) for record in records]

    @app.get("/api/v1/tenants/{tenant_id}", response_model=TenantOut)
    def get_tenant(
        tenant_id: str,
        authorization: str | None = Header(default=None),
        x_platform_id: str | None = Header(default=None, alias="X-Platform-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> TenantOut:
        try:
            snapshot, _, _ = engine.get_tenant(
                _token(authorization),
                tenant_id,
                claimed_platform_id=x_platform_id,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
            )
        except TenantAuthorityError as exc:
            raise _deny(exc, x_request_id) from exc
        return _tenant_out(snapshot)

    @app.get("/api/v1/tenants/{tenant_id}/lifecycle", response_model=LifecycleOut)
    def tenant_lifecycle(
        tenant_id: str,
        authorization: str | None = Header(default=None),
        x_platform_id: str | None = Header(default=None, alias="X-Platform-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    ) -> LifecycleOut:
        try:
            decision, _, _ = engine.lifecycle_status(
                _token(authorization),
                tenant_id,
                claimed_platform_id=x_platform_id,
                request_id=x_request_id,
            )
        except TenantAuthorityError as exc:
            raise _deny(exc, x_request_id) from exc
        return _lifecycle_out(decision)

    @app.get("/api/v1/tenants/{tenant_id}/transitions", response_model=list[TransitionOut])
    def tenant_transitions(
        tenant_id: str,
        authorization: str | None = Header(default=None),
        x_platform_id: str | None = Header(default=None, alias="X-Platform-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    ) -> list[TransitionOut]:
        try:
            history, _, _ = engine.transition_history(
                _token(authorization),
                tenant_id,
                claimed_platform_id=x_platform_id,
                request_id=x_request_id,
            )
        except TenantAuthorityError as exc:
            raise _deny(exc, x_request_id) from exc
        return [_transition_out(item) for item in history]

    @app.post("/api/v1/tenants/{tenant_id}/transitions", response_model=TransitionOut)
    def transition_tenant(
        tenant_id: str,
        payload: TransitionIn,
        authorization: str | None = Header(default=None),
        x_platform_id: str | None = Header(default=None, alias="X-Platform-Id"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TransitionOut:
        try:
            transition, _, _ = engine.transition_tenant(
                _token(authorization),
                tenant_id,
                payload.to_state,
                claimed_platform_id=x_platform_id,
                reason=payload.reason,
                request_id=x_request_id,
                correlation_id=x_correlation_id,
                idempotency_key=idempotency_key,
            )
        except TenantAuthorityError as exc:
            raise _deny(exc, x_request_id) from exc
        return _transition_out(transition)

    return app


_deployment = build_deployment({"platform_id": "plt_demo"}, seed_demo=True, with_http=True)

#: ASGI application of the standalone demo deployment (`tenant_authority.api:app`).
app = _deployment.http_app
