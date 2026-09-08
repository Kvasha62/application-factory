"""The audit journal and the consumer boundary of Learning.

Audit (ARCHITECTURE.md §27): every served operation and every refusal is
recorded with the machine-readable reason and the caller's request /
correlation context — including a replay and a conflict of IS-005. The
journal is owned data and is not readable through the published surface.

Boundary: a consumer receives the value-only client and nothing else. No
attribute path from the client reaches the application, the engine, the
store or the audit journal; an unknown channel is a closed door, and a
dropped client revokes its channel on the provider side.
"""

from __future__ import annotations

import gc
import uuid

import pytest
from fastapi.testclient import TestClient

from datetime import datetime, timezone

from learning_service.config import LearningConfig
from learning_service.consumed import DecisionAnswer, SubjectContext
from learning_service.engine import LearningEngine
from learning_service.models import Submission
from learning_service.errors import ContractViolation
from learning_service.reader import LearningClient
from learning_service.transport import channel_count
from tests.conftest import (
    LEARNING_SERVICE_A,
    LEARNING_STUDENT_A,
    LEARNING_TEACHER_A,
    StubLearningAuthorizationPort,
    StubLearningIdentityPort,
    learning_harness,
    monolith_with_learning,
)

TEACHER = LEARNING_TEACHER_A
STUDENT = LEARNING_STUDENT_A


def http() -> TestClient:
    return monolith_with_learning().learning_http()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def key() -> str:
    return f"key-{uuid.uuid4().hex}"


# --------------------------------------------------------------------- audit
def test_a_served_command_is_audited_with_context():
    harness = learning_harness()
    harness.http.post(
        "/api/v1/learning/courses",
        json={"title": "Audited", "description": ""},
        headers={
            **auth(TEACHER),
            "Idempotency-Key": key(),
            "X-Request-Id": "req-audit-1",
            "X-Correlation-Id": "corr-audit-1",
        },
    )
    served = [
        event
        for event in harness.store.audit
        if event.action == "learning.create_course" and event.decision == "ALLOW"
    ]
    assert served, "the served command must be audited"
    event = served[-1]
    assert event.identity_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_type == "learning.course"
    assert event.request_id == "req-audit-1"
    assert event.correlation_id == "corr-audit-1"
    assert event.platform_id == "plt_demo"


def test_every_refusal_is_audited_with_its_code():
    harness = learning_harness()
    harness.http.get(
        "/api/v1/learning/courses/crs_absent",
        headers={**auth(TEACHER), "X-Request-Id": "req-audit-2"},
    )
    refusals = [
        event
        for event in harness.store.audit
        if event.reason == "RESOURCE_NOT_FOUND" and event.decision == "DENY"
    ]
    assert refusals
    assert refusals[-1].request_id == "req-audit-2"


def test_identity_failures_are_audited_without_inventing_a_subject():
    harness = learning_harness()
    harness.http.get("/api/v1/learning/courses/crs_demo_a")
    refusals = [
        event
        for event in harness.store.audit
        if event.reason == "AUTHENTICATION_REQUIRED"
    ]
    assert refusals
    assert refusals[-1].identity_id is None
    assert refusals[-1].tenant_id is None


def test_a_replay_is_audited_by_the_guard_sink():
    harness = learning_harness()
    headers = {**auth(TEACHER), "Idempotency-Key": "audit-replay-1"}
    body = {"title": "Twice", "description": ""}
    assert harness.http.post("/api/v1/learning/courses", json=body, headers=headers).status_code == 200
    assert harness.http.post("/api/v1/learning/courses", json=body, headers=headers).status_code == 200
    replays = [event for event in harness.store.audit if event.action == "idempotency_replay"]
    assert replays and replays[-1].tenant_id == "ten_a"


def test_the_audit_journal_is_not_on_the_published_surface():
    client = http()
    assert client.get("/api/v1/learning/audit").status_code == 404
    assert client.get("/api/v1/audit").status_code == 404
    app = client.app
    for route in app.routes:
        path = getattr(route, "path", "")
        assert "audit" not in path, path
    document = client.get("/openapi.json").json()
    for path in document["paths"]:
        assert "audit" not in path and "store" not in path


# ------------------------------------------------------------------ boundary
def test_the_published_client_is_values_only():
    client = monolith_with_learning().learning_client()
    # Exactly one value of state: the opaque channel handle.
    assert client.__slots__ == ("_channel", "__weakref__")
    assert isinstance(client._channel, str)
    public = [name for name in dir(client) if not name.startswith("_")]
    assert sorted(public) == [
        "create_assignment",
        "create_course",
        "create_lesson",
        "create_module",
        "create_submission",
        "enroll",
        "get_assignment",
        "get_course",
        "get_my_enrollment",
        "get_submission",
        "publish_course",
    ]


def test_no_attribute_of_the_client_reaches_the_internals():
    instance = monolith_with_learning()
    client = instance.learning_client()
    forbidden = (instance.learning.engine, instance.learning.store, instance.learning_app)
    for name in dir(client):
        try:
            value = getattr(client, name)
        except Exception:  # pragma: no cover - nothing should raise here
            continue
        for internal in forbidden:
            assert value is not internal, f"{name} exposes an internal"


def test_an_unknown_channel_is_a_closed_door():
    client = LearningClient("lrn-not-a-real-handle")
    with pytest.raises(ContractViolation):
        client.get_course(TEACHER, "crs_demo_a")


def test_a_dropped_client_revokes_its_channel():
    harness = learning_harness()
    before = channel_count()
    client = harness.deployment.publish()
    handle = client._channel
    assert channel_count() == before + 1
    del client
    gc.collect()
    assert channel_count() == before
    with pytest.raises(ContractViolation):
        LearningClient(handle).get_course(TEACHER, "crs_demo_a")


def test_importing_the_reader_publishes_no_transport():
    import importlib

    reader = importlib.import_module("learning_service.reader")
    assert not hasattr(reader, "transport")
    assert not hasattr(reader, "_CHANNELS")
    assert not hasattr(reader, "call_contract")
    assert not hasattr(reader, "open_channel")


# --------------------------------------------- chain ordering, engine level
def engine_with_recording_authorization() -> tuple[LearningEngine, StubLearningAuthorizationPort]:
    from tests.conftest import CountingLearningStore

    counting = CountingLearningStore()
    counting.seed_demo()
    counting.reset_counters()
    authority = StubLearningAuthorizationPort(
        DecisionAnswer(decision="ALLOW", reason="permitted")
    )
    engine = LearningEngine(
        store=counting,
        config=LearningConfig(platform_id="plt_demo", environment="test"),
        identity=StubLearningIdentityPort(
            SubjectContext(
                identity_id="idn_human_a",
                tenant_id="ten_a",
                kind="HUMAN",
                source="verified_identity",
            )
        ),
        authorization=authority,
    )
    return engine, authority


def test_a_tenant_mismatch_is_decided_at_this_boundary_without_asking_is_003():
    """The foreign course belongs to ten_b; the subject's tenant is ten_a."""
    engine, authority = engine_with_recording_authorization()
    course = engine.store.courses["crs_demo_a"]
    engine.store.courses["crs_demo_a"] = type(course)(
        course_id=course.course_id,
        tenant_id="ten_b",
        title=course.title,
        description=course.description,
        status=course.status,
        created_by=course.created_by,
        created_at=course.created_at,
        updated_at=course.updated_at,
    )
    try:
        engine.get_course("token", "crs_demo_a")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "TENANT_MISMATCH"
        assert getattr(exc, "status_code", None) == 403
    else:  # pragma: no cover
        raise AssertionError("a foreign course must be refused")
    # The mismatch is data ownership at this boundary: no authority call.
    assert authority.calls == []


def test_a_student_cannot_read_another_students_submission():
    engine, _ = engine_with_recording_authorization()
    # A full chain and a submission of another student in the same tenant.
    stamp = datetime.now(timezone.utc).isoformat()
    from learning_service.models import Assignment, Lesson, Module

    engine.store.save_module(
        Module(
            module_id="mod_demo",
            course_id="crs_demo_a",
            tenant_id="ten_a",
            title="M",
            position=1,
            status="DRAFT",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    engine.store.save_lesson(
        Lesson(
            lesson_id="les_demo",
            module_id="mod_demo",
            tenant_id="ten_a",
            title="L",
            content="",
            position=1,
            status="DRAFT",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    engine.store.save_assignment(
        Assignment(
            assignment_id="asg_demo",
            lesson_id="les_demo",
            tenant_id="ten_a",
            title="A",
            instructions="",
            status="PUBLISHED",
        )
    )
    engine.store.save_submission(
        Submission(
            submission_id="sub_foreign",
            tenant_id="ten_a",
            assignment_id="asg_demo",
            student_identity_id="idn_human_a",
            attempt=1,
            content="someone else's answer",
            status="SUBMITTED",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    # The reader is a student of the same tenant: allowed to read submissions,
    # not allowed to author content — so not the owner means refused.
    student = SubjectContext(
        identity_id="idn_human_c", tenant_id="ten_a", kind="HUMAN", source="verified_identity"
    )
    # The student holds learning.submission.read but not learning.course.write:
    # allowed to read submissions, never the author capability.
    class StudentVocabulary:
        def decide(self, subject_credential, *, operation, **kwargs):  # noqa: ANN001, ANN003
            if operation == "learning.course.write":
                return DecisionAnswer(decision="DENY", reason="permission_not_granted")
            return DecisionAnswer(decision="ALLOW", reason="permitted")

    engine = LearningEngine(
        store=engine.store,
        config=LearningConfig(platform_id="plt_demo", environment="test"),
        identity=StubLearningIdentityPort(student),
        authorization=StudentVocabulary(),
    )
    try:
        engine.get_submission("token", "sub_foreign")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "AUTHORIZATION_DENIED"
        # The refusal is the authority's denial of the author capability,
        # enforced at this boundary: the owner rule needs no second verdict.
        assert exc.details == {"authority_reason": "permission_not_granted"}
        # ... and the denial of the fallback question is audited.
        assert any(
            event.reason == "AUTHORIZATION_DENIED" and event.decision == "DENY"
            for event in engine.store.audit
        )
    else:  # pragma: no cover
        raise AssertionError("a student must not read another student's submission")


def test_a_service_identity_without_grants_is_authenticated_but_denied():
    client = http()
    refused = client.post(
        "/api/v1/learning/courses",
        json={"title": "Svc", "description": ""},
        headers={**auth(LEARNING_SERVICE_A), "Idempotency-Key": key()},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "AUTHORIZATION_DENIED"
