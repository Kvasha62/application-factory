"""Platform ownership — инварианты T-003, T-007 (Issue #10, §3 Scope).

Проверяется правило: `tenant.platform_id == current_platform_id`. Tenant одной
Platform Instance нельзя получить из контекста другой — ни через lookup, ни
через авторизованную операцию, ни через сервисную идентичность чужой инстанции.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from identity_service.config import IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.errors import AccessDenied
from identity_service.models import (
    DenyReason,
    IdentityKind,
    TenantAssociation,
    VerifiedIdentity,
)
from identity_service.store import IdentityStore
from tenant_authority.api import create_app
from tenant_authority.contracts import TenantState
from tenant_authority.deployment import build_deployment
from tenant_authority.errors import (
    AuthorizationDenied,
    OwnershipDenied,
    TenantNotFound,
)
from tenant_authority.errors import (
    DenyReason as AuthorityDeny,
)
from tenant_authority.models import Decision as AuthorityDecision
from tests.conftest import DEMO_CONFIG, PLATFORM_ID

OTHER_PLATFORM_CONFIG = {"platform_id": "plt_other", "environment": "test"}


def test_a_tenant_is_registered_in_exactly_one_platform():
    authority = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    assert authority.engine.lookup("ten_a").platform_id == PLATFORM_ID
    with pytest.raises(OwnershipDenied):
        authority.engine.lookup("ten_a", expected_platform_id="plt_other")


def test_context_of_another_platform_instance_cannot_see_this_tenants():
    """A second Platform Instance context never resolves Tenant A of platform P."""
    from tenant_authority.models import TenantRecord

    other = build_deployment(OTHER_PLATFORM_CONFIG, seed_demo=False, with_http=True)
    other.store.tenants["ten_b_own"] = TenantRecord(
        "ten_b_own",
        "plt_other",
        TenantState.ACTIVE,
        "2026-09-08T00:00:00+00:00",
        "2026-09-08T00:00:00+00:00",
    )
    assert other.current_platform_id == "plt_other"
    assert other.engine.lookup(
        "ten_b_own", expected_platform_id="plt_other"
    ).tenant_id == ("ten_b_own")
    # Tenant A of platform P is unreachable from the context of platform B.
    with pytest.raises(TenantNotFound) as exc:
        other.engine.lookup("ten_a", expected_platform_id="plt_other")
    assert exc.value.reason is AuthorityDeny.TENANT_NOT_FOUND
    with pytest.raises(TenantNotFound):
        other.engine.lifecycle_decision("ten_a", expected_platform_id="plt_other")
    # ...and a Tenant that is present but owned by P is refused by the ownership check.
    other.store.tenants["ten_a"] = TenantRecord(
        "ten_a",
        PLATFORM_ID,
        TenantState.ACTIVE,
        "2026-09-08T00:00:00+00:00",
        "2026-09-08T00:00:00+00:00",
    )
    with pytest.raises(OwnershipDenied) as exc:
        other.engine.lookup("ten_a", expected_platform_id="plt_other")
    assert exc.value.reason is AuthorityDeny.FOREIGN_TENANT


def test_authorized_operation_on_foreign_tenant_is_denied_with_not_found_shape():
    authority = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    with pytest.raises(OwnershipDenied) as exc:
        authority.engine.get_tenant("svc-token-admin", "ten_foreign")
    assert exc.value.reason is AuthorityDeny.FOREIGN_TENANT
    assert exc.value.status_code == 404
    denial = authority.store.audit[-1]
    assert denial.reason == AuthorityDeny.FOREIGN_TENANT.value
    assert denial.decision.value == "DENY"
    assert denial.tenant_id == "ten_foreign"


def test_foreign_tenant_does_not_appear_in_platform_listings():
    authority = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    listed, _, _ = authority.engine.list_tenants("svc-token-admin")
    ids = {snapshot.tenant_id for snapshot in listed}
    assert "ten_a" in ids and "ten_b" in ids
    assert "ten_foreign" not in ids
    assert {snapshot.platform_id for snapshot in listed} == {PLATFORM_ID}


def test_service_identity_of_another_platform_cannot_operate_this_registry():
    authority = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    with pytest.raises(AuthorizationDenied) as exc:
        authority.engine.transition_tenant(
            "svc-token-foreign-admin", "ten_a", "suspended"
        )
    assert exc.value.reason is AuthorityDeny.PLATFORM_MISMATCH
    assert authority.engine.lookup("ten_a").state is TenantState.ACTIVE
    assert authority.store.audit[-1].reason == AuthorityDeny.PLATFORM_MISMATCH.value


def test_claimed_platform_id_is_only_a_cross_check():
    """A caller cannot retarget an operation by rewriting `platform_id` (T-007)."""
    authority = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    with pytest.raises(AuthorizationDenied) as exc:
        authority.engine.create_tenant(
            "svc-token-admin", claimed_platform_id="plt_other", tenant_id="ten_spoof"
        )
    assert exc.value.reason is AuthorityDeny.PLATFORM_MISMATCH
    assert "ten_spoof" not in authority.store.tenants

    matching, _, _ = authority.engine.create_tenant(
        "svc-token-admin", claimed_platform_id=PLATFORM_ID, tenant_id="ten_ok"
    )
    assert matching.platform_id == PLATFORM_ID


def test_effective_tenant_of_foreign_platform_never_resolves_in_identity_context():
    """Cross-check through IS-001: an identity bound to a foreign Tenant is denied."""
    authority = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    store = IdentityStore()
    store.seed_demo()
    store.identities["idn_foreign"] = VerifiedIdentity(
        "idn_foreign",
        IdentityKind.HUMAN,
        "foreign@example.test",
        home_tenant_id="ten_foreign",
    )
    store.tokens["token-foreign"] = "idn_foreign"
    store.associations[("idn_foreign", "ten_foreign")] = TenantAssociation(
        "idn_foreign", "ten_foreign", frozenset({"records.read", "records.write"})
    )
    engine = IdentityEngine(
        store=store,
        tenant_authority=authority.publish(
            credential="svc-token-identity", expected_platform_id=PLATFORM_ID
        ),
        config=IdentityConfig.from_mapping({"current_platform_id": PLATFORM_ID}),
    )

    # The foreign Tenant resolves in the identity association, but the Tenant
    # Authority ownership check denies it at the authorization boundary. Over the
    # published contract a Tenant of another Platform Instance is answered exactly
    # like a missing one, so the consumer only ever learns "tenant unknown".
    with pytest.raises(AccessDenied) as exc:
        engine.write_record("token-foreign", "rec_spoof", "payload", "ten_foreign")
    assert exc.value.reason is DenyReason.TENANT_UNKNOWN
    assert engine.store.audit[-1].reason == DenyReason.TENANT_UNKNOWN.value
    assert "rec_spoof" not in engine.store.records
    # The authority records the precise security-relevant reason for investigation.
    denial = authority.store.audit[-1]
    assert denial.reason == "foreign_tenant"
    assert denial.action == "tenant.read"
    assert denial.decision is AuthorityDecision.DENY


def test_http_surface_never_returns_a_foreign_tenant():
    authority = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    client = TestClient(create_app(authority))
    headers = {"Authorization": "Bearer svc-token-admin"}

    denied = client.get("/api/v1/tenants/ten_foreign", headers=headers)
    assert denied.status_code == 404
    # The response is indistinguishable from "no such Tenant" (no existence leak);
    # the precise reason is preserved in the audit journal asserted above.
    assert denied.json()["detail"]["reason"] == "tenant_not_found"
    assert authority.store.audit[-1].reason == "foreign_tenant"

    allowed = client.get("/api/v1/tenants/ten_a", headers=headers)
    assert allowed.status_code == 200
    assert allowed.json()["platform_id"] == PLATFORM_ID
