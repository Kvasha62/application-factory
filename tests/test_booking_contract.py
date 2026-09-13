"""Conformance of the machine-readable Component Contract of SCS-003 Booking.

Every check compares the published documents with live objects: declared
operations against the routes the application actually serves (resolving
the `servers: /api/v1/booking` prefix plus relative OpenAPI paths),
declared reason codes against the reasons the engine produces, the
declared enforcement chain against the engine's chain, the approved error
envelope against real refusals of every documented status and code,
declared configuration against the loader, response shapes against the
OpenAPI schemas, and the declared time/lifecycle semantics against the
implementation constants. Seven operations are published: create and
read one owned Resource, declare and list its Availability Windows,
create, read and cancel one Reservation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from booking_service import COMPONENT_ID, COMPONENT_VERSION
from booking_service.api import ERROR_CODES, error_code_for
from booking_service.config import BookingConfig
from booking_service.consumed import DecisionAnswer
from booking_service.contracts import (
    OWN_DENY_REASONS,
    PUBLISHED_DENY_REASONS,
    RESERVATION_STATES,
    RESOURCE_STATES,
    OwnDenyReason,
)
from booking_service.engine import ENFORCEMENT_CHAIN
from booking_service.errors import ConfigurationError
from booking_service.models import OwnedResource
from booking_service.store import BookingStore
from tests.test_booking_boundary import StubAuthorizationPort, booking_deployment
from tests.test_booking_skeleton import (
    BASE,
    SUBJECT_A,
    SUBJECT_C,
    WINDOW_END,
    WINDOW_START,
    auth,
    bookable_resource,
    booking_harness,
    cancel_reservation,
    create_availability,
    create_reservation,
    create_resource,
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

CONTRACT = Path("components/booking/contract/component_contract.json")
OPENAPI = Path("components/booking/contract/openapi.yaml")
README = Path("components/booking/README.md")

ALLOW = DecisionAnswer(
    decision="ALLOW", reason="permitted", subject_id="idn_human_a", tenant_id="ten_a"
)

SLOT = ("2026-10-01T10:00:00+00:00", "2026-10-01T11:00:00+00:00")

BUSINESS_OPERATIONS = {
    ("/api/v1/booking/resources", "post"),
    ("/api/v1/booking/resources/{resource_id}", "get"),
    ("/api/v1/booking/resources/{resource_id}/availability", "post"),
    ("/api/v1/booking/resources/{resource_id}/availability", "get"),
    ("/api/v1/booking/reservations", "post"),
    ("/api/v1/booking/reservations/{reservation_id}", "get"),
    ("/api/v1/booking/reservations/{reservation_id}/cancel", "post"),
}


def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def openapi_text() -> str:
    return OPENAPI.read_text(encoding="utf-8")


def openapi() -> dict:
    """A minimal reading of the published OpenAPI document (no YAML library).

    The Quality Gate has no YAML dependency, so this reads exactly the parts
    the checks need: ``openapi``/``info.version``, ``servers``, the
    ``paths`` with their methods, per-method ``parameters`` names,
    ``operationId`` and ``security``, and the ``components.schemas`` with
    their ``properties``, ``required``, ``additionalProperties`` and the
    ``enum`` of scalar properties. The document is indented in two-space
    steps, as every contract of this repository is.
    """
    text = openapi_text()
    document: dict = {"paths": {}, "components": {"schemas": {}}, "servers": []}
    for line in text.splitlines():
        if line.startswith("openapi:"):
            document["openapi"] = line.split(":", 1)[1].strip()
        elif line.startswith("  version:") and "info" not in document:
            document["info"] = {"version": line.split(":", 1)[1].strip()}
    top_servers = text.split("servers:", 1)[1].split("paths:", 1)[0]
    document["servers"] = [
        {"url": s.split("url:", 1)[1].strip()}
        for s in top_servers.splitlines()
        if "url:" in s
    ]
    paths_text = text.split("\npaths:\n", 1)[1].split("\ncomponents:\n", 1)[0]
    path = method = None
    item: dict = {}
    for line in paths_text.splitlines():
        if line.startswith("  /") and line.rstrip().endswith(":"):
            path = line.strip()[:-1]
            item = document["paths"].setdefault(path, {})
            method = None
        elif (
            line.startswith("    ")
            and not line.startswith("     ")
            and line.strip().endswith(":")
        ):
            key = line.strip()[:-1]
            if key in ("get", "post", "put", "patch", "delete"):
                method = key
                item[method] = {"parameters": [], "security": None}
            elif key == "servers":
                item["servers"] = []
                method = None
            else:
                method = None
        elif method is None and "servers" in item and "url:" in line:
            item["servers"].append({"url": line.split("url:", 1)[1].strip()})
        elif method is not None:
            stripped = line.strip()
            if stripped.startswith("operationId:"):
                item[method]["operationId"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("- subjectAuth:"):
                item[method]["security"] = [{"subjectAuth": []}]
            elif stripped.startswith("name:") and line.startswith("          name:"):
                item[method]["parameters"].append(
                    {"name": stripped.split(":", 1)[1].strip()}
                )
    schemas_text = text.split("\n  schemas:\n", 1)[1]
    schema = None
    prop = None
    for line in schemas_text.splitlines():
        if (
            line.startswith("    ")
            and not line.startswith("     ")
            and line.rstrip().endswith(":")
        ):
            schema = document["components"]["schemas"].setdefault(
                line.strip()[:-1], {"properties": {}}
            )
            prop = None
            in_properties = False
        elif schema is None:
            continue
        elif line.startswith("      ") and not line.startswith("       "):
            stripped = line.strip()
            in_properties = stripped == "properties:"
            prop = None
            if stripped.startswith("additionalProperties:"):
                schema["additionalProperties"] = stripped.endswith("true")
            elif stripped.startswith("required:"):
                schema["required"] = [
                    r.strip() for r in stripped.split("[", 1)[1].rstrip("]").split(",")
                ]
        elif (
            in_properties
            and line.startswith("        ")
            and not line.startswith("         ")
        ):
            prop = schema["properties"].setdefault(line.strip()[:-1], {})
        elif (
            prop is not None
            and line.strip().startswith("enum:")
            and line.startswith("          enum")
        ):
            prop["enum"] = [
                e.strip().strip('"')
                for e in line.split("[", 1)[1].rstrip("]").split(",")
            ]
    error = document["components"]["schemas"]["Error"]
    error_text = schemas_text.split("    Error:\n", 1)[1]
    error["properties"]["error"] = {
        "required": [
            r.strip()
            for r in error_text.split("required: [", 2)[2].split("]", 1)[0].split(",")
        ],
        "properties": {
            "code": {
                "enum": [
                    e.strip()
                    for e in error_text.split("enum: [", 1)[1]
                    .split("]", 1)[0]
                    .split(",")
                ]
            }
        },
    }
    return document


def published_operations(document: dict) -> tuple[str, set[tuple[str, str]]]:
    """The servers base plus the set of (resolved_path, method).

    Relative booking paths resolve under the servers base; health paths
    carry their own `servers: /` override and resolve at the instance root.
    """
    servers_base = document["servers"][0]["url"]
    operations: set[tuple[str, str]] = set()
    for path, item in document["paths"].items():
        override = item["servers"][0]["url"] if item.get("servers") else None
        base = override or servers_base
        resolved = path if base == "/" else base.rstrip("/") + path
        for method in ("get", "post", "put", "patch", "delete"):
            if method in item:
                operations.add((resolved, method))
    return servers_base, operations


# ------------------------------------------------------------- required minimum
def test_component_documents_exist():
    assert CONTRACT.exists()
    assert OPENAPI.exists()
    assert README.exists()
    assert "SCS-003" in README.read_text(encoding="utf-8")
    assert "#57" in README.read_text(encoding="utf-8")


def test_component_contract_declares_the_required_minimum():
    data = contract()
    assert not REQUIRED - set(data)
    assert data["component_id"] == COMPONENT_ID == "booking"
    assert data["component_version"] == COMPONENT_VERSION == "0.1.0"
    assert data["class"] == "business_system"
    assert data["scs_id"] == "SCS-003"
    assert data["maturity_level"] == "level_0_modular_monolith"
    assert "#57" in data["task"]
    assert "ADR-0014" in data["task"]


def test_contract_declares_the_required_shapes():
    data = contract()
    authz = data["authz"]
    assert authz["enforcement_boundary"] == "resource_owner"
    assert authz["decision_source"] == "authorization"
    assert authz["default_decision"] == "DENY"
    assert authz["tenant_context_source"] == "identity"
    assert authz["operations_guarded"] == [
        "booking.resources.create",
        "booking.resources.read",
        "booking.availability.create",
        "booking.availability.read",
        "booking.reservations.create",
        "booking.reservations.read",
        "booking.reservations.cancel",
    ]

    ownership = data["data_ownership"]
    assert ownership["owner"] == "booking"
    assert ownership["logical_schema"] == "booking"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    assert scopes == {
        "resources": "tenant-scoped",
        "availability_windows": "tenant-scoped",
        "reservations": "tenant-scoped",
        "access_audit": "platform-scoped",
    }

    assert data["events"]["status"] == "declared_only"
    assert data["events"]["published"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []
    assert data["api"]["supported_majors"] == ["v1"]
    assert "v2" not in json.dumps(data)


def test_contract_declares_the_dependencies_on_published_contracts():
    dependencies = contract()["dependencies"]
    assert len(dependencies) == 3
    by_component = {d["component_id"]: d for d in dependencies}
    assert set(by_component) == {"authorization", "idempotency", "identity"}
    for dependency in dependencies:
        assert Path(dependency["contract"]).exists(), dependency
        declared = json.loads(Path(dependency["contract"]).read_text(encoding="utf-8"))
        low, high = dependency["version_range"].split(",")
        actual = tuple(int(p) for p in declared["component_version"].split("."))
        assert actual >= tuple(int(p) for p in low[2:].split("."))
        assert actual < tuple(int(p) for p in high[1:].split("."))
    assert by_component["authorization"]["kind"] == "api"
    assert by_component["idempotency"]["kind"] == "internal-consumer-surface"
    assert by_component["identity"]["kind"] == "api"
    # No dependency on a sibling Business System (ADR-0014 §11).
    assert "commerce" not in by_component
    assert "learning" not in by_component


# --------------------------------------------------------------- surface match
def test_openapi_uses_servers_and_relative_paths():
    document = openapi()
    assert document["openapi"].startswith("3.")
    assert document["info"]["version"] == COMPONENT_VERSION
    assert document["servers"][0]["url"] == "/api/v1/booking"
    paths = set(document["paths"])
    assert paths == {
        "/resources",
        "/resources/{resource_id}",
        "/resources/{resource_id}/availability",
        "/reservations",
        "/reservations/{reservation_id}",
        "/reservations/{reservation_id}/cancel",
        "/health",
        "/ready",
    }
    assert not any(path.startswith("/api/") for path in paths)


def test_published_api_matches_the_implementation_in_both_directions():
    data = contract()
    assert Path(data["api"]["openapi"]).read_text(encoding="utf-8") == openapi_text()
    assert data["api"]["base_path"] == "/api/v1/booking"
    assert data["api"]["servers"] == "/api/v1/booking"

    servers_base, declared = published_operations(openapi())
    assert servers_base == "/api/v1/booking"
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (path, method.lower())
        for route in booking_harness().booking.contract_app().routes
        for path in [getattr(route, "path", "")]
        for method in getattr(route, "methods", set())
        if path and path not in framework_routes and method not in {"HEAD", "OPTIONS"}
    }
    assert declared == implemented
    assert declared == BUSINESS_OPERATIONS | {("/health", "get"), ("/ready", "get")}

    contract_operations = {
        (operation["path"], operation["method"].lower())
        for operation in data["api"]["operations"]
    }
    assert contract_operations == BUSINESS_OPERATIONS
    for operation in data["api"]["operations"]:
        assert operation["path"] == "/api/v1/booking" + operation["openapi_path"]
        assert (
            operation["authorization_operation"] in data["authz"]["operations_guarded"]
        )
        assert operation["idempotency"] == (
            "required" if operation["method"] == "POST" else "natural"
        )


def test_operation_ids_and_guarded_operations_are_named_in_openapi():
    text = openapi_text()
    for operation_id in (
        "createResource",
        "readResource",
        "createAvailability",
        "listAvailability",
        "createReservation",
        "readReservation",
        "cancelReservation",
    ):
        assert f"operationId: {operation_id}" in text, operation_id
    for guarded in contract()["authz"]["operations_guarded"]:
        assert guarded in text, guarded
    document = openapi()
    for path, item in document["paths"].items():
        if path in ("/health", "/ready"):
            continue
        for method in ("get", "post"):
            if method not in item:
                continue
            names = {p["name"] for p in item[method]["parameters"]}
            assert {"X-Tenant-Id", "X-Request-Id", "X-Correlation-Id"} <= names
            assert ("Idempotency-Key" in names) == (method == "post"), (path, method)
            assert item[method]["security"] == [{"subjectAuth": []}]


def test_declared_enforcement_chain_and_model_match_the_engine():
    data = contract()
    model = data["api"]["enforcement_model"]
    assert tuple(model["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert tuple(data["booking_boundary"]["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert model["default_outcome"] == "deny"
    assert model["owned_data_operations"] == [
        "create_resource",
        "serve_resource",
        "create_availability",
        "availability_of",
        "create_reservation",
        "serve_reservation",
        "cancel_reservation",
    ]
    for name in model["owned_data_operations"]:
        assert callable(getattr(BookingStore, name)), name
    assert set(model["own_deny_reasons"]) == OWN_DENY_REASONS
    assert (
        list(model["resource_states"])
        == list(RESOURCE_STATES)
        == ["ACTIVE", "INACTIVE"]
    )
    assert (
        list(model["reservation_states"])
        == list(RESERVATION_STATES)
        == ["ACTIVE", "CANCELLED"]
    )
    assert model["reservation_transitions"] == ["ACTIVE -> CANCELLED"]
    assert "half-open" in model["time_semantics"]["interval"]
    assert "UTC" in model["time_semantics"]["storage_and_comparison"]


def test_declared_models_match_the_implementation():
    boundary = contract()["booking_boundary"]["resource_model"]
    assert boundary["resource"] == [
        "resource_id",
        "tenant_id",
        "name",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    ]
    assert boundary["reservation"] == [
        "reservation_id",
        "tenant_id",
        "resource_id",
        "booker_identity_id",
        "start_at",
        "end_at",
        "status",
        "created_at",
        "cancelled_at",
    ]
    schemas = openapi()["components"]["schemas"]
    assert set(schemas) == {
        "Resource",
        "ResourceCreate",
        "Availability",
        "AvailabilityCreate",
        "AvailabilityList",
        "Reservation",
        "ReservationCreate",
        "Error",
    }
    assert set(schemas["Resource"]["properties"]) == set(boundary["published_view"])
    assert set(schemas["Availability"]["properties"]) == set(
        boundary["availability_published_view"]
    )
    assert set(schemas["Reservation"]["properties"]) == set(
        boundary["reservation_published_view"]
    )
    for schema in schemas.values():
        assert schema["additionalProperties"] is False
    assert "tenant_id" not in json.dumps(schemas)
    assert "booker_identity_id" not in schemas["ReservationCreate"]["properties"]
    assert schemas["Resource"]["properties"]["status"]["enum"] == ["ACTIVE", "INACTIVE"]
    assert schemas["Reservation"]["properties"]["status"]["enum"] == [
        "ACTIVE",
        "CANCELLED",
    ]


def test_declared_refusal_vocabulary_matches_the_published_reasons():
    refused = contract()["api"]["refusal_semantics"]["refused"]
    declared = set()
    for reasons in refused.values():
        declared.update(reason.strip() for reason in reasons.split(","))
    assert declared == PUBLISHED_DENY_REASONS
    for reason in (
        "resource_not_found",
        "resource_inactive",
        "availability_not_found",
        "outside_availability",
        "reservation_conflict",
        "reservation_not_found",
        "invalid_state_transition",
        "booker_mismatch",
        "permission_not_granted",
        "resource_tenant_mismatch",
    ):
        assert reason in declared, reason


# ------------------------------------------------------- approved error envelope
def test_error_code_vocabulary_matches_the_contract_and_openapi():
    codes = contract()["api"]["refusal_semantics"]["envelope"]["codes"]
    assert set(codes) == set(ERROR_CODES)
    for code, spec in codes.items():
        assert spec["status"] == ERROR_CODES[code]["status"], code
        assert spec["message"] == ERROR_CODES[code]["message"], code
    enum = openapi()["components"]["schemas"]["Error"]["properties"]["error"][
        "properties"
    ]["code"]["enum"]
    assert set(enum) == set(ERROR_CODES)
    assert codes["RESERVATION_CONFLICT"]["status"] == 409
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
    # Every published reason maps to a code of the status it is refused with.
    for status, reasons in refused.items():
        for reason in (r.strip() for r in reasons.split(",")):
            assert ERROR_CODES[error_code_for(reason)]["status"] == int(status), reason


def test_openapi_declares_the_error_envelope():
    text = openapi_text()
    assert "Error:" in text
    for code in ERROR_CODES:
        assert code in text, code
    assert '"Detail"' not in text
    assert "'Detail'" not in text
    error = openapi()["components"]["schemas"]["Error"]
    assert error["required"] == ["error", "request_id", "correlation_id"]
    assert error["properties"]["error"]["required"] == ["code", "message", "details"]


# ------------------------------------------------- behavioral refusal semantics
def test_every_documented_refusal_status_and_code_is_produced_with_the_envelope():
    refused = contract()["api"]["refusal_semantics"]["refused"]

    def fresh():
        harness = booking_harness(store=BookingStore())
        return harness, harness.http()

    def scenario_400():
        _, http = fresh()
        return http.post(
            f"{BASE}/resources", headers=auth(SUBJECT_A), json={"name": "x"}
        )

    def scenario_401():
        return booking_harness().http().get(f"{BASE}/resources/res_a1")

    def scenario_403():
        return (
            booking_harness()
            .http()
            .get(f"{BASE}/resources/res_b1", headers=auth(SUBJECT_A))
        )

    def scenario_403_booker():
        harness, http = fresh()
        resource_id = bookable_resource(harness, "c403")
        reservation_id = create_reservation(
            http, resource_id, "c403-1", start_at=SLOT[0], end_at=SLOT[1]
        ).json()["reservation_id"]
        return cancel_reservation(http, reservation_id, "c403-2", token=SUBJECT_C)

    def scenario_404():
        return (
            booking_harness()
            .http()
            .get(f"{BASE}/resources/missing", headers=auth(SUBJECT_A))
        )

    def scenario_404_reservation():
        return (
            booking_harness()
            .http()
            .get(f"{BASE}/reservations/missing", headers=auth(SUBJECT_A))
        )

    def scenario_409_idempotency():
        _, http = fresh()
        assert create_resource(http, "c409").status_code == 201
        return create_resource(http, "c409", name="Changed")

    def scenario_409_inactive():
        _, http = fresh()
        resource_id = create_resource(http, "c409i", status="INACTIVE").json()[
            "resource_id"
        ]
        create_availability(http, resource_id, "c409i-avl")
        return create_reservation(
            http, resource_id, "c409i-1", start_at=SLOT[0], end_at=SLOT[1]
        )

    def scenario_409_outside():
        harness, http = fresh()
        resource_id = bookable_resource(harness, "c409o")
        return create_reservation(
            http,
            resource_id,
            "c409o-1",
            start_at="2026-10-01T17:30:00Z",
            end_at="2026-10-01T18:30:00Z",
        )

    def scenario_409_no_window():
        _, http = fresh()
        resource_id = create_resource(http, "c409n").json()["resource_id"]
        return create_reservation(
            http, resource_id, "c409n-1", start_at=SLOT[0], end_at=SLOT[1]
        )

    def scenario_409_conflict():
        harness, http = fresh()
        resource_id = bookable_resource(harness, "c409c")
        assert (
            create_reservation(
                http, resource_id, "c409c-1", start_at=SLOT[0], end_at=SLOT[1]
            ).status_code
            == 201
        )
        return create_reservation(
            http,
            resource_id,
            "c409c-2",
            token=SUBJECT_C,
            start_at=SLOT[0],
            end_at=SLOT[1],
        )

    def scenario_409_state():
        harness, http = fresh()
        resource_id = bookable_resource(harness, "c409s")
        reservation_id = create_reservation(
            http, resource_id, "c409s-1", start_at=SLOT[0], end_at=SLOT[1]
        ).json()["reservation_id"]
        assert cancel_reservation(http, reservation_id, "c409s-2").status_code == 200
        return cancel_reservation(http, reservation_id, "c409s-3")

    def scenario_422():
        _, http = fresh()
        return create_resource(http, "c422", name="   ")

    def scenario_422_naive():
        harness, http = fresh()
        resource_id = bookable_resource(harness, "c422n")
        return create_reservation(
            http,
            resource_id,
            "c422n-1",
            start_at="2026-10-01T10:00:00",
            end_at="2026-10-01T11:00:00",
        )

    def scenario_503():
        deployment = booking_deployment(StubAuthorizationPort(RuntimeError("boom")))
        return TestClient(deployment.contract_app()).get(
            f"{BASE}/resources/res_a1", headers=auth(SUBJECT_A)
        )

    scenarios = {
        400: [(scenario_400, "IDEMPOTENCY_KEY_REQUIRED", "idempotency_key_required")],
        401: [(scenario_401, "AUTHENTICATION_REQUIRED", "missing_identity")],
        403: [
            (scenario_403, "AUTHORIZATION_DENIED", None),
            (scenario_403_booker, "AUTHORIZATION_DENIED", "booker_mismatch"),
        ],
        404: [
            (scenario_404, "NOT_FOUND", "resource_not_found"),
            (scenario_404_reservation, "NOT_FOUND", "reservation_not_found"),
        ],
        409: [
            (scenario_409_idempotency, "IDEMPOTENCY_CONFLICT", "idempotency_conflict"),
            (scenario_409_inactive, "RESOURCE_INACTIVE", "resource_inactive"),
            (scenario_409_outside, "OUTSIDE_AVAILABILITY", "outside_availability"),
            (scenario_409_no_window, "OUTSIDE_AVAILABILITY", "availability_not_found"),
            (scenario_409_conflict, "RESERVATION_CONFLICT", "reservation_conflict"),
            (
                scenario_409_state,
                "INVALID_STATE_TRANSITION",
                "invalid_state_transition",
            ),
        ],
        422: [
            (scenario_422, "VALIDATION_ERROR", "validation_error"),
            (scenario_422_naive, "VALIDATION_ERROR", "validation_error"),
        ],
        503: [(scenario_503, "DEPENDENCY_UNAVAILABLE", "authorization_unavailable")],
    }
    assert set(refused) == {str(status) for status in scenarios}
    produced_codes = set()
    for status, calls in scenarios.items():
        for scenario, expected_code, expected_reason in calls:
            response = scenario()
            assert response.status_code == status, (
                status,
                expected_code,
                response.text,
            )
            body = envelope_of(response)
            assert body["error"]["code"] == expected_code
            assert body["error"]["message"] == ERROR_CODES[expected_code]["message"]
            documented = {r.strip() for r in refused[str(status)].split(",")}
            assert body["error"]["details"]["reason"] in documented
            if expected_reason:
                assert body["error"]["details"]["reason"] == expected_reason
            produced_codes.add(expected_code)
    # Every published code except the schema-level one is produced by a
    # domain scenario above; INVALID_REQUEST is produced below.
    assert produced_codes == set(ERROR_CODES) - {"INVALID_REQUEST"}


def test_invalid_request_is_produced_for_a_body_the_contract_cannot_read():
    http = booking_harness().http()
    response = http.post(
        f"{BASE}/reservations",
        headers=auth(SUBJECT_A, "c-invalid"),
        json={"resource_id": "res_a1", "start_at": SLOT[0]},  # end_at missing
    )
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["reason"] == "malformed_request"


def test_owner_mismatch_is_a_documented_403():
    store = BookingStore()
    store.seed_demo()
    store.resources["res_foreign"] = OwnedResource(
        resource_id="res_foreign",
        tenant_id="ten_a",
        owner_component="identity",
        name="Foreign resource",
        status="ACTIVE",
        created_by="idn_human_a",
        created_at="2026-09-13T00:00:00+00:00",
        updated_at="2026-09-13T00:00:00+00:00",
    )
    deployment = booking_deployment(StubAuthorizationPort(ALLOW), store=store)
    response = TestClient(deployment.contract_app()).get(
        f"{BASE}/resources/res_foreign", headers=auth(SUBJECT_A)
    )
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == OwnDenyReason.OWNER_MISMATCH


def test_deny_from_the_decision_port_never_reaches_the_owned_data_operation():
    port = StubAuthorizationPort(
        DecisionAnswer(
            decision="DENY",
            reason="permission_not_granted",
            subject_id="idn_human_a",
            tenant_id="ten_a",
        )
    )
    deployment = booking_deployment(port)
    http = TestClient(deployment.contract_app())
    response = create_reservation(
        http, "res_a1", "deny-1", start_at=SLOT[0], end_at=SLOT[1]
    )
    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"] == "permission_not_granted"
    )
    assert deployment.store.reservations == {}
    assert port.calls[-1]["operation"] == "booking.reservations.create"
    assert port.calls[-1]["resource_tenant_id"] == "ten_a"


def test_nonauthoritative_answer_fails_closed():
    port = StubAuthorizationPort(
        DecisionAnswer(
            decision="ALLOW",
            reason="because",
            subject_id="idn_human_a",
            tenant_id="ten_a",
        )
    )
    deployment = booking_deployment(port)
    response = TestClient(deployment.contract_app()).get(
        f"{BASE}/resources/res_a1", headers=auth(SUBJECT_A)
    )
    assert response.status_code == 503
    assert envelope_of(response)["error"]["details"]["reason"] == (
        "authorization_unavailable"
    )


# ------------------------------------------------------------- response shapes
def test_response_shapes_match_openapi_schemas():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    schemas = openapi()["components"]["schemas"]

    resource = create_resource(http, "shape-res").json()
    assert set(resource) == set(schemas["Resource"]["properties"])
    window = create_availability(
        http,
        resource["resource_id"],
        "shape-avl",
        start_at=WINDOW_START,
        end_at=WINDOW_END,
    ).json()
    assert set(window) == set(schemas["Availability"]["properties"])
    listed = http.get(
        f"{BASE}/resources/{resource['resource_id']}/availability",
        headers=auth(SUBJECT_A),
    ).json()
    assert set(listed) == set(schemas["AvailabilityList"]["properties"])
    reservation = create_reservation(
        http, resource["resource_id"], "shape-rsv", start_at=SLOT[0], end_at=SLOT[1]
    ).json()
    assert set(reservation) == set(schemas["Reservation"]["properties"])
    assert set(reservation) == set(schemas["Reservation"]["required"])
    for record in (window, reservation):
        assert record["start_at"].endswith("+00:00")
        assert record["end_at"].endswith("+00:00")


# ---------------------------------------------------------------- configuration
def test_declared_configuration_schema_matches_the_loader():
    schema = contract()["configuration_schema"]
    assert schema["required"] == ["platform_id"]
    assert schema["additionalProperties"] is False
    BookingConfig.from_mapping({"platform_id": "plt_demo"})
    with pytest.raises(ConfigurationError):
        BookingConfig.from_mapping({"platform_id": "plt_demo", "undeclared": 1})
    with pytest.raises(ConfigurationError):
        BookingConfig.from_mapping({})


def test_dependency_isolation_is_declared_with_the_real_module_names():
    import importlib

    consumes = contract()["api"]["consumes"]
    assert len(consumes) == 2
    by_component = {c["component_id"]: c for c in consumes}
    assert by_component["authorization"]["operations"] == ["decide"]
    assert by_component["identity"]["operations"] == ["resolve_context"]
    for consumed in consumes:
        for key in ("local_port", "local_adapter", "local_answer_type"):
            module_name, _, attribute = consumed[key].rpartition(".")
            assert module_name.startswith("booking_service.")
            assert hasattr(importlib.import_module(module_name), attribute), consumed[
                key
            ]
    surface = contract()["api"]["consumer_surface"]
    assert surface["module"] == "booking_service.reader"
    assert surface["published_as"] == "BookingClient"
    for name in surface["internal_modules"]:
        importlib.import_module(name)
    for returned in surface["returns"]:
        module_name, _, attribute = returned.rpartition(".")
        assert hasattr(importlib.import_module(module_name), attribute), returned
