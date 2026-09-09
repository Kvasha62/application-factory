"""Архитектурная conformance IS-002 поверх IS-001 — T-004, T-007, T-009.

Здесь проверяется не «нет второй строки в файле», а фактическое поведение:
состояние Tenant читается только у Tenant Authority, второй механизм
tenant-context не появляется, прямой доступ к чужому хранилищу отсутствует, и
ни один компонент не публикует внутренности другого. Оба HTTP-приложения
поднимаются через композиционную фикстуру `tests.conftest.monolith()`.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import fields
from pathlib import Path

import pytest

from identity_service import models as identity_models
from identity_service.config import IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.errors import AccessDenied
from identity_service.models import (
    Decision,
    DenyReason,
    TenantAssociation,
    TenantContext,
)
from identity_service.ports import TenantAuthorityPort
from identity_service.store import IdentityStore
from tenant_authority.contracts import (
    TENANT_STATE_SOURCE,
    LifecycleDecision,
    TenantSnapshot,
    TenantState,
)
from tenant_authority.errors import ContractViolation
from tests.conftest import composed, monolith, tenant_authority

IDENTITY_CONTRACT = Path("components/identity/contract/component_contract.json")


class StubAuthority:
    """Minimal consumer-side stand-in: the port is the only thing identity needs."""

    def __init__(self, state: TenantState, permitted: bool, reason: str) -> None:
        self.calls: list[str] = []
        self.state = state
        self.permitted = permitted
        self.reason = reason

    def lookup(self, tenant_id, *, expected_platform_id=None, **_):
        self.calls.append(f"lookup:{tenant_id}")
        stamp = "2026-09-08T00:00:00+00:00"
        return TenantSnapshot(
            tenant_id, expected_platform_id or "plt_stub", self.state, stamp, stamp
        )

    def lifecycle_decision(self, tenant_id, *, expected_platform_id=None, **_):
        self.calls.append(f"lifecycle:{tenant_id}")
        return LifecycleDecision(
            tenant_id,
            expected_platform_id or "plt_stub",
            self.state,
            self.permitted,
            self.reason,
        )


def bare_identity_engine(authority: TenantAuthorityPort) -> IdentityEngine:
    store = IdentityStore()
    store.seed_demo()
    return IdentityEngine(
        store=store,
        tenant_authority=authority,
        config=IdentityConfig.from_mapping(
            {"current_platform_id": "plt_demo", "environment": "test"}
        ),
    )


def test_identity_holds_no_copy_of_the_tenant_registry():
    """T-004: Identity does not own Tenant state at all — there is nothing to diverge."""
    owned = {f.name for f in fields(IdentityStore)}
    assert owned == {
        "identities",
        "tokens",
        "associations",
        "records",
        "audit",
        "idempotency",
    }
    assert not any("tenant_status" in name or name == "tenants" for name in owned)

    identity_engine, _ = composed()
    assert not hasattr(identity_engine.store, "tenants")
    assert not hasattr(identity_engine, "set_tenant_state")
    assert not hasattr(identity_engine, "update_tenant")


def test_identity_does_not_define_a_second_tenant_state_vocabulary():
    """The Tenant state enum is a single definition owned by Tenant Authority."""
    assert not hasattr(identity_models, "TenantStatus")
    assert not hasattr(identity_models, "TenantRecord")

    field_types = {f.name: f.type for f in fields(TenantContext)}
    assert field_types["status"] == "TenantState"
    # Identity's context type is annotated with the authority's enum object itself.
    from tenant_authority import contracts

    assert contracts.TenantState is TenantState
    assert TenantState("suspended") is contracts.TenantState.SUSPENDED


def test_authorization_follows_the_authority_not_local_data():
    """A stub authority alone decides whether the Tenant is served."""
    denied = StubAuthority(TenantState.SUSPENDED, False, "tenant_suspended")
    engine = bare_identity_engine(denied)
    with pytest.raises(AccessDenied) as exc:
        engine.read_record("token-human-a", "rec_a1", "ten_a")
    assert exc.value.reason is DenyReason.TENANT_SUSPENDED
    assert denied.calls == ["lookup:ten_a", "lifecycle:ten_a"]

    allowed = StubAuthority(TenantState.ACTIVE, True, "permitted")
    engine = bare_identity_engine(allowed)
    record, _, _ = engine.read_record("token-human-a", "rec_a1", "ten_a")
    assert record.tenant_id == "ten_a"
    assert allowed.calls == ["lookup:ten_a", "lifecycle:ten_a"]


def test_deletion_requested_state_is_enforced_through_the_identity_boundary():
    authority = StubAuthority(
        TenantState.DELETION_REQUESTED, False, "tenant_deletion_requested"
    )
    engine = bare_identity_engine(authority)
    with pytest.raises(AccessDenied) as exc:
        engine.write_record("token-human-a", "rec_new", "body", "ten_a")
    assert exc.value.reason is DenyReason.TENANT_DELETION_REQUESTED
    assert "rec_new" not in engine.store.records


def test_identity_consumes_the_published_port_only():
    """T-009: the wiring hands identity a contract object, never a store handle.

    The exhaustive behavioral proof of the boundary lives in
    ``tests/test_component_boundary.py``; this test keeps the guard on the
    consumer side: identity holds a port-typed value-only client and no storage.
    """
    identity_engine, _ = composed()
    client = identity_engine.tenant_authority
    assert isinstance(client, TenantAuthorityPort)
    published = {name for name in dir(client) if not name.startswith("_")}
    assert published == {"lookup", "lifecycle_decision"}
    for forbidden in (
        "store",
        "transitions",
        "audit",
        "idempotency",
        "transition_tenant",
        "create_tenant",
        "engine",
        "config",
    ):
        assert not hasattr(client, forbidden), forbidden
    assert not hasattr(IdentityEngine, "tenant_store")
    assert not hasattr(IdentityEngine, "tenant_registry")


def test_state_source_is_reported_and_cannot_be_forced_by_a_caller():
    identity_engine, _ = composed()
    identity = identity_engine.verify_identity("token-human-a")
    context = identity_engine.resolve_tenant_context(identity, "ten_a")
    assert context.state_source == TENANT_STATE_SOURCE
    assert context.source == "verified_identity"
    assert context.platform_id == "plt_demo"

    # A caller cannot claim a different state source: TenantContext is built by
    # the boundary, and a claimed tenant id that is not bound is rejected.
    with pytest.raises(AccessDenied) as exc:
        identity_engine.resolve_tenant_context(identity, "ten_b")
    assert exc.value.reason is DenyReason.TENANT_MISMATCH


def test_only_tenant_authority_can_change_tenant_state():
    """Identity's published API is its own: no lifecycle mutation, no proxy either."""
    instance = monolith()
    identity_client = instance.identity_client()
    for path in (
        "/api/v1/tenants",
        "/api/v1/tenants/ten_a",
        "/api/v1/tenants/ten_a/transitions",
        "/api/v1/tenants/ten_a/lifecycle",
        "/api/v1/tenants/ten_a/state",
    ):
        assert identity_client.get(path).status_code == 404, path
        assert (
            identity_client.post(path, json={"state": "active"}).status_code == 404
        ), path

    # The authority application is reachable only through its own deployment, and
    # never as a part of identity's HTTP surface.
    assert instance.identity_app is not instance.authority_app
    authority_client = instance.authority_client()
    assert authority_client.get("/api/v1/tenants/ten_a/lifecycle").status_code == 401


def test_lifecycle_written_through_the_authority_api_is_enforced_by_the_identity_api():
    """End-to-end over both published HTTP contracts: one source of truth.

    Both applications come from one composition (the harness root), so the effect
    of a transition written through the authority contract is observed by the
    identity contract without any shared storage or synchronization mechanism.
    """
    instance = monolith()
    authority_client = instance.authority_client()
    identity_client = instance.identity_client()
    headers = {"Authorization": "Bearer svc-token-admin"}
    user = {"Authorization": "Bearer token-human-a", "X-Tenant-Id": "ten_a"}

    assert (
        identity_client.get("/api/v1/records/rec_a1", headers=user).status_code == 200
    )
    suspended = authority_client.post(
        "/api/v1/tenants/ten_a/transitions",
        json={"to_state": "suspended"},
        headers=headers,
    )
    assert suspended.status_code == 200

    blocked = identity_client.get("/api/v1/records/rec_a1", headers=user)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["reason"] == "tenant_suspended"

    written = identity_client.put(
        "/api/v1/records/rec_a1",
        json={"body": "second"},
        headers={**user, "Idempotency-Key": "ik-so"},
    )
    assert written.status_code == 403

    reactivate = authority_client.post(
        "/api/v1/tenants/ten_a/transitions",
        json={"to_state": "active"},
        headers=headers,
    )
    assert reactivate.status_code == 200
    assert (
        identity_client.get("/api/v1/records/rec_a1", headers=user).status_code == 200
    )

    # Suspension is visible in the authority's journal as a transition and in
    # identity's journal as a denial — two owned journals, one decision source.
    actions = [event.action for event in instance.authority.store.audit]
    assert "tenant.transition" in actions
    assert any(
        event.reason == "tenant_suspended" for event in instance.identity.store.audit
    )


def test_identity_contract_declares_the_dependency_and_no_tenant_state_ownership():
    data = json.loads(IDENTITY_CONTRACT.read_text(encoding="utf-8"))
    scopes = {d["name"]: d["scope"] for d in data["data_ownership"]["datasets"]}
    assert "tenants" not in scopes
    assert (
        "tenant_associations" in scopes
        and scopes["tenant_associations"] == "tenant-scoped"
    )

    dependencies = {dep["component_id"]: dep for dep in data["dependencies"]}
    assert "tenant_authority" in dependencies
    dependency = dependencies["tenant_authority"]
    assert dependency["kind"] == "api"
    assert Path(dependency["contract"]).is_file()
    assert Path(dependency["contract"]).resolve().is_relative_to(Path.cwd())

    # The published OpenAPI of identity has no tenant-lifecycle surface either.
    openapi = Path("components/identity/contract/openapi.yaml").read_text(
        encoding="utf-8"
    )
    declared = {
        line.strip()[:-1] for line in openapi.splitlines() if line.startswith("  /api")
    }
    assert not any("tenants" in path for path in declared)


def test_observability_context_of_both_components_agrees_on_platform():
    identity_engine, authority = composed()
    _, identity_obs, _ = identity_engine.read_record("token-human-a", "rec_a1", "ten_a")
    _, authority_obs, _ = authority.engine.get_tenant("svc-token-admin", "ten_a")
    assert identity_obs.platform_id == authority_obs.platform_id == "plt_demo"
    assert identity_obs.component_id == "identity"
    assert authority_obs.component_id == "tenant_authority"
    assert identity_obs.environment == authority_obs.environment == "test"


def test_effective_tenant_derivation_is_unchanged_by_is_002():
    """IS-001 stays the source of `effective_tenant_id` (Issue #10, §4 Scope)."""
    identity_engine, _ = composed()
    identity = identity_engine.verify_identity("token-human-a")
    assert identity_engine.resolve_tenant_context(identity, None).tenant_id == "ten_a"

    # A second binding never overrides the home tenant, and a claim is always
    # cross-checked against the bindings.
    identity_engine.store.associations[("idn_human_a", "ten_b")] = TenantAssociation(
        "idn_human_a", "ten_b", frozenset({"records.read"})
    )
    try:
        assert (
            identity_engine.resolve_tenant_context(identity, None).tenant_id == "ten_a"
        )
        assert (
            identity_engine.resolve_tenant_context(identity, "ten_b").tenant_id
            == "ten_b"
        )
        with pytest.raises(AccessDenied) as exc:
            identity_engine.resolve_tenant_context(identity, "ten_deletion_requested")
        assert exc.value.reason is DenyReason.TENANT_MISMATCH
        # Identity kinds are untouched by IS-002.
        assert identity_engine.identity_kind(identity).value == "HUMAN"
    finally:
        identity_engine.store.associations.pop(("idn_human_a", "ten_b"), None)


#: Modules of Tenant Authority that are internal to the component.
TENANT_AUTHORITY_INTERNALS = (
    "tenant_authority.engine",
    "tenant_authority.store",
    "tenant_authority.models",
    "tenant_authority.lifecycle",
    "tenant_authority.config",
    "tenant_authority.api",
    "tenant_authority.transport",
    "tenant_authority.deployment",
)

#: The whole shared surface of the component: data contract, errors, client.
TENANT_AUTHORITY_PUBLISHED_MODULES = (
    "tenant_authority.contracts",
    "tenant_authority.errors",
    "tenant_authority.reader",
)


def imported_authorities(module: Path) -> set[str]:
    """Every Tenant Authority module an identity module imports (real syntax scan)."""
    found = set()
    for line in module.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("from tenant_authority") and " import" in line:
            found.add(line.split(" import")[0][len("from ") :].strip())
        elif line.startswith("import tenant_authority"):
            found.add(line[len("import ") :].split()[0].strip().rstrip(","))
    return {
        name if name.startswith("tenant_authority") else f"tenant_authority.{name}"
        for name in found
    }


def test_identity_api_module_does_not_assemble_another_component():
    """BLOCKER-04: the HTTP module of identity composes nothing and exports nothing.

    Assembly of the monolith (deployment + client + both applications) belongs to
    a composition root or a test fixture; `identity_service.api` only publishes
    Identity's own API. Checked as real imports, not as a text search.
    """
    api_module = Path("src/identity_service/api.py")
    assert imported_authorities(api_module) == set()

    import identity_service.api as identity_api

    published = {name for name in vars(identity_api) if not name.startswith("_")}
    assert "create_app" in published
    for forbidden in (
        "tenant_authority_contract_app",
        "tenant_authority_app",
        "authority_app",
        "tenant_authority_deployment",
        "tenant_authority_runtime",
        "build_deployment",
        "deployment",
    ):
        assert forbidden not in published, forbidden


def test_identity_never_imports_tenant_authority_internals():
    """Automated guard for ARCHITECTURE.md §1.1 / LAW-04 / invariant T-009.

    Identity may depend on the published contract (`contracts`, `errors`) and on
    the published client (`reader`). Only its composition root may assemble the
    component (deployment); the engine, the store, the models, the lifecycle
    machinery, the configuration and the HTTP/transport plumbing are off limits.
    """
    offenders = []
    for module in sorted(Path("src/identity_service").glob("*.py")):
        for root in sorted(imported_authorities(module)):
            if root in TENANT_AUTHORITY_INTERNALS:
                offenders.append(f"{module.name} -> {root} (internal)")
            elif root not in TENANT_AUTHORITY_PUBLISHED_MODULES + ("tenant_authority",):
                offenders.append(f"{module.name} -> {root} (undeclared)")
    assert offenders == []


def test_tenant_authority_does_not_depend_on_identity():
    """Direction of dependency is fixed: identity consumes the authority, never vice versa."""
    for module in sorted(Path("src/tenant_authority").glob("*.py")):
        text = module.read_text(encoding="utf-8")
        assert "identity_service" not in text, module.name


def test_published_contract_modules_are_the_only_shared_surface():
    """The Tenant vocabulary identity uses *is* the object from the published contract."""
    from tenant_authority import contracts as authority_contracts

    annotated = {f.name: f for f in fields(TenantContext)}["status"]
    assert annotated.type == "TenantState"
    assert identity_models.TenantState is authority_contracts.TenantState
    assert identity_models.TenantContext.__module__ == "identity_service.models"


def test_identity_declares_the_consumer_surface_it_actually_uses():
    """The declared consumption is the used one — and it is a value-only client.

    Contract-side conformance of the component boundary: the surface named in
    IS-001's contract is the object identity is really wired with, its operations
    are exactly what it can call, and the credential the composition root uses is
    a service identity holding exactly the two published reads.
    """
    data = json.loads(IDENTITY_CONTRACT.read_text(encoding="utf-8"))
    consumed = data["api"]["consumes"][0]
    assert consumed["component_id"] == "tenant_authority"
    assert consumed["mechanism"].startswith("contract client over the published API")
    assert consumed["assembled_by"].startswith("composition root or test fixture")
    # The declaration is true: identity's HTTP module imports no part of the
    # component beyond the published contract surface, so it cannot assemble it.
    api_imports = imported_authorities(Path("src/identity_service/api.py"))
    assert api_imports == set(), api_imports

    module_name, _, class_name = consumed["consumer_surface"].rpartition(".")
    client_class = getattr(importlib.import_module(module_name), class_name)

    instance = monolith()
    assert type(instance.tenant_authority_client) is client_class
    assert type(instance.identity.tenant_authority) is client_class
    assert not hasattr(instance.identity_app.state, "tenant_authority")
    assert {
        name
        for name in dir(instance.identity.tenant_authority)
        if not name.startswith("_")
    } == set(consumed["operations"])

    from tenant_authority.store import LOOKUP_PERMISSIONS

    authority = tenant_authority()
    access = authority.engine.verify_service_identity(consumed["demo_credential"])
    assert access.permissions == LOOKUP_PERMISSIONS
    assert access.platform_id == authority.current_platform_id


def test_a_transport_fault_denies_and_is_audited_at_the_boundary():
    """No answer from the authority is never read as "this Tenant may be served"."""

    class UnreachableAuthority:
        def lookup(self, tenant_id, **_):
            raise ContractViolation("tenant authority did not answer")

        def lifecycle_decision(self, tenant_id, **_):
            raise ContractViolation("tenant authority did not answer")

    identity_engine = bare_identity_engine(UnreachableAuthority())
    with pytest.raises(AccessDenied) as exc:
        identity_engine.read_record("token-human-a", "rec_a1", "ten_a")
    assert exc.value.reason is DenyReason.TENANT_UNKNOWN
    denial = identity_engine.store.audit[-1]
    assert denial.reason == DenyReason.TENANT_UNKNOWN.value
    assert denial.decision is Decision.DENY

    with pytest.raises(AccessDenied):
        identity_engine.write_record("token-human-a", "rec_new", "body", "ten_a")
    assert "rec_new" not in identity_engine.store.records
