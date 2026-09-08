"""Tenant Registry — инварианты T-001, T-002, T-003 (Issue #10, §1 Scope)."""

from __future__ import annotations

from dataclasses import fields

import pytest

from tests.conftest import tenant_authority
from tenant_authority.contracts import TENANT_STATE_SOURCE, TenantState
from tenant_authority.errors import (
    DenyReason,
    OwnershipDenied,
    TenantConflict,
    TenantNotFound,
)


def test_created_tenant_has_the_minimal_registry_model():
    ta = tenant_authority()
    snapshot, _, _ = ta.engine.create_tenant("svc-token-admin", tenant_id="ten_new")

    assert snapshot.tenant_id == "ten_new"
    assert snapshot.platform_id == ta.current_platform_id
    assert snapshot.state is TenantState.PROVISIONING
    assert snapshot.created_at and snapshot.updated_at
    assert snapshot.state_source == TENANT_STATE_SOURCE


def test_tenant_id_identifies_exactly_one_tenant():
    """T-001: один tenant_id идентифицирует ровно один Tenant."""
    ta = tenant_authority()
    first, _, _ = ta.engine.create_tenant("svc-token-admin")
    second, _, _ = ta.engine.create_tenant("svc-token-admin")

    assert first.tenant_id != second.tenant_id
    ids = list(ta.store.tenants)
    assert len(ids) == len(set(ids))
    for snapshot in (first, second):
        assert ta.engine.lookup(snapshot.tenant_id).tenant_id == snapshot.tenant_id
    assert [tid for tid in ta.store.tenants if tid == first.tenant_id] == [first.tenant_id]


def test_tenant_id_and_creation_time_are_immutable():
    """T-002: `tenant_id` immutable; identity row of the registry is never rewritten."""
    ta = tenant_authority()
    snapshot, _, _ = ta.engine.create_tenant("svc-token-admin", tenant_id="ten_immutable")
    created_at = snapshot.created_at

    ta.engine.transition_tenant("svc-token-admin", "ten_immutable", TenantState.ACTIVE)
    ta.engine.transition_tenant("svc-token-admin", "ten_immutable", TenantState.SUSPENDED)
    after = ta.engine.lookup("ten_immutable")

    assert after.tenant_id == "ten_immutable"
    assert after.platform_id == snapshot.platform_id
    assert after.created_at == created_at
    assert after.updated_at >= created_at
    assert after.state is TenantState.SUSPENDED


def test_engine_exposes_no_bypass_of_the_state_machine():
    """The only mutation surface is the guarded operation set (T-005)."""
    ta = tenant_authority()
    for forbidden in (
        "update_tenant",
        "set_state",
        "set_tenant_state",
        "delete_tenant",
        "hard_delete",
        "purge_audit",
        "clear_audit",
        "rewrite_audit",
    ):
        assert not hasattr(ta.engine, forbidden), forbidden


def test_unknown_tenant_is_rejected_and_audited():
    ta = tenant_authority()
    with pytest.raises(TenantNotFound) as exc:
        ta.engine.lookup("ten_missing")
    assert exc.value.reason is DenyReason.TENANT_NOT_FOUND

    with pytest.raises(TenantNotFound) as exc:
        ta.engine.get_tenant("svc-token-admin", "ten_missing")
    assert exc.value.reason is DenyReason.TENANT_NOT_FOUND
    denial = [ev for ev in ta.store.audit if ev.reason == DenyReason.TENANT_NOT_FOUND.value]
    assert denial and denial[-1].decision.value == "DENY"


def test_creating_an_existing_tenant_does_not_duplicate_the_registry_row():
    ta = tenant_authority()
    with pytest.raises(TenantConflict) as exc:
        ta.engine.create_tenant("svc-token-admin", tenant_id="ten_a")
    assert exc.value.reason is DenyReason.TENANT_EXISTS
    assert list(ta.store.tenants).count("ten_a") == 1
    assert ta.engine.lookup("ten_a").state is TenantState.ACTIVE


def test_tenant_has_exactly_one_current_state():
    """A state change replaces the single current state; no second marker exists."""
    ta = tenant_authority()
    snapshot, _, _ = ta.engine.create_tenant("svc-token-admin", tenant_id="ten_one_state")
    record = ta.store.tenants["ten_one_state"]

    assert record.state is snapshot.state
    ta.engine.transition_tenant("svc-token-admin", "ten_one_state", TenantState.ACTIVE)
    record_after = ta.store.tenants["ten_one_state"]

    assert record_after.state is TenantState.ACTIVE
    # The registry row carries exactly one state field: no shadow copy, no
    # secondary "is_suspended"/"pending_deletion" flag could diverge from it.
    registry_state_fields = [
        f.name for f in fields(record_after) if f.name.endswith(("state", "status"))
    ]
    assert registry_state_fields == ["state"]
    # History lives in the transition log, not in a second current-state marker.
    history = ta.store.transitions
    assert [item.previous_state for item in history if item.tenant_id == "ten_one_state"] == [
        TenantState.PROVISIONING
    ]


def test_tenant_of_another_platform_is_invisible_from_this_platform_context():
    """T-003: Tenant принадлежит ровно одной Platform Instance."""
    ta = tenant_authority()
    with pytest.raises(OwnershipDenied) as exc:
        ta.engine.lookup("ten_foreign", expected_platform_id=ta.current_platform_id)
    assert exc.value.reason is DenyReason.FOREIGN_TENANT
    # Existence is not disclosed across Platform Instance boundaries.
    assert exc.value.status_code == 404
