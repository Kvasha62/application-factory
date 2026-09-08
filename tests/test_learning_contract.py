"""Conformance of the machine-readable Learning contract (sections 8-14).

The contract is only worth something if it cannot drift from the
implementation. Every check here compares the published documents with live
objects: declared operations against the routes the application actually
serves, declared error codes against the codes the engine produces, declared
lifecycle states against the owned state machine, declared constraints
against the request schemas, and the declared idempotency requirements
against the commands the engine refuses to run without a key.

The published OpenAPI document is YAML, and this component adds no YAML
dependency to the platform: the small parser below reads exactly the
disciplined subset the document is written in (block maps, block sequences,
quoted/plain scalars, flow sequences).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from learning_service import COMPONENT_ID, COMPONENT_VERSION
from learning_service.contracts import ERROR_STATUS, PUBLISHED_ERROR_CODES
from learning_service.models import (
    CONTENT_STATES,
    COURSE_STATES,
    COURSE_TRANSITIONS,
    ENROLLMENT_STATES,
    SUBMISSION_STATES,
)
from tests.conftest import monolith_with_learning

CONTRACT = Path("components/learning/contract/component_contract.json")
OPENAPI = Path("components/learning/contract/openapi.yaml")

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

#: The seven state-changing commands of the slice — the exact set that must
#: demand an Idempotency-Key, no more and no less.
COMMAND_PATHS = {
    "/api/v1/learning/courses",
    "/api/v1/learning/courses/{course_id}/publish",
    "/api/v1/learning/courses/{course_id}/modules",
    "/api/v1/learning/modules/{module_id}/lessons",
    "/api/v1/learning/lessons/{lesson_id}/assignments",
    "/api/v1/learning/courses/{course_id}/enrollments",
    "/api/v1/learning/assignments/{assignment_id}/submissions",
}
READ_PATHS = {
    "/api/v1/learning/courses/{course_id}",
    "/api/v1/learning/assignments/{assignment_id}",
    "/api/v1/learning/courses/{course_id}/enrollments/me",
    "/api/v1/learning/submissions/{submission_id}",
}


# ----------------------------------------------------- the YAML subset parser
def _flow(inner: str) -> list[str]:
    inner = inner.strip()
    if not inner:
        return []
    items, buf, quote = [], "", False
    for ch in inner:
        if ch == '"':
            quote = not quote
            buf += ch
        elif ch == "," and not quote:
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    return [item.strip().strip('"') for item in items if item.strip()]


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].replace('\\"', '"')
    if raw.startswith("[") and raw.endswith("]"):
        return _flow(raw[1:-1])
    if raw == "{}":
        return {}
    if raw == "[]":
        return []
    if raw in ("true", "false"):
        return raw == "true"
    if raw in ("null", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(lines: list[str], i: int, indent: int) -> tuple[Any, int]:
    if lines[i].lstrip(" ").startswith("- "):
        return _sequence(lines, i, indent)
    return _mapping(lines, i, indent)


def _mapping(lines: list[str], i: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while i < len(lines):
        line = lines[i]
        if _indent(line) < indent:
            break
        assert _indent(line) == indent, f"unexpected indentation: {line!r}"
        stripped = line.strip()
        assert not stripped.startswith("- "), f"unexpected sequence item: {line!r}"
        key, _, rest = stripped.partition(":")
        key = key.strip().strip('"')
        rest = rest.strip()
        if rest:
            result[key] = _scalar(rest)
            i += 1
            continue
        i += 1
        if i < len(lines) and _indent(lines[i]) > indent:
            result[key], i = _block(lines, i, _indent(lines[i]))
        else:
            result[key] = None
    return result, i


def _sequence(lines: list[str], i: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while i < len(lines):
        line = lines[i]
        if _indent(line) < indent or not line.lstrip(" ").startswith("- "):
            break
        assert _indent(line) == indent, f"unexpected indentation: {line!r}"
        item = line.strip()[2:].strip()
        if ":" in item and not item.startswith('"'):
            # A single-line mapping item: `- $ref: "#/..."`.
            key, _, rest = item.partition(":")
            entry = {key.strip().strip('"'): _scalar(rest.strip())}
            result.append(entry)
            i += 1
        else:
            result.append(_scalar(item))
            i += 1
    return result, i


def load_openapi() -> dict[str, Any]:
    document, _ = _mapping(
        [line for line in OPENAPI.read_text(encoding="utf-8").splitlines() if line.strip()],
        0,
        0,
    )
    return document


def contract() -> dict[str, Any]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def declared_operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(path, method) -> operation, for the learning API paths."""
    operations: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in document["paths"].items():
        for method in ("get", "post"):
            if isinstance(item.get(method), dict):
                operations[(path, method)] = item[method]
    return operations


def app_operations(app: Any) -> set[tuple[str, str, str]]:
    """(path, method, operation_id) of the routes the application serves."""
    documentation = {"/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json"}
    result = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path in documentation:
            continue
        methods = getattr(route, "methods", None) or set()
        operation_id = getattr(route, "operation_id", None)
        for method in methods:
            if method in ("GET", "POST"):
                result.add((path, method.lower(), operation_id or ""))
    return result


def instance() -> Any:
    return monolith_with_learning()


# ------------------------------------------------------------- the documents
def test_component_contract_declares_the_required_minimum():
    data = contract()
    assert not REQUIRED - set(data)
    assert data["component_id"] == COMPONENT_ID == "learning"
    assert data["component_version"] == COMPONENT_VERSION == "0.1.0"
    assert data["class"] == "business_system"
    assert data["maturity_level"] == "level_0_modular_monolith"


def test_component_contract_versions_the_api_as_required():
    """Section 13: component = learning, version = 0.1.0, API = v1."""
    api = contract()["api"]
    assert api["base_path"] == "/api/v1/learning"
    assert api["supported_majors"] == ["v1"]
    assert api["openapi"] == "components/learning/contract/openapi.yaml"
    document = load_openapi()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "0.1.0"
    marker = document["info"]["x-component"]
    assert marker["component"] == "learning"
    assert marker["version"] == "0.1.0"
    assert marker["api"] == "v1"


def test_published_operations_are_exactly_the_slice():
    """Section 8: only the eleven published operations exist — no extras."""
    document = load_openapi()
    operations = declared_operations(document)
    api_paths = {path for path, _ in operations}
    assert api_paths == COMMAND_PATHS | READ_PATHS | {"/health", "/ready"}
    for path in COMMAND_PATHS:
        assert "post" in operations[(path, "post")] or (path, "post") in operations
    for path in READ_PATHS:
        assert (path, "get") in operations


def test_served_routes_match_the_published_document():
    """The live application serves exactly what the contract declares."""
    app = instance().learning_app
    served = app_operations(app)
    document = load_openapi()
    declared = {
        (path, method, operation["operationId"])
        for (path, method), operation in declared_operations(document).items()
    }
    assert served == declared


def test_no_generic_crud_operation_exists_anywhere():
    """Invariant 12: no PUT, PATCH or DELETE — in the contract or the server."""
    document = load_openapi()
    for path, item in document["paths"].items():
        for method in ("put", "patch", "delete"):
            assert method not in item, f"{method.upper()} declared for {path}"
    app = instance().learning_app
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        assert not methods & {"PUT", "PATCH", "DELETE"}, getattr(route, "path", "")


def test_every_command_requires_the_idempotency_key_header():
    """Section 11: the key is a required header of exactly the seven commands."""
    document = load_openapi()
    parameters = document["components"]["parameters"]
    key_parameter = parameters["IdempotencyKey"]
    assert key_parameter["required"] is True
    assert key_parameter["name"] == "Idempotency-Key"
    schema = key_parameter["schema"]
    assert schema["minLength"] == 1 and schema["maxLength"] == 128

    for path in COMMAND_PATHS:
        operation = declared_operations(document)[(path, "post")]
        refs = [p["$ref"].rsplit("/", 1)[-1] for p in operation.get("parameters", [])]
        assert "IdempotencyKey" in refs, f"{path} does not require Idempotency-Key"
    for path in READ_PATHS:
        operation = declared_operations(document)[(path, "get")]
        refs = [p["$ref"].rsplit("/", 1)[-1] for p in operation.get("parameters", [])]
        assert "IdempotencyKey" not in refs, f"read {path} must not require a key"


def test_every_operation_declares_authentication_and_ids():
    """Sections 9.2/9.3: bearer authentication; diagnostic headers may be supplied."""
    document = load_openapi()
    for (path, method), operation in declared_operations(document).items():
        if path in ("/health", "/ready"):
            assert operation.get("security") == []
            continue
        assert operation.get("security") == [{"bearerAuth": []}], f"{path} {method}"
        names = {
            parameter.get("$ref", "").rsplit("/", 1)[-1]
            for parameter in operation.get("parameters", [])
        }
        assert "RequestId" in names and "CorrelationId" in names, f"{path} {method}"
    assert "bearerAuth" in document["components"]["securitySchemes"]
    scheme = document["components"]["securitySchemes"]["bearerAuth"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"


def test_error_envelope_is_the_published_shape():
    """Section 12: the envelope, its closed code set and its nullability."""
    document = load_openapi()
    schemas = document["components"]["schemas"]
    envelope = schemas["ErrorEnvelope"]
    assert envelope["required"] == ["error"]
    body = schemas["ErrorBody"]
    assert body["required"] == ["code", "message", "details"]
    codes = schemas["ErrorCodeEnum"]["enum"]
    assert set(codes) == set(PUBLISHED_ERROR_CODES)
    assert set(codes) == {
        "AUTHENTICATION_REQUIRED",
        "AUTHORIZATION_DENIED",
        "TENANT_MISMATCH",
        "RESOURCE_NOT_FOUND",
        "RESOURCE_OWNER_MISMATCH",
        "INVALID_STATE_TRANSITION",
        "COURSE_NOT_PUBLISHED",
        "ASSIGNMENT_NOT_PUBLISHED",
        "ENROLLMENT_REQUIRED",
        "DUPLICATE_ENROLLMENT",
        "IDEMPOTENCY_KEY_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "VALIDATION_ERROR",
        "DEPENDENCY_UNAVAILABLE",
    }
    # request_id/correlation_id are the caller's context, echoed or null.
    assert schemas["ErrorEnvelope"]["properties"]["request_id"]["type"] == ["string", "null"]
    assert schemas["ErrorEnvelope"]["properties"]["correlation_id"]["type"] == ["string", "null"]


def test_declared_error_codes_match_declared_statuses_and_the_implementation():
    """One code — one status, and every documented code is documented on its operation."""
    for code, status in ERROR_STATUS.items():
        if code in {"AUTHENTICATION_REQUIRED", "AUTHORIZATION_DENIED", "TENANT_MISMATCH",
                    "RESOURCE_NOT_FOUND", "VALIDATION_ERROR", "DEPENDENCY_UNAVAILABLE"}:
            assert status in (401, 403, 404, 422, 503)
    document = load_openapi()
    for (path, method), operation in declared_operations(document).items():
        if path in ("/health", "/ready"):
            continue
        documented = set(operation["x-error-codes"])
        assert documented <= set(PUBLISHED_ERROR_CODES), f"{path}: unknown code declared"
        statuses = {str(status) for status in operation["responses"]}
        for code in documented:
            assert str(ERROR_STATUS[code]) in statuses, (
                f"{path} {method}: {code} maps to {ERROR_STATUS[code]}, not declared"
            )
        if (path, method) in COMMAND_PATHS | {(p, "post") for p in COMMAND_PATHS}:
            assert {"IDEMPOTENCY_KEY_REQUIRED", "IDEMPOTENCY_CONFLICT"} <= documented, path


def test_declared_lifecycle_matches_the_owned_state_machine():
    """Section 10.3/13: closed enums, and only one transition command."""
    document = load_openapi()
    schemas = document["components"]["schemas"]
    assert schemas["CourseStatus"]["enum"] == list(COURSE_STATES)
    assert schemas["ContentStatus"]["enum"] == list(CONTENT_STATES)
    assert schemas["EnrollmentStatus"]["enum"] == list(ENROLLMENT_STATES)
    assert schemas["SubmissionStatus"]["enum"] == list(SUBMISSION_STATES)
    assert list(COURSE_TRANSITIONS) == ["publish"]
    assert COURSE_TRANSITIONS["publish"] == ("DRAFT", "PUBLISHED")
    declared = contract()["api"]["lifecycle"]
    assert declared["course"]["commands"]["publish"] == ["DRAFT", "PUBLISHED"]


def test_request_schemas_declare_the_field_constraints():
    """Section 10: every constraint of the normative request bodies."""
    schemas = load_openapi()["components"]["schemas"]

    def props(name: str) -> dict[str, Any]:
        schema = schemas[name]
        assert schema["additionalProperties"] is False
        return schema["properties"]

    course = props("CreateCourseRequest")
    assert schemas["CreateCourseRequest"]["required"] == ["title", "description"]
    assert course["title"]["minLength"] == 1 and course["title"]["maxLength"] == 200
    assert course["description"]["maxLength"] == 5000

    module = props("CreateModuleRequest")
    assert module["position"]["minimum"] == 1 and module["position"]["type"] == "integer"

    lesson = props("CreateLessonRequest")
    assert lesson["content"]["maxLength"] == 20000

    assignment = props("CreateAssignmentRequest")
    assert assignment["instructions"]["maxLength"] == 20000

    submission = props("CreateSubmissionRequest")
    assert submission["attempt"]["minimum"] == 1
    assert submission["content"]["minLength"] == 1
    assert submission["content"]["maxLength"] == 50000

    empty = schemas["EmptyCommand"]
    assert empty["additionalProperties"] is False and empty["properties"] == {}


def test_representation_schemas_are_exactly_the_published_fields():
    """No endpoint may serve an undocumented field; ownership fields are facts."""
    schemas = load_openapi()["components"]["schemas"]
    assert set(schemas["Course"]["required"]) == {
        "course_id", "tenant_id", "title", "description", "status",
        "created_by", "created_at", "updated_at",
    }
    assert set(schemas["Assignment"]["required"]) == {
        "assignment_id", "lesson_id", "tenant_id", "title", "instructions", "status",
    }
    assert set(schemas["Enrollment"]["required"]) == {
        "enrollment_id", "tenant_id", "course_id", "student_identity_id", "status", "created_at",
    }
    assert set(schemas["Submission"]["required"]) == {
        "submission_id", "tenant_id", "assignment_id", "student_identity_id",
        "attempt", "content", "status", "created_at", "updated_at",
    }
    # The request schemas declare none of the server-controlled fields.
    for name, forbidden in {
        "CreateCourseRequest": {"course_id", "tenant_id", "status", "created_by"},
        "CreateSubmissionRequest": {"submission_id", "tenant_id", "student_identity_id", "status"},
    }.items():
        assert not forbidden & set(schemas[name]["properties"]), name


def test_component_contract_declares_data_ownership_and_boundaries():
    data = contract()
    ownership = data["data_ownership"]
    assert ownership["owner"] == "learning"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    for dataset in ("courses", "modules", "lessons", "assignments", "enrollments", "submissions"):
        assert scopes[dataset] == "tenant-scoped"
    assert scopes["access_audit"] == "platform-scoped"
    assert data["authz"]["enforcement_boundary"] == "data_owner"
    assert data["authz"]["decision_source"] == "authorization"
    assert data["authz"]["default_decision"] == "DENY"
    assert data["authz"]["tenant_context_source"] == "identity"
    assert data["authn"]["human"] == "delegated_to_identity"
    assert data["events"]["status"] == "declared_only"
    assert data["events"]["published"] == []
    assert data["data_export_cdc"]["streams"] == []
    assert data["api"]["no_generic_crud"] == {
        "put": False,
        "patch": False,
        "delete": False,
        "note": data["api"]["no_generic_crud"]["note"],
    }


def test_component_contract_declares_the_consumed_dependencies():
    data = contract()
    consumed = {item["component_id"]: item for item in data["api"]["consumes"]}
    assert set(consumed) == {"identity", "authorization", "idempotency_guard"}
    assert consumed["identity"]["local_port"] == "learning_service.ports.IdentityContextPort"
    assert consumed["authorization"]["local_port"] == "learning_service.ports.AuthorizationPort"
    assert consumed["idempotency_guard"]["consumer_surface"] == "idempotency.guard.IdempotencyGuard"
    # The chain is published and ordered.
    assert data["resource_boundary"]["enforcement_chain"] == [
        "identity_context",
        "effective_tenant",
        "ownership_boundary",
        "authorization_decision",
        "owned_data_operation",
    ]


def test_idempotency_contract_binds_the_key_to_everything():
    """Section 11: the key is bound by identity, tenant, operation, target, fingerprint."""
    idempotency = contract()["idempotency"]
    assert set(idempotency["required_commands"]) == {
        "create_course",
        "publish_course",
        "create_module",
        "create_lesson",
        "create_assignment",
        "enroll_in_course",
        "create_submission",
    }
    assert set(idempotency["key_binding"]) == {
        "identity",
        "effective tenant",
        "operation",
        "target/resource",
        "request fingerprint",
    }
    assert idempotency["denial_creates_no_record"] is True
    assert "no_bypass" in idempotency


def test_published_health_and_readiness_exist():
    app = instance().learning_app
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
