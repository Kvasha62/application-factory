"""Published data contract of the Identity / Tenant Context component (IS-001).

This module publishes the identity vocabulary and the immutable value a consumer
receives across the boundary. Internal modules (``engine``, ``store``,
``models``, ``api``, the audit journal and the record operations) are not part of
the contract and must not be imported by another component
(ARCHITECTURE.md §1.1, LAW-04).

The object a consumer receives is
``identity_service.reader.IdentityContextClient`` — one read operation over the
published API, values in and values out.

``VerifiedContext`` is the answer to a single question: *who is the caller and
which Tenant is effective for this request*. The effective Tenant is derived from
the verified identity only (``source == "verified_identity"``); a caller-supplied
``tenant_id`` is a cross-check and never a source of tenant identity (LAW-16a).
Tenant lifecycle state is deliberately absent: it belongs to Tenant Authority
(IS-002, invariant T-004) and a consumer must read it there, live, instead of
using a snapshot taken by this component.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Where the effective tenant of a request comes from — never a caller claim.
TENANT_CONTEXT_SOURCE = "verified_identity"


class IdentityKind(StrEnum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"


class DenyReason(StrEnum):
    """Machine-readable reasons an identity/tenant-context read is refused."""

    MISSING_IDENTITY = "missing_identity"
    INVALID_IDENTITY = "invalid_identity"
    UNKNOWN_IDENTITY = "unknown_identity"
    MISSING_TENANT_CONTEXT = "missing_tenant_context"
    TENANT_MISMATCH = "tenant_mismatch"
    TENANT_SUSPENDED = "tenant_suspended"
    TENANT_DELETED = "tenant_deleted"
    TENANT_DELETION_REQUESTED = "tenant_deletion_requested"
    TENANT_NOT_ACTIVE = "tenant_not_active"
    TENANT_UNKNOWN = "tenant_unknown"
    PLATFORM_OWNERSHIP_MISMATCH = "platform_ownership_mismatch"
    INSUFFICIENT_AUTHORIZATION = "insufficient_authorization"
    UNKNOWN_RESOURCE = "unknown_resource"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


@dataclass(frozen=True, slots=True)
class VerifiedContext:
    """Verified subject plus the effective Tenant of one request.

    A value, never a live object: holding it keeps no handle on the identity
    store, and mutating it is impossible. ``source`` proves that the effective
    tenant came from the verified identity, so a consumer cannot mistake a
    caller claim for tenant identity.
    """

    identity_id: str
    kind: IdentityKind
    subject: str
    tenant_id: str
    platform_id: str
    source: str = TENANT_CONTEXT_SOURCE


__all__ = [
    "TENANT_CONTEXT_SOURCE",
    "DenyReason",
    "IdentityKind",
    "VerifiedContext",
]
