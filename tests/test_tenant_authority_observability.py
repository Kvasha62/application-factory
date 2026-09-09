"""Observability контекст — инвариант T-011 (Issue #10, §7 Scope).

Используется стандартный контекст архитектуры (§26 ARCHITECTURE.md, ADR-0010 AMD-10).
`saga_id` намеренно отсутствует: компонент не выполняет межкомпонентные саги,
и вымышленный saga_id был бы ложной наблюдаемостью.
"""

from __future__ import annotations

from datetime import datetime

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.contracts import TenantState
from tests.conftest import composed, monolith, tenant_authority

STANDARD_CONTEXT = (
    "timestamp",
    "environment",
    "platform_id",
    "component_id",
    "component_version",
    "tenant_id",
    "request_id",
    "trace_id",
    "correlation_id",
    "service_id",
)


def test_observability_context_carries_the_architectural_standard():
    ta = tenant_authority()
    _, obs, event = ta.engine.transition_tenant(
        "svc-token-admin", "ten_provisioning", TenantState.ACTIVE
    )

    for field in STANDARD_CONTEXT:
        assert hasattr(obs, field), field
    assert obs.component_id == COMPONENT_ID
    assert obs.component_version == COMPONENT_VERSION
    assert obs.platform_id == ta.current_platform_id
    assert obs.environment == "test"
    assert obs.tenant_id == "ten_provisioning"
    assert obs.service_id == "svc_tenant_admin"
    assert obs.request_id and obs.trace_id and obs.correlation_id
    # The audit record is traceable by the same identifiers.
    assert event.request_id == obs.request_id
    assert event.correlation_id == obs.correlation_id


def test_caller_supplied_identifiers_are_respected_and_generated_when_absent():
    ta = tenant_authority()
    _, provided, _ = ta.engine.get_tenant(
        "svc-token-admin",
        "ten_a",
        request_id="req-external",
        correlation_id="cor-external",
    )
    assert (provided.request_id, provided.correlation_id, provided.trace_id) == (
        "req-external",
        "cor-external",
        "req-external",
    )

    _, generated, _ = ta.engine.get_tenant("svc-token-admin", "ten_a")
    assert generated.request_id.startswith("req_")
    # Without an explicit correlation id the request id is used, so a single
    # operation is never split across two uncorrelated traces.
    assert generated.correlation_id == generated.request_id


def test_timestamps_are_machine_readable_utc():
    ta = tenant_authority()
    _, obs, _ = ta.engine.list_tenants("svc-token-admin")
    parsed = datetime.fromisoformat(obs.timestamp)
    assert parsed.tzinfo is not None


def test_platform_context_is_the_deployment_one_not_a_claimed_value():
    ta = tenant_authority()
    try:
        ta.engine.create_tenant("svc-token-admin", claimed_platform_id="plt_attacker")
    except Exception:
        pass
    denial = ta.store.audit[-1]
    assert denial.reason == "platform_mismatch"
    # The recorded context keeps the authoritative platform of this instance; the
    # claimed value exists only as evidence inside the audited details.
    assert denial.platform_id == ta.current_platform_id
    assert denial.details == {"claimed_platform_id": "plt_attacker"}


def test_denied_operation_still_produces_an_observability_context():
    ta = tenant_authority()
    before = len(ta.store.audit)
    try:
        ta.engine.get_tenant("svc-token-invalid", "ten_a", request_id="req-unauth")
    except Exception:
        pass
    event = ta.store.audit[before]
    assert event.request_id == "req-unauth"
    assert event.correlation_id == "req-unauth"
    # An unverified caller is never attributed as an actor; the attempted target
    # is recorded so the denial remains investigable.
    assert event.actor_id is None
    assert event.tenant_id == "ten_a"


def test_one_request_is_traceable_in_both_component_journals():
    """Identity and Tenant Authority record the same request/correlation ids.

    One tenant-scoped HTTP request crosses two components; both journals must be
    joinable without inventing a second correlation mechanism. The two published
    applications are taken from one composition (the harness root), not from
    another component's module.
    """
    from identity_service.models import TenantAssociation

    instance = monolith()
    client = instance.identity_client()
    identity_engine = instance.identity
    forged = TenantAssociation("idn_human_a", "ten_ghost", frozenset({"records.read"}))
    identity_engine.store.associations[("idn_human_a", "ten_ghost")] = forged

    response = client.get(
        "/api/v1/records/rec_a1",
        headers={
            "Authorization": "Bearer token-human-a",
            "X-Tenant-Id": "ten_ghost",
            "X-Request-Id": "req-cross-1",
            "X-Correlation-Id": "cor-cross-1",
        },
    )

    assert response.status_code == 403
    identity_event = identity_engine.store.audit[-1]
    assert identity_event.reason == "tenant_unknown"
    assert (identity_event.request_id, identity_event.correlation_id) == (
        "req-cross-1",
        "cor-cross-1",
    )

    authority_event = instance.authority.store.audit[-1]
    assert authority_event.reason == "tenant_not_found"
    assert (authority_event.request_id, authority_event.correlation_id) == (
        "req-cross-1",
        "cor-cross-1",
    )
    # The consumer of the contract is identifiable in the authority's journal.
    assert authority_event.actor_id == "svc_identity"


def test_lifecycle_read_preserves_the_caller_request_and_correlation_ids():
    """BLOCKER-07: `GET /api/v1/tenants/{tenant_id}/lifecycle` keeps the ids it got.

    The endpoint took `X-Request-Id` and silently had no `X-Correlation-Id`, so a
    lifecycle answer could not be tied back to the call that asked for it — while
    every other read could. Nothing is generated here that the caller did not ask
    for: the same identifiers reach the observability context and the audit journal
    through the one gate that every read passes.
    """
    instance = monolith()
    authority = instance.authority

    # 1. Over the published HTTP contract.
    response = instance.authority_client().get(
        "/api/v1/tenants/ten_a/lifecycle",
        headers={
            "Authorization": "Bearer svc-token-identity",
            "X-Request-Id": "req-lc-http",
            "X-Correlation-Id": "cor-lc-http",
        },
    )
    assert response.status_code == 200
    assert response.json()["tenant_id"] == "ten_a"
    audited = authority.store.audit[-1]
    assert (audited.action, audited.decision) == ("tenant.lifecycle", "ALLOW")
    assert (audited.request_id, audited.correlation_id) == ("req-lc-http", "cor-lc-http")
    assert audited.tenant_id == "ten_a"
    assert audited.actor_id == "svc_identity"

    # 2. Over the Level 0 published client: the header pair it already sent now
    #    survives instead of being dropped by the route.
    decision = instance.tenant_authority_client.lifecycle_decision(
        "ten_b", request_id="req-lc-client", correlation_id="cor-lc-client"
    )
    assert decision.permitted is True
    via_client = authority.store.audit[-1]
    assert (via_client.request_id, via_client.correlation_id) == (
        "req-lc-client",
        "cor-lc-client",
    )

    # 3. The observability context of the same read is bound to those identifiers —
    #    audit and context carry one pair, not two mechanisms.
    _, obs, event = authority.engine.lifecycle_status(
        "svc-token-identity",
        "ten_a",
        request_id="req-lc-obs",
        correlation_id="cor-lc-obs",
    )
    assert obs.request_id == "req-lc-obs"
    assert obs.correlation_id == "cor-lc-obs"
    assert obs.tenant_id == "ten_a"
    assert obs.platform_id == authority.current_platform_id
    assert obs.service_id == "svc_identity"
    assert obs.trace_id
    assert event.correlation_id == obs.correlation_id
    assert event.request_id == obs.request_id

    # 4. A denial on the same endpoint keeps them too: one gate authenticates,
    #    authorizes, answers and audits — the trace survives the refusal.
    denied = instance.authority_client().get(
        "/api/v1/tenants/ten_ghost/lifecycle",
        headers={
            "Authorization": "Bearer svc-token-identity",
            "X-Request-Id": "req-lc-deny",
            "X-Correlation-Id": "cor-lc-deny",
        },
    )
    assert denied.status_code == 404
    assert denied.json()["detail"]["request_id"] == "req-lc-deny"
    denial = authority.store.audit[-1]
    assert (denial.decision, denial.reason) == ("DENY", "tenant_not_found")
    assert (denial.request_id, denial.correlation_id) == ("req-lc-deny", "cor-lc-deny")

    # 5. And nothing is invented when the caller sends no identifiers: they are
    #    generated once, by the existing mechanism, and stay consistent.
    before = len(authority.store.audit)
    instance.tenant_authority_client.lifecycle_decision("ten_a")
    generated = authority.store.audit[before]
    assert generated.request_id
    assert generated.correlation_id == generated.request_id


def test_identity_context_exposes_platform_and_tenant_for_allowed_operations():
    identity_engine, _ = composed()
    _, obs, audit = identity_engine.read_record("token-human-a", "rec_a1", None)
    assert obs.platform_id == "plt_demo"
    assert obs.tenant_id == "ten_a"
    assert obs.actor_id == "idn_human_a"
    assert obs.component_id == "identity"
    assert audit.correlation_id == obs.correlation_id
    assert obs.timestamp
