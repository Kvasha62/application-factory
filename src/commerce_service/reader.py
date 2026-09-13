"""Published consumer surface of Commerce (SCS-002) — values in, values out.

A consumer is handed exactly one object — :class:`CommerceClient` — and it
receives immutable values back. The client performs exactly the published
operations (Stage 2: create and read one owned Product) and nothing else;
the exact same API is available to a remote consumer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from commerce_service.contracts import PUBLISHED_DENY_REASONS, ProductView
from commerce_service.errors import AccessRefused, ContractViolation

__all__ = ["CommerceClient", "build_client"]


class CommerceClient:
    """Value-only access reader over the published commerce contract.

    It performs exactly the published operations — create and read one
    owned Product — and nothing else; the exact same API is available to a
    remote consumer.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that
    issued them.
    """

    __slots__ = ("__weakref__", "_channel")

    def __init__(self, channel: str) -> None:
        if not isinstance(channel, str) or not channel:
            raise ValueError("a commerce contract channel handle is required")
        self._channel = channel

    # -------------------------------------------------------------- operations
    def create_product(
        self,
        subject_credential: str | None,
        *,
        name: str,
        description: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ProductView:
        """Create one owned Product through the enforcement chain.

        ``subject_credential`` is the credential presented by the subject;
        it is verified by IS-001 inside the IS-003 decision, so a consumer
        cannot assert who the subject is. ``claimed_tenant_id`` is forwarded
        as a cross-check only: it can never select the effective tenant.
        ``idempotency_key`` is mandatory: the command is delivered through
        IS-005, so an exact replay returns the recorded product without a
        second effect and a changed binding is refused.
        """
        status, payload = self._call(
            "POST",
            "/api/v1/commerce/products",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            body={"name": name, "description": description},
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _product_from(payload)

    def read_product(
        self,
        subject_credential: str | None,
        product_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ProductView:
        """Read one owned Product through the enforcement chain.

        ``subject_credential`` is the credential presented by the subject;
        it is verified by IS-001 inside the IS-003 decision, so a consumer
        cannot assert who the subject is. ``claimed_tenant_id`` is forwarded
        as a cross-check only: it can never select the effective tenant.
        """
        status, payload = self._call(
            "GET",
            f"/api/v1/commerce/products/{quote(product_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _product_from(payload)

    # --------------------------------------------------------------- internals
    def _call(
        self,
        method: str,
        path: str,
        *,
        subject_credential: str | None,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
        idempotency_key: str | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        headers: list[tuple[str, str]] = []
        if subject_credential is not None:
            headers.append(("authorization", f"Bearer {subject_credential}"))
        if claimed_tenant_id:
            headers.append(("x-tenant-id", claimed_tenant_id))
        if idempotency_key:
            headers.append(("idempotency-key", idempotency_key))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))
        return _call_contract()(self._channel, method, path, headers, body)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        refusal = _refusal_of(payload)
        if refusal is None:
            if status >= 500:
                return ContractViolation(
                    f"commerce contract failed with status {status}"
                )
            return ContractViolation(
                "the commerce contract refused without a published reason"
            )
        reason, request_id, correlation_id = refusal
        return AccessRefused(
            reason,
            status_code=status,
            request_id=request_id,
            correlation_id=correlation_id,
        )


def _product_from(payload: Mapping[str, Any]) -> ProductView:
    """Rebuild the published product value; an outside answer fails closed."""
    try:
        return ProductView(
            product_id=_text_field(payload, "product_id"),
            name=_text_field(payload, "name"),
            description=_text_field(payload, "description"),
            status=_text_field(payload, "status"),
            created_by=_text_field(payload, "created_by"),
            created_at=_text_field(payload, "created_at"),
            updated_at=_text_field(payload, "updated_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the commerce contract answered outside its published product model"
        ) from exc


def _text_field(payload: Mapping[str, Any], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _refusal_of(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Read the approved error envelope: (reason, request_id, correlation_id).

    The envelope shape is strict: ``error.code`` must be present, the
    internal reason must come from ``error.details.reason`` and belong to
    the published vocabulary, and top-level ``request_id`` /
    ``correlation_id`` must be present. Anything else fails closed.
    """
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code:
        return None
    details = error.get("details")
    if not isinstance(details, Mapping):
        return None
    reason = details.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    if reason != "malformed_request" and reason not in PUBLISHED_DENY_REASONS:
        return None
    request_id = payload.get("request_id")
    correlation_id = payload.get("correlation_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(correlation_id, str) or not correlation_id:
        return None
    return (reason, request_id, correlation_id)


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the
    boundary: after ``import commerce_service.reader`` this module's
    namespace holds no module object, no channel table, no transport class
    and no application.
    """
    from commerce_service.transport import call_contract

    return call_contract


def build_client(app: Any) -> CommerceClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — the component's published ASGI contract — is handed to the
    provider's own channel table and stays there: the client receives the
    handle back, not the application. A client dropped by its consumer
    revokes that handle, so the table is not a place where applications
    accumulate.
    """
    from commerce_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = CommerceClient(channel)
    revoke_on_death(client, channel)
    return client
