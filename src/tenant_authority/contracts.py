"""Public contract of the Tenant Authority component (IS-002).

This module is the only Tenant Authority surface that other components may
depend on. It publishes the Tenant vocabulary, the authoritative read model and
the error classes. Internal modules (``store``, ``engine`` internals, audit
journal) are not part of the contract and must not be imported by consumers
(ARCHITECTURE.md §1.1, LAW-04, invariant T-009).

Consumers inside the Level 0 modular monolith use these dataclasses through the
published operation semantics of ``GET /api/v1/tenants/...`` — the in-process
client adapter returns exactly the same objects the HTTP contract serializes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

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
    """Authoritative read model of a Tenant.

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


class AuthoritativeSource(Protocol):
    """Operations an implementation must provide to be published to consumers."""

    def lookup(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        consumer_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantSnapshot: ...

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        consumer_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleDecision: ...


class TenantAuthorityReader:
    """The narrow in-process view of the authority handed to other components.

    Only the published read operations are reachable. A consumer gets no handle
    on the registry store, the audit journal, the idempotency table or any
    mutation operation — invariant T-009 is enforced by the shape of this object,
    not by convention. Reaching ``_source`` is a boundary violation, not a
    supported integration path.
    """

    __slots__ = ("_source",)

    def __init__(self, source: AuthoritativeSource) -> None:
        self._source = source

    def lookup(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        consumer_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantSnapshot:
        return self._source.lookup(
            tenant_id,
            expected_platform_id=expected_platform_id,
            consumer_id=consumer_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        consumer_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleDecision:
        return self._source.lifecycle_decision(
            tenant_id,
            expected_platform_id=expected_platform_id,
            consumer_id=consumer_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )


__all__ = [
    "AuthoritativeSource",
    "TenantAuthorityReader",
    "AuthenticationDenied",
    "AuthorizationDenied",
    "DenyReason",
    "InvalidTransition",
    "LifecycleDecision",
    "OwnershipDenied",
    "TENANT_STATE_SOURCE",
    "TenantAuthorityError",
    "TenantConflict",
    "TenantNotFound",
    "TenantSnapshot",
    "TenantState",
]
