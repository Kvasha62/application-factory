"""Validation at the published boundary (section 12, VALIDATION_ERROR).

The schemas forbid what the client must not control: identifiers, tenant
ownership, identity ownership, lifecycle state, timestamps. An unreadable
request is a ``422`` — refused before any operation, without creating a
business effect, an authorization question or an idempotency record, and
audited with the caller's request/correlation context only.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import (
    LEARNING_STUDENT_A,
    LEARNING_TEACHER_A,
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


def envelope_of(response: Any) -> dict:  # noqa: ANN401
    body = response.json()
    assert set(body) == {"error", "request_id", "correlation_id"}
    assert set(body["error"]) == {"code", "message", "details"}
    return body


# --------------------------------------------------------- field constraints
def test_course_field_constraints():
    client = http()
    cases = [
        {"title": "", "description": ""},  # empty title
        {"title": "x" * 201, "description": ""},  # title too long
        {"description": "y" * 5001},  # description missing
        {"title": "T", "description": "z" * 5001},  # description too long
        {"title": "T"},  # description missing entirely
    ]
    for body in cases:
        refused = client.post(
            "/api/v1/learning/courses",
            json=body,
            headers={**auth(TEACHER), "Idempotency-Key": key()},
        )
        assert refused.status_code == 422, body
        assert envelope_of(refused)["error"]["code"] == "VALIDATION_ERROR"


def test_module_lesson_assignment_constraints():
    client = http()
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "V", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["course"]["course_id"]
    module_id = client.post(
        f"/api/v1/learning/courses/{course_id}/modules",
        json={"title": "M", "position": 1},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["module"]["module_id"]
    lesson_id = client.post(
        f"/api/v1/learning/modules/{module_id}/lessons",
        json={"title": "L", "content": "", "position": 1},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["lesson"]["lesson_id"]

    refused = client.post(
        f"/api/v1/learning/courses/{course_id}/modules",
        json={"title": "M", "position": 0},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert refused.status_code == 422
    refused = client.post(
        f"/api/v1/learning/modules/{module_id}/lessons",
        json={"title": "L", "content": "c" * 20001, "position": 1},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert refused.status_code == 422
    refused = client.post(
        f"/api/v1/learning/lessons/{lesson_id}/assignments",
        json={"title": "A", "instructions": "i" * 20001},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert refused.status_code == 422


def test_submission_constraints():
    client = http()
    for body in (
        {"attempt": 0, "content": "x"},
        {"attempt": 1, "content": ""},
        {"attempt": 1, "content": "c" * 50001},
        {"content": "x"},
        {"attempt": 1},
    ):
        refused = client.post(
            "/api/v1/learning/assignments/asg_any/submissions",
            json=body,
            headers={**auth(STUDENT), "Idempotency-Key": key()},
        )
        assert refused.status_code == 422, body


# ------------------------------------------------- server-controlled fields
def test_the_client_cannot_declare_server_owned_fields():
    client = http()
    hijacks = [
        {"title": "T", "description": "", "course_id": "crs_choosen"},
        {"title": "T", "description": "", "tenant_id": "ten_b"},
        {"title": "T", "description": "", "status": "PUBLISHED"},
        {"title": "T", "description": "", "created_by": "idn_human_b"},
        {"title": "T", "description": "", "created_at": "2020-01-01T00:00:00+00:00"},
        {"title": "T", "description": "", "updated_at": "2020-01-01T00:00:00+00:00"},
    ]
    for body in hijacks:
        refused = client.post(
            "/api/v1/learning/courses",
            json=body,
            headers={**auth(TEACHER), "Idempotency-Key": key()},
        )
        assert refused.status_code == 422, body
        assert envelope_of(refused)["error"]["code"] == "VALIDATION_ERROR"


def test_the_publish_and_enroll_commands_accept_no_status_or_identity():
    client = http()
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "Closed", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["course"]["course_id"]
    for body in (
        {"status": "PUBLISHED"},
        {"course_id": "crs_other"},
        {"student_identity_id": "idn_human_b"},
        {"tenant_id": "ten_b"},
        {"enrollment_id": "enr_x"},
        {"enrollment_status": "ACTIVE"},
    ):
        refused = client.post(
            f"/api/v1/learning/courses/{course_id}/publish",
            json=body,
            headers={**auth(TEACHER), "Idempotency-Key": key()},
        )
        assert refused.status_code == 422, body
        refused = client.post(
            f"/api/v1/learning/courses/{course_id}/enrollments",
            json=body,
            headers={**auth(STUDENT), "Idempotency-Key": key()},
        )
        assert refused.status_code == 422, body


def test_the_submission_cannot_declare_ownership_or_state():
    client = http()
    for body in (
        {"attempt": 1, "content": "x", "submission_id": "sub_x"},
        {"attempt": 1, "content": "x", "tenant_id": "ten_b"},
        {"attempt": 1, "content": "x", "student_identity_id": "idn_human_b"},
        {"attempt": 1, "content": "x", "status": "SUBMITTED"},
    ):
        refused = client.post(
            "/api/v1/learning/assignments/asg_any/submissions",
            json=body,
            headers={**auth(STUDENT), "Idempotency-Key": key()},
        )
        assert refused.status_code == 422, body


# ------------------------------------------------------------ unreadable input
def test_a_non_json_body_is_a_validation_error():
    client = http()
    refused = client.post(
        "/api/v1/learning/courses",
        content=b"not json at all",
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert refused.status_code == 422
    body = envelope_of(refused)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    # The payload is not echoed back: only location and kind of the problem.
    for problem in body["error"]["details"]["problems"]:
        assert "not json" not in problem


def test_validation_refuses_before_any_operation_or_record():
    harness = learning_harness()
    client = harness.http
    bad = client.post(
        "/api/v1/learning/courses",
        json={"title": "", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": "attempt-1"},
    )
    assert bad.status_code == 422
    assert harness.store.courses_created == 0
    # The unreadable attempt was audited; with no caller context supplied the
    # journal still carries a generated request id (foundation semantics).
    unreadable = [
        event for event in harness.store.audit if event.action == "learning.access"
    ]
    assert unreadable and unreadable[0].decision == "DENY"
    assert unreadable[0].request_id
    # The same key is still fresh: the 422 consumed no idempotency record.
    good = client.post(
        "/api/v1/learning/courses",
        json={"title": "Now valid", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": "attempt-1"},
    )
    assert good.status_code == 200
    assert harness.store.courses_created == 1


def test_the_request_id_and_correlation_id_are_echoed_in_refusals():
    client = http()
    refused = client.get(
        "/api/v1/learning/courses/crs_absent",
        headers={
            **auth(TEACHER),
            "X-Request-Id": "req-contract-1",
            "X-Correlation-Id": "corr-contract-1",
        },
    )
    assert refused.status_code == 404
    body = envelope_of(refused)
    assert body["request_id"] == "req-contract-1"
    assert body["correlation_id"] == "corr-contract-1"


def test_messages_are_static_and_leak_nothing():
    client = http()
    secret = "tenant-b-secret-value"
    refused = client.post(
        "/api/v1/learning/courses",
        json={"title": secret, "description": ""},
        headers={**auth(STUDENT), "Idempotency-Key": key()},
    )
    assert refused.status_code == 403
    body = envelope_of(refused)
    assert body["error"]["message"] == "Operation is not permitted."
    assert secret not in refused.text
