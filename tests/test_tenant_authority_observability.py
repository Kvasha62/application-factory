"""Observability контекст — инвариант T-011 (Issue #10, §7 Scope).

Используется стандартный контекст архитектуры (§26 ARCHITECTURE.md, ADR-0010 AMD-10).
`saga_id` намеренно отсутствует: компонент не выполняет межкомпонентные саги,
и вымышленный saga_id был бы ложной наблюдаемостью.
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from tests.conftest import composed, tenant_authority
from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.contracts import TenantState

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
    joinable without inventing a second correlation mechanism.
    """
    from identity_service.api import app as identity_app
    from identity_service.api import engine as identity_engine
    # A test may inspect the composition root's internals; a consumer cannot.
    from identity_service.api import _tenant_authority_deployment as authority_deployment
    from identity_service.models import TenantAssociation

    client = TestClient(identity_app)
    forged = TenantAssociation("idn_human_a", "ten_ghost", frozenset({"records.read"}))
    identity_engine.store.associations[("idn_human_a", "ten_ghost")] = forged
    try:
        response = client.get(
            "/api/v1/records/rec_a1",
            headers={
                "Authorization": "Bearer token-human-a",
                "X-Tenant-Id": "ten_ghost",
                "X-Request-Id": "req-cross-1",
                "X-Correlation-Id": "cor-cross-1",
            },
        )
    finally:
        identity_engine.store.associations.pop(("idn_human_a", "ten_ghost"), None)

    assert response.status_code == 403
    identity_event = identity_engine.store.audit[-1]
    assert identity_event.reason == "tenant_unknown"
    assert (identity_event.request_id, identity_event.correlation_id) == (
        "req-cross-1",
        "cor-cross-1",
    )

    authority_event = authority_deployment.store.audit[-1]
    assert authority_event.reason == "tenant_not_found"
    assert (authority_event.request_id, authority_event.correlation_id) == (
        "req-cross-1",
        "cor-cross-1",
    )


def test_identity_context_exposes_platform_and_tenant_for_allowed_operations():
    identity_engine, _ = composed()
    _, obs, audit = identity_engine.read_record("token-human-a", "rec_a1", None)
    assert obs.platform_id == "plt_demo"
    assert obs.tenant_id == "ten_a"
    assert obs.actor_id == "idn_human_a"
    assert obs.component_id == "identity"
    assert audit.correlation_id == obs.correlation_id
    assert obs.timestamp
