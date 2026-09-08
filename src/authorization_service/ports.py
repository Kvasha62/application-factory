"""Ports through which the Authorization Boundary consumes other components.

IS-003 may not read another component's storage or internal modules
(ARCHITECTURE.md §1.1, LAW-04). It depends on two narrow ports — the published
read surfaces of IS-001 and IS-002 — and the only implementations it is wired
with are the value-only clients over those contracts:

* :class:`IdentityContextPort` — the verified subject and the effective tenant
  of a request. This is why IS-003 contains no identity verification and no
  tenant derivation of its own: there is no second tenant-context mechanism,
  because there is no mechanism here at all (invariant 2).
* :class:`TenantAuthorityPort` — whether the Tenant may be served right now.
  The lifecycle state machine and its operational policy stay in IS-002; the
  verdict is re-read at decision time and never cached (invariant 7).

A port declares what this component *needs*, not what another component
happens to offer: nothing here can create a Tenant, change a lifecycle state,
read a store or choose an actor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from identity_service.contracts import VerifiedContext
from tenant_authority.contracts import LifecycleDecision


@runtime_checkable
class IdentityContextPort(Protocol):
    """Published context read of Identity / Tenant Context (IS-001)."""

    def resolve_context(
        self,
        credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> VerifiedContext: ...


@runtime_checkable
class TenantAuthorityPort(Protocol):
    """Published lifecycle read of Tenant Authority (IS-002)."""

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleDecision: ...


__all__ = [
    "IdentityContextPort",
    "LifecycleDecision",
    "TenantAuthorityPort",
    "VerifiedContext",
]
