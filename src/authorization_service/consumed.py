"""Local value types for what IS-003 consumes from other components.

The Authorization Boundary depends on two facts it does not own: *who the
subject is together with the tenant that is effective for it* (IS-001) and
*whether that Tenant may be served right now* (IS-002). It obtains both through
the published consumer surfaces of those components — and it stores neither of
them, so this is not a second tenant-context mechanism: the values below exist
only for the duration of one decision and are produced from an answer of the
owning component, never derived here (invariant 2).

Why local types at all: a consumed answer that keeps the provider's own classes
would make the provider's internals part of this component's code. The engine
would then be coupled to another component's implementation instead of to its
contract, and a change inside IS-001 or IS-002 would reach into IS-003 without
passing through a contract at all (ARCHITECTURE.md §1.1, §6.1, LAW-04).

So the boundary is expressed in this module's own vocabulary:

* :class:`SubjectContext` — the verified subject and the effective tenant, as
  answered by IS-001;
* :class:`TenantVerdict` — whether the Tenant may be served, as answered by
  IS-002;
* :class:`DependencyRefusal` — the dependency did not produce a usable answer;
  ``reason_code`` is the published code it stated, or ``None`` when it stated
  nothing this component can read. Either way the decision is a denial: a
  dependency that cannot answer is never an implicit yes.

Reason codes stay plain strings on purpose. They are translated into this
component's own :class:`~authorization_service.contracts.Reason` values by
:mod:`authorization_service.policy`, and an unknown code fails closed there.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ContextAnswer",
    "DependencyRefusal",
    "LifecycleAnswer",
    "SubjectContext",
    "TenantVerdict",
]


@dataclass(frozen=True, slots=True)
class SubjectContext:
    """The verified subject of a request and the tenant effective for it.

    Produced from the answer of IS-001 and used for exactly one decision. It is
    a value: holding one grants nothing, and no operation of this component
    accepts one from a consumer.
    """

    identity_id: str
    tenant_id: str
    kind: str | None = None
    subject: str | None = None
    platform_id: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class TenantVerdict:
    """Whether the Tenant may be served, as decided by its owning component."""

    permitted: bool
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyRefusal:
    """A dependency refused, or failed, to answer within its published contract."""

    reason_code: str | None = None


#: What the identity port returns: a context, or the reason there is none.
ContextAnswer = SubjectContext | DependencyRefusal

#: What the tenant authority port returns: a verdict, or the reason there is none.
LifecycleAnswer = TenantVerdict | DependencyRefusal
