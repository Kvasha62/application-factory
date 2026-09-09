"""Published consumer surface of the Authorization Boundary component (IS-003).

A consumer receives exactly one object: :class:`AuthorizationClient`. It offers
the single operation of the published contract — ask for a decision — and
returns an immutable
:class:`authorization_service.contracts.AuthorizationDecision`.

Its state is two values: an opaque contract-channel handle and the consuming
component's own service credential. No callable, closure or cell is stored, so
the component's ASGI application, deployment, engine, grant store and audit
journal are unreachable through any attribute of the client. This module keeps
no reference to the internal transport either — the executor is resolved at call
time — so importing the published surface does not open a door to the channel
table or to the application behind it.

What the client cannot do is as important as what it can: it cannot grant or
revoke a permission, cannot read the grants of a Tenant, cannot enumerate
subjects, cannot read the audit journal and cannot enforce anything. Enforcement
belongs to the component that owns the resource: the client hands back a value,
and refusing to serve the request is that component's own act (invariant 8).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from authorization_service.contracts import (
    AuthorizationDecision,
    AuthorizationServiceError,
    CallerDenyReason,
    CallerNotAuthenticated,
    CallerNotAuthorized,
    ContractViolation,
    Decision,
    MalformedDecisionRequest,
    Reason,
    ResourceRef,
)

#: The published error classes are rebuilt from the status code and the
#: machine-readable reason, so a consumer catches the same exception types the
#: component documents — no transport-specific error type crosses the boundary.
_ERROR_BY_REASON: dict[CallerDenyReason, type[AuthorizationServiceError]] = {
    CallerDenyReason.MISSING_SERVICE_IDENTITY: CallerNotAuthenticated,
    CallerDenyReason.INVALID_SERVICE_IDENTITY: CallerNotAuthenticated,
    CallerDenyReason.UNKNOWN_SERVICE: CallerNotAuthenticated,
    CallerDenyReason.INSUFFICIENT_AUTHORIZATION: CallerNotAuthorized,
    CallerDenyReason.PLATFORM_MISMATCH: CallerNotAuthorized,
    CallerDenyReason.MALFORMED_REQUEST: MalformedDecisionRequest,
}

_DEFAULT_REASON_BY_STATUS: dict[int, CallerDenyReason] = {
    401: CallerDenyReason.MISSING_SERVICE_IDENTITY,
    403: CallerDenyReason.INSUFFICIENT_AUTHORIZATION,
    422: CallerDenyReason.MALFORMED_REQUEST,
}

__all__ = [
    "AuthorizationClient",
    "AuthorizationDecision",
    "AuthorizationServiceError",
    "CallerNotAuthenticated",
    "CallerNotAuthorized",
    "ContractViolation",
    "Decision",
    "Reason",
    "ResourceRef",
    "build_client",
]


class AuthorizationClient:
    """Value-only decision reader over the published Authorization contract.

    ``POST /api/v1/decisions`` is the only operation it performs; the exact same
    API is available to a remote consumer.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that issued
    them.
    """

    __slots__ = ("__weakref__", "_channel", "_credential")

    def __init__(self, channel: str, credential: str) -> None:
        if not credential:
            raise ValueError("an authorization service credential is required")
        if not isinstance(channel, str) or not channel:
            raise ValueError("an authorization contract channel handle is required")
        self._channel = channel
        self._credential = credential

    def decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource: ResourceRef,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AuthorizationDecision:
        """Ask whether the subject may perform ``operation`` on ``resource``.

        ``subject_credential`` is the credential the subject presented to the
        data owner; it is verified by IS-001 inside the decision, so a consumer
        cannot assert who the subject is. ``claimed_tenant_id`` is forwarded as
        a cross-check only: it can never select the effective tenant.
        """
        headers: list[tuple[str, str]] = [
            ("authorization", f"Bearer {self._credential}"),
        ]
        if subject_credential is not None:
            headers.append(("x-subject-authorization", f"Bearer {subject_credential}"))
        if claimed_tenant_id:
            headers.append(("x-tenant-id", claimed_tenant_id))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))

        body: dict[str, Any] = {
            "operation": operation,
            "resource": {
                "resource_type": resource.resource_type,
                "resource_id": resource.resource_id,
                "tenant_id": resource.tenant_id,
            },
        }
        status, payload = _call_contract()(
            self._channel, "POST", "/api/v1/decisions", headers, body
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _decision_from(payload)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        if status >= 500:
            return ContractViolation(f"authorization contract failed with status {status}")
        detail = payload.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        reason = _as_caller_reason(detail.get("reason")) or _DEFAULT_REASON_BY_STATUS.get(
            status, CallerDenyReason.MALFORMED_REQUEST
        )
        error_type = _ERROR_BY_REASON.get(reason, AuthorizationServiceError)
        return error_type(reason, str(detail.get("reason") or reason.value), details=dict(detail))


def _decision_from(payload: Mapping[str, Any]) -> AuthorizationDecision:
    """Rebuild the published value; an answer outside the contract fails closed."""
    try:
        resource = payload["resource"]
        return AuthorizationDecision(
            decision=Decision(payload["decision"]),
            reason=Reason(payload["reason"]),
            operation=payload["operation"],
            resource=ResourceRef(
                resource_type=resource["resource_type"],
                resource_id=resource["resource_id"],
                tenant_id=resource.get("tenant_id"),
            ),
            subject_id=payload.get("subject_id"),
            tenant_id=payload.get("tenant_id"),
            request_id=payload.get("request_id"),
            correlation_id=payload.get("correlation_id"),
            decided_by=payload.get("decided_by", "authorization"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the authorization contract answered outside its published decision model"
        ) from exc


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the boundary:
    after ``import authorization_service.reader`` this module's namespace holds
    no module object, no channel table, no transport class and no application.
    """
    from authorization_service.transport import call_contract

    return call_contract


def _as_caller_reason(value: Any) -> CallerDenyReason | None:
    if not isinstance(value, str):
        return None
    try:
        return CallerDenyReason(value)
    except ValueError:
        return None


def build_client(app: Any, *, credential: str) -> AuthorizationClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — the component's published ASGI contract — is handed to the
    provider's own channel table and stays there: the client receives the handle
    back, not the application. A client dropped by its consumer revokes that
    handle, so the table is not a place where applications accumulate.
    """
    from authorization_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = AuthorizationClient(channel, credential)
    revoke_on_death(client, channel)
    return client
