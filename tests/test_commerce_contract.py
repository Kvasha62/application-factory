"""Conformance of the machine-readable Component Contract of SCS-002.

Every check compares the published documents with live objects: declared
operations against the routes the application actually serves (resolving
the `servers: /api/v1/commerce` prefix plus relative OpenAPI paths),
declared reason codes against the reasons the engine produces, the
declared enforcement chain against the engine's chain, the approved error
envelope against real refusals, declared configuration against the loader,
and the declared refusal semantics against real requests. Stage 2
publishes exactly two operations: create and read one owned Product.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from commerce_service import COMPONENT_ID, COMPONENT_VERSION
from commerce_service.api import ERROR_CODES, error_code_for
from commerce_service.config import CommerceConfig
from commerce_service.consumed import DecisionAnswer
from commerce_service.contracts import (
    OWN_DENY_REASONS,
    PUBLISHED_DENY_REASONS,
    OwnDenyReason,
)
from commerce_service.engine import ENFORCEMENT_CHAIN
from commerce_service.errors import ConfigurationError
from commerce_service.models import OwnedProduct
from commerce_service.store import PRODUCT_STATES, CommerceStore
from tests.test_commerce_boundary import StubAuthorizationPort, commerce_deployment
from tests.test_commerce_skeleton import (
    SUBJECT_A,
    commerce_harness,
    create_payload,
    envelope_of,
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

CONTRACT = Path("components/commerce/contract/component_contract.json")
OPENAPI = Path("components/commerce/contract/openapi.yaml")

ALLOW = DecisionAnswer(
    decision="ALLOW", reason="permitted", subject_id="idn_human_a", tenant_id="ten_a"
)


def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def openapi_text() -> str:
    return OPENAPI.read_text(encoding="utf-8")


def published_operations(text: str) -> tuple[str, set[tuple[str, str]]]:
    """Read `servers:` and the `paths:` section of the published OpenAPI.

    Returns the servers base plus the set of (resolved_path, method).
    Relative commerce paths resolve under the servers base; health paths
    carry their own `servers: /` override and resolve at the instance root.
    """
    servers_base = ""
    operations: set[tuple[str, str]] = set()
    current_path: str | None = None
    current_servers: str | None = None
    in_paths = False
    in_servers = False
    methods = {"get", "post", "put", "patch", "delete"}
    for line in text.splitlines():
        if line.startswith("servers:"):
            in_servers = not in_paths
            continue
        if in_servers and "url:" in line:
            url = line.split("url:", 1)[1].strip()
            if not servers_base:
                servers_base = url
            in_servers = False
            continue
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
            current_servers = None
        elif current_path is not None and stripped == "servers:":
            current_servers = ""
        elif current_servers == "" and stripped.startswith("- url:"):
            current_servers = stripped.split("url:", 1)[1].strip()
        elif (
            current_path is not None
            and line.startswith("    ")
            and not line.startswith("     ")
            and stripped.endswith(":")
            and stripped[:-1] in methods
        ):
            base = current_servers if current_servers else servers_base
            if base == "/":
                resolved = current_path
            else:
                resolved = base.rstrip("/") + current_path
            operations.add((resolved, stripped[:-1]))
    return servers_base, operations


# ------------------------------------------------------------- required minimum
def test_component_contract_declares_the_required_minimum():
    data = contract()
    assert not REQUIRED - set(data)
    assert data["component_id"] == COMPONENT_ID == "commerce"
    assert data["component_version"] == COMPONENT_VERSION
    assert data["class"] == "business_system"
    assert data["scs_id"] == "SCS-002"
    assert data["maturity_level"] == "level_0_modular_monolith"


def test_contract_declares_the_required_shapes():
    data = contract()
    authz = data["authz"]
    assert authz["enforcement_boundary"] == "resource_owner"
    assert authz["decision_source"] == "authorization"
    assert authz["default_decision"] == "DENY"
    assert authz["tenant_context_source"] == "identity"
    assert authz["operations_guarded"] == [
        "commerce.products.create",
        "commerce.products.read",
    ]

    ownership = data["data_ownership"]
    assert ownership["owner"] == "commerce"
    assert ownership["logical_schema"] == "commerce"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    assert scopes["products"] == "tenant-scoped"
    assert scopes["offers"] == "tenant-scoped"
    assert scopes["prices"] == "tenant-scoped"
    assert scopes["carts"] == "tenant-scoped"
    assert scopes["cart_lines"] == "tenant-scoped"
    assert scopes["orders"] == "tenant-scoped"
    assert scopes["order_lines"] == "tenant-scoped"
    assert scopes["payment_state"] == "tenant-scoped"
    assert scopes["access_audit"] == "platform-scoped"

    assert data["events"]["status"] == "declared_only"
    assert data["events"]["published"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []

    assert data["api"]["supported_majors"] == ["v1"]
    model = data["api"]["enforcement_model"]
    assert model["product_states"] == ["ACTIVE"]
    assert "v2" not in json.dumps(data)


def test_contract_declares_the_dependencies_on_published_contracts():
    data = contract()
    dependencies = data["dependencies"]
    assert len(dependencies) == 3
    by_component = {
        dependency["component_id"]: dependency for dependency in dependencies
    }
    authorization = by_component["authorization"]
    assert authorization["kind"] == "api"
    assert authorization["version_range"] == ">=0.1.0,<0.2.0"
    assert Path(authorization["contract"]).exists()
    idempotency = by_component["idempotency"]
    assert idempotency["kind"] == "internal-consumer-surface"
    assert idempotency["version_range"] == ">=0.1.0,<0.2.0"
    assert Path(idempotency["contract"]).exists()
    identity = by_component["identity"]
    assert identity["kind"] == "api"
    assert identity["version_range"] == ">=0.3.0,<0.4.0"
    assert Path(identity["contract"]).exists()


# --------------------------------------------------------------- surface match
def test_openapi_uses_servers_and_relative_paths():
    text = openapi_text()
    assert "servers:" in text
    assert "url: /api/v1/commerce" in text
    paths_section = text.split("paths:", 1)[1]
    assert "/products:" in paths_section
    assert "/products/{product_id}:" in paths_section
    assert "/api/v1/commerce/products" not in paths_section


def test_published_api_matches_the_implementation_in_both_directions():
    data = contract()
    assert Path(data["api"]["openapi"]).read_text(encoding="utf-8") == openapi_text()
    assert data["api"]["base_path"] == "/api/v1/commerce"
    assert data["api"]["servers"] == "/api/v1/commerce"
    assert data["api"]["supported_majors"] == ["v1"]

    servers_base, declared = published_operations(openapi_text())
    assert servers_base == "/api/v1/commerce"
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (path, method.lower())
        for route in commerce_harness().commerce.contract_app().routes
        for path in [getattr(route, "path", "")]
        for method in getattr(route, "methods", set())
        if path and path not in framework_routes and method not in {"HEAD", "OPTIONS"}
    }
    assert declared == implemented
    assert declared == {
        ("/api/v1/commerce/products", "post"),
        ("/api/v1/commerce/products/{product_id}", "get"),
        ("/health", "get"),
        ("/ready", "get"),
    }

    contract_operations = {
        (operation["path"], operation["method"].lower())
        for operation in data["api"]["operations"]
    }
    assert contract_operations <= declared
    # Exactly the two product operations of Stage 2; health/readiness live
    # in the OpenAPI document and the app, not in the operations list.
    assert contract_operations == {
        ("/api/v1/commerce/products", "post"),
        ("/api/v1/commerce/products/{product_id}", "get"),
    }


def test_declared_enforcement_chain_and_model_match_the_engine():
    data = contract()
    model = data["api"]["enforcement_model"]
    assert tuple(model["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert tuple(data["commerce_boundary"]["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert model["default_outcome"] == "deny"
    assert model["owned_data_operations"] == [
        "create_product",
        "serve_product",
    ]
    assert set(model["own_deny_reasons"]) == OWN_DENY_REASONS
    assert set(model["product_states"]) == set(PRODUCT_STATES)
    assert data["commerce_boundary"]["product_model"]["product"] == [
        "product_id",
        "tenant_id",
        "name",
        "description",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    ]


def test_declared_refusal_vocabulary_matches_the_published_reasons():
    refused = contract()["api"]["refusal_semantics"]["refused"]
    declared = set()
    for reasons in refused.values():
        declared.update(reason.strip() for reason in reasons.split(","))
    assert declared == PUBLISHED_DENY_REASONS
    assert "permission_not_granted" in declared
    assert "resource_tenant_mismatch" in declared
    assert "product_unknown" in declared


# ------------------------------------------------------- approved error envelope
def test_error_code_vocabulary_matches_the_contract():
    codes = contract()["api"]["refusal_semantics"]["envelope"]["codes"]
    assert set(codes) == set(ERROR_CODES)
    for code, spec in codes.items():
        assert spec["status"] == ERROR_CODES[code]["status"], code
        assert spec["message"] == ERROR_CODES[code]["message"], code
    # The approved example, locked.
    assert codes["AUTHORIZATION_DENIED"]["status"] == 403
    assert codes["AUTHORIZATION_DENIED"]["message"] == "Operation is not permitted."


def test_error_code_mapping_matches_the_refused_reasons():
    codes = contract()["api"]["refusal_semantics"]["envelope"]["codes"]
    refused = contract()["api"]["refusal_semantics"]["refused"]
    for code, spec in codes.items():
        for reason in (r.strip() for r in spec["reasons"].split(",")):
            assert error_code_for(reason) == code, reason
            if code != "INVALID_REQUEST":
                assert reason in {
                    r.strip() for r in refused[str(spec["status"])].split(",")
                }, reason


def test_openapi_declares_the_error_envelope():
    text = openapi_text()
    assert "Error:" in text
    for code in ERROR_CODES:
        assert code in text, code
    assert '"Detail"' not in text
    assert "'Detail'" not in text


# ------------------------------------------------- behavioral refusal semantics
def test_every_documented_refusal_status_is_produced_with_the_envelope():
    refused = contract()["api"]["refusal_semantics"]["refused"]

    def scenario_400():
        return (
            commerce_harness(store=CommerceStore())
            .http()
            .post(
                "/api/v1/commerce/products",
                headers={"authorization": f"Bearer {SUBJECT_A}"},
                json=create_payload(),
            )
        )

    def scenario_401():
        return commerce_harness().http().get("/api/v1/commerce/products/prd_a1")

    def scenario_403():
        return (
            commerce_harness()
            .http()
            .get(
                "/api/v1/commerce/products/prd_b1",
                headers={"authorization": f"Bearer {SUBJECT_A}"},
            )
        )

    def scenario_404():
        return (
            commerce_harness()
            .http()
            .get(
                "/api/v1/commerce/products/missing",
                headers={"authorization": f"Bearer {SUBJECT_A}"},
            )
        )

    def scenario_409():
        http = commerce_harness(store=CommerceStore()).http()
        headers = {
            "authorization": f"Bearer {SUBJECT_A}",
            "idempotency-key": "contract-409",
        }
        first = http.post(
            "/api/v1/commerce/products", headers=headers, json=create_payload()
        )
        assert first.status_code == 201
        return http.post(
            "/api/v1/commerce/products",
            headers=headers,
            json=create_payload("Changed name"),
        )

    def scenario_422():
        return (
            commerce_harness(store=CommerceStore())
            .http()
            .post(
                "/api/v1/commerce/products",
                headers={
                    "authorization": f"Bearer {SUBJECT_A}",
                    "idempotency-key": "contract-422",
                },
                json={"name": "   ", "description": ""},
            )
        )

    def scenario_503():
        deployment = commerce_deployment(StubAuthorizationPort(RuntimeError("boom")))
        http = TestClient(deployment.contract_app())
        return http.get(
            "/api/v1/commerce/products/prd_a1",
            headers={"authorization": f"Bearer {SUBJECT_A}"},
        )

    scenarios = {
        400: [scenario_400],
        401: [scenario_401],
        403: [scenario_403],
        404: [scenario_404],
        409: [scenario_409],
        422: [scenario_422],
        503: [scenario_503],
    }
    assert set(refused) == {str(status) for status in scenarios}
    expected_codes = {
        400: "IDEMPOTENCY_KEY_REQUIRED",
        401: "AUTHENTICATION_REQUIRED",
        403: "AUTHORIZATION_DENIED",
        404: "NOT_FOUND",
        409: "IDEMPOTENCY_CONFLICT",
        422: "VALIDATION_ERROR",
        503: "DEPENDENCY_UNAVAILABLE",
    }
    for status, calls in scenarios.items():
        for scenario in calls:
            response = scenario()
            assert response.status_code == status, (status, response.text)
            body = envelope_of(response)
            assert body["error"]["code"] == expected_codes[status]
            assert (
                body["error"]["message"]
                == ERROR_CODES[expected_codes[status]]["message"]
            )
            documented = {r.strip() for r in refused[str(status)].split(",")}
            assert body["error"]["details"]["reason"] in documented


def test_owner_mismatch_is_a_documented_403():
    store = CommerceStore()
    store.seed_demo()
    store.products["prd_foreign"] = OwnedProduct(
        product_id="prd_foreign",
        tenant_id="ten_a",
        owner_component="identity",
        name="Foreign product",
        description="Owned by another component.",
        status="ACTIVE",
        created_by="idn_human_a",
        created_at="2026-09-13T00:00:00+00:00",
        updated_at="2026-09-13T00:00:00+00:00",
    )
    deployment = commerce_deployment(StubAuthorizationPort(ALLOW), store=store)
    response = TestClient(deployment.contract_app()).get(
        "/api/v1/commerce/products/prd_foreign",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == OwnDenyReason.OWNER_MISMATCH


def test_product_response_shape_matches_openapi_schema():
    harness = commerce_harness()
    response = harness.http().get(
        "/api/v1/commerce/products/prd_a1",
        headers={"authorization": f"Bearer {SUBJECT_A}"},
    )
    assert response.status_code == 200
    item = response.json()
    text = openapi_text()
    for field in (
        "product_id",
        "name",
        "description",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    ):
        assert field in text
        assert field in item


# ---------------------------------------------------------------- configuration
def test_declared_configuration_schema_matches_the_loader():
    schema = contract()["configuration_schema"]
    assert schema["required"] == ["platform_id"]
    assert schema["additionalProperties"] is False

    CommerceConfig.from_mapping({"platform_id": "plt_demo"})
    with pytest.raises(ConfigurationError):
        CommerceConfig.from_mapping({"platform_id": "plt_demo", "undeclared": 1})
    with pytest.raises(ConfigurationError):
        CommerceConfig.from_mapping({})


def test_dependency_isolation_is_declared_with_the_real_module_names():
    consumes = contract()["api"]["consumes"]
    assert len(consumes) == 2
    by_component = {c["component_id"]: c for c in consumes}
    consumed = by_component["authorization"]
    assert consumed["local_port"] == "commerce_service.ports.AuthorizationPort"
    assert (
        consumed["local_adapter"]
        == "commerce_service.adapters.AuthorizationDecisionAdapter"
    )
    assert consumed["local_answer_type"] == "commerce_service.consumed.DecisionAnswer"
    assert consumed["operations"] == ["decide"]
    identity = by_component["identity"]
    assert identity["local_port"] == "commerce_service.ports.TenantContextPort"
    assert identity["local_adapter"] == "commerce_service.adapters.TenantContextAdapter"
    assert (
        identity["local_answer_type"] == "commerce_service.consumed.TenantContextAnswer"
    )
    assert identity["operations"] == ["resolve_context"]


# ------------------------------------------------------------ product contract
def test_product_operations_are_declared_in_openapi():
    text = openapi_text()
    assert "/products:" in text
    assert "/products/{product_id}:" in text
    assert "operationId: createProduct" in text
    assert "operationId: readProduct" in text
    assert "Idempotency-Key" in text
    assert "commerce.products.create" in text
    assert "commerce.products.read" in text


def test_product_operations_are_declared_in_the_component_contract():
    data = contract()
    operations = {
        (operation["method"], operation["path"]): operation
        for operation in data["api"]["operations"]
    }
    create = operations[("POST", "/api/v1/commerce/products")]
    assert create["authorization_operation"] == "commerce.products.create"
    assert create["idempotency"] == "required"
    assert create["openapi_path"] == "/products"
    read = operations[("GET", "/api/v1/commerce/products/{product_id}")]
    assert read["authorization_operation"] == "commerce.products.read"
    assert read["idempotency"] == "natural"
    assert read["openapi_path"] == "/products/{product_id}"


def test_product_lifecycle_remains_exactly_active():
    data = contract()
    states = data["api"]["enforcement_model"]["product_states"]
    assert states == ["ACTIVE"]
    assert list(PRODUCT_STATES) == ["ACTIVE"]
    for forbidden in ("DRAFT", "PUBLISHED", "ARCHIVED", "DELETED"):
        assert forbidden not in states


def test_no_payment_customer_or_checkout_entities_exist():
    import ast
    from dataclasses import fields

    forbidden_classes = {
        "PaymentService",
        "PaymentGateway",
        "PaymentProvider",
        "PaymentProcessor",
        "CustomerService",
        "Customer",
        "UserService",
        "ProfileService",
        "Checkout",
        "CheckoutEntity",
        "CheckoutDocument",
    }
    package = Path("src/commerce_service")
    class_names: set[str] = set()
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_names.add(node.name)
    assert forbidden_classes.isdisjoint(class_names), class_names

    # The catalog boundary holds at the field level: a price belongs to a
    # sellable proposition, never to the catalog object.
    product_fields = {field.name for field in fields(OwnedProduct)}
    assert "price" not in product_fields
    assert "amount" not in product_fields


def test_no_later_slice_endpoints_exist_in_openapi():
    text = openapi_text()
    paths_section = text.split("paths:", 1)[1].split("components:", 1)[0]
    for forbidden in (
        "\n  /offers",
        "\n  /prices",
        "\n  /carts",
        "\n  /checkout",
        "\n  /orders",
        "\n  /payment",
        "\n  /refund",
    ):
        assert forbidden not in paths_section, forbidden
