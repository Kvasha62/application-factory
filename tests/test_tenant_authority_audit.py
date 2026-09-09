"""Audit перехода состояний Tenant — инвариант T-010 (Issue #10, §6 Scope).

Каждый lifecycle transition аудируется минимум с: `tenant_id`, `previous_state`,
`new_state`, `actor/service_id`, `timestamp`, `request_id`, `correlation_id`.
Отказы (в том числе probe state machine) наблюдаемы в том же журнале.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from tenant_authority.contracts import TenantState
from tenant_authority.errors import AuthenticationDenied, InvalidTransition
from tenant_authority.models import Decision
from tests.conftest import provision_tenant, tenant_authority

REQUIRED_AUDIT_FIELDS = (
    "event_id",
    "action",
    "decision",
    "reason",
    "actor_id",
    "platform_id",
    "tenant_id",
    "request_id",
    "correlation_id",
    "timestamp",
    "details",
)


def test_accepted_transition_is_fully_auditable():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "audit", TenantState.PROVISIONING)

    transition, _, event = ta.engine.transition_tenant(
        "svc-token-admin",
        tenant_id,
        TenantState.ACTIVE,
        request_id="req-audit-1",
        correlation_id="cor-audit-1",
        reason="first activation",
    )

    assert transition.tenant_id == tenant_id
    assert transition.previous_state is TenantState.PROVISIONING
    assert transition.new_state is TenantState.ACTIVE
    assert transition.actor_id == "svc_tenant_admin"
    assert transition.request_id == "req-audit-1"
    assert transition.correlation_id == "cor-audit-1"
    assert transition.timestamp
    assert transition.reason == "first activation"

    assert event.decision is Decision.ALLOW
    assert event.action == "tenant.transition"
    assert event.details == {
        "transition_id": transition.transition_id,
        "previous_state": "provisioning",
        "new_state": "active",
    }
    # The audit event and the transition record describe the same fact.
    assert event.request_id == transition.request_id
    assert event.correlation_id == transition.correlation_id
    assert event.tenant_id == transition.tenant_id
    assert event.actor_id == transition.actor_id


def test_audit_event_carries_the_full_field_set():
    ta = tenant_authority()
    provision_tenant(ta, "fields", TenantState.SUSPENDED)
    for event in ta.store.audit:
        names = {f.name for f in fields(event)}
        assert set(REQUIRED_AUDIT_FIELDS) <= names
        for field in REQUIRED_AUDIT_FIELDS:
            if field == "reason":
                continue
            assert getattr(event, field) is not None, field
        assert event.actor_id == "svc_tenant_admin"

    # `reason` is empty on ALLOW and names the denial on DENY.
    assert all(
        event.reason is None
        for event in ta.store.audit
        if event.decision is Decision.ALLOW
    )


def test_rejected_transition_is_audited_as_a_deny():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "deny", TenantState.DELETED)

    with pytest.raises(InvalidTransition):
        ta.engine.transition_tenant(
            "svc-token-admin", tenant_id, TenantState.ACTIVE, request_id="req-probe"
        )

    denial = ta.store.audit[-1]
    assert denial.decision is Decision.DENY
    assert denial.reason == "invalid_transition"
    assert denial.tenant_id == tenant_id
    assert denial.request_id == "req-probe"
    assert denial.details == {
        "previous_state": "deleted",
        "requested_state": "active",
    }
    # A rejected transition leaves no trace in the lifecycle history.
    assert [item for item in ta.store.transitions if item.tenant_id == tenant_id][
        -1
    ].new_state is TenantState.DELETED


@pytest.mark.parametrize("state", [state for state in TenantState])
def test_every_accepted_state_change_has_exactly_one_audit_record(state):
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, f"count-{state.value}", state)

    transitions = [item for item in ta.store.transitions if item.tenant_id == tenant_id]
    allowed = [
        event
        for event in ta.store.audit
        if event.action == "tenant.transition"
        and event.tenant_id == tenant_id
        and event.decision is Decision.ALLOW
    ]
    assert len(transitions) == list(TenantState).index(state)
    assert len(allowed) == len(transitions)
    assert {(event.details.get("transition_id")) for event in allowed} == {
        item.transition_id for item in transitions
    }


def test_terminal_state_has_a_deny_entry_for_every_resurrection_attempt():
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, "resurrect", TenantState.DELETED)
    before = len(ta.store.audit)

    for requested in TenantState:
        with pytest.raises(InvalidTransition):
            ta.engine.transition_tenant("svc-token-admin", tenant_id, requested)

    denials = ta.store.audit[before:]
    assert len(denials) == len(list(TenantState))
    assert {event.reason for event in denials} == {"invalid_transition"}
    assert {event.decision for event in denials} == {Decision.DENY}


def test_audit_journal_is_append_only():
    ta = tenant_authority()
    ta.engine.create_tenant("svc-token-admin", tenant_id="ten_append")
    snapshot_before = list(ta.store.audit)

    ta.engine.transition_tenant("svc-token-admin", "ten_append", TenantState.ACTIVE)

    assert ta.store.audit[: len(snapshot_before)] == snapshot_before
    assert len(ta.store.audit) == len(snapshot_before) + 1  # the accepted transition
    event_ids = [event.event_id for event in ta.store.audit]
    assert len(event_ids) == len(set(event_ids))
    for forbidden in ("delete_audit", "purge_audit", "clear_audit", "update_audit"):
        assert not hasattr(ta.engine, forbidden), forbidden
    assert not hasattr(ta.engine, "rewrite_transition")


def test_audit_never_records_credentials():
    ta = tenant_authority()
    provision_tenant(ta, "credentials", TenantState.ACTIVE)
    with pytest.raises(InvalidTransition):
        ta.engine.transition_tenant(
            "svc-token-admin", "ten_credentials", TenantState.DELETED
        )
    # A forged credential is denied and never written into the journal.
    with pytest.raises(AuthenticationDenied):
        ta.engine.transition_tenant(
            "svc-token-forged-secret", "ten_credentials", TenantState.SUSPENDED
        )
    denial = ta.store.audit[-1]
    assert denial.reason == "invalid_service_identity"
    assert denial.actor_id is None
    recorded = repr(ta.store.audit)
    assert "svc-token-forged-secret" not in recorded
    assert "svc-token-admin" not in recorded


def test_denied_lifecycle_operations_of_another_platform_are_auditable():
    ta = tenant_authority()
    with pytest.raises(Exception) as exc:
        ta.engine.transition_tenant(
            "svc-token-admin", "ten_foreign", TenantState.SUSPENDED
        )
    assert exc.value.reason.value == "foreign_tenant"
    denial = ta.store.audit[-1]
    assert denial.decision is Decision.DENY
    assert denial.reason == "foreign_tenant"
    assert denial.actor_id == "svc_tenant_admin"
    assert denial.platform_id == ta.current_platform_id
