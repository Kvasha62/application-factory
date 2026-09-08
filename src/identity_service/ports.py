"""Ports through which Identity consumes other components.

IS-001 may not read another component's storage or internal modules
(ARCHITECTURE.md §1.1, LAW-04, invariant T-009). It depends on this narrow
port — the published read surface of Tenant Authority (IS-002) — and the only
implementation it is wired with is a value-only client over that contract.
Tenant state and Platform Instance ownership therefore come from the authority
and never from a local copy, so the repository cannot grow a second source of
Tenant state (invariant T-004).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tenant_authority.contracts import LifecycleDecision, TenantSnapshot, TenantState


@runtime_checkable
class TenantAuthorityPort(Protocol):
    """Published read operations of Tenant Authority used by Identity."""

    def lookup(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantSnapshot: ...

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleDecision: ...


__all__ = [
    "LifecycleDecision",
    "TenantAuthorityPort",
    "TenantSnapshot",
    "TenantState",
]
