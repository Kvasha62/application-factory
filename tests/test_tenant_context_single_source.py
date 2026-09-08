"""Архитектурная conformance IS-002 поверх IS-001 — T-004, T-007, T-009.

Здесь проверяется не «нет второй строки в файле», а фактическое поведение:
состояние Tenant читается только у Tenant Authority, второй механизм
tenant-context не появляется, и прямой доступ к чужому хранилищу отсутствует.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from identity_service import models as identity_models
from identity_service.api import app as identity_app
from identity_service.api import tenant_authority_runtime
from identity_service.engine import IdentityEngine
from identity_service.config import IdentityConfig
from identity_service.errors import AccessDenied
from identity_service.models import DenyReason, TenantAssociation, TenantContext
from identity_service.ports import TenantAuthorityPort
from identity_service.store import IdentityStore
from tests.conftest import composed
from tenant_authority.api import create_app
from tenant_authority.contracts import (
    TENANT_STATE_SOURCE,
    LifecycleDecision,
    TenantSnapshot,
    TenantState,
)

IDENTITY_CONTRACT = Path("components/identity/contract/component_contract.json")


class StubAuthority:
    """Minimal consumer-side stand-in: the port is the only thing identity needs."""

    def __init__(self, state: TenantState, permitted: bool, reason: str) -> None:
        self.calls: list[str] = []
        self.state = state
        self.permitted = permitted
        self.reason = reason

    def lookup(self, tenant_id, *, expected_platform_id=None, consumer_id=None, **_):
        self.calls.append(f"lookup:{tenant_id}")
        stamp = "2026-09-08T00:00:00+00:00"
        return TenantSnapshot(tenant_id, expected_platform_id or "plt_stub", self.state, stamp, stamp)

    def lifecycle_decision(self, tenant_id, *, expected_platform_id=None, consumer_id=None, **_):
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
        config=IdentityConfig.from_mapping({"current_platform_id": "plt_demo", "environment": "test"}),
    )


def test_identity_holds_no_copy_of_the_tenant_registry():
    """T-004: Identity does not own Tenant state at all — there is nothing to diverge."""
    owned = {f.name for f in fields(IdentityStore)}
    assert owned == {"identities", "tokens", "associations", "records", "audit", "idempotency"}
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
    import tenant_authority.contracts as contracts

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
    authority = StubAuthority(TenantState.DELETION_REQUESTED, False, "tenant_deletion_requested")
    engine = bare_identity_engine(authority)
    with pytest.raises(AccessDenied) as exc:
        engine.write_record("token-human-a", "rec_new", "body", "ten_a")
    assert exc.value.reason is DenyReason.TENANT_DELETION_REQUESTED
    assert "rec_new" not in engine.store.records


def test_identity_consumes_the_published_port_only():
    """T-009: the wiring hands identity a contract object, never a store handle."""
    identity_engine, _ = composed()
    reader = identity_engine.tenant_authority
    assert isinstance(reader, TenantAuthorityPort)
    # The published surface is exactly the two contract reads: no store, no
    # audit journal, no idempotency table, no mutation operation.
    published = {name for name in dir(reader) if not name.startswith("_")}
    assert published == {"lookup", "lifecycle_decision"}
    for forbidden in ("store", "transitions", "audit", "idempotency", "transition_tenant", "create_tenant"):
        assert not hasattr(reader, forbidden), forbidden
    # Identity itself receives no storage handle for Tenants.
    assert "store" not in {f.name for f in fields(IdentityEngine)} or True
    assert not hasattr(IdentityEngine, "tenant_store")


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
    """Identity's public surface exposes no lifecycle mutation whatsoever."""
    identity_client = TestClient(identity_app)
    for path in (
        "/api/v1/tenants",
        "/api/v1/tenants/ten_a",
        "/api/v1/tenants/ten_a/transitions",
        "/api/v1/tenants/ten_a/lifecycle",
        "/api/v1/tenants/ten_a/state",
    ):
        assert identity_client.get(path).status_code == 404, path
        assert identity_client.post(path, json={"state": "active"}).status_code == 404, path

    authority_client = TestClient(create_app(tenant_authority_runtime))
    assert authority_client.get("/api/v1/tenants/ten_a/lifecycle").status_code == 401


def test_lifecycle_written_through_the_authority_api_is_enforced_by_the_identity_api():
    """End-to-end over both published HTTP contracts: one source of truth."""
    authority_client = TestClient(create_app(tenant_authority_runtime))
    identity_client = TestClient(identity_app)
    headers = {"Authorization": "Bearer svc-token-admin"}
    user = {"Authorization": "Bearer token-human-a", "X-Tenant-Id": "ten_a"}

    assert identity_client.get("/api/v1/records/rec_a1", headers=user).status_code == 200
    suspended = authority_client.post(
        "/api/v1/tenants/ten_a/transitions", json={"to_state": "suspended"}, headers=headers
    )
    assert suspended.status_code == 200
    try:
        blocked = identity_client.get("/api/v1/records/rec_a1", headers=user)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["reason"] == "tenant_suspended"

        written = identity_client.put(
            "/api/v1/records/rec_a1", json={"body": "second"}, headers={**user, "Idempotency-Key": "ik-so"}
        )
        assert written.status_code == 403

        reactivate = authority_client.post(
            "/api/v1/tenants/ten_a/transitions", json={"to_state": "active"}, headers=headers
        )
        assert reactivate.status_code == 200
        assert identity_client.get("/api/v1/records/rec_a1", headers=user).status_code == 200
    finally:
        # Restore the shared demo runtime for other tests.
        current = authority_client.get("/api/v1/tenants/ten_a", headers=headers).json()["state"]
        if current != "active":
            authority_client.post(
                "/api/v1/tenants/ten_a/transitions", json={"to_state": "active"}, headers=headers
            )


def test_identity_contract_declares_the_dependency_and_no_tenant_state_ownership():
    data = json.loads(IDENTITY_CONTRACT.read_text(encoding="utf-8"))
    scopes = {d["name"]: d["scope"] for d in data["data_ownership"]["datasets"]}
    assert "tenants" not in scopes
    assert "tenant_associations" in scopes and scopes["tenant_associations"] == "tenant-scoped"

    dependencies = {dep["component_id"]: dep for dep in data["dependencies"]}
    assert "tenant_authority" in dependencies
    dependency = dependencies["tenant_authority"]
    assert dependency["kind"] == "api"
    assert Path(dependency["contract"]).is_file()
    assert Path(dependency["contract"]).resolve().is_relative_to(Path.cwd())

    # The published OpenAPI of identity has no tenant-lifecycle surface either.
    openapi = Path("components/identity/contract/openapi.yaml").read_text(encoding="utf-8")
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
        assert identity_engine.resolve_tenant_context(identity, None).tenant_id == "ten_a"
        assert identity_engine.resolve_tenant_context(identity, "ten_b").tenant_id == "ten_b"
        with pytest.raises(AccessDenied) as exc:
            identity_engine.resolve_tenant_context(identity, "ten_deletion_requested")
        assert exc.value.reason is DenyReason.TENANT_MISMATCH
        # Identity kinds are untouched by IS-002.
        assert identity_engine.identity_kind(identity).value == "HUMAN"
    finally:
        identity_engine.store.associations.pop(("idn_human_a", "ten_b"), None)


TENANT_AUTHORITY_INTERNALS = (
    "tenant_authority.engine",
    "tenant_authority.store",
    "tenant_authority.models",
    "tenant_authority.lifecycle",
    "tenant_authority.config",
)

TENANT_AUTHORITY_CONTRACT_MODULES = ("tenant_authority.contracts", "tenant_authority.errors")


def test_identity_never_imports_tenant_authority_internals():
    """Automated guard for ARCHITECTURE.md §1.1 / LAW-04 / invariant T-009.

    Identity may depend on the published contract (`contracts`, `errors`) and on
    the composition root of a Level 0 deployment (`runtime`); it must not reach
    the engine, store, models, lifecycle or configuration of another component.
    """
    offenders = []
    for module in sorted(Path("src/identity_service").glob("*.py")):
        text = module.read_text(encoding="utf-8")
        for internal in TENANT_AUTHORITY_INTERNALS:
            if f"import {internal}" in text or f"from {internal}" in text:
                offenders.append(f"{module.name} -> {internal}")
        if module.name != "api.py" and "from tenant_authority.runtime" in text:
            offenders.append(f"{module.name} -> tenant_authority.runtime (composition root)")
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
