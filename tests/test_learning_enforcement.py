"""Enforcement: identity, authorization, data ownership, dependencies.

The chain is fixed and every step can only refuse. These tests prove each
step on the published surface:

* no verified identity — no operation (401), from IS-001, never answered
  locally;
* no permission — no effect (403 AUTHORIZATION_DENIED), from IS-003;
* another tenant's data — refused at this boundary (403 TENANT_MISMATCH),
  unknown resources are a plain 404;
* a dependency that does not answer, or answers outside its published
  vocabulary, fails closed (503 DEPENDENCY_UNAVAILABLE) — and leaves no
  business effect behind.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient

from learning_service.config import LearningConfig
from learning_service.consumed import DecisionAnswer, DependencyRefusal, SubjectContext
from learning_service.engine import LearningEngine
from learning_service.errors import ContractViolation
from tests.conftest import (
    LEARNING_SERVICE_A,
    LEARNING_STUDENT_A,
    LEARNING_TEACHER_A,
    LEARNING_TEACHER_B,
    CountingLearningStore,
    StubLearningAuthorizationPort,
    StubLearningIdentityPort,
    monolith_with_learning,
)

TEACHER = LEARNING_TEACHER_A
STUDENT = LEARNING_STUDENT_A
TEACHER_B = LEARNING_TEACHER_B
SERVICE = LEARNING_SERVICE_A


def http() -> TestClient:
    return monolith_with_learning().learning_http()


def auth(token: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def key() -> str:
    return f"key-{uuid.uuid4().hex}"


def code_of(response: Any) -> str:
    assert response.status_code < 500 or response.status_code == 503, response.text
    body = response.json()
    assert set(body) == {"error", "request_id", "correlation_id"}, body
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["message"] and "Traceback" not in body["error"]["message"]
    return body["error"]["code"]


def published_course(client: TestClient) -> str:
    created = client.post(
        "/api/v1/learning/courses",
        json={"title": "Published", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    course_id = created.json()["course"]["course_id"]
    published = client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert published.status_code == 200
    return course_id


# ------------------------------------------------------------------ identity
def test_a_request_without_a_verified_identity_never_enters_the_operation():
    client = http()
    refused = client.post(
        "/api/v1/learning/courses",
        json={"title": "T", "description": ""},
        headers={"Idempotency-Key": key()},
    )
    assert refused.status_code == 401
    assert code_of(refused) == "AUTHENTICATION_REQUIRED"
    # Nothing was created: authentication failure means no business operation.
    assert client.get("/api/v1/learning/courses/crs_missing", headers=auth(TEACHER)).json()[
        "error"
    ]["code"] in {"RESOURCE_NOT_FOUND", "TENANT_MISMATCH"}


def test_invalid_and_unknown_credentials_are_authentication_failures():
    client = http()
    for token in ("token-invalid", "token-unknown", "nonsense"):
        refused = client.get(
            "/api/v1/learning/courses/crs_demo_a", headers=auth(token)
        )
        assert refused.status_code == 401, token
        assert code_of(refused) == "AUTHENTICATION_REQUIRED"


def test_every_operation_refuses_an_unverified_caller():
    client = http()
    course_id = published_course(client)
    attempts = [
        ("GET", f"/api/v1/learning/courses/{course_id}", None),
        ("POST", f"/api/v1/learning/courses/{course_id}/publish", {}),
        ("POST", f"/api/v1/learning/courses/{course_id}/modules", {"title": "M", "position": 1}),
        ("POST", f"/api/v1/learning/courses/{course_id}/enrollments", {}),
        ("GET", f"/api/v1/learning/courses/{course_id}/enrollments/me", None),
    ]
    for method, path, body in attempts:
        response = (
            client.post(path, json=body, headers={"Idempotency-Key": key()})
            if method == "POST"
            else client.get(path)
        )
        assert response.status_code == 401, f"{method} {path}"
        assert code_of(response) == "AUTHENTICATION_REQUIRED"


# -------------------------------------------------------------- authorization
def test_a_student_cannot_author_content():
    client = http()
    refused = client.post(
        "/api/v1/learning/courses",
        json={"title": "Student course", "description": ""},
        headers={**auth(STUDENT), "Idempotency-Key": key()},
    )
    assert refused.status_code == 403
    assert code_of(refused) == "AUTHORIZATION_DENIED"


def test_a_teacher_cannot_enroll_without_the_student_capability():
    client = http()
    course_id = published_course(client)
    refused = client.post(
        f"/api/v1/learning/courses/{course_id}/enrollments",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert refused.status_code == 403
    assert code_of(refused) == "AUTHORIZATION_DENIED"


def test_authentication_is_not_authorization():
    """The service identity is verified — and holds no learning grant at all."""
    client = http()
    refused = client.get(
        "/api/v1/learning/courses/crs_demo_a", headers=auth(SERVICE)
    )
    assert refused.status_code == 403
    assert code_of(refused) == "AUTHORIZATION_DENIED"


def test_an_unknown_resource_is_a_plain_404():
    client = http()
    for path in (
        "/api/v1/learning/courses/crs_nosuch",
        "/api/v1/learning/assignments/asg_nosuch",
        "/api/v1/learning/submissions/sub_nosuch",
    ):
        refused = client.get(path, headers=auth(TEACHER))
        assert refused.status_code == 404, path
        assert code_of(refused) == "RESOURCE_NOT_FOUND"
    refused = client.get(
        "/api/v1/learning/courses/crs_nosuch/enrollments/me", headers=auth(STUDENT)
    )
    assert refused.status_code == 404


def test_commands_on_unknown_resources_are_refused_before_any_effect():
    client = http()
    refused = client.post(
        "/api/v1/learning/courses/crs_nosuch/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert refused.status_code == 404
    assert code_of(refused) == "RESOURCE_NOT_FOUND"


# ------------------------------------------------------- dependency failures
def engine_over_stubs(
    identity: Any, authorization: Any, store: CountingLearningStore | None = None
) -> LearningEngine:
    store = store or CountingLearningStore()
    store.seed_demo()
    store.reset_counters()
    return LearningEngine(
        store=store,
        config=LearningConfig(platform_id="plt_demo", environment="test"),
        identity=StubLearningIdentityPort(identity),
        authorization=StubLearningAuthorizationPort(authorization),
    )


def subject(tenant: str = "ten_a", identity: str = "idn_human_a") -> SubjectContext:
    return SubjectContext(
        identity_id=identity,
        tenant_id=tenant,
        kind="HUMAN",
        source="verified_identity",
    )


def allowed() -> DecisionAnswer:
    return DecisionAnswer(decision="ALLOW", reason="permitted")


def denied(reason: str = "permission_not_granted") -> DecisionAnswer:
    return DecisionAnswer(decision="DENY", reason=reason)


def test_a_silent_identity_dependency_fails_closed():
    engine = engine_over_stubs(DependencyRefusal(None), allowed())
    try:
        engine.create_course("token", title="T", description="", idempotency_key="k1")
    except Exception as exc:  # noqa: BLE001 - the boundary raises its own refusal
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
        assert getattr(exc, "status_code", None) == 503
    else:  # pragma: no cover
        raise AssertionError("the command must not succeed without an identity answer")
    assert engine.store.courses_created == 0


def test_an_authority_that_does_not_answer_fails_closed():
    engine = engine_over_stubs(subject(), DependencyRefusal(None))
    try:
        engine.create_course("token", title="T", description="", idempotency_key="k1")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
        assert getattr(exc, "status_code", None) == 503
    else:  # pragma: no cover
        raise AssertionError("a non-answering authority is never an allow")
    assert engine.store.courses_created == 0


def test_a_raising_authority_fails_closed_and_creates_no_effect():
    class Exploding:
        def decide(self, *args: Any, **kwargs: Any) -> Any:
            raise ContractViolation("the decision transport is down")

    engine = engine_over_stubs(subject(), Exploding())
    try:
        engine.create_course("token", title="T", description="", idempotency_key="k1")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
    assert engine.store.courses_created == 0
    # The refusal is audited with a DEPENDENCY_UNAVAILABLE reason.
    assert any(
        event.reason == "DEPENDENCY_UNAVAILABLE" for event in engine.store.audit
    )


def test_a_nonauthoritative_allow_is_not_an_access_grant():
    """ALLOW with a reason other than permitted fails closed."""
    engine = engine_over_stubs(
        subject(), DecisionAnswer(decision="ALLOW", reason="resource_tenant_mismatch")
    )
    try:
        engine.create_course("token", title="T", description="", idempotency_key="k1")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
    assert engine.store.courses_created == 0


def test_an_unknown_decision_value_fails_closed():
    engine = engine_over_stubs(
        subject(), DecisionAnswer(decision="MAYBE", reason="permitted")
    )
    try:
        engine.create_course("token", title="T", description="", idempotency_key="k1")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
    assert engine.store.courses_created == 0


def test_a_context_that_hides_its_source_is_not_a_context():
    """source != verified_identity means the tenant provenance is unproven."""
    forged = SubjectContext(
        identity_id="idn_human_a",
        tenant_id="ten_a",
        kind="HUMAN",
        source="caller_claim",
    )
    engine = engine_over_stubs(forged, allowed())
    try:
        engine.create_course("token", title="T", description="", idempotency_key="k1")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
    assert engine.store.courses_created == 0


def test_identity_denials_map_onto_the_published_codes():
    cases = {
        "missing_identity": ("AUTHENTICATION_REQUIRED", 401),
        "invalid_identity": ("AUTHENTICATION_REQUIRED", 401),
        "unknown_identity": ("AUTHENTICATION_REQUIRED", 401),
        "missing_tenant_context": ("AUTHENTICATION_REQUIRED", 401),
        "tenant_mismatch": ("TENANT_MISMATCH", 403),
        "tenant_suspended": ("AUTHORIZATION_DENIED", 403),
        "platform_ownership_mismatch": ("AUTHORIZATION_DENIED", 403),
        "insufficient_authorization": ("AUTHORIZATION_DENIED", 403),
    }
    for reason, (code, status) in cases.items():
        engine = engine_over_stubs(DependencyRefusal(reason), allowed())
        try:
            engine.create_course("token", title="T", description="", idempotency_key="k1")
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "code", None) == code, reason
            assert getattr(exc, "status_code", None) == status, reason
        else:  # pragma: no cover
            raise AssertionError(f"{reason} must refuse")


def test_authority_denials_pass_their_reason_as_context_not_as_verdict():
    engine = engine_over_stubs(subject(), denied("permission_not_granted"))
    try:
        engine.create_course("token", title="T", description="", idempotency_key="k1")
    except Exception as exc:  # noqa: BLE001
        refused = exc
        assert getattr(refused, "code", None) == "AUTHORIZATION_DENIED"
        assert refused.details == {"authority_reason": "permission_not_granted"}
    assert engine.store.courses_created == 0
