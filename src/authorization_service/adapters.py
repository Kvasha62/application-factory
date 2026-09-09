"""Adapters from the published consumer surfaces of IS-001 / IS-002 to the ports.

This is the only place in IS-003 that knows the *shape* of another component's
published answer, and it deliberately knows nothing else about it:

* it is handed a value-only client — ``identity_service.reader``
  ``IdentityContextClient`` and ``tenant_authority.reader``
  ``TenantAuthorityClient`` — by a composition root; it never builds one,
  never imports one and never reaches for a deployment, an engine, a store or
  an ASGI application;
* it reads from the answer only what the published contract documents, and
  converts it immediately into a value of :mod:`authorization_service.consumed`.
  No provider object outlives the adapter call, so no provider type can appear
  in the engine, in a decision or in the audit journal;
* a refusal is read the same way: the published error of a consumer surface
  carries a machine-readable reason code, and that code — a plain string — is
  all this component takes from it. Anything else the dependency may raise is a
  dependency that did not answer, which denies (fail closed).

Structural, not nominal, on purpose: nothing here imports ``identity_service``
or ``tenant_authority``. The dependency is on the published contract, so a
composition root may equally wire a remote client, a stub or a future in-process
implementation, as long as it answers the documented contract. That property is
what the boundary tests check, and it is why an IS-001 or IS-002 refactoring
cannot reach into this component without going through a contract.

This module is used by a composition root only. Importing it does not import
anything of another component, so the rest of IS-003 stays free of them too.
"""

from __future__ import annotations

from typing import Any

from authorization_service.consumed import (
    ContextAnswer,
    DependencyRefusal,
    LifecycleAnswer,
    SubjectContext,
    TenantVerdict,
)

__all__ = [
    "IdentityContextAdapter",
    "TenantAuthorityAdapter",
    "identity_port",
    "tenant_authority_port",
]


def _code(value: Any) -> str | None:
    """Read a published machine-readable code out of a contract value."""
    value = getattr(value, "value", value)
    return value if isinstance(value, str) and value else None


def _refusal_code(exc: BaseException) -> str | None:
    """The stated reason of a refusal, or ``None`` when nothing was stated."""
    return _code(getattr(exc, "reason", None))


class IdentityContextAdapter:
    """Identity / Tenant Context (IS-001) seen as :class:`IdentityContextPort`.

    ``client`` is the published context reader. The adapter forwards the
    caller's request context so one decision stays traceable across the journals
    of the Platform Instance, and returns a local value.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def resolve_context(
        self,
        credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextAnswer:
        try:
            answer = self._client.resolve_context(
                credential,
                claimed_tenant_id=claimed_tenant_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # published denial or unusable dependency
            return DependencyRefusal(_refusal_code(exc))
        try:
            identity_id = answer.identity_id
            tenant_id = answer.tenant_id
        except AttributeError:
            # An answer outside the published shape is not a context.
            return DependencyRefusal(None)
        if not isinstance(identity_id, str) or not isinstance(tenant_id, str):
            return DependencyRefusal(None)
        return SubjectContext(
            identity_id=identity_id,
            tenant_id=tenant_id,
            kind=_code(getattr(answer, "kind", None)),
            subject=getattr(answer, "subject", None),
            platform_id=getattr(answer, "platform_id", None),
            source=_code(getattr(answer, "source", None)),
        )


class TenantAuthorityAdapter:
    """Tenant Authority (IS-002) seen as :class:`TenantAuthorityPort`.

    Only the lifecycle verdict is consumed: whether the Tenant may be served,
    and why not when it may not. The state machine, the registry and every
    write operation stay in the component that owns them.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleAnswer:
        try:
            verdict = self._client.lifecycle_decision(
                tenant_id,
                expected_platform_id=expected_platform_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # published refusal or unusable dependency
            return DependencyRefusal(_refusal_code(exc))
        permitted = getattr(verdict, "permitted", None)
        if not isinstance(permitted, bool):
            # No readable verdict means no permission to serve the Tenant.
            return DependencyRefusal(None)
        return TenantVerdict(
            permitted=permitted, reason_code=_code(getattr(verdict, "reason", None))
        )


def identity_port(client: Any) -> IdentityContextAdapter:
    """Wire a published IS-001 context reader as this component's identity port."""
    return IdentityContextAdapter(client)


def tenant_authority_port(client: Any) -> TenantAuthorityAdapter:
    """Wire a published IS-002 lifecycle reader as this component's tenant port."""
    return TenantAuthorityAdapter(client)
