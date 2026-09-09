"""Published consumer surface of the Tenant Authority component (IS-002).

A consumer receives exactly one object: :class:`TenantAuthorityClient`. It
offers the read operations of the published contract and returns immutable
values. Its own state is three values — an opaque contract-channel handle, a
service credential and a Platform Instance binding — and no callable, closure or
cell: the component's ASGI application, deployment, engine, store, audit journal,
idempotency table and mutation operations are therefore unreachable through any
attribute of the client — including the cells of a closure, of which it now has
none. This module keeps no reference to the internal transport either, so importing
the published surface does not open a door to the channel table or to the
component's application. Every read still crosses authentication, authorization,
ownership validation and audit, because the channel executes the published
contract inside the component.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
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

# BLOCKER-06: nothing of `tenant_authority.transport` is bound in this module —
# not even privately. The transport owns the channel table and, through it, the
# component's ASGI application, and it is declared internal by the contract
# (`api.consumer_surface.internal_modules`); a module-level import would make the
# published surface a door into that table. The executor is therefore resolved
# when a read happens and is never stored here (see `_call_contract`).

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


__all__ = [
    "AuthenticationDenied",
    "AuthorizationDenied",
    "ContractViolation",
    "InvalidTransition",
    "LifecycleDecision",
    "OwnershipDenied",
    "TenantAuthorityClient",
    "TenantAuthorityError",
    "TenantConflict",
    "TenantNotFound",
    "TenantSnapshot",
    "TenantState",
    "build_client",
]


class TenantAuthorityClient:
    """Value-only reader over the published Tenant Authority contract.

    ``GET /api/v1/tenants/{tenant_id}`` and
    ``GET /api/v1/tenants/{tenant_id}/lifecycle`` are the only operations it
    performs; the exact same API is available to a remote consumer.

    ``channel`` is the opaque handle of a contract channel opened by the provider.
    It is deliberately not a callable: a consumer holding values cannot walk from
    them into the object graph of the component that issued them.
    """

    __slots__ = ("__weakref__", "_channel", "_credential", "_expected_platform_id")

    def __init__(
        self,
        channel: str,
        credential: str,
        expected_platform_id: str | None = None,
    ) -> None:
        if not credential:
            raise ValueError("a Tenant Authority service credential is required")
        if not isinstance(channel, str) or not channel:
            raise ValueError("a Tenant Authority contract channel handle is required")
        self._channel = channel
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
            self._expected_platform_id
            if expected_platform_id is None
            else expected_platform_id
        )
        headers: list[tuple[str, str]] = [
            ("authorization", f"Bearer {self._credential}")
        ]
        if platform:
            headers.append(("x-platform-id", platform))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))

        status, payload = _call_contract()(self._channel, "GET", path, headers, None)
        if 200 <= status < 300:
            return payload
        raise self._error(status, payload)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        if status >= 500:
            return ContractViolation(
                f"tenant authority contract failed with status {status}"
            )
        detail = payload.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        reason = _as_reason(detail.get("reason")) or _DEFAULT_REASON_BY_STATUS.get(
            status, DenyReason.TENANT_NOT_FOUND
        )
        error_type = _ERROR_BY_REASON.get(reason, TenantAuthorityError)
        message = str(detail.get("reason") or reason.value)
        return error_type(reason, message, details=dict(detail))


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the boundary: after
    ``import tenant_authority.reader`` this module's namespace holds no module
    object, no channel table, no transport class and no application — a consumer
    that wants the transport has to import that internal module itself, which the
    component contract forbids for a consuming component.
    """
    from tenant_authority.transport import call_contract

    return call_contract


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
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — the component's published ASGI contract — is handed to the provider's
    own channel table and stays there: the client receives the handle back, not the
    application. A client dropped by its consumer revokes that handle, so the table
    is not a place where applications accumulate.
    """
    from tenant_authority.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = TenantAuthorityClient(channel, credential, expected_platform_id)
    # The registration is revoked together with the client it was published to, so
    # the provider-side table never accumulates applications.
    revoke_on_death(client, channel)
    return client
