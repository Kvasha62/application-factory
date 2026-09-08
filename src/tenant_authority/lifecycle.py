"""Tenant lifecycle state machine — the single authority over allowed transitions.

The chain proven by IS-002 (Issue #10)::

    Verified Identity
          ↓
    Identity / Tenant Context        (IS-001 — source of effective_tenant_id)
          ↓
    Tenant Authority                 (IS-002 — this component)
       ├─ tenant exists?
       ├─ belongs to Platform Instance?
       ├─ lifecycle state?
       └─ operation permitted?
          ↓
    Authorization Boundary
          ↓
    Tenant-owned Resource

The transition set is exactly the one required by the Issue. No additional
operational policy is invented: the operation policy is the minimum needed to
prove invariants T-005, T-006 and T-008.
"""

from __future__ import annotations

from tenant_authority.contracts import LifecycleDecision, TenantState
from tenant_authority.errors import DenyReason, InvalidTransition

ALLOWED_TRANSITIONS: dict[TenantState, frozenset[TenantState]] = {
    TenantState.PROVISIONING: frozenset({TenantState.ACTIVE}),
    TenantState.ACTIVE: frozenset({TenantState.SUSPENDED, TenantState.DELETION_REQUESTED}),
    TenantState.SUSPENDED: frozenset({TenantState.ACTIVE, TenantState.DELETION_REQUESTED}),
    TenantState.DELETION_REQUESTED: frozenset({TenantState.DELETED}),
    # `deleted` is terminal: no transition may leave it (T-006).
    TenantState.DELETED: frozenset(),
}

LIFECYCLE_CHAIN: tuple[TenantState, ...] = (
    TenantState.PROVISIONING,
    TenantState.ACTIVE,
    TenantState.SUSPENDED,
    TenantState.DELETION_REQUESTED,
    TenantState.DELETED,
)

# reason -> (permitted, deny reason published to consumers)
OPERATION_POLICY: dict[TenantState, tuple[bool, str]] = {
    TenantState.PROVISIONING: (False, "provisioning_not_served"),
    TenantState.ACTIVE: (True, "permitted"),
    TenantState.SUSPENDED: (False, "tenant_suspended"),
    TenantState.DELETION_REQUESTED: (False, "tenant_deletion_requested"),
    TenantState.DELETED: (False, "tenant_deleted"),
}


def allowed_targets(previous: TenantState) -> frozenset[TenantState]:
    return ALLOWED_TRANSITIONS[previous]


def is_allowed_transition(previous: TenantState, new: TenantState) -> bool:
    return new in ALLOWED_TRANSITIONS[previous]


def assert_transition_allowed(previous: TenantState, new: TenantState) -> None:
    """Raise the contract-level rejection for a transition outside the machine."""
    if not is_allowed_transition(previous, new):
        raise InvalidTransition(
            DenyReason.INVALID_TRANSITION,
            f"tenant lifecycle transition {previous.value} -> {new.value} is not allowed",
            details={"previous_state": previous.value, "requested_state": new.value},
        )


def operation_decision(
    tenant_id: str,
    platform_id: str,
    state: TenantState,
) -> LifecycleDecision:
    permitted, reason = OPERATION_POLICY[state]
    return LifecycleDecision(
        tenant_id=tenant_id,
        platform_id=platform_id,
        state=state,
        permitted=permitted,
        reason=reason,
    )
