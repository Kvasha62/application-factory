"""Published data contract of the Authorization Boundary component (IS-003).

This module publishes the whole vocabulary a consumer needs: what a decision
request refers to (:class:`ResourceRef`), what comes back
(:class:`AuthorizationDecision`), the closed set of stable reason codes
(:class:`Reason`) and the error classes. Internal modules (``engine``,
``store``, ``models``, ``policy``, ``api``, ``deployment``, ``transport``) are
not part of the contract and must not be imported or reached through the
published surface (ARCHITECTURE.md §1.1, LAW-04).

The object a consumer receives is
``authorization_service.reader.AuthorizationClient`` — one operation, values in
and values out.

Two facts about the model are deliberate:

* the decision carries no data of the resource and no permission list — it is
  the minimum needed to enforce: a verdict and a reason;
* the decision is advice at the *asking* component's boundary: the final
  enforcement is performed by the component that owns the data, which is why
  nothing here can read or write a resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from authorization_service.errors import (
    AuthorizationServiceError,
    CallerDenyReason,
    CallerNotAuthenticated,
    CallerNotAuthorized,
    ContractViolation,
    MalformedDecisionRequest,
)

#: Component that produced a decision; a consumer can never mistake a locally
#: cached verdict for the authoritative one.
DECISION_SOURCE = "authorization"


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class Reason(StrEnum):
    """Stable reason codes of a decision — a closed, published set.

    Exactly one reason accompanies every decision. ``PERMITTED`` is the only
    reason an ``ALLOW`` may carry; every other member explains a ``DENY``.
    """

    PERMITTED = "permitted"

    # -- the subject was not established (invariants 1 and 5) -----------------
    MISSING_IDENTITY = "missing_identity"
    INVALID_IDENTITY = "invalid_identity"
    UNKNOWN_IDENTITY = "unknown_identity"

    # -- the effective tenant of the subject (invariants 2 and 3) -------------
    MISSING_TENANT_CONTEXT = "missing_tenant_context"
    TENANT_MISMATCH = "tenant_mismatch"
    TENANT_UNKNOWN = "tenant_unknown"
    PLATFORM_OWNERSHIP_MISMATCH = "platform_ownership_mismatch"

    # -- the resource is owned by another Tenant (invariant 4) ----------------
    RESOURCE_TENANT_UNKNOWN = "resource_tenant_unknown"
    RESOURCE_TENANT_MISMATCH = "resource_tenant_mismatch"

    # -- the Tenant is not in a state that may be served (invariant 7) --------
    TENANT_NOT_ACTIVE = "tenant_not_active"
    TENANT_SUSPENDED = "tenant_suspended"
    TENANT_DELETION_REQUESTED = "tenant_deletion_requested"
    TENANT_DELETED = "tenant_deleted"

    # -- no explicit permission (invariants 5 and 6) --------------------------
    PERMISSION_NOT_GRANTED = "permission_not_granted"

    # -- an authority did not answer: fail closed -----------------------------
    AUTHORITY_UNAVAILABLE = "authority_unavailable"


#: Every reason that denies. Derived from the published set, so a new reason
#: cannot be added without deciding on which side of the boundary it falls.
DENY_REASONS: frozenset[Reason] = frozenset(
    reason for reason in Reason if reason is not Reason.PERMITTED
)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Reference to a tenant-scoped resource, as stated by its data owner.

    ``tenant_id`` is the Tenant that *owns the resource*. It is supplied by the
    component that owns the data — the only component that knows it — and it is
    never a statement about the identity of the caller: the tenant of the
    subject comes from IS-001 and from nowhere else. The comparison of the two
    is what makes cross-tenant access impossible (LAW-16).
    """

    resource_type: str
    resource_id: str
    tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Minimal decision: ``ALLOW`` or ``DENY`` plus a stable reason.

    A value, never a live object: it grants nothing by existing, cannot be
    mutated and holds no handle on the authority. ``request_id`` and
    ``correlation_id`` are the same ones the decision was audited with, so a
    denial seen by a consumer can be found in this component's journal.
    """

    decision: Decision
    reason: Reason
    operation: str
    resource: ResourceRef
    subject_id: str | None = None
    tenant_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    decided_by: str = DECISION_SOURCE

    @property
    def allowed(self) -> bool:
        """True only for an explicit ``ALLOW``: the default is always deny."""
        return self.decision is Decision.ALLOW


__all__ = [
    "DECISION_SOURCE",
    "DENY_REASONS",
    "AuthorizationDecision",
    "AuthorizationServiceError",
    "CallerDenyReason",
    "CallerNotAuthenticated",
    "CallerNotAuthorized",
    "ContractViolation",
    "Decision",
    "MalformedDecisionRequest",
    "Reason",
    "ResourceRef",
]
