"""Published consumer surface of the Tenant Authority component (IS-002).

A consumer receives exactly one object: :class:`TenantAuthorityClient`. It
offers the read operations of the published contract and returns immutable
values. It holds no reference to the engine, the registry store, the audit
journal, the idempotency table or any mutation operation — so no internal object
is reachable through the boundary, and every read crosses authentication,
authorization, ownership validation and audit like any other contract request.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from tenant_authority.contracts import LifecycleDecision, TenantSnapshot, TenantState
from tenant_authority.errors import (
    AuthenticationDenied,
    AuthorizationDenied,
    ContractViolation,
    DenyReason,
    InvalidTransition,
    OwnershipDenied,
    TenantAuthorityError,
    TenantConflict,
    TenantNotFound,
)
from tenant_authority.transport import ContractTransport, asgi_transport

#: The published error classes are rebuilt from the status code and the
#: machine-readable reason, so a consumer catches the same exception types the
#: component documents — no transport-specific error type crosses the boundary.
_ERROR_BY_REASON: dict[DenyReason, type[TenantAuthorityError]] = {
    DenyReason.TENANT_NOT_FOUND: TenantNotFound,
    # Kept for deployments that answer the precise reason; the demo transport
    # masks it as `tenant_not_found` (non-disclosure across platforms).
    DenyReason.FOREIGN_TENANT: OwnershipDenied,
    DenyReason.INVALID_TRANSITION: InvalidTransition,
    DenyReason.TENANT_EXISTS: TenantConflict,
    DenyReason.IDEMPOTENCY_CONFLICT: TenantConflict,
    DenyReason.PLATFORM_MISMATCH: AuthorizationDenied,
    DenyReason.INSUFFICIENT_AUTHORIZATION: AuthorizationDenied,
    DenyReason.MISSING_SERVICE_IDENTITY: AuthenticationDenied,
    DenyReason.INVALID_SERVICE_IDENTITY: AuthenticationDenied,
    DenyReason.UNKNOWN_SERVICE: AuthenticationDenied,
}

_DEFAULT_REASON_BY_STATUS: dict[int, DenyReason] = {
    401: DenyReason.MISSING_SERVICE_IDENTITY,
    403: DenyReason.INSUFFICIENT_AUTHORIZATION,
    404: DenyReason.TENANT_NOT_FOUND,
    409: DenyReason.TENANT_EXISTS,
}


class TenantAuthorityClient:
    """Value-only reader over the published Tenant Authority contract.

    ``GET /api/v1/tenants/{tenant_id}`` and
    ``GET /api/v1/tenants/{tenant_id}/lifecycle`` are the only operations it
    performs; the exact same API is available to a remote consumer.
    """

    __slots__ = ("_transport", "_credential", "_expected_platform_id")

    def __init__(
        self,
        transport: ContractTransport,
        credential: str,
        expected_platform_id: str | None = None,
    ) -> None:
        if not credential:
            raise ValueError("a Tenant Authority service credential is required")
        self._transport = transport
        self._credential = credential
        self._expected_platform_id = expected_platform_id

    def lookup(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantSnapshot:
        """Authoritative Tenant record: state plus Platform Instance ownership."""
        payload = self._get(
            f"/api/v1/tenants/{quote(tenant_id, safe='')}",
            expected_platform_id=expected_platform_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return TenantSnapshot(
            tenant_id=payload["tenant_id"],
            platform_id=payload["platform_id"],
            state=TenantState(payload["state"]),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            state_source=payload["state_source"],
        )

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleDecision:
        """Whether an ordinary tenant-scoped operation is permitted right now."""
        payload = self._get(
            f"/api/v1/tenants/{quote(tenant_id, safe='')}/lifecycle",
            expected_platform_id=expected_platform_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return LifecycleDecision(
            tenant_id=payload["tenant_id"],
            platform_id=payload["platform_id"],
            state=TenantState(payload["state"]),
            permitted=bool(payload["permitted"]),
            reason=payload["reason"],
        )

    # ------------------------------------------------------------------ internals
    def _get(
        self,
        path: str,
        *,
        expected_platform_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
    ) -> Mapping[str, Any]:
        platform = (
            self._expected_platform_id if expected_platform_id is None else expected_platform_id
        )
        headers: list[tuple[str, str]] = [("authorization", f"Bearer {self._credential}")]
        if platform:
            headers.append(("x-platform-id", platform))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))

        status, payload = self._transport("GET", path, headers, None)
        if 200 <= status < 300:
            return payload
        raise self._error(status, payload)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        if status >= 500:
            return ContractViolation(f"tenant authority contract failed with status {status}")
        detail = payload.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        reason = _as_reason(detail.get("reason")) or _DEFAULT_REASON_BY_STATUS.get(
            status, DenyReason.TENANT_NOT_FOUND
        )
        error_type = _ERROR_BY_REASON.get(reason, TenantAuthorityError)
        message = str(detail.get("reason") or reason.value)
        return error_type(reason, message, details=dict(detail))


def _as_reason(value: Any) -> DenyReason | None:
    if not isinstance(value, str):
        return None
    try:
        return DenyReason(value)
    except ValueError:
        return None


def build_client(
    app: Any,
    *,
    credential: str,
    expected_platform_id: str | None = None,
) -> TenantAuthorityClient:
    """Publish the component for one consumer: contract app in, client out.

    ``app`` is the component's published ASGI contract (not an internal object);
    the resulting client never exposes it and never exposes anything else either.
    """
    return TenantAuthorityClient(
        asgi_transport(app),
        credential,
        expected_platform_id,
    )
