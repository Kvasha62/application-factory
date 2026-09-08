"""Security failure cases — Issue #10 «Failure cases» (authn, authz, spoofing, прямой доступ)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from identity_service.errors import AccessDenied
from identity_service.models import DenyReason as DenyIdentity
from tests.conftest import composed, provision_tenant, tenant_authority
from tenant_authority.api import create_app
from tenant_authority.contracts import TenantState
from tenant_authority.errors import (
    AuthenticationDenied,
    AuthorizationDenied,
    DenyReason,
    OwnershipDenied,
    TenantNotFound,
)


@pytest.mark.parametrize("token", [None, "", "   ", "not-a-service-token", "svc-token-nope"])
def test_absent_or_unverifiable_service_identity_is_denied(token):
    ta = tenant_authority()
    with pytest.raises(AuthenticationDenied) as exc:
        ta.engine.transition_tenant(token, "ten_a", TenantState.SUSPENDED)
    assert exc.value.reason in {
        DenyReason.MISSING_SERVICE_IDENTITY,
        DenyReason.INVALID_SERVICE_IDENTITY,
    }
    assert exc.value.status_code == 401
    assert ta.store.audit[-1].decision.value == "DENY"


def test_revoked_service_identity_is_denied():
    """A token that resolves to a subject which no longer exists is not trusted."""
    ta = tenant_authority()
    with pytest.raises(AuthenticationDenied) as exc:
        ta.engine.get_tenant("svc-token-unknown", "ten_a")
    assert exc.value.reason is DenyReason.UNKNOWN_SERVICE


def test_authentication_does_not_imply_authorization():
    """A read-only service identity cannot mutate the lifecycle it is allowed to read."""
    ta = tenant_authority()
    readable, _, _ = ta.engine.get_tenant("svc-token-identity", "ten_a")
    assert readable.tenant_id == "ten_a"

    with pytest.raises(AuthorizationDenied) as exc:
        ta.engine.transition_tenant("svc-token-identity", "ten_a", TenantState.SUSPENDED)
    assert exc.value.reason is DenyReason.INSUFFICIENT_AUTHORIZATION
    assert ta.engine.lookup("ten_a").state is TenantState.ACTIVE

    with pytest.raises(AuthorizationDenied):
        ta.engine.create_tenant("svc-token-identity", tenant_id="ten_unauthorized")


def test_platform_admin_without_list_permission_cannot_enumerate_tenants():
    ta = tenant_authority()
    with pytest.raises(AuthorizationDenied) as exc:
        ta.engine.list_tenants("svc-token-identity")
    assert exc.value.reason is DenyReason.INSUFFICIENT_AUTHORIZATION


def test_unknown_tenant_operations_are_denied():
    ta = tenant_authority()
    with pytest.raises(TenantNotFound):
        ta.engine.get_tenant("svc-token-admin", "ten_missing")
    with pytest.raises(TenantNotFound):
        ta.engine.lifecycle_decision("ten_missing")
    with pytest.raises(TenantNotFound):
        ta.engine.transition_tenant("svc-token-admin", "ten_missing", TenantState.ACTIVE)


def test_foreign_tenant_is_never_returned_through_an_authorized_read():
    ta = tenant_authority()
    with pytest.raises(OwnershipDenied):
        ta.engine.get_tenant("svc-token-admin", "ten_foreign")
    with pytest.raises(OwnershipDenied):
        ta.engine.lifecycle_decision("ten_foreign", expected_platform_id=ta.current_platform_id)
    with pytest.raises(OwnershipDenied):
        ta.engine.transition_tenant("svc-token-admin", "ten_foreign", TenantState.SUSPENDED)
    assert ta.store.tenants["ten_foreign"].state is TenantState.ACTIVE


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (TenantState.PROVISIONING, "provisioning_not_served"),
        (TenantState.SUSPENDED, "tenant_suspended"),
        (TenantState.DELETION_REQUESTED, "tenant_deletion_requested"),
        (TenantState.DELETED, "tenant_deleted"),
    ],
)
def test_not_serving_states_report_a_stable_reason_code(state, reason):
    ta = tenant_authority()
    tenant_id = provision_tenant(ta, f"reason-{state.value}", state)
    decision = ta.engine.lifecycle_decision(tenant_id)
    assert decision.permitted is False
    assert decision.reason == reason


def test_tenant_spoofing_by_caller_cannot_change_the_effective_tenant():
    """T-007: a caller cannot move to another Tenant by rewriting `tenant_id`."""
    identity_engine, _ = composed()

    # Human A is bound to ten_a and ten_suspended; claiming a foreign Tenant fails.
    with pytest.raises(AccessDenied) as exc:
        identity_engine.read_record("token-human-a", "rec_b1", "ten_b")
    assert exc.value.reason is DenyIdentity.TENANT_MISMATCH

    # Claiming a Tenant that exists but is not bound to the identity fails too.
    with pytest.raises(AccessDenied) as exc:
        identity_engine.read_record("token-human-b", "rec_a1", "ten_a")
    assert exc.value.reason is DenyIdentity.TENANT_MISMATCH

    # A matching claim is accepted but never widens the effective tenant.
    record, _, _ = identity_engine.read_record("token-human-a", "rec_a1", "ten_a")
    assert record.tenant_id == "ten_a"
    assert identity_engine.resolve_tenant_context(
        identity_engine.verify_identity("token-human-a"), None
    ).tenant_id == "ten_a"


def test_missing_or_absent_authorization_at_the_identity_boundary():
    identity_engine, _ = composed()
    with pytest.raises(AccessDenied) as missing_identity:
        identity_engine.read_record(None, "rec_a1", None)
    assert missing_identity.value.reason is DenyIdentity.MISSING_IDENTITY

    with pytest.raises(AccessDenied) as no_permission:
        identity_engine.write_record("token-service", "rec_x", "nope", "ten_a")
    assert no_permission.value.reason is DenyIdentity.INSUFFICIENT_AUTHORIZATION


def test_denials_are_returned_as_http_errors_without_leaking_internals():
    ta = tenant_authority()
    client = TestClient(create_app(ta))

    unauthorized = client.get("/api/v1/tenants/ten_a")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"] == {
        "decision": "DENY",
        "reason": "missing_service_identity",
        "request_id": None,
    }

    forbidden = client.get("/api/v1/tenants/ten_a", headers={"Authorization": "Bearer svc-token-unknown"})
    assert forbidden.status_code == 401

    not_found = client.get(
        "/api/v1/tenants/ten_missing", headers={"Authorization": "Bearer svc-token-admin"}
    )
    assert not_found.status_code == 404
    assert not_found.json()["detail"]["reason"] == "tenant_not_found"

    invalid = client.post(
        "/api/v1/tenants/ten_deleted/transitions",
        json={"to_state": "active"},
        headers={"Authorization": "Bearer svc-token-admin"},
    )
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["reason"] == "invalid_transition"


def test_error_payloads_do_not_disclose_tenant_existence_across_platforms():
    ta = tenant_authority()
    client = TestClient(create_app(ta))
    headers = {"Authorization": "Bearer svc-token-admin"}

    foreign = client.get("/api/v1/tenants/ten_foreign", headers=headers)
    missing = client.get("/api/v1/tenants/ten_does_not_exist", headers=headers)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["detail"] == missing.json()["detail"]
    # The audit journal still distinguishes the two cases for operators.
    reasons = [event.reason for event in ta.store.audit if event.action == "tenant.read"]
    assert "foreign_tenant" in reasons and "tenant_not_found" in reasons
