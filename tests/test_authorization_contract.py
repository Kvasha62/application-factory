"""Conformance of the machine-readable Component Contract of IS-003 (Issue #12).

The contract is only worth something if it cannot drift from the implementation.
Every check here compares the published document with live objects: declared
operations against the routes the application actually serves, declared reason
codes against the enum the engine returns, declared configuration against the
loader, declared permissions against the ones enforced, and declared
dependencies against the ports the component is wired with.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from authorization_service import COMPONENT_ID, COMPONENT_VERSION
from authorization_service.api import create_app
from authorization_service.config import AuthorizationConfig
from authorization_service.contracts import Decision, Reason
from authorization_service.errors import (
    CallerDenyReason,
    CallerNotAuthorized,
    ConfigurationError,
)
from authorization_service.models import AuditEvent
from authorization_service.policy import DECISION_CHAIN
from authorization_service.store import PERM_DECIDE
from tests.conftest import monolith

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

CONTRACT = Path("components/authorization/contract/component_contract.json")
OPENAPI = Path("components/authorization/contract/openapi.yaml")


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
    assert not REQUIRED - set(data)
    assert data["component_id"] == COMPONENT_ID == "authorization"
    assert data["component_version"] == COMPONENT_VERSION
    assert data["class"] == "platform_service"
    assert data["maturity_level"] == "level_0_modular_monolith"


def test_contract_does_not_announce_unimplemented_events_or_cdc_or_ui():
    data = contract()
    assert data["events"]["published"] == []
    assert data["events"]["consumed"] == []
    assert data["events"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"
    assert data["ui"]["kind"] == "none"


def test_published_api_matches_the_implementation_in_both_directions():
    data = contract()
    assert Path(data["api"]["openapi"]).read_text(encoding="utf-8") == OPENAPI.read_text(
        encoding="utf-8"
    )
    assert data["api"]["base_path"] == "/api/v1"
    assert data["api"]["supported_majors"] == ["v1"]

    declared = published_operations(OPENAPI.read_text(encoding="utf-8"))
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (path, method.lower())
        for route in create_app(monolith().authorization).routes
        for path in [getattr(route, "path", "")]
        for method in getattr(route, "methods", set())
        if path and path not in framework_routes and method not in {"HEAD", "OPTIONS"}
    }
    assert declared == implemented

    contract_operations = {
        (operation["path"], operation["method"].lower())
        for operation in data["api"]["operations"]
    }
    assert contract_operations <= declared


def test_declared_reason_codes_are_exactly_the_implemented_ones():
    data = contract()["api"]["decision_model"]
    declared = set(data["allow_reasons"]) | set(data["deny_reasons"])
    assert declared == {reason.value for reason in Reason}
    assert data["allow_reasons"] == [Reason.PERMITTED.value]
    assert data["default"] == "deny"
    assert data["response"] == [
        "decision: ALLOW or DENY",
        "reason: one stable code from the published set",
    ]
    assert tuple(data["decision_chain"]) == DECISION_CHAIN


def test_declared_refusal_semantics_match_the_error_surface():
    refusals = contract()["api"]["refusal_semantics"]["refused"]
    declared = {reason for reasons in refusals.values() for reason in reasons.split(", ")}
    assert declared == {reason.value for reason in CallerDenyReason}
    # And the statuses are the ones the error classes actually carry.
    from authorization_service.errors import (
        CallerNotAuthenticated,
        MalformedDecisionRequest,
    )

    assert CallerNotAuthenticated.status_code == 401
    assert CallerNotAuthorized.status_code == 403
    assert MalformedDecisionRequest.status_code == 422


def test_declared_permission_is_the_one_that_is_enforced():
    data = contract()
    assert data["authz"]["permissions"] == [PERM_DECIDE]
    assert data["authz"]["default_decision"] == Decision.DENY.value
    assert data["authz"]["authentication_does_not_imply_authorization"] is True
    assert data["authz"]["enforcement_point"] == "the component that owns the data"
    assert data["authz"]["tenant_context_source"] == "identity"
    assert data["authz"]["tenant_state_source"] == "tenant_authority"

    instance = monolith()
    granted = set().union(*(s.permissions for s in instance.authorization.store.services.values()))
    assert granted <= set(data["authz"]["permissions"])

    # A service that holds no permission cannot ask, whatever it is called.
    from authorization_service.reader import build_client

    client = build_client(
        instance.authorization.contract_app(), credential="authz-svc-token-reporting"
    )
    from authorization_service.contracts import ResourceRef

    with pytest.raises(CallerNotAuthorized):
        client.decide(
            "token-human-a",
            operation="records.read",
            resource=ResourceRef("record", "rec_a1", "ten_a"),
        )


def test_data_ownership_declares_exactly_one_scope_per_dataset():
    ownership = contract()["data_ownership"]
    assert ownership["owner"] == "authorization"
    assert ownership["logical_schema"] == "authorization"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    assert len(scopes) == len(ownership["datasets"])
    assert scopes["permission_grants"] == "tenant-scoped"
    assert scopes["audit_events"] == "platform-scoped"
    for name, scope in scopes.items():
        assert scope in {"tenant-scoped", "platform-scoped", "system-scoped"}, name
    # The declared datasets are the ones the store actually owns.
    instance = monolith()
    owned = set(vars(instance.authorization.store))
    assert owned == {"grants", "services", "service_tokens", "audit"}
    assert set(scopes) == {"permission_grants", "service_access", "audit_events"}


def test_declared_audit_fields_exist_on_a_real_audit_record():
    declared = set(contract()["audit"]["decision_fields"])
    assert declared <= set(AuditEvent.__slots__)
    instance = monolith()
    from authorization_service.contracts import ResourceRef

    instance.authorization_client.decide(
        "token-human-a", operation="records.read", resource=ResourceRef("record", "rec_a1", "ten_a")
    )
    event = instance.authorization.store.audit[-1]
    for field in declared:
        assert hasattr(event, field), field


def test_every_declared_refusal_is_audited_as_the_contract_claims():
    """`refusals_audited` is a promise about all of them, schema included."""
    audit = contract()["audit"]
    assert audit["refusals_audited"] is True
    declared = contract()["api"]["refusal_semantics"]["refused"]

    instance = monolith()
    http = instance.authorization_http()
    valid = {
        "operation": "records.read",
        "resource": {"resource_type": "record", "resource_id": "rec_a1", "tenant_id": "ten_a"},
    }
    # One request per declared status family, including the one the published
    # schema rejects before any handler of this component runs.
    requests = {
        "401": ({}, valid),
        "403": ({"Authorization": "Bearer authz-svc-token-reporting"}, valid),
        "422": (
            {"Authorization": "Bearer authz-svc-token-records"},
            {"resource": valid["resource"]},
        ),
    }
    assert set(requests) == set(declared)

    for status, (extra_headers, body) in requests.items():
        request_id = f"req-declared-{status}"
        response = http.post(
            "/api/v1/decisions",
            headers={**extra_headers, "X-Request-Id": request_id, "X-Correlation-Id": "cor-decl"},
            json=body,
        )
        assert response.status_code == int(status)
        reason = response.json()["detail"]["reason"]
        assert reason in declared[status].split(", ")
        event = next(
            item for item in instance.authorization.store.audit if item.request_id == request_id
        )
        assert event.details["refused"] == reason
        assert event.correlation_id == "cor-decl"


def test_declared_configuration_schema_matches_the_loader():
    schema = contract()["configuration_schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["platform_id"]
    assert set(schema["properties"]) == {"platform_id", "environment", "service_token_prefix"}

    config = AuthorizationConfig.from_mapping({"platform_id": "plt_demo"})
    assert config.environment == schema["properties"]["environment"]["default"]
    assert config.service_token_prefix == schema["properties"]["service_token_prefix"]["const"]
    # ARCHITECTURE.md §20: an unknown configuration key is an error, not a default.
    with pytest.raises(ConfigurationError):
        AuthorizationConfig.from_mapping({"platform_id": "plt_demo", "allow_everything": True})
    with pytest.raises(ConfigurationError):
        AuthorizationConfig.from_mapping({})


def test_declared_dependencies_are_the_ones_that_are_wired():
    data = contract()
    dependencies = {item["component_id"]: item for item in data["dependencies"]}
    assert set(dependencies) == {"identity", "tenant_authority"}
    for item in dependencies.values():
        assert item["kind"] == "api"
        assert Path(item["contract"]).is_file()

    consumed = {item["component_id"]: item for item in data["api"]["consumes"]}
    assert set(consumed) == set(dependencies)

    import importlib

    def resolve(path: str):
        module_name, _, class_name = path.rpartition(".")
        return getattr(importlib.import_module(module_name), class_name)

    instance = monolith()
    engine = instance.authorization.engine
    for component_id, wired in (
        ("identity", engine.identity),
        ("tenant_authority", engine.tenant_authority),
    ):
        entry = consumed[component_id]
        # What is wired into the engine is this component's own adapter over the
        # declared consumer surface — never the provider's client itself.
        assert type(wired) is resolve(entry["local_adapter"])
        assert isinstance(wired, resolve(entry["local_port"]))
        assert resolve(entry["local_adapter"]).__module__.startswith("authorization_service.")
        assert resolve(entry["local_answer_type"]).__module__.startswith("authorization_service.")

        # The declared operations exist on the published surface of the provider
        # and on the port, and neither offers anything beyond the declared reads.
        surface = resolve(entry["consumer_surface"])
        declared_operations = set(entry["operations"])
        available = {name for name in dir(surface) if not name.startswith("_")}
        assert declared_operations <= available
        assert available <= declared_operations | {"lookup"}
        port_operations = {n for n in dir(resolve(entry["local_port"])) if not n.startswith("_")}
        assert port_operations == declared_operations


def test_the_declared_version_ranges_admit_the_versions_in_this_repository():
    import identity_service
    import tenant_authority

    ranges = {item["component_id"]: item["version_range"] for item in contract()["dependencies"]}
    versions = {
        "identity": identity_service.COMPONENT_VERSION,
        "tenant_authority": tenant_authority.COMPONENT_VERSION,
    }

    def parse(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    for component_id, spec in ranges.items():
        low, high = spec.split(",")
        assert parse(versions[component_id]) >= parse(low.removeprefix(">="))
        assert parse(versions[component_id]) < parse(high.removeprefix("<"))


def test_the_invariants_of_the_issue_are_declared_in_the_contract():
    invariants = " ".join(contract()["authorization_boundary"]["invariants"])
    for fragment in (
        "verified identity",
        "effective tenant comes from IS-001",
        "cross-check",
        "another Tenant",
        "grants no access",
        "explicit permission",
        "suspended",
        "owns the data",
        "request_id and correlation_id",
        "not a consumer contract",
    ):
        assert fragment in invariants, fragment


def test_the_internal_modules_declared_are_not_the_published_one():
    surface = contract()["api"]["consumer_surface"]
    assert surface["module"] == "authorization_service.reader"
    assert surface["published_as"] == "AuthorizationClient"
    assert surface["operations"] == ["decide"]
    assert surface["module"] not in surface["internal_modules"]
    assert "authorization_service.contracts" not in surface["internal_modules"]
    for internal in surface["internal_modules"]:
        import importlib

        assert importlib.import_module(internal) is not None
