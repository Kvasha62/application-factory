"""Ports through which the Saga boundary consumes other components.

IS-006 may not read another component's storage, internal modules or internal
types (ARCHITECTURE.md §1.1, §6.1, LAW-04), and it may not re-implement what
another component already owns. It therefore depends on exactly two narrow
ports, both expressed in this component's own vocabulary:

* :class:`TenantContextPort` — the verified subject and effective Tenant of one
  delivery, answered by IS-001. This is why IS-006 contains no token handling,
  no tenant registry and no tenant derivation: the tenant of a saga is resolved
  once, by the component that owns identity, and is immutable afterwards
  (invariant 9);
* :class:`CommandSafetyPort` — the exactly-once execution of one step effect,
  answered by the IS-005 Idempotency / Command Safety Boundary. Every step
  effect and every compensation is delivered through it, so IS-006 owns no
  second idempotency mechanism (invariant 4, invariant 10).

The port declares what this component *needs*, not what the provider happens to
offer: nothing here can verify a credential, grant a permission, enumerate
subjects or read an audit journal. The implementations wired in by a composition
root are adapters over the published consumer surfaces of IS-001 and IS-005;
nothing in this module — or anywhere else in the component — imports another
business component.

Business steps themselves are not a port: they are the callables a workflow
declares, and the composition root wires them to the published contract of
whichever component owns the data. That is the boundary the Issue requires —
``Saga -> component contract -> component data owner`` — and it is why this
component never holds a store handle, an engine or a session of anybody else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar, runtime_checkable

from saga.consumed import TenantContextOutcome

T = TypeVar("T")


@runtime_checkable
class TenantContextPort(Protocol):
    """Source of the verified subject and the effective Tenant (IS-001).

    ``claimed_tenant_id`` is forwarded as the cross-check the IS-001 contract
    defines: it can agree with the tenant derived from the verified identity or
    be refused, and it can never select one (LAW-16a). The operation is total —
    it answers with a value of :mod:`saga.consumed`, including when the answer
    is "no context": a refusal is data this component decides on, not an
    exception vocabulary borrowed from the provider.
    """

    def resolve(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantContextOutcome: ...


@runtime_checkable
class CommandSafetyPort(Protocol):
    """The IS-005 command-safety boundary, as this component consumes it.

    The shape is exactly the published surface of IS-005
    (``idempotency.guard.IdempotencyGuard.execute``): one logical command
    identity executes at most once, an exact replay returns the recorded result
    without executing the effect again, and a differing context or payload for
    the same key raises ``IdempotencyConflict``. IS-006 supplies the command
    identity of a step (:func:`saga.models.step_idempotency_key`) and nothing
    else — no second key scheme, no second store, no second replay rule.
    """

    def execute(
        self,
        key: str | None,
        *,
        identity: str | None,
        tenant_id: str | None,
        operation: str,
        resource: str | None,
        fingerprint: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
        effect: Callable[[], T],
    ) -> T: ...


__all__ = [
    "CommandSafetyPort",
    "TenantContextOutcome",
    "TenantContextPort",
]
