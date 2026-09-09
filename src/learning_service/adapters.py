"""Adapter from the published consumer surface of IS-003 to the port.

This is the only place in Learning that knows the *shape* of another
component's published answer, and it deliberately knows nothing else
about it:

* it is handed a value-only client — ``authorization_service.reader``
  ``AuthorizationClient`` — by a composition root; it never builds one,
  never imports one and never reaches for a deployment, an engine, a store
  or an ASGI application;
* it reads from the answer only what the published contract documents
  (``decision``, ``reason``, ``subject_id``, ``tenant_id``,
  ``request_id``, ``correlation_id``) and converts it immediately into a
  value of :mod:`learning_service.consumed`. No provider object outlives
  the adapter call, so no provider type can appear in the engine, in a
  served view or in the audit journal;
* a refusal is read the same way: whatever the dependency raises is a
  dependency that did not answer (:class:`DependencyRefusal`), and the
  access will deny — fail closed.

The resource question is stated with a local :class:`ResourceClaim`: a
value carrying exactly the three attributes the IS-003 contract documents
for a resource reference (``resource_type``, ``resource_id``,
``tenant_id``). The published client reads those attributes and nothing
else, so the adapter needs no type of the provider to ask it a question.

Structural, not nominal, on purpose: nothing here imports
``authorization_service``. That property is what the boundary tests check,
and it is why a refactoring inside IS-003 cannot reach into this
component without going through a contract.

This module is used by a composition root only. Importing it does not
import anything of another component, so the rest of Learning stays free
of it too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from learning_service.consumed import DecisionAnswer, DependencyRefusal

__all__ = [
    "AuthorizationDecisionAdapter",
    "ResourceClaim",
    "authorization_port",
]


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    """A resource reference in the shape documented by the IS-003 contract.

    Three documented attributes and nothing else; the published decision
    client serializes exactly these. This value is owned by Learning: it is
    not a type of the provider, and the provider's own resource type never
    enters this component.
    """

    resource_type: str
    resource_id: str
    tenant_id: str | None = None


def _text(value: Any) -> str | None:
    """Read a documented string field out of a contract value (enum or str)."""
    value = getattr(value, "value", value)
    return value if isinstance(value, str) and value else None


def _refusal_code(exc: BaseException) -> str | None:
    """The stated reason of a refusal, or ``None`` when nothing was stated."""
    return _text(getattr(exc, "reason", None))


class AuthorizationDecisionAdapter:
    """The Authorization Boundary (IS-003) seen as :class:`AuthorizationPort`.

    ``client`` is the published decision reader. The adapter forwards the
    caller's request context so one access stays traceable across the
    journals of the Platform Instance, and returns a local value — always:
    an exception of the dependency is a :class:`DependencyRefusal`, and an
    answer outside the documented shape is one too, so the engine only ever
    sees values of this component.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DecisionAnswer | DependencyRefusal:
        try:
            answer = self._client.decide(
                subject_credential,
                operation=operation,
                resource=ResourceClaim(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    tenant_id=resource_tenant_id,
                ),
                claimed_tenant_id=claimed_tenant_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # published refusal or unusable dependency
            return DependencyRefusal(_refusal_code(exc))
        decision = _text(getattr(answer, "decision", None))
        reason = _text(getattr(answer, "reason", None))
        if decision is None or reason is None:
            # An answer outside the published shape is not an answer.
            return DependencyRefusal(None)
        subject_id = _text(getattr(answer, "subject_id", None))
        tenant_id = _text(getattr(answer, "tenant_id", None))
        return DecisionAnswer(
            decision=decision,
            reason=reason,
            subject_id=subject_id,
            tenant_id=tenant_id,
            request_id=_text(getattr(answer, "request_id", None)) or request_id,
            correlation_id=_text(getattr(answer, "correlation_id", None))
            or correlation_id,
        )


def authorization_port(client: Any) -> AuthorizationDecisionAdapter:
    """Wire a published IS-003 decision reader as this component's port."""
    return AuthorizationDecisionAdapter(client)
