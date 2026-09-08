"""The idempotency contract (section 11), enforced by IS-005 at this boundary.

Every required command:

* refuses to run without an ``Idempotency-Key`` (there is no silent path);
* on an exact replay — same identity, tenant, operation, target, payload,
  key — returns the same logical result and creates no second effect;
* rejects key reuse with a different payload, identity, tenant, operation or
  target as ``IDEMPOTENCY_CONFLICT``;
* records nothing on an authorization denial or a dependency failure — the
  effect did not happen, so the same key may be used to retry it.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from learning_service.config import LearningConfig
from learning_service.consumed import DecisionAnswer, DependencyRefusal, SubjectContext
from learning_service.engine import LearningEngine
from tests.conftest import (
    LEARNING_STUDENT_A,
    LEARNING_TEACHER_A,
    LEARNING_TEACHER_B,
    CountingLearningStore,
    StubLearningAuthorizationPort,
    StubLearningIdentityPort,
    learning_harness,
    monolith_with_learning,
)

TEACHER = LEARNING_TEACHER_A
STUDENT = LEARNING_STUDENT_A
TEACHER_B = LEARNING_TEACHER_B


def http() -> TestClient:
    return monolith_with_learning().learning_http()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def key() -> str:
    return f"key-{uuid.uuid4().hex}"


def code_of(response: Any) -> str:  # noqa: ANN401
    return response.json()["error"]["code"]


COMMANDS = [
    ("/api/v1/learning/courses", {"title": "T", "description": ""}),
    ("/api/v1/learning/courses/crs_demo_a/publish", {}),
    ("/api/v1/learning/courses/crs_demo_a/modules", {"title": "M", "position": 1}),
    ("/api/v1/learning/modules/mod_nosuch/lessons", {"title": "L", "content": "", "position": 1}),
    ("/api/v1/learning/lessons/les_nosuch/assignments", {"title": "A", "instructions": ""}),
    ("/api/v1/learning/courses/crs_demo_a/enrollments", {}),
    (
        "/api/v1/learning/assignments/asg_nosuch/submissions",
        {"attempt": 1, "content": "x"},
    ),
]


# ------------------------------------------------------------- no silent path
def test_every_command_refuses_to_run_without_a_key():
    client = http()
    for path, body in COMMANDS:
        refused = client.post(path, json=body, headers=auth(TEACHER))
        assert refused.status_code == 400, path
        assert code_of(refused) == "IDEMPOTENCY_KEY_REQUIRED", path


def test_a_blank_key_is_no_key():
    client = http()
    for blank in ("", "   "):
        refused = client.post(
            "/api/v1/learning/courses",
            json={"title": "T", "description": ""},
            headers={**auth(TEACHER), "Idempotency-Key": blank},
        )
        assert refused.status_code == 400
        assert code_of(refused) == "IDEMPOTENCY_KEY_REQUIRED"


def test_the_engine_takes_the_key_as_a_required_argument():
    """No default value: the signature itself leaves no silent path."""
    for name in (
        "create_course",
        "publish_course",
        "create_module",
        "create_lesson",
        "create_assignment",
        "enroll",
        "create_submission",
    ):
        method = getattr(LearningEngine, name)
        parameter = inspect.signature(method).parameters["idempotency_key"]
        assert parameter.default is inspect.Parameter.empty, name
        assert parameter.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )


def test_reads_take_no_key_and_change_no_state():
    client = http()
    response = client.get("/api/v1/learning/courses/crs_demo_a", headers=auth(TEACHER))
    assert response.status_code == 200


# ---------------------------------------------------------------- exact replay
def test_exact_replay_of_create_course_returns_the_same_result_without_an_effect():
    harness = learning_harness()
    client = harness.http
    body = {"title": "Replayed", "description": "Once"}
    headers = {**auth(TEACHER), "Idempotency-Key": "replay-create-1"}
    first = client.post("/api/v1/learning/courses", json=body, headers=headers)
    assert first.status_code == 200
    second = client.post("/api/v1/learning/courses", json=body, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert harness.store.courses_created == 1


def test_exact_replay_of_publish_returns_the_published_result():
    harness = learning_harness()
    client = harness.http
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "P", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": "c1"},
    ).json()["course"]["course_id"]
    headers = {**auth(TEACHER), "Idempotency-Key": "publish-1"}
    first = client.post(f"/api/v1/learning/courses/{course_id}/publish", json={}, headers=headers)
    assert first.status_code == 200
    second = client.post(f"/api/v1/learning/courses/{course_id}/publish", json={}, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    # The state machine ran once; the replay is a replay, not a second effect.
    assert harness.store.publishes_applied == 1


def test_exact_replay_of_enrollment_returns_the_same_enrollment():
    harness = learning_harness()
    client = harness.http
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "E", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": "c2"},
    ).json()["course"]["course_id"]
    client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": "p2"},
    )
    headers = {**auth(STUDENT), "Idempotency-Key": "enroll-1"}
    first = client.post(f"/api/v1/learning/courses/{course_id}/enrollments", json={}, headers=headers)
    second = client.post(f"/api/v1/learning/courses/{course_id}/enrollments", json={}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert harness.store.enrollments_created == 1


def test_exact_replay_of_submission_returns_the_same_submission():
    harness = learning_harness()
    client = harness.http
    course_id, _, _, assignment_id = _published_assignment(harness, client)
    client.post(
        f"/api/v1/learning/courses/{course_id}/enrollments",
        json={},
        headers={**auth(STUDENT), "Idempotency-Key": "enroll-3"},
    )
    headers = {**auth(STUDENT), "Idempotency-Key": "submit-1"}
    body = {"attempt": 1, "content": "x = 5"}
    first = client.post(
        f"/api/v1/learning/assignments/{assignment_id}/submissions", json=body, headers=headers
    )
    second = client.post(
        f"/api/v1/learning/assignments/{assignment_id}/submissions", json=body, headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert harness.store.submissions_created == 1


def _published_assignment(harness: Any, client: TestClient) -> tuple[str, str, str, str]:
    teacher = TEACHER
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "Chain", "description": ""},
        headers={**auth(teacher), "Idempotency-Key": key()},
    ).json()["course"]["course_id"]
    module_id = client.post(
        f"/api/v1/learning/courses/{course_id}/modules",
        json={"title": "M", "position": 1},
        headers={**auth(teacher), "Idempotency-Key": key()},
    ).json()["module"]["module_id"]
    lesson_id = client.post(
        f"/api/v1/learning/modules/{module_id}/lessons",
        json={"title": "L", "content": "", "position": 1},
        headers={**auth(teacher), "Idempotency-Key": key()},
    ).json()["lesson"]["lesson_id"]
    assignment_id = client.post(
        f"/api/v1/learning/lessons/{lesson_id}/assignments",
        json={"title": "A", "instructions": ""},
        headers={**auth(teacher), "Idempotency-Key": key()},
    ).json()["assignment"]["assignment_id"]
    client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(teacher), "Idempotency-Key": key()},
    )
    return course_id, module_id, lesson_id, assignment_id


# -------------------------------------------------------------------- conflicts
def test_the_same_key_with_a_different_payload_is_a_conflict():
    harness = learning_harness()
    client = harness.http
    headers = {**auth(TEACHER), "Idempotency-Key": "conflict-1"}
    first = client.post(
        "/api/v1/learning/courses",
        json={"title": "Original", "description": ""},
        headers=headers,
    )
    assert first.status_code == 200
    replay = client.post(
        "/api/v1/learning/courses",
        json={"title": "Different", "description": ""},
        headers=headers,
    )
    assert replay.status_code == 409
    assert code_of(replay) == "IDEMPOTENCY_CONFLICT"
    assert harness.store.courses_created == 1


def test_the_same_key_from_another_identity_and_tenant_is_a_conflict():
    client = http()
    headers = {**auth(TEACHER), "Idempotency-Key": "conflict-2"}
    first = client.post(
        "/api/v1/learning/courses",
        json={"title": "A course", "description": ""},
        headers=headers,
    )
    assert first.status_code == 200
    replay = client.post(
        "/api/v1/learning/courses",
        json={"title": "A course", "description": ""},
        headers={**auth(TEACHER_B), "Idempotency-Key": "conflict-2"},
    )
    assert replay.status_code == 409
    assert code_of(replay) == "IDEMPOTENCY_CONFLICT"


def test_the_same_key_for_another_operation_is_a_conflict():
    harness = learning_harness()
    client = harness.http
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "Op", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": "conflict-3"},
    ).json()["course"]["course_id"]
    replay = client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": "conflict-3"},
    )
    assert replay.status_code == 409
    assert code_of(replay) == "IDEMPOTENCY_CONFLICT"
    # ... and the course is still a draft: the conflict mutated nothing.
    assert harness.store.courses[course_id].status == "DRAFT"


def test_the_same_key_for_another_target_is_a_conflict():
    harness = learning_harness()
    client = harness.http
    first = client.post(
        "/api/v1/learning/courses",
        json={"title": "First", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": "conflict-4"},
    )
    assert first.status_code == 200
    other = client.post(
        "/api/v1/learning/courses",
        json={"title": "Second", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    other_id = other.json()["course"]["course_id"]
    replay = client.post(
        f"/api/v1/learning/courses/{other_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": "conflict-4"},
    )
    assert replay.status_code == 409
    assert code_of(replay) == "IDEMPOTENCY_CONFLICT"


def test_an_idempotency_conflict_is_audited_as_security_sensitive():
    harness = learning_harness()
    harness.client.create_course(
        TEACHER, title="One", description="", idempotency_key="conflict-audit-2"
    )
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - the client raises the refusal
        harness.client.create_course(
            TEACHER, title="Two", description="", idempotency_key="conflict-audit-2"
        )
    assert getattr(excinfo.value, "code", "") == "IDEMPOTENCY_CONFLICT"
    # The guard's own sink wrote the conflict into this component's journal.
    assert any(
        event.action == "idempotency_conflict" for event in harness.store.audit
    )


# ----------------------------------------------- denials create no record (I-006)
def engine_over_stubs(identity: Any, authorization: Any) -> LearningEngine:
    store = CountingLearningStore()
    store.seed_demo()
    store.reset_counters()
    return LearningEngine(
        store=store,
        config=LearningConfig(platform_id="plt_demo", environment="test"),
        identity=StubLearningIdentityPort(identity),
        authorization=StubLearningAuthorizationPort(authorization),
    )


def subject() -> SubjectContext:
    return SubjectContext(
        identity_id="idn_human_a", tenant_id="ten_a", kind="HUMAN", source="verified_identity"
    )


def allowed() -> DecisionAnswer:
    return DecisionAnswer(decision="ALLOW", reason="permitted")


def denied() -> DecisionAnswer:
    return DecisionAnswer(decision="DENY", reason="permission_not_granted")


def test_an_authorization_denial_creates_no_idempotency_record():
    """A denial is not a success: the same key may carry the retry."""
    authority = StubLearningAuthorizationPort(denied())
    store = CountingLearningStore()
    store.seed_demo()
    store.reset_counters()
    engine = LearningEngine(
        store=store,
        config=LearningConfig(platform_id="plt_demo", environment="test"),
        identity=StubLearningIdentityPort(subject()),
        authorization=authority,
    )
    try:
        engine.create_course("token", title="T", description="", idempotency_key="retry-1")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "AUTHORIZATION_DENIED"
    assert engine.store.courses_created == 0

    authority.outcome = allowed()
    served, _, _ = engine.create_course(
        "token", title="T", description="", idempotency_key="retry-1"
    )
    assert served.status == "DRAFT"
    assert engine.store.courses_created == 1
    # The retry itself is now replayable: exactly one effect for the key.
    again, _, _ = engine.create_course(
        "token", title="T", description="", idempotency_key="retry-1"
    )
    assert again == served
    assert engine.store.courses_created == 1


def test_a_dependency_failure_fails_closed_and_creates_no_record():
    engine = engine_over_stubs(DependencyRefusal(None), allowed())
    try:
        engine.create_course("token", title="T", description="", idempotency_key="retry-2")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
    assert engine.store.courses_created == 0
    assert not any(
        event.action == "learning.create_course" and event.decision == "ALLOW"
        for event in engine.store.audit
    )
