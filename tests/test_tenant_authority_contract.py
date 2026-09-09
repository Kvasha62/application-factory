"""Conformance машинночитаемого Component Contract Tenant Authority (Issue #10, §8)."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tenant_authority.api import create_app
from tenant_authority.contracts import TenantState
from tenant_authority.deployment import build_deployment
from tenant_authority.errors import AuthorizationDenied, DenyReason
from tenant_authority.lifecycle import ALLOWED_TRANSITIONS, OPERATION_POLICY
from tenant_authority.models import AuditEvent, LifecycleTransition
from tests.conftest import DEMO_CONFIG, tenant_authority

REQUIRED = {
    "api",
    "events",
    "data_export_cdc",
    "ui",
    "configuration_schema",
    "data_ownership",
    "authn",
    "authz",
    "compatibility_policy",
    "dependencies",
}

CONTRACT = Path("components/tenant_authority/contract/component_contract.json")
OPENAPI = Path("components/tenant_authority/contract/openapi.yaml")


def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def published_operations(text: str) -> set[tuple[str, str]]:
    """Read the `paths:` section of the published OpenAPI document."""
    operations: set[tuple[str, str]] = set()
    in_paths = False
    current_path: str | None = None
    methods = {"get", "post", "put", "patch", "delete"}
    for line in text.splitlines():
        if line.startswith("paths:"):
            in_paths = True
            continue
        if in_paths and line and not line.startswith(" "):
            break
        if not in_paths:
            continue
        stripped = line.strip()
        if line.startswith("  /") and stripped.endswith(":"):
            current_path = stripped[:-1]
        elif (
            current_path is not None
            and line.startswith("    ")
            and not line.startswith("     ")
            and stripped.endswith(":")
            and stripped[:-1] in methods
        ):
            operations.add((current_path, stripped[:-1]))
    return operations


def test_component_contract_declares_the_required_minimum():
    data = contract()
    missing = REQUIRED - set(data)
    assert not missing
    assert data["component_id"] == "tenant_authority"
    assert data["class"] == "platform_service"
    assert data["maturity_level"] == "level_0_modular_monolith"


def test_tenant_registry_data_ownership_is_platform_scoped():
    """DoD: Data ownership Tenant Registry объявлен как `platform-scoped`."""
    data = contract()
    ownership = data["data_ownership"]
    assert ownership["owner"] == "tenant_authority"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    assert scopes["tenant_registry"] == "platform-scoped"
    assert set(scopes.values()) == {"platform-scoped"}
    # Exactly one scope per dataset (LAW-16a).
    assert len(ownership["datasets"]) == len(scopes)
    for name, scope in scopes.items():
        assert scope in {"tenant-scoped", "platform-scoped", "system-scoped"}, name


def test_contract_does_not_announce_unimplemented_events_or_cdc():
    data = contract()
    assert data["events"]["published"] == []
    assert data["events"]["consumed"] == []
    assert data["events"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"
    assert data["ui"]["kind"] == "none"


def test_published_api_matches_the_implementation_in_both_directions():
    data = contract()
    assert Path(data["api"]["openapi"]).read_text(
        encoding="utf-8"
    ) == OPENAPI.read_text(encoding="utf-8")
    assert data["api"]["base_path"] == "/api/v1"
    assert data["api"]["supported_majors"] == ["v1"]

    declared = published_operations(OPENAPI.read_text(encoding="utf-8"))
    # Framework-provided documentation routes are not part of any contract.
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (path, method.lower())
        for route in create_app(tenant_authority()).routes
        for path in [getattr(route, "path", "")]
        for method in getattr(route, "methods", set())
        if path and path not in framework_routes and method not in {"HEAD", "OPTIONS"}
    }
    assert declared == implemented
    for operation in data["api"]["operations"]:
        assert (operation["path"], operation["method"].lower()) in declared


def declared_header_parameters(text: str, path: str, method: str) -> set[str]:
    """Header parameters the published document declares for one operation.

    The document is the contract, the schema is the implementation: comparing the
    two names per operation catches the drift this task was about — a header the
    route accepts but the contract does not promise, or one the contract promises
    and the route quietly drops.
    """
    wanted_path = f"  {path}:"
    wanted_method = f"    {method}:"
    names: set[str] = set()
    in_paths = False
    on_path = False
    on_method = False
    in_parameters = False
    expect_name = False
    for line in text.splitlines():
        if line.startswith("paths:"):
            in_paths = True
            continue
        if not in_paths:
            continue
        if line and not line.startswith(" "):
            break
        if line.startswith("  /"):
            on_path = line.rstrip() == wanted_path
            on_method = in_parameters = expect_name = False
        elif on_path and line.startswith("    ") and not line.startswith("     "):
            on_method = line.rstrip() == wanted_method
            in_parameters = expect_name = False
        elif on_method and line.strip() == "parameters:":
            in_parameters = True
        elif (
            in_parameters
            and line.startswith("        ")
            and not line.startswith("         ")
        ):
            stripped = line.strip()
            expect_name = stripped == "- in: header"
            if stripped.startswith("- in:") and not expect_name:
                continue
        elif in_parameters and expect_name and line.strip().startswith("name:"):
            names.add(line.strip().split(":", 1)[1].strip())
            expect_name = False
    return names


def test_declared_headers_of_the_lifecycle_read_match_the_implementation():
    """The contract promises exactly the headers the route accepts — no more, no less.

    `GET /api/v1/tenants/{tenant_id}/lifecycle` is the operation that lost
    `X-Correlation-Id`: the document declared only `X-Platform-Id` and
    `X-Request-Id`, and the route ignored correlation entirely. Both sides of the
    comparison are checked, so the contract cannot run ahead of the code either.
    """
    path, method = "/api/v1/tenants/{tenant_id}/lifecycle", "get"
    declared = declared_header_parameters(
        OPENAPI.read_text(encoding="utf-8"), path, method
    )
    schema = create_app(tenant_authority()).openapi()
    implemented = {
        parameter["name"]
        for parameter in schema["paths"][path][method].get("parameters", [])
        if parameter.get("in") == "header"
    } - {
        "authorization"
    }  # declared as security, not as a parameter

    assert "X-Correlation-Id" in declared
    assert "X-Request-Id" in declared
    assert declared == implemented, declared ^ implemented


def test_declared_permissions_exist_and_are_enforced():
    data = contract()
    declared = set(data["authz"]["permissions"])
    assert data["authz"]["boundary"] == "data_owner"
    assert data["authz"]["authentication_does_not_imply_authorization"] is True
    assert data["authz"]["lifecycle_lookup_is_not_authorization"] is True
    assert data["authz"]["platform_ownership_check"] == (
        "tenant.platform_id == current_platform_id"
    )

    ta = tenant_authority()
    granted = set().union(*(s.permissions for s in ta.store.services.values()))
    assert granted <= declared

    # A declared permission that no service holds still cannot be used to write.
    with pytest.raises(AuthorizationDenied) as exc:
        ta.engine.create_tenant("svc-token-identity", tenant_id="ten_x")
    assert exc.value.reason is DenyReason.INSUFFICIENT_AUTHORIZATION


def test_contract_declares_the_lifecycle_of_the_component_itself():
    data = contract()
    lifecycle = data["data_scope_authority"]["role"]
    assert lifecycle == "Tenant Authority"
    invariants = " ".join(data["data_scope_authority"]["invariants"])
    for expected in ("T-001", "T-002", "T-003", "T-004", "T-005", "T-006", "T-008"):
        assert expected in invariants


def test_declared_consumer_surface_is_the_real_published_surface():
    """Conformance of the boundary declaration: what the contract names exists.

    The check is behavioral — the declared module, type, operations, return values
    and transport are resolved and exercised at runtime.
    """
    import importlib
    import importlib.util
    import inspect

    surface = contract()["api"]["consumer_surface"]
    module = importlib.import_module(surface["module"])
    client_class = getattr(module, surface["published_as"])

    # The publisher named by the contract really publishes the client.
    holder_path, _, publisher_attr = surface["published_by"].rpartition(".")
    holder_module_name, _, holder_name = holder_path.rpartition(".")
    assert publisher_attr == "publish"
    holder_module = importlib.import_module(holder_module_name)
    publisher = getattr(holder_module, holder_name)
    assert callable(getattr(publisher, publisher_attr))

    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    client = deployment.publish(credential="svc-token-identity")
    assert type(client) is client_class

    declared_operations = set(surface["operations"])
    assert {n for n in dir(client) if not n.startswith("_")} == declared_operations
    # The declared operations are implemented by the published module itself, not
    # delegated to an internal object that the consumer could then reach.
    for name in declared_operations:
        owner = inspect.getmodule(getattr(client_class, name))
        assert owner.__name__ == surface["module"], name

    returns = {entry.rsplit(".", 1)[1] for entry in surface["returns"]}
    assert returns == {
        type(client.lookup("ten_a")).__name__,
        type(client.lifecycle_decision("ten_a")).__name__,
    }

    # The declared transport is the real one: opening a channel, calling the
    # published contract through it and revoking it are the declared operations,
    # and a call returns data only.
    handles = surface["transport_handles"]
    transport_module = importlib.import_module(handles["open"].rpartition(".")[0])
    open_channel = getattr(transport_module, handles["open"].rpartition(".")[2])
    call_contract = getattr(transport_module, handles["call"].rpartition(".")[2])
    close_channel = getattr(transport_module, handles["close"].rpartition(".")[2])
    channel = open_channel(deployment.contract_app())
    assert isinstance(channel, str) and channel
    try:
        status, payload = call_contract(
            channel,
            "GET",
            "/api/v1/tenants/ten_a",
            [("authorization", "Bearer svc-token-identity")],
            None,
        )
    finally:
        close_channel(channel)
    assert status == 200
    assert payload["tenant_id"] == "ten_a"
    assert payload["state_source"] == "tenant_authority"
    assert set(payload) == {
        "tenant_id",
        "platform_id",
        "state",
        "created_at",
        "updated_at",
        "state_source",
    }
    # A revoked channel is refused, not answered: the declared fail-closed
    # guarantee holds for the transport the published client is built on.
    with pytest.raises(transport_module.ContractViolation, match="closed"):
        call_contract(channel, "GET", "/api/v1/tenants/ten_a", [], None)

    # The declared consumer state is what a published client really carries: one
    # value per declared entry, all of them strings or None — no transport object,
    # hence no closure that could lead back to the application.
    declared_state = surface["consumer_state"]
    own_slots = [name for name in client_class.__slots__ if not name.startswith("__")]
    assert len(declared_state) == len(own_slots)
    published = deployment.publish(credential="svc-token-identity")
    assert all(
        isinstance(getattr(published, name), (str, type(None))) for name in own_slots
    )
    assert all("(str" in entry for entry in declared_state), declared_state

    # The published module re-exports none of the declared internal modules, and
    # every declared internal module exists (nothing is declared that was removed).
    for internal in surface["internal_modules"]:
        assert importlib.util.find_spec(internal) is not None, internal
        assert not hasattr(module, internal.rsplit(".", 1)[1]), internal
    assert importlib.util.find_spec("tenant_authority.runtime") is None
    assert not hasattr(module, "TenantAuthorityReader")

    # What the contract keeps with the composition root must really stay there.
    root_only = " ".join(surface["composition_root_only"])
    assert "TenantAuthorityDeployment" in root_only
    assert "ASGI" in root_only
    for declared in surface["internal_modules"]:
        assert not hasattr(client_class, declared.rsplit(".", 1)[1]), declared


def test_published_state_machine_equals_the_runtime_state_machine():
    client = TestClient(create_app(tenant_authority()))
    published = client.get("/api/v1/lifecycle").json()

    assert [state.value for state in TenantState] == published["lifecycle"]
    assert published["allowed_transitions"] == {
        state.value: sorted(target.value for target in targets)
        for state, targets in ALLOWED_TRANSITIONS.items()
    }
    assert published["terminal_states"] == [TenantState.DELETED.value]


def test_denial_reasons_are_a_closed_declared_set():
    data = contract()
    declared = set(data["api"]["denial_semantics"]["reasons"])
    from tenant_authority.errors import DenyReason

    assert declared == {reason.value for reason in DenyReason}
    assert data["api"]["denial_semantics"]["foreign_tenant_response"].startswith(
        "answered as tenant_not_found"
    )


def test_openapi_declares_the_state_and_reason_vocabulary():
    text = OPENAPI.read_text(encoding="utf-8")

    state_block = text.split("    TenantState:\n", 1)[1].split("    Tenant:", 1)[0]
    declared_states = {
        line.strip()[2:]
        for line in state_block.splitlines()
        if line.strip().startswith("- ")
    }
    assert declared_states == {state.value for state in TenantState}

    reason_block = text.split("    LifecycleDecision:", 1)[1].split("      enum:\n", 1)[
        1
    ]
    declared_reasons = {
        line.strip()[2:]
        for line in reason_block.splitlines()
        if line.strip().startswith("- ")
    }
    produced_reasons = {policy[1] for policy in OPERATION_POLICY.values()}
    assert produced_reasons <= declared_reasons
    assert "unsupported_state" in declared_reasons
    # Every state is covered by the operational policy, so a caller can never
    # receive an undeclared reason.
    assert set(OPERATION_POLICY) == set(TenantState)


def test_lifecycle_reason_codes_are_the_declared_vocabulary():
    reasons = {value[1] for value in OPERATION_POLICY.values()}
    assert reasons == {
        "permitted",
        "provisioning_not_served",
        "tenant_suspended",
        "tenant_deletion_requested",
        "tenant_deleted",
    }
    assert set(OPERATION_POLICY) == set(TenantState)


def test_audit_and_transition_record_shapes_are_declared_and_real():
    data = contract()
    transition_fields = set(data["audit"]["transition_fields"])
    assert transition_fields <= {f.name for f in fields(LifecycleTransition)}
    assert transition_fields == {
        "tenant_id",
        "previous_state",
        "new_state",
        "actor_id",
        "timestamp",
        "request_id",
        "correlation_id",
    }
    assert data["audit"]["denials_audited"] is True
    audit_fields = {f.name for f in fields(AuditEvent)}
    assert {
        "request_id",
        "correlation_id",
        "actor_id",
        "tenant_id",
        "decision",
    } <= audit_fields


def test_component_has_no_declared_dependencies():
    """Tenant Authority is a foundation component: it must not reach into Identity."""
    data = contract()
    assert data["dependencies"] == []


def test_configuration_schema_rejects_unknown_keys_at_build_time():
    from tenant_authority.config import TenantAuthorityConfig
    from tenant_authority.errors import ConfigurationError

    data = contract()
    schema = data["configuration_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "platform_id",
        "environment",
        "service_token_prefix",
    }
    assert schema["required"] == ["platform_id"]

    config = TenantAuthorityConfig.from_mapping(
        {"platform_id": "plt_x", "environment": "test"}
    )
    assert config.platform_id == "plt_x"
    with pytest.raises(ConfigurationError):
        TenantAuthorityConfig.from_mapping({"platform_id": "plt_x", "unknown": 1})
    with pytest.raises(ConfigurationError):
        TenantAuthorityConfig.from_mapping({"environment": "test"})
