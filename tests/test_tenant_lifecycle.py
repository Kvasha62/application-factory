"""Tenant lifecycle state machine — инварианты T-005, T-006 (Issue #10, §2 Scope).

The permitted set is exactly the one the architecture and the Issue require; the
tests below prove the machine behaviourally over the whole state cross-product
instead of asserting on a table in isolation.
"""

from __future__ import annotations

import itertools

import pytest

from tenant_authority.contracts import TenantState
from tenant_authority.errors import DenyReason, InvalidTransition
from tenant_authority.lifecycle import ALLOWED_TRANSITIONS, is_allowed_transition
from tests.conftest import provision_tenant, tenant_authority

ALL_STATES = list(TenantState)


def test_provisioning_becomes_active():
    ta = tenant_authority()
    ta.engine.create_tenant("svc-token-admin", tenant_id="ten_new")
    transition, _, _ = ta.engine.transition_tenant(
        "svc-token-admin", "ten_new", TenantState.ACTIVE
    )
    assert transition.previous_state is TenantState.PROVISIONING
    assert transition.new_state is TenantState.ACTIVE
    assert ta.engine.lookup("ten_new").state is TenantState.ACTIVE


@pytest.mark.parametrize("state", ALL_STATES)
def test_every_lifecycle_state_is_reachable_through_allowed_transitions(state: TenantState):
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "reach", state)
    assert ta.engine.lookup(tenant_id).state is state


def test_reactivation_from_suspended_is_allowed():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "reactivate", TenantState.SUSPENDED)
    ta.engine.transition_tenant("svc-token-admin", tenant_id, TenantState.ACTIVE)
    assert ta.engine.lookup(tenant_id).state is TenantState.ACTIVE


def test_suspended_can_also_proceed_to_deletion():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "susp-del", TenantState.SUSPENDED)
    ta.engine.transition_tenant("svc-token-admin", tenant_id, TenantState.DELETION_REQUESTED)
    ta.engine.transition_tenant("svc-token-admin", tenant_id, TenantState.DELETED)
    assert ta.engine.lookup(tenant_id).state is TenantState.DELETED


def test_full_transition_matrix_matches_the_published_state_machine():
    """T-005: transitions are limited to the state machine — nothing more, nothing less."""
    for previous, requested in itertools.product(ALL_STATES, ALL_STATES):
        ta = tenant_authority()
        tenant_id = provision_tenant(ta, f"m-{previous.value}", previous)
        expected = requested in ALLOWED_TRANSITIONS[previous]
        assert expected is is_allowed_transition(previous, requested)
        if expected:
            ta.engine.transition_tenant("svc-token-admin", tenant_id, requested)
            assert ta.engine.lookup(tenant_id).state is requested
        else:
            with pytest.raises(InvalidTransition) as exc:
                ta.engine.transition_tenant("svc-token-admin", tenant_id, requested)
            assert exc.value.reason is DenyReason.INVALID_TRANSITION
            assert ta.engine.lookup(tenant_id).state is previous


def test_only_the_six_required_transitions_are_permitted():
    permitted = {
        (previous.value, target.value)
        for previous, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    }
    assert permitted == {
        ("provisioning", "active"),
        ("active", "suspended"),
        ("suspended", "active"),
        ("active", "deletion_requested"),
        ("suspended", "deletion_requested"),
        ("deletion_requested", "deleted"),
    }


def test_no_transition_leaves_deleted():
    """T-006: `deleted` is terminal — a Tenant cannot be resurrected."""
    for requested in ALL_STATES:
        ta = tenant_authority()
        tenant_id = provision_tenant(ta, "terminal", TenantState.DELETED)
        with pytest.raises(InvalidTransition):
            ta.engine.transition_tenant("svc-token-admin", tenant_id, requested)
        assert ta.engine.lookup(tenant_id).state is TenantState.DELETED
        assert ta.store.tenants[tenant_id].state is TenantState.DELETED


def test_deleted_tenant_stays_visible_as_deleted_not_erased():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "erase", TenantState.DELETED)
    snapshot = ta.engine.lookup(tenant_id)
    assert snapshot.state is TenantState.DELETED
    assert snapshot.state.is_terminal


def test_self_transition_is_rejected():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "self", TenantState.ACTIVE)
    with pytest.raises(InvalidTransition):
        ta.engine.transition_tenant("svc-token-admin", tenant_id, TenantState.ACTIVE)
    assert ta.engine.lookup(tenant_id).state is TenantState.ACTIVE


def test_rejected_transition_does_not_change_state_or_produce_a_transition_record():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "reject", TenantState.PROVISIONING)
    before = len(ta.store.transitions)
    with pytest.raises(InvalidTransition):
        ta.engine.transition_tenant("svc-token-admin", tenant_id, TenantState.DELETED)
    assert ta.engine.lookup(tenant_id).state is TenantState.PROVISIONING
    assert len(ta.store.transitions) == before
    denial = ta.store.audit[-1]
    assert denial.decision.value == "DENY"
    assert denial.reason == DenyReason.INVALID_TRANSITION.value
    assert denial.tenant_id == tenant_id


def test_transition_state_accepts_the_contract_vocabulary():
    ta = tenant_authority()
    ta.engine.create_tenant("svc-token-admin", tenant_id="ten_str")
    ta.engine.transition_tenant("svc-token-admin", "ten_str", "active")
    assert ta.engine.lookup("ten_str").state is TenantState.ACTIVE

    # A state outside the published vocabulary is a rejection, not a crash.
    with pytest.raises(InvalidTransition) as exc:
        ta.engine.transition_tenant("svc-token-admin", "ten_str", "archived")
    assert exc.value.details == {"requested_state": "archived"}
    assert ta.engine.lookup("ten_str").state is TenantState.ACTIVE
    assert ta.store.audit[-1].reason == DenyReason.INVALID_TRANSITION.value


def test_state_filter_accepts_the_published_vocabulary_only():
    ta = tenant_authority()
    listed, _, _ = ta.engine.list_tenants("svc-token-admin", state="deleted")
    assert [record.tenant_id for record in listed] == ["ten_deleted"]
    listed_enum, _, _ = ta.engine.list_tenants("svc-token-admin", state=TenantState.DELETED)
    assert [record.tenant_id for record in listed_enum] == ["ten_deleted"]
    with pytest.raises(ValueError):
        ta.engine.list_tenants("svc-token-admin", state="archived")


def test_state_machine_is_published_by_the_component_and_not_invented_by_callers():
    ta = tenant_authority()
    decision = ta.engine.lifecycle_decision("ten_a")
    assert decision.state is TenantState.ACTIVE
    assert ta.engine.lookup("ten_a").state is decision.state
