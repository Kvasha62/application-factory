"""Conformance of the machine-readable Component Contract of IS-004 (Issue #14).

The contract is only worth something if it cannot drift from the implementation.
Every check here compares the published document with live objects: declared
operations against the routes the application actually serves, declared
reason codes against the reasons the engine produces, the declared
enforcement chain against the engine's chain, declared configuration against
the loader, and the declared refusal semantics against real requests — each
documented status is produced live, and each produced reason is documented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from records_service import COMPONENT_ID, COMPONENT_VERSION
from records_service.config import RecordsConfig
from records_service.consumed import DecisionAnswer
from records_service.contracts import (
    OWN_DENY_REASONS,
    PUBLISHED_DENY_REASONS,
    OwnDenyReason,
)
from records_service.engine import ENFORCEMENT_CHAIN
from records_service.errors import ConfigurationError
from records_service.models import OwnedResource
from records_service.store import STATES, TRANSITIONS
from tests.conftest import (
    CountingRecordsStore,
    StubAuthorizationPort,
    monolith,
    records_deployment,
)

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

CONTRACT = Path("components/records/contract/component_contract.json")
OPENAPI = Path("components/records/contract/openapi.yaml")

TOKEN_A = "token-human-a"

ALLOW = DecisionAnswer(
    decision="ALLOW", reason="permitted", subject_id="idn_human_a", tenant_id="ten_a"
)


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


# ------------------------------------------------------------- required minimum
def test_component_contract_declares_the_required_minimum():
    data = contract()
    assert not REQUIRED - set(data)
    assert data["component_id"] == COMPONENT_ID == "records"
    assert data["component_version"] == COMPONENT_VERSION
    assert data["class"] == "platform_service"
    assert data["maturity_level"] == "level_0_modular_monolith"


def test_contract_declares_the_issue_required_authorization_shape():
    """The exact declarations Issue #14 requires of the component contract."""
    data = contract()
    authz = data["authz"]
    assert authz["enforcement_boundary"] == "resource_owner"
    assert authz["decision_source"] == "authorization"
    assert authz["default_decision"] == "DENY"
    assert authz["tenant_context_source"] == "identity"

    ownership = data["data_ownership"]
    assert ownership["owner"] == "records"
    assert ownership["logical_schema"] == "records"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    assert scopes["resources"] == "tenant-scoped"
    assert scopes["access_audit"] == "platform-scoped"

    assert data["events"]["status"] == "declared_only"
    assert data["events"]["published"] == []
    assert data["events"]["consumed"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []


def test_contract_declares_exactly_one_dependency_on_the_published_contract():
    data = contract()
    dependencies = data["dependencies"]
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert dependency["component_id"] == "authorization"
    assert dependency["kind"] == "api"
    assert dependency["version_range"] == ">=0.1.0,<0.2.0"
    assert Path(dependency["contract"]).exists()


# --------------------------------------------------------------- surface match
def test_published_api_matches_the_implementation_in_both_directions():
    data = contract()
    assert Path(data["api"]["openapi"]).read_text(
        encoding="utf-8"
    ) == OPENAPI.read_text(encoding="utf-8")
    assert data["api"]["base_path"] == "/api/v1"
    assert data["api"]["supported_majors"] == ["v1"]

    declared = published_operations(OPENAPI.read_text(encoding="utf-8"))
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (path, method.lower())
        for route in monolith().records_app.routes
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


def test_declared_enforcement_chain_and_model_match_the_engine():
    data = contract()
    model = data["api"]["enforcement_model"]
    assert tuple(model["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert tuple(data["resource_boundary"]["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert model["default_outcome"] == "deny"
    assert set(model["own_deny_reasons"]) == OWN_DENY_REASONS
    assert set(model["states"]) == set(STATES)
    assert model["transitions"] == {
        name: list(edge) for name, edge in TRANSITIONS.items()
    }
    assert data["resource_boundary"]["resource_model"] == [
        "resource_id",
        "resource_type",
        "owner_component",
        "tenant_id",
        "state",
    ]


def test_declared_refusal_vocabulary_matches_the_published_reasons():
    refused = contract()["api"]["refusal_semantics"]["refused"]
    declared = set()
    for reasons in refused.values():
        declared.update(reason.strip() for reason in reasons.split(","))
    # Everything the contract promises is a reason the implementation can
    # refuse; the transport refusal of an unreadable request completes the set.
    assert declared == PUBLISHED_DENY_REASONS | {"malformed_request"}


# ------------------------------------------------- behavioral refusal semantics
def refusal_scenarios() -> dict[int, tuple[str, callable]]:
    """One live scenario per documented refusal status."""

    def scenario_401() -> tuple[int, str]:
        response = monolith().records_http().get("/api/v1/resources/rec_a1")
        return response.status_code, response.json()["detail"]["reason"]

    def scenario_403() -> tuple[int, str]:
        response = (
            monolith()
            .records_http()
            .get(
                "/api/v1/resources/rec_b1",
                headers={"authorization": f"Bearer {TOKEN_A}"},
            )
        )
        return response.status_code, response.json()["detail"]["reason"]

    def scenario_404() -> tuple[int, str]:
        response = (
            monolith()
            .records_http()
            .get(
                "/api/v1/resources/missing",
                headers={"authorization": f"Bearer {TOKEN_A}"},
            )
        )
        return response.status_code, response.json()["detail"]["reason"]

    def scenario_409() -> tuple[int, str]:
        response = (
            monolith()
            .records_http()
            .post(
                "/api/v1/resources/rec_a1/transitions",
                json={"transition": "activate"},
                headers={"authorization": f"Bearer {TOKEN_A}"},
            )
        )
        return response.status_code, response.json()["detail"]["reason"]

    def scenario_422() -> tuple[int, str]:
        response = (
            monolith()
            .records_http()
            .post(
                "/api/v1/resources/rec_a2/transitions",
                json={"transition": "activate", "extra": 1},
                headers={"authorization": f"Bearer {TOKEN_A}"},
            )
        )
        return response.status_code, response.json()["detail"]["reason"]

    def scenario_503() -> tuple[int, str]:
        from fastapi.testclient import TestClient

        deployment = records_deployment(StubAuthorizationPort(RuntimeError("boom")))
        http = TestClient(deployment.contract_app())
        result = http.get(
            "/api/v1/resources/rec_a1",
            headers={"authorization": f"Bearer {TOKEN_A}"},
        )
        return result.status_code, result.json()["detail"]["reason"]

    return {
        401: scenario_401,
        403: scenario_403,
        404: scenario_404,
        409: scenario_409,
        422: scenario_422,
        503: scenario_503,
    }


def test_every_documented_refusal_status_is_produced_and_every_reason_documented():
    refused = contract()["api"]["refusal_semantics"]["refused"]
    scenarios = refusal_scenarios()

    assert set(refused) == {str(status) for status in scenarios}
    for status, scenario in scenarios.items():
        produced_status, reason = scenario()
        assert produced_status == status, (status, produced_status, reason)
        documented = {r.strip() for r in refused[str(status)].split(",")}
        assert reason in documented, (status, reason, documented)


def test_owner_mismatch_is_a_documented_403():
    store = CountingRecordsStore()
    deployment = records_deployment(StubAuthorizationPort(ALLOW), store=store)
    store.resources["rec_foreign"] = OwnedResource(
        resource_id="rec_foreign",
        resource_type="record",
        owner_component="identity",
        tenant_id="ten_a",
        state="active",
    )
    from fastapi.testclient import TestClient

    response = TestClient(deployment.contract_app()).get(
        "/api/v1/resources/rec_foreign",
        headers={"authorization": f"Bearer {TOKEN_A}"},
    )
    assert response.status_code == 403
    reason = response.json()["detail"]["reason"]
    assert reason == OwnDenyReason.OWNER_MISMATCH
    documented = {
        r.strip()
        for r in contract()["api"]["refusal_semantics"]["refused"]["403"].split(",")
    }
    assert reason in documented


# ---------------------------------------------------------------- configuration
def test_declared_configuration_schema_matches_the_loader():
    schema = contract()["configuration_schema"]
    assert schema["required"] == ["platform_id"]
    assert schema["additionalProperties"] is False

    RecordsConfig.from_mapping({"platform_id": "plt_demo"})
    with pytest.raises(ConfigurationError):
        RecordsConfig.from_mapping({"platform_id": "plt_demo", "undeclared": 1})
    with pytest.raises(ConfigurationError):
        RecordsConfig.from_mapping({})


def test_dependency_isolation_is_declared_with_the_real_module_names():
    consumes = contract()["api"]["consumes"]
    assert len(consumes) == 1
    consumed = consumes[0]
    assert consumed["component_id"] == "authorization"
    assert consumed["local_port"] == "records_service.ports.AuthorizationPort"
    assert (
        consumed["local_adapter"]
        == "records_service.adapters.AuthorizationDecisionAdapter"
    )
    assert consumed["local_answer_type"] == "records_service.consumed.DecisionAnswer"
    assert consumed["operations"] == ["decide"]
