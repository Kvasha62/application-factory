"""Published consumer surface of the Identity / Tenant Context component (IS-001).

A consumer receives exactly one object: :class:`IdentityContextClient`. It
offers the single read of the published contract —
``GET /api/v1/context`` — and returns an immutable
:class:`identity_service.contracts.VerifiedContext`.

Its own state is one value: an opaque contract-channel handle. No callable,
closure or cell is stored, so the component's ASGI application, engine, store,
audit journal, record operations and idempotency table are unreachable through
any attribute of the client. This module keeps no reference to the internal
transport either — the executor is resolved at call time (see
:func:`_call_contract`) — so importing the published surface does not open a
door to the channel table or to the component's application.

The client holds no credential of its own: the subject's credential is presented
per call, exactly as over HTTP, and the answer is a context — never a grant. A
consumer that needs an access decision asks the Authorization Boundary (IS-003);
a data owner enforces at its own boundary (ARCHITECTURE.md §6.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from identity_service.contracts import (
    DenyReason,
    IdentityKind,
    VerifiedContext,
)
from identity_service.errors import AccessDenied, ContractViolation

_DEFAULT_REASON_BY_STATUS: dict[int, DenyReason] = {
    401: DenyReason.MISSING_IDENTITY,
    403: DenyReason.INSUFFICIENT_AUTHORIZATION,
    404: DenyReason.UNKNOWN_RESOURCE,
}

__all__ = [
    "AccessDenied",
    "ContractViolation",
    "DenyReason",
    "IdentityContextClient",
    "VerifiedContext",
    "build_client",
]


class IdentityContextClient:
    """Value-only reader over the published Identity context contract.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that issued
    them.
    """

    __slots__ = ("__weakref__", "_channel")

    def __init__(self, channel: str) -> None:
        if not isinstance(channel, str) or not channel:
            raise ValueError("an identity contract channel handle is required")
        self._channel = channel

    def resolve_context(
        self,
        credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> VerifiedContext:
        """Verify the presented credential and return its effective tenant context.

        ``claimed_tenant_id`` is forwarded as the cross-check the contract
        defines: it can only agree with the tenant derived from the verified
        identity or be rejected (``tenant_mismatch``); it can never select one.
        """
        headers: list[tuple[str, str]] = []
        if credential is not None:
            headers.append(("authorization", f"Bearer {credential}"))
        if claimed_tenant_id:
            headers.append(("x-tenant-id", claimed_tenant_id))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))

        status, payload = _call_contract()(
            self._channel, "GET", "/api/v1/context", headers, None
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return VerifiedContext(
            identity_id=payload["identity_id"],
            kind=IdentityKind(payload["kind"]),
            subject=payload["subject"],
            tenant_id=payload["tenant_id"],
            platform_id=payload["platform_id"],
            source=payload["source"],
        )

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        if status >= 500:
            return ContractViolation(f"identity contract failed with status {status}")
        detail = payload.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        reason = _as_reason(detail.get("reason")) or _DEFAULT_REASON_BY_STATUS.get(
            status, DenyReason.INVALID_IDENTITY
        )
        return AccessDenied(reason, str(detail.get("reason") or reason.value))


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the boundary:
    after ``import identity_service.reader`` this module's namespace holds no
    module object, no channel table, no transport class and no application.
    """
    from identity_service.transport import call_contract

    return call_contract


def _as_reason(value: Any) -> DenyReason | None:
    if not isinstance(value, str):
        return None
    try:
        return DenyReason(value)
    except ValueError:
        return None


def build_client(app: Any) -> IdentityContextClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — this component's published ASGI contract — is handed to the
    provider-side channel table and stays there: the client receives the handle
    back, not the application. A client dropped by its consumer revokes that
    handle, so the table is not a place where applications accumulate.
    """
    from identity_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = IdentityContextClient(channel)
    revoke_on_death(client, channel)
    return client
