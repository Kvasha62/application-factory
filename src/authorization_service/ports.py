"""Ports through which the Authorization Boundary consumes other components.

IS-003 may not read another component's storage, internal modules or internal
types (ARCHITECTURE.md §1.1, §6.1, LAW-04). It depends on two narrow ports —
expressed entirely in this component's own vocabulary — and on nothing else:

* :class:`IdentityContextPort` — the verified subject and the effective tenant
  of a request. This is why IS-003 contains no identity verification and no
  tenant derivation of its own: there is no second tenant-context mechanism,
  because there is no mechanism here at all (invariant 2).
* :class:`TenantAuthorityPort` — whether the Tenant may be served right now.
  The lifecycle state machine and its operational policy stay in IS-002; the
  verdict is re-read at decision time and never cached (invariant 7).

A port declares what this component *needs*, not what another component happens
to offer: nothing here can create a Tenant, change a lifecycle state, read a
store or choose an actor. Both operations are total — they answer with a value
of :mod:`authorization_service.consumed`, including when the answer is "no" or
"none": a refusal of a dependency is data this component decides on, not an
exception vocabulary borrowed from the provider.

The implementations wired in by a composition root are the adapters of
:mod:`authorization_service.adapters`, which sit over the published consumer
surfaces of IS-001 and IS-002. Nothing in this module — or anywhere else in the
component — imports those components.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from authorization_service.consumed import ContextAnswer, LifecycleAnswer


@runtime_checkable
class IdentityContextPort(Protocol):
    """Source of the verified subject and of the effective tenant (IS-001)."""

    def resolve_context(
        self,
        credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextAnswer: ...


@runtime_checkable
class TenantAuthorityPort(Protocol):
    """Source of the verdict on whether a Tenant may be served (IS-002)."""

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleAnswer: ...


__all__ = [
    "ContextAnswer",
    "IdentityContextPort",
    "LifecycleAnswer",
    "TenantAuthorityPort",
]
