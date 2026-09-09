"""Lifecycle enforcement + security acceptance scenario — T-008 (Issue #10, §5, §Security).

Обычные tenant-scoped операции запрещены в состояниях, когда Tenant не должен
обслуживаться. Порядок шагов повторяет security acceptance scenario Issue #10.
"""

from __future__ import annotations

import pytest

from identity_service.errors import AccessDenied
from identity_service.models import (
    Decision,
    DenyReason,
    IdentityKind,
    TenantAssociation,
    VerifiedIdentity,
)
from tenant_authority.contracts import TenantState
from tenant_authority.errors import InvalidTransition
from tenant_authority.models import Decision as AuthorityDecision
from tests.conftest import composed, provision_tenant, tenant_authority

ADMIN_TOKEN = "svc-token-admin"
ACTIVE = TenantState.ACTIVE
SUSPENDED = TenantState.SUSPENDED
DELETION = TenantState.DELETION_REQUESTED
DELETED = TenantState.DELETED

NOT_SERVING = {
    TenantState.PROVISIONING: DenyReason.TENANT_NOT_ACTIVE,
    SUSPENDED: DenyReason.TENANT_SUSPENDED,
    DELETION: DenyReason.TENANT_DELETION_REQUESTED,
    DELETED: DenyReason.TENANT_DELETED,
}


def bind_identity(
    identity_engine, identity_id: str, tenant_id: str, permissions
) -> None:
    """IS-001 owns identity <-> Tenant association data; it does not copy Tenant state."""
    identity_engine.store.identities[identity_id] = VerifiedIdentity(
        identity_id,
        IdentityKind.HUMAN,
        f"{identity_id}@example.test",
        home_tenant_id=tenant_id,
    )
    identity_engine.store.tokens[f"token-{identity_id}"] = identity_id
    identity_engine.store.associations[(identity_id, tenant_id)] = TenantAssociation(
        identity_id, tenant_id, frozenset(permissions)
    )


def write_outcome(identity_engine, *, token: str, record_id: str, claimed: str | None):
    try:
        identity_engine.write_record(token, record_id, "payload", claimed)
        return "ALLOW", None
    except AccessDenied as exc:
        return "DENY", exc.reason.value


def read_outcome(identity_engine, *, token: str, record_id: str, claimed: str | None):
    try:
        identity_engine.read_record(token, record_id, claimed)
        return "ALLOW", None
    except AccessDenied as exc:
        return "DENY", exc.reason.value


def test_security_acceptance_scenario_of_issue_10():
    identity_engine, authority = composed()
    ta = authority.engine

    # 1. Platform Instance P exists (its identity is deployment configuration).
    platform_p = authority.current_platform_id

    # 2. Tenant A and Tenant B are created by the Tenant Authority.
    snapshot_a, _, _ = ta.create_tenant(
        ADMIN_TOKEN, tenant_id="ten_A", request_id="req-create"
    )
    ta.create_tenant(ADMIN_TOKEN, tenant_id="ten_B", request_id="req-create")
    assert snapshot_a.state is TenantState.PROVISIONING

    bind_identity(
        identity_engine, "idn_scenario_a", "ten_A", {"records.read", "records.write"}
    )
    bind_identity(
        identity_engine, "idn_scenario_b", "ten_B", {"records.read", "records.write"}
    )

    # An unactivated Tenant is not served yet.
    assert write_outcome(
        identity_engine,
        token="token-idn_scenario_a",
        record_id="rec_A",
        claimed="ten_A",
    ) == ("DENY", DenyReason.TENANT_NOT_ACTIVE.value)

    # 3. Tenant A and Tenant B are activated.
    ta.transition_tenant(ADMIN_TOKEN, "ten_A", ACTIVE, request_id="req-activate-a")
    ta.transition_tenant(ADMIN_TOKEN, "ten_B", ACTIVE, request_id="req-activate-b")
    assert ta.lookup("ten_A", expected_platform_id=platform_p).state is ACTIVE

    # 4. Verified Identity A receives Tenant Context A.
    identity_a = identity_engine.verify_identity("token-idn_scenario_a")
    context_a = identity_engine.resolve_tenant_context(identity_a, None)
    assert context_a.tenant_id == "ten_A"
    assert context_a.status is ACTIVE
    assert context_a.platform_id == platform_p
    assert context_a.source == "verified_identity"
    assert context_a.state_source == "tenant_authority"

    # 5. Operation with tenant=A -> ALLOW.
    assert write_outcome(
        identity_engine,
        token="token-idn_scenario_a",
        record_id="rec_A",
        claimed="ten_A",
    ) == ("ALLOW", None)

    # 6. Same identity, claimed tenant_id=B -> DENY (a rewritten id is not a context change).
    assert write_outcome(
        identity_engine,
        token="token-idn_scenario_a",
        record_id="rec_A",
        claimed="ten_B",
    ) == ("DENY", DenyReason.TENANT_MISMATCH.value)

    # 7. Tenant A: active -> suspended.
    ta.transition_tenant(ADMIN_TOKEN, "ten_A", SUSPENDED, request_id="req-suspend")
    assert ta.lookup("ten_A").state is SUSPENDED

    # 8. Ordinary tenant-scoped write for A -> DENY; Tenant B is unaffected.
    assert write_outcome(
        identity_engine,
        token="token-idn_scenario_a",
        record_id="rec_A",
        claimed="ten_A",
    ) == ("DENY", DenyReason.TENANT_SUSPENDED.value)
    assert read_outcome(
        identity_engine,
        token="token-idn_scenario_a",
        record_id="rec_A",
        claimed="ten_A",
    ) == ("DENY", DenyReason.TENANT_SUSPENDED.value)
    assert write_outcome(
        identity_engine,
        token="token-idn_scenario_b",
        record_id="rec_B",
        claimed="ten_B",
    ) == ("ALLOW", None)

    # 9. Tenant A: suspended -> active. 10. The operation is allowed again.
    ta.transition_tenant(ADMIN_TOKEN, "ten_A", ACTIVE, request_id="req-reactivate")
    assert write_outcome(
        identity_engine,
        token="token-idn_scenario_a",
        record_id="rec_A",
        claimed="ten_A",
    ) == ("ALLOW", None)

    # 11. Tenant A: active -> deletion_requested. 12. Ordinary operation -> DENY.
    ta.transition_tenant(
        ADMIN_TOKEN, "ten_A", DELETION, request_id="req-delete-request"
    )
    assert write_outcome(
        identity_engine,
        token="token-idn_scenario_a",
        record_id="rec_A",
        claimed="ten_A",
    ) == ("DENY", DenyReason.TENANT_DELETION_REQUESTED.value)

    # 13. Tenant A: deletion_requested -> deleted. 14. Any ordinary operation -> DENY.
    ta.transition_tenant(ADMIN_TOKEN, "ten_A", DELETED, request_id="req-deleted")
    for operation in (write_outcome, read_outcome):
        assert operation(
            identity_engine,
            token="token-idn_scenario_a",
            record_id="rec_A",
            claimed="ten_A",
        ) == ("DENY", DenyReason.TENANT_DELETED.value)

    # 15. deleted -> active is refused: `deleted` is terminal.
    with pytest.raises(InvalidTransition):
        ta.transition_tenant(ADMIN_TOKEN, "ten_A", ACTIVE, request_id="req-resurrect")
    assert ta.lookup("ten_A").state is DELETED

    # 16. Every accepted transition has an audit record.
    ten_a_transitions = [
        item for item in ta.store.transitions if item.tenant_id == "ten_A"
    ]
    audited_transitions = [
        event
        for event in ta.store.audit
        if event.action == "tenant.transition"
        and event.decision is AuthorityDecision.ALLOW
    ]
    assert [item.previous_state for item in ten_a_transitions] == [
        TenantState.PROVISIONING,
        ACTIVE,
        SUSPENDED,
        ACTIVE,
        DELETION,
    ]
    assert [item.new_state for item in ten_a_transitions][-1] is DELETED
    assert len(ten_a_transitions) == 5
    assert len(audited_transitions) == 6  # five for Tenant A, one for Tenant B
    for item in ten_a_transitions:
        assert item.actor_id == "svc_tenant_admin"
        assert item.request_id and item.correlation_id and item.timestamp

    # 17. Security-sensitive denials are observable in both journals.
    assert any(
        event.decision is Decision.DENY
        and event.reason == DenyReason.TENANT_MISMATCH.value
        for event in identity_engine.store.audit
    )
    denial = ta.store.audit[-1]
    assert denial.decision is AuthorityDecision.DENY
    assert denial.reason == "invalid_transition"
    assert denial.tenant_id == "ten_A"
    assert denial.request_id == "req-resurrect"


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    sorted(NOT_SERVING.items(), key=lambda kv: kv[0].value),
)
def test_not_serving_states_block_ordinary_tenant_scoped_operations(
    state, expected_reason
):
    identity_engine, authority = composed()
    tenant_id = provision_tenant(authority, f"gate-{state.value}", state)

    decision = authority.engine.lifecycle_decision(tenant_id)
    assert decision.permitted is False
    assert decision.reason != "permitted"

    bind_identity(
        identity_engine, "idn_gate", tenant_id, {"records.read", "records.write"}
    )
    assert write_outcome(
        identity_engine, token="token-idn_gate", record_id="rec_gate", claimed=tenant_id
    ) == ("DENY", expected_reason.value)


def test_active_state_permits_ordinary_tenant_scoped_operations():
    ta = tenant_authority()
    decision = ta.engine.lifecycle_decision("ten_a")
    assert decision.permitted is True
    assert decision.reason == "permitted"
    assert decision.state is ACTIVE


def test_enforcement_is_live_not_a_snapshot_taken_at_context_resolution():
    """State is read at the authorization boundary: a transition takes effect for
    the very next operation instead of being cached by the caller."""
    identity_engine, authority = composed()
    ta = authority.engine
    bind_identity(
        identity_engine, "idn_live", "ten_a", {"records.read", "records.write"}
    )

    assert (
        write_outcome(
            identity_engine,
            token="token-idn_live",
            record_id="rec_live",
            claimed="ten_a",
        )[0]
        == "ALLOW"
    )
    ta.transition_tenant(ADMIN_TOKEN, "ten_a", SUSPENDED)
    assert write_outcome(
        identity_engine, token="token-idn_live", record_id="rec_live", claimed="ten_a"
    ) == ("DENY", DenyReason.TENANT_SUSPENDED.value)
    ta.transition_tenant(ADMIN_TOKEN, "ten_a", ACTIVE)
    assert (
        write_outcome(
            identity_engine,
            token="token-idn_live",
            record_id="rec_live",
            claimed="ten_a",
        )[0]
        == "ALLOW"
    )
    actions = [event.action for event in authority.store.audit]
    last_transition = len(actions) - 1 - actions[::-1].index("tenant.transition")
    # Enforcement is live: the consumer's own reads at the authorization boundary
    # are the events that follow the transition in the authority's journal.
    assert actions[last_transition + 1 :] == ["tenant.read", "tenant.lifecycle"]
    enforcing = authority.store.audit[last_transition + 1 :]
    assert {event.actor_id for event in enforcing} == {"svc_identity"}
    assert {event.decision for event in enforcing} == {AuthorityDecision.ALLOW}
    assert {event.tenant_id for event in enforcing} == {"ten_a"}


def test_suspension_blocks_reads_as_well_as_writes():
    identity_engine, _ = composed()
    bind_identity(
        identity_engine, "idn_read", "ten_suspended", {"records.read", "records.write"}
    )
    assert read_outcome(
        identity_engine,
        token="token-idn_read",
        record_id="rec_a1",
        claimed="ten_suspended",
    ) == ("DENY", DenyReason.TENANT_SUSPENDED.value)


def test_serving_state_still_requires_an_explicit_permission():
    """Lifecycle enforcement does not replace authorization at the data owner."""
    identity_engine, _ = composed()
    bind_identity(identity_engine, "idn_no_perm", "ten_b", set())
    assert write_outcome(
        identity_engine, token="token-idn_no_perm", record_id="rec_b", claimed="ten_b"
    ) == ("DENY", DenyReason.INSUFFICIENT_AUTHORIZATION.value)


def test_registry_row_of_a_deleted_tenant_stays_authoritative_and_inspectable():
    """`deleted` is a lifecycle state here, not a physical deletion engine."""
    ta = tenant_authority()
    with pytest.raises(InvalidTransition):
        ta.engine.transition_tenant(ADMIN_TOKEN, "ten_deleted", ACTIVE)
    assert ta.engine.lookup("ten_deleted").state is DELETED
    snapshot, _, _ = ta.engine.get_tenant(ADMIN_TOKEN, "ten_deleted")
    assert snapshot.tenant_id == "ten_deleted"
    assert snapshot.state is DELETED


def test_lifecycle_enforcement_is_applied_per_tenant_not_globally():
    ta = tenant_authority()
    suspended = provision_tenant(ta, "isolated-s", SUSPENDED)
    active = provision_tenant(ta, "isolated-a", ACTIVE)
    assert ta.engine.lifecycle_decision(suspended).permitted is False
    assert ta.engine.lifecycle_decision(active).permitted is True
