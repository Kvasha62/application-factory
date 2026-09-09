"""Published data contract of the Tenant Authority component (IS-002).

This module publishes the Tenant vocabulary, the immutable read model returned
across the boundary and the error classes. Internal modules (``engine``,
``store``, ``models``, ``lifecycle``, the audit journal and the mutation
operations) are not part of the contract and must not be imported or reached
through the published surface (ARCHITECTURE.md §1.1, LAW-04, invariant T-009).

The object a consumer receives is ``tenant_authority.reader.TenantAuthorityClient``
— two read operations over the published API, values in and values out.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tenant_authority.errors import (
    AuthenticationDenied,
    AuthorizationDenied,
    DenyReason,
    InvalidTransition,
    OwnershipDenied,
    TenantAuthorityError,
    TenantConflict,
    TenantNotFound,
)

TENANT_STATE_SOURCE = "tenant_authority"


class TenantState(StrEnum):
    """Tenant lifecycle states defined by ARCHITECTURE.md §2.3 (AMD-02)."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETION_REQUESTED = "deletion_requested"
    DELETED = "deleted"

    @property
    def is_terminal(self) -> bool:
        return self is TenantState.DELETED


@dataclass(frozen=True, slots=True)
class TenantSnapshot:
    """Immutable read model of a Tenant.

    A value, never a live record: mutating it is impossible and cannot affect the
    registry, and holding it never keeps a handle on the authority's storage.

    ``state_source`` names the component that owns the lifecycle state, so a
    consumer can never mistake a locally cached copy for the authoritative one.
    """

    tenant_id: str
    platform_id: str
    state: TenantState
    created_at: str
    updated_at: str
    state_source: str = TENANT_STATE_SOURCE


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    """Whether an ordinary tenant-scoped operation is permitted right now.

    This is a statement about the Tenant lifecycle only. It is not an
    authorization decision: the data owner still applies authentication,
    permissions and tenant scoping at its own boundary (ARCHITECTURE.md §6.2).
    """

    tenant_id: str
    platform_id: str
    state: TenantState
    permitted: bool
    reason: str


__all__ = [
    "TENANT_STATE_SOURCE",
    "AuthenticationDenied",
    "AuthorizationDenied",
    "DenyReason",
    "InvalidTransition",
    "LifecycleDecision",
    "OwnershipDenied",
    "TenantAuthorityError",
    "TenantConflict",
    "TenantNotFound",
    "TenantSnapshot",
    "TenantState",
]
