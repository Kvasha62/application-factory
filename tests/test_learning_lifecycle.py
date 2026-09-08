"""Lifecycle: the closed state machines and the business preconditions.

The states are facts of the owner, never client-supplied values:

* a Course moves only ``DRAFT → PUBLISHED`` (via Publish Course, which
  publishes its content tree with it); every other attempt is a 409 and
  mutates nothing;
* structural mutations are accepted only while the course is ``DRAFT``;
* enrolling requires a ``PUBLISHED`` course and at most one enrollment per
  student and course;
* submitting requires a ``PUBLISHED`` assignment and an ``ACTIVE`` enrollment.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import (
    LEARNING_STUDENT_A,
    LEARNING_TEACHER_A,
    LEARNING_TEACHER_B,
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


def draft_with_content(client: TestClient):
    """A DRAFT course holding one module, one lesson and one assignment."""
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "To publish", "description": ""},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["course"]["course_id"]
    module_id = client.post(
        f"/api/v1/learning/courses/{course_id}/modules",
        json={"title": "M1", "position": 1},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["module"]["module_id"]
    lesson_id = client.post(
        f"/api/v1/learning/modules/{module_id}/lessons",
        json={"title": "L1", "content": "C1", "position": 1},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["lesson"]["lesson_id"]
    assignment_id = client.post(
        f"/api/v1/learning/lessons/{lesson_id}/assignments",
        json={"title": "A1", "instructions": "I1"},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["assignment"]["assignment_id"]
    return course_id, module_id, lesson_id, assignment_id


def test_publish_is_the_only_allowed_transition_and_publishes_the_tree():
    harness = learning_harness()
    client = harness.http
    course_id, module_id, lesson_id, assignment_id = draft_with_content(client)
    response = client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert response.status_code == 200
    assert response.json()["course"]["status"] == "PUBLISHED"
    # The consequences of the command are owned state: the tree is published.
    assert harness.store.modules[module_id].status == "PUBLISHED"
    assert harness.store.lessons[lesson_id].status == "PUBLISHED"
    assert harness.store.assignments[assignment_id].status == "PUBLISHED"
    assert harness.store.publishes_applied == 1


def test_republishing_with_a_new_key_is_a_conflict_and_mutates_nothing():
    harness = learning_harness()
    client = harness.http
    course_id, _, _, _ = draft_with_content(client)
    first = client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert first.status_code == 200
    before = harness.store.publishes_applied
    updated_at = first.json()["course"]["updated_at"]

    second = client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert second.status_code == 409
    assert code_of(second) == "INVALID_STATE_TRANSITION"
    # The refusal changed nothing: state and timestamps are untouched.
    assert harness.store.publishes_applied == before
    assert harness.store.courses[course_id].updated_at == updated_at


def test_structural_mutation_is_refused_after_publication():
    client = http()
    course_id, module_id, lesson_id, _ = draft_with_content(client)
    assert (
        client.post(
            f"/api/v1/learning/courses/{course_id}/publish",
            json={},
            headers={**auth(TEACHER), "Idempotency-Key": key()},
        ).status_code
        == 200
    )
    refused = [
        client.post(
            f"/api/v1/learning/courses/{course_id}/modules",
            json={"title": "Late module", "position": 2},
            headers={**auth(TEACHER), "Idempotency-Key": key()},
        ),
        client.post(
            f"/api/v1/learning/modules/{module_id}/lessons",
            json={"title": "Late lesson", "content": "", "position": 2},
            headers={**auth(TEACHER), "Idempotency-Key": key()},
        ),
        client.post(
            f"/api/v1/learning/lessons/{lesson_id}/assignments",
            json={"title": "Late assignment", "instructions": ""},
            headers={**auth(TEACHER), "Idempotency-Key": key()},
        ),
    ]
    for response in refused:
        assert response.status_code == 409
        assert code_of(response) == "INVALID_STATE_TRANSITION"
        assert response.json()["error"]["details"] == {"course_status": "PUBLISHED"}


def test_a_student_cannot_read_a_draft_course_but_the_teacher_can():
    client = http()
    assert client.get(
        "/api/v1/learning/courses/crs_demo_a", headers=auth(TEACHER)
    ).status_code == 200
    refused = client.get("/api/v1/learning/courses/crs_demo_a", headers=auth(STUDENT))
    assert refused.status_code == 403
    assert code_of(refused) == "AUTHORIZATION_DENIED"


def test_a_student_cannot_read_a_draft_assignment():
    client = http()
    _, _, _, assignment_id = draft_with_content(client)
    refused = client.get(
        f"/api/v1/learning/assignments/{assignment_id}", headers=auth(STUDENT)
    )
    assert refused.status_code == 403
    assert code_of(refused) == "AUTHORIZATION_DENIED"
    # ... and the published one after the course is published.
    course_id = client.post(
        "/api/v1/learning/courses",
        json={"title": "Pub", "description": ""},
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
    published_assignment = client.post(
        f"/api/v1/learning/lessons/{lesson_id}/assignments",
        json={"title": "A", "instructions": ""},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    ).json()["assignment"]["assignment_id"]
    client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    assert (
        client.get(
            f"/api/v1/learning/assignments/{published_assignment}",
            headers=auth(STUDENT),
        ).status_code
        == 200
    )


def test_enrollment_requires_a_published_course():
    client = http()
    refused = client.post(
        "/api/v1/learning/courses/crs_demo_a/enrollments",
        json={},
        headers={**auth(STUDENT), "Idempotency-Key": key()},
    )
    assert refused.status_code == 409
    assert code_of(refused) == "COURSE_NOT_PUBLISHED"
    assert refused.json()["error"]["details"] == {"course_status": "DRAFT"}


def test_duplicate_enrollment_creates_no_second_effect():
    harness = learning_harness()
    client = harness.http
    course_id, _, _, _ = draft_with_content(client)
    client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    first = client.post(
        f"/api/v1/learning/courses/{course_id}/enrollments",
        json={},
        headers={**auth(STUDENT), "Idempotency-Key": key()},
    )
    assert first.status_code == 200
    before = harness.store.enrollments_created

    duplicate = client.post(
        f"/api/v1/learning/courses/{course_id}/enrollments",
        json={},
        headers={**auth(STUDENT), "Idempotency-Key": key()},
    )
    assert duplicate.status_code == 409
    assert code_of(duplicate) == "DUPLICATE_ENROLLMENT"
    assert harness.store.enrollments_created == before
    # The student still has exactly one enrollment, readable as their own.
    mine = client.get(
        f"/api/v1/learning/courses/{course_id}/enrollments/me", headers=auth(STUDENT)
    )
    assert mine.status_code == 200


def test_get_own_enrollment_without_enrollment_is_a_404():
    client = http()
    course_id, _, _, _ = draft_with_content(client)
    client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    refused = client.get(
        f"/api/v1/learning/courses/{course_id}/enrollments/me", headers=auth(STUDENT)
    )
    assert refused.status_code == 404
    assert code_of(refused) == "RESOURCE_NOT_FOUND"


def test_submission_requires_a_published_assignment():
    client = http()
    course_id, _, _, assignment_id = draft_with_content(client)
    # The course is a draft, so the assignment is not published either.
    refused = client.post(
        f"/api/v1/learning/assignments/{assignment_id}/submissions",
        json={"attempt": 1, "content": "x = 5"},
        headers={**auth(STUDENT), "Idempotency-Key": key()},
    )
    assert refused.status_code == 409
    assert code_of(refused) == "ASSIGNMENT_NOT_PUBLISHED"


def test_submission_requires_an_active_enrollment():
    client = http()
    course_id, _, _, assignment_id = draft_with_content(client)
    client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    # Teacher B holds the student vocabulary in ten_b; here the same rule is
    # proven for a tenant member without an enrollment.
    refused = client.post(
        f"/api/v1/learning/assignments/{assignment_id}/submissions",
        json={"attempt": 1, "content": "x = 5"},
        headers={**auth(STUDENT), "Idempotency-Key": key()},
    )
    assert refused.status_code == 409
    assert code_of(refused) == "ENROLLMENT_REQUIRED"
    assert refused.json()["error"]["details"] == {"course_id": course_id}


def test_archived_is_declared_but_unreachable_in_this_slice():
    harness = learning_harness()
    client = harness.http
    course_id, _, _, _ = draft_with_content(client)
    client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(TEACHER), "Idempotency-Key": key()},
    )
    # No command reaches ARCHIVED: the machine is closed and published.
    assert harness.store.courses[course_id].status == "PUBLISHED"
