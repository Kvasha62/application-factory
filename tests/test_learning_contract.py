"""Conformance of the machine-readable Component Contract of SCS-001.

Every check compares the published documents with live objects: declared
operations against the routes the application actually serves (resolving
the `servers: /api/v1/learning` prefix plus relative OpenAPI paths),
declared reason codes against the reasons the engine produces, the
declared enforcement chain against the engine's chain, the approved error
envelope against real refusals, declared configuration against the loader,
and the declared refusal semantics against real requests. The Slice 1
single read and the Slice 2 teacher discovery list are the read surface;
the Slice 3 review command is the single state-changing operation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from learning_service import COMPONENT_ID, COMPONENT_VERSION
from learning_service.api import ERROR_CODES, error_code_for
from learning_service.config import LearningConfig
from learning_service.consumed import DecisionAnswer
from learning_service.contracts import (
    OWN_DENY_REASONS,
    PUBLISHED_DENY_REASONS,
    OwnDenyReason,
)
from learning_service.engine import ENFORCEMENT_CHAIN
from learning_service.errors import ConfigurationError
from learning_service.models import OwnedAssignment, OwnedSubmission
from learning_service.store import ASSIGNMENT_STATES, SUBMISSION_STATES, LearningStore
from tests.test_learning_slice1 import TEACHER_A, envelope_of, learning_harness

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

CONTRACT = Path("components/learning/contract/component_contract.json")
OPENAPI = Path("components/learning/contract/openapi.yaml")

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
    Relative learning paths resolve under the servers base; health paths
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
    assert data["component_id"] == COMPONENT_ID == "learning"
    assert data["component_version"] == COMPONENT_VERSION
    assert data["class"] == "business_system"
    assert data["scs_id"] == "SCS-001"
    assert data["maturity_level"] == "level_0_modular_monolith"


def test_contract_declares_the_required_shapes():
    data = contract()
    authz = data["authz"]
    assert authz["enforcement_boundary"] == "resource_owner"
    assert authz["decision_source"] == "authorization"
    assert authz["default_decision"] == "DENY"
    assert authz["tenant_context_source"] == "identity"
    assert authz["operations_guarded"] == [
        "learning.submissions.read",
        "learning.submissions.list",
        "learning.submissions.review",
        "learning.courses.create",
        "learning.modules.create",
        "learning.lessons.create",
        "learning.assignments.create",
        "learning.courses.publish",
        "learning.courses.archive",
        "learning.courses.read",
        "learning.courses.read_unpublished",
    ]

    ownership = data["data_ownership"]
    assert ownership["owner"] == "learning"
    assert ownership["logical_schema"] == "learning"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    assert scopes["courses"] == "tenant-scoped"
    assert scopes["modules"] == "tenant-scoped"
    assert scopes["lessons"] == "tenant-scoped"
    assert scopes["assignments"] == "tenant-scoped"
    assert scopes["submissions"] == "tenant-scoped"
    assert scopes["access_audit"] == "platform-scoped"

    assert data["events"]["status"] == "declared_only"
    assert data["events"]["published"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []

    assert data["api"]["supported_majors"] == ["v1"]
    model = data["api"]["enforcement_model"]
    assert model["submission_states"] == ["DRAFT", "SUBMITTED"]
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
    idempotency = by_component["idempotency_guard"]
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
    assert "url: /api/v1/learning" in text
    paths_section = text.split("paths:", 1)[1]
    assert "/submissions/{submission_id}:" in paths_section
    assert "/assignments/{assignment_id}/submissions:" in paths_section
    assert "/api/v1/learning/submissions" not in paths_section
    assert "/api/v1/learning/assignments" not in paths_section


def test_published_api_matches_the_implementation_in_both_directions():
    data = contract()
    assert Path(data["api"]["openapi"]).read_text(encoding="utf-8") == openapi_text()
    assert data["api"]["base_path"] == "/api/v1/learning"
    assert data["api"]["servers"] == "/api/v1/learning"
    assert data["api"]["supported_majors"] == ["v1"]

    servers_base, declared = published_operations(openapi_text())
    assert servers_base == "/api/v1/learning"
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (path, method.lower())
        for route in learning_harness().learning.contract_app().routes
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
    # Exactly the two submission reads, the review command, the seven
    # content-authoring operations plus health/readiness.
    assert contract_operations == {
        ("/api/v1/learning/submissions/{submission_id}", "get"),
        ("/api/v1/learning/assignments/{assignment_id}/submissions", "get"),
        ("/api/v1/learning/submissions/{submission_id}/review", "post"),
        ("/api/v1/learning/courses", "post"),
        ("/api/v1/learning/courses/{course_id}", "get"),
        ("/api/v1/learning/courses/{course_id}/modules", "post"),
        ("/api/v1/learning/modules/{module_id}/lessons", "post"),
        ("/api/v1/learning/lessons/{lesson_id}/assignments", "post"),
        ("/api/v1/learning/courses/{course_id}/publish", "post"),
        ("/api/v1/learning/courses/{course_id}/archive", "post"),
    }


def test_declared_enforcement_chain_and_model_match_the_engine():
    data = contract()
    model = data["api"]["enforcement_model"]
    assert tuple(model["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert tuple(data["learning_boundary"]["enforcement_chain"]) == ENFORCEMENT_CHAIN
    assert model["default_outcome"] == "deny"
    assert model["owned_data_operations"] == [
        "serve_submission",
        "list_submissions_for_assignment",
        "apply_review",
        "create_course",
        "create_module",
        "create_lesson",
        "create_assignment",
        "publish_course",
        "archive_course",
        "course_hierarchy",
    ]
    assert set(model["own_deny_reasons"]) == OWN_DENY_REASONS
    assert set(model["submission_states"]) == set(SUBMISSION_STATES)
    assert set(model["assignment_states"]) == set(ASSIGNMENT_STATES)
    assert data["learning_boundary"]["resource_model"] == [
        "submission_id",
        "assignment_id",
        "student_identity_id",
        "attempt",
        "content",
        "status",
        "created_at",
        "updated_at",
        "reviewed_by",
        "reviewed_at",
    ]


def test_declared_refusal_vocabulary_matches_the_published_reasons():
    refused = contract()["api"]["refusal_semantics"]["refused"]
    declared = set()
    for reasons in refused.values():
        declared.update(reason.strip() for reason in reasons.split(","))
    assert declared == PUBLISHED_DENY_REASONS
    assert "permission_not_granted" in declared
    assert "resource_tenant_mismatch" in declared
    assert "assignment_unknown" in declared


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
        from tests.test_learning_slice3 import review_harness

        harness = review_harness()
        return harness.http().post(
            "/api/v1/learning/submissions/sub_a1_1/review",
            headers={"authorization": f"Bearer {TEACHER_A}"},
        )

    def scenario_401():
        return learning_harness().http().get("/api/v1/learning/submissions/sub_a1_1")

    def scenario_401_list():
        return (
            learning_harness()
            .http()
            .get("/api/v1/learning/assignments/asg_a1/submissions")
        )

    def scenario_403():
        return (
            learning_harness()
            .http()
            .get(
                "/api/v1/learning/submissions/sub_b1_1",
                headers={"authorization": f"Bearer {TEACHER_A}"},
            )
        )

    def scenario_403_list():
        return (
            learning_harness()
            .http()
            .get(
                "/api/v1/learning/assignments/asg_b1/submissions",
                headers={"authorization": f"Bearer {TEACHER_A}"},
            )
        )

    def scenario_404():
        return (
            learning_harness()
            .http()
            .get(
                "/api/v1/learning/submissions/missing",
                headers={"authorization": f"Bearer {TEACHER_A}"},
            )
        )

    def scenario_409():
        from tests.test_learning_slice3 import review_harness

        harness = review_harness()
        return harness.http().post(
            "/api/v1/learning/submissions/sub_a1_2/review",
            headers={
                "authorization": f"Bearer {TEACHER_A}",
                "idempotency-key": "contract-409",
            },
        )

    def scenario_404_list():
        return (
            learning_harness()
            .http()
            .get(
                "/api/v1/learning/assignments/missing/submissions",
                headers={"authorization": f"Bearer {TEACHER_A}"},
            )
        )

    def scenario_503():
        from tests.test_learning_boundary import (
            StubAuthorizationPort,
            learning_deployment,
        )

        deployment = learning_deployment(StubAuthorizationPort(RuntimeError("boom")))
        http = TestClient(deployment.contract_app())
        return http.get(
            "/api/v1/learning/submissions/sub_a1_1",
            headers={"authorization": f"Bearer {TEACHER_A}"},
        )

    def scenario_503_list():
        from tests.test_learning_boundary import (
            StubAuthorizationPort,
            learning_deployment,
        )

        deployment = learning_deployment(StubAuthorizationPort(RuntimeError("boom")))
        http = TestClient(deployment.contract_app())
        return http.get(
            "/api/v1/learning/assignments/asg_a1/submissions",
            headers={"authorization": f"Bearer {TEACHER_A}"},
        )

    def scenario_422():
        from tests.test_learning_authoring import authoring_harness

        harness = authoring_harness()
        return harness.http().post(
            "/api/v1/learning/courses",
            headers={
                "authorization": f"Bearer {TEACHER_A}",
                "idempotency-key": "contract-422",
            },
            json={"title": "   ", "description": ""},
        )

    scenarios = {
        400: [scenario_400],
        401: [scenario_401, scenario_401_list],
        403: [scenario_403, scenario_403_list],
        404: [scenario_404, scenario_404_list],
        409: [scenario_409],
        422: [scenario_422],
        503: [scenario_503, scenario_503_list],
    }
    assert set(refused) == {str(status) for status in scenarios}
    expected_codes = {
        400: "IDEMPOTENCY_KEY_REQUIRED",
        401: "AUTHENTICATION_REQUIRED",
        403: "AUTHORIZATION_DENIED",
        404: "NOT_FOUND",
        409: "INVALID_STATE_TRANSITION",
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
    from tests.test_learning_boundary import StubAuthorizationPort, learning_deployment

    store = LearningStore()
    store.seed_demo()
    store.submissions["sub_foreign"] = OwnedSubmission(
        submission_id="sub_foreign",
        assignment_id="asg_a1",
        tenant_id="ten_a",
        student_identity_id="idn_human_c",
        attempt=1,
        content={},
        status="SUBMITTED",
        created_at="2026-09-08T00:00:00+00:00",
        updated_at="2026-09-08T00:00:00+00:00",
        owner_component="identity",
    )
    deployment = learning_deployment(StubAuthorizationPort(ALLOW), store=store)
    response = TestClient(deployment.contract_app()).get(
        "/api/v1/learning/submissions/sub_foreign",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == OwnDenyReason.OWNER_MISMATCH


def test_foreign_assignment_is_a_documented_403_for_list():
    from tests.test_learning_boundary import StubAuthorizationPort, learning_deployment

    store = LearningStore()
    store.seed_demo()
    store.assignments["asg_foreign"] = OwnedAssignment(
        assignment_id="asg_foreign",
        tenant_id="ten_a",
        owner_component="identity",
        status="PUBLISHED",
        created_at="2026-09-08T00:00:00+00:00",
        updated_at="2026-09-08T00:00:00+00:00",
    )
    deployment = learning_deployment(StubAuthorizationPort(ALLOW), store=store)
    response = TestClient(deployment.contract_app()).get(
        "/api/v1/learning/assignments/asg_foreign/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == OwnDenyReason.OWNER_MISMATCH


def test_submission_response_shape_matches_openapi_schema():
    harness = learning_harness()
    response = harness.http().get(
        "/api/v1/learning/submissions/sub_a1_1",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert response.status_code == 200
    item = response.json()
    text = openapi_text()
    for field in (
        "submission_id",
        "assignment_id",
        "student_identity_id",
        "attempt",
        "content",
        "status",
        "created_at",
        "updated_at",
        "reviewed_by",
        "reviewed_at",
    ):
        assert field in text
        assert field in item


def test_submission_list_response_shape_matches_openapi_schema():
    harness = learning_harness()
    response = harness.http().get(
        "/api/v1/learning/assignments/asg_a1/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items"}
    assert "SubmissionList" in openapi_text()
    for item in body["items"]:
        for field in (
            "submission_id",
            "assignment_id",
            "student_identity_id",
            "attempt",
            "content",
            "status",
            "created_at",
            "updated_at",
        ):
            assert field in item


# ---------------------------------------------------------------- configuration
def test_declared_configuration_schema_matches_the_loader():
    schema = contract()["configuration_schema"]
    assert schema["required"] == ["platform_id"]
    assert schema["additionalProperties"] is False

    LearningConfig.from_mapping({"platform_id": "plt_demo"})
    with pytest.raises(ConfigurationError):
        LearningConfig.from_mapping({"platform_id": "plt_demo", "undeclared": 1})
    with pytest.raises(ConfigurationError):
        LearningConfig.from_mapping({})


def test_dependency_isolation_is_declared_with_the_real_module_names():
    consumes = contract()["api"]["consumes"]
    assert len(consumes) == 2
    by_component = {c["component_id"]: c for c in consumes}
    consumed = by_component["authorization"]
    assert consumed["local_port"] == "learning_service.ports.AuthorizationPort"
    assert (
        consumed["local_adapter"]
        == "learning_service.adapters.AuthorizationDecisionAdapter"
    )
    assert consumed["local_answer_type"] == "learning_service.consumed.DecisionAnswer"
    assert consumed["operations"] == ["decide"]
    identity = by_component["identity"]
    assert identity["local_port"] == "learning_service.ports.TenantContextPort"
    assert identity["local_adapter"] == "learning_service.adapters.TenantContextAdapter"
    assert (
        identity["local_answer_type"] == "learning_service.consumed.TenantContextAnswer"
    )
    assert identity["operations"] == ["resolve_context"]


# ------------------------------------------------------------ review contract
def test_review_operation_is_declared_in_openapi():
    text = openapi_text()
    assert "/submissions/{submission_id}/review:" in text
    assert "operationId: reviewSubmission" in text
    assert "Idempotency-Key" in text
    # The review command must declare the idempotency requirement.
    assert "learning.submissions.review" in text


def test_review_operation_is_declared_in_the_component_contract():
    data = contract()
    operations = {
        (operation["method"], operation["path"]): operation
        for operation in data["api"]["operations"]
    }
    review = operations[("POST", "/api/v1/learning/submissions/{submission_id}/review")]
    assert review["authorization_operation"] == "learning.submissions.review"
    assert review["idempotency"] == "required"
    assert review["openapi_path"] == "/submissions/{submission_id}/review"


def test_submission_lifecycle_remains_draft_submitted():
    data = contract()
    states = data["api"]["enforcement_model"]["submission_states"]
    assert states == ["DRAFT", "SUBMITTED"]
    for forbidden in ("REVIEWED", "EVALUATED", "REJECTED"):
        assert forbidden not in states
        assert forbidden not in SUBMISSION_STATES
    # Review is not a lifecycle transition: the transitions map stays empty.
    assert data["api"]["enforcement_model"]["transitions"] == {}


def test_no_review_entity_and_no_grading_features_exist():
    import ast
    from dataclasses import fields

    forbidden_classes = {
        "Review",
        "Evaluation",
        "Grade",
        "Feedback",
        "LearningResult",
        "Comment",
    }
    package = Path("src/learning_service")
    class_names: set[str] = set()
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_names.add(node.name)
    assert forbidden_classes.isdisjoint(class_names), class_names

    forbidden_fields = {"grade", "score", "points", "feedback", "comment", "evaluation"}
    submission_fields = {field.name for field in fields(OwnedSubmission)}
    assert forbidden_fields.isdisjoint(submission_fields), submission_fields


def test_no_forbidden_endpoints_exist_in_openapi():
    text = openapi_text()
    for forbidden in (
        "/unreview",
        "/reset-review",
        "/evaluate",
        "/grade",
        "/feedback",
        "/comments",
    ):
        assert forbidden not in text, forbidden


def test_review_semantics_are_declared_as_fact_not_grading():
    data = contract()
    review = data["api"]["enforcement_model"]["review"]
    assert review["no_review_entity"].startswith("no Review")
    assert "reviewed_by" in review["semantics"]
    assert review["state_rule"]  # SUBMITTED only


def test_contract_declares_the_review_fact_is_immutable():
    data = contract()
    review = data["api"]["enforcement_model"]["review"]
    assert "immutability" in review
    assert "immutable" in review["immutability"]
    assert "immutable" in review["semantics"]
    # The immutability is expressed without a new lifecycle state.
    assert data["api"]["enforcement_model"]["submission_states"] == [
        "DRAFT",
        "SUBMITTED",
    ]


def test_openapi_declares_the_review_fact_is_immutable():
    text = openapi_text()
    assert "неизменяем" in text
    assert "ALREADY_REVIEWED" in text
    assert "already_reviewed" in text
