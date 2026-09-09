"""Идемпотентность операций Tenant Authority — ARCHITECTURE.md §6.5 (AMD-05).

Lifecycle transition и создание Tenant меняют состояние, поэтому повтор с тем же
`Idempotency-Key` не создаёт второго эффекта, а replay привязан к исходному
логическому запросу (actor, platform, operation, tenant, fingerprint).
"""

from __future__ import annotations

import pytest

from tenant_authority.contracts import TenantState
from tenant_authority.errors import (
    AuthorizationDenied,
    DenyReason,
    InvalidTransition,
    TenantConflict,
)
from tenant_authority.models import Decision, ServiceAccess
from tests.conftest import tenant_authority


def test_replayed_transition_does_not_create_a_second_effect():
    ta = tenant_authority()
    first, _, first_audit = ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-1"
    )
    replay, _, replay_audit = ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-1"
    )

    assert replay.transition_id == first.transition_id
    assert len(ta.store.transitions) == 1
    assert ta.engine.lookup("ten_a").state is TenantState.SUSPENDED
    assert replay_audit.reason == "idempotent_replay"
    assert replay_audit.decision is Decision.ALLOW
    assert first_audit.reason is None


def test_replay_after_reactivation_returns_the_original_transition():
    ta = tenant_authority()
    original, _, _ = ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-2"
    )
    ta.engine.transition_tenant("svc-token-admin", "ten_a", TenantState.ACTIVE)

    # The same key cannot be used to "re-run" an already superseded transition:
    # replay is not a retry, and it never applies the effect twice.
    replay, _, _ = ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-2"
    )
    assert replay.transition_id == original.transition_id
    assert ta.engine.lookup("ten_a").state is TenantState.ACTIVE


def test_same_key_with_another_target_state_is_a_conflict():
    ta = tenant_authority()
    ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-3"
    )
    with pytest.raises(TenantConflict) as exc:
        ta.engine.transition_tenant(
            "svc-token-admin", "ten_a", TenantState.DELETION_REQUESTED, idempotency_key="ik-3"
        )
    assert exc.value.reason is DenyReason.IDEMPOTENCY_CONFLICT
    assert ta.engine.lookup("ten_a").state is TenantState.SUSPENDED
    assert ta.store.audit[-1].reason == DenyReason.IDEMPOTENCY_CONFLICT.value


def test_same_key_for_another_tenant_is_a_conflict():
    ta = tenant_authority()
    ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-shared"
    )
    with pytest.raises(TenantConflict):
        ta.engine.transition_tenant(
            "svc-token-admin", "ten_b", TenantState.SUSPENDED, idempotency_key="ik-shared"
        )
    assert ta.engine.lookup("ten_b").state is TenantState.ACTIVE
    assert len(ta.store.transitions) == 1


def test_same_key_from_another_actor_does_not_replay_a_foreign_result():
    ta = tenant_authority()
    # Second administrator of the same Platform Instance (fixture data).
    ta.store.services["svc_other_admin"] = ServiceAccess(
        "svc_other_admin",
        platform_id=ta.current_platform_id,
        permissions=frozenset({"tenant.create", "tenant.read", "tenant.transition"}),
    )
    ta.store.service_tokens["svc-token-other-admin"] = "svc_other_admin"

    ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-actor"
    )
    with pytest.raises(TenantConflict) as exc:
        ta.engine.transition_tenant(
            "svc-token-other-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-actor"
        )
    assert exc.value.reason is DenyReason.IDEMPOTENCY_CONFLICT
    # The other actor receives neither the effect nor the result of the first request.
    assert [item for item in ta.store.transitions if item.actor_id == "svc_other_admin"] == []


def test_replay_requires_the_same_authorization_as_the_original_operation():
    ta = tenant_authority()
    ta.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-authz"
    )
    with pytest.raises(AuthorizationDenied) as exc:
        ta.engine.transition_tenant(
            "svc-token-identity", "ten_a", TenantState.SUSPENDED, idempotency_key="ik-authz"
        )
    assert exc.value.reason is DenyReason.INSUFFICIENT_AUTHORIZATION
    assert len(ta.store.transitions) == 1


def test_key_is_scoped_per_operation():
    ta = tenant_authority()
    created, _, _ = ta.engine.create_tenant(
        "svc-token-admin", tenant_id="ten_idem", idempotency_key="ik-cross"
    )
    with pytest.raises(TenantConflict) as exc:
        ta.engine.transition_tenant(
            "svc-token-admin", "ten_idem", TenantState.ACTIVE, idempotency_key="ik-cross"
        )
    assert exc.value.reason is DenyReason.IDEMPOTENCY_CONFLICT
    assert created.state is TenantState.PROVISIONING
    assert ta.engine.lookup("ten_idem").state is TenantState.PROVISIONING


def test_create_is_idempotent_and_does_not_duplicate_the_registry_row():
    ta = tenant_authority()
    first, _, _ = ta.engine.create_tenant(
        "svc-token-admin", tenant_id="ten_once", idempotency_key="ik-create"
    )
    replay, _, audit = ta.engine.create_tenant(
        "svc-token-admin", tenant_id="ten_once", idempotency_key="ik-create"
    )
    assert first.tenant_id == replay.tenant_id == "ten_once"
    assert list(ta.store.tenants).count("ten_once") == 1
    assert audit.reason == "idempotent_replay"


def test_create_without_tenant_id_replays_the_generated_identifier():
    ta = tenant_authority()
    size = len(ta.store.tenants)
    first, _, _ = ta.engine.create_tenant("svc-token-admin", idempotency_key="ik-gen")
    replay, _, _ = ta.engine.create_tenant("svc-token-admin", idempotency_key="ik-gen")

    assert first.tenant_id == replay.tenant_id
    assert len(ta.store.tenants) == size + 1
    assert list(ta.store.tenants).count(first.tenant_id) == 1


def test_without_a_key_the_same_request_is_a_new_operation():
    ta = tenant_authority()
    ta.engine.transition_tenant("svc-token-admin", "ten_a", TenantState.SUSPENDED)
    # A repeated *request* is not a replay: the second one is a real transition
    # attempt and is therefore rejected by the state machine.
    with pytest.raises(InvalidTransition) as exc:
        ta.engine.transition_tenant("svc-token-admin", "ten_a", TenantState.SUSPENDED)
    assert exc.value.reason is DenyReason.INVALID_TRANSITION
