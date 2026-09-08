"""The published API of Learning, exercised as the contract documents it.

Every request here goes over the published HTTP contract of a composed
Platform Instance: identity verified by IS-001, decisions by IS-003, effects
delivered through IS-005 — and nothing of any dependency's internals touched.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import (
    LEARNING_STUDENT_A,
    LEARNING_TEACHER_A,
    LEARNING_TEACHER_B,
    monolith_with_learning,
)


def http() -> TestClient:
    return monolith_with_learning().learning_http()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def key() -> str:
    return f"key-{uuid.uuid4().hex}"


def author_course(client: TestClient, *, token: str = LEARNING_TEACHER_A):
    """Author a full course through the published commands and publish it."""
    created = client.post(
        "/api/v1/learning/courses",
        json={"title": "Introduction to Algebra", "description": "Basic algebra course"},
        headers={**auth(token), "Idempotency-Key": key()},
    )
    assert created.status_code == 200, created.text
    course = created.json()["course"]

    module = client.post(
        f"/api/v1/learning/courses/{course['course_id']}/modules",
        json={"title": "Linear Equations", "position": 1},
        headers={**auth(token), "Idempotency-Key": key()},
    )
    assert module.status_code == 200, module.text
    lesson = client.post(
        f"/api/v1/learning/modules/{module.json()['module']['module_id']}/lessons",
        json={
            "title": "Solving x + 5 = 10",
            "content": "Subtract 5 from both sides.",
            "position": 1,
        },
        headers={**auth(token), "Idempotency-Key": key()},
    )
    assert lesson.status_code == 200, lesson.text
    assignment = client.post(
        f"/api/v1/learning/lessons/{lesson.json()['lesson']['lesson_id']}/assignments",
        json={
            "title": "Solve five equations",
            "instructions": "Solve the following five equations.",
        },
        headers={**auth(token), "Idempotency-Key": key()},
    )
    assert assignment.status_code == 200, assignment.text

    published = client.post(
        f"/api/v1/learning/courses/{course['course_id']}/publish",
        json={},
        headers={**auth(token), "Idempotency-Key": key()},
    )
    assert published.status_code == 200, published.text
    return (
        course["course_id"],
        module.json()["module"]["module_id"],
        lesson.json()["lesson"]["lesson_id"],
        assignment.json()["assignment"]["assignment_id"],
    )


# ------------------------------------------------------------------- health
def test_health_and_readiness_name_the_component():
    client = http()
    health = client.get("/health").json()
    assert health["component_id"] == "learning"
    assert health["platform_id"] == "plt_demo"
    assert client.get("/ready").json()["status"] == "ready"


# ------------------------------------------------------------------- course
def test_teacher_creates_a_draft_course_in_the_effective_tenant():
    client = http()
    created = client.post(
        "/api/v1/learning/courses",
        json={"title": "Introduction to Algebra", "description": "Basic algebra course"},
        headers={**auth(LEARNING_TEACHER_A), "Idempotency-Key": key()},
    )
    assert created.status_code == 200
    course = created.json()["course"]
    # Server-owned facts: opaque id, effective tenant, DRAFT, verified author.
    assert course["course_id"] and isinstance(course["course_id"], str)
    assert course["tenant_id"] == "ten_a"
    assert course["status"] == "DRAFT"
    assert course["created_by"] == "idn_human_a"
    assert course["created_at"] == course["updated_at"]
    assert course["title"] == "Introduction to Algebra"


def test_teacher_creates_module_lesson_and_assignment():
    client = http()
    course_id, module_id, lesson_id, assignment_id = author_course(client)
    # Everything along the chain exists and is addressable by its own id.
    assert course_id.startswith("crs_")
    assert module_id.startswith("mod_")
    assert lesson_id.startswith("les_")
    assert assignment_id.startswith("asg_")


def test_publish_course_flips_the_course_to_published():
    client = http()
    course_id, _, _, _ = author_course(client)
    course = client.get(
        f"/api/v1/learning/courses/{course_id}", headers=auth(LEARNING_TEACHER_A)
    ).json()["course"]
    assert course["status"] == "PUBLISHED"
    assert course["updated_at"] >= course["created_at"]


def test_student_reads_a_published_course():
    client = http()
    course_id, _, _, _ = author_course(client)
    served = client.get(
        f"/api/v1/learning/courses/{course_id}", headers=auth(LEARNING_STUDENT_A)
    )
    assert served.status_code == 200
    assert served.json()["course"]["status"] == "PUBLISHED"


def test_teacher_of_another_tenant_authors_in_their_own_tenant():
    client = http()
    created = client.post(
        "/api/v1/learning/courses",
        json={"title": "Course B", "description": "Of tenant B"},
        headers={**auth(LEARNING_TEACHER_B), "Idempotency-Key": key()},
    )
    assert created.status_code == 200
    assert created.json()["course"]["tenant_id"] == "ten_b"
    assert created.json()["course"]["created_by"] == "idn_human_b"


# --------------------------------------------------------------- enrollment
def test_student_enrolls_into_a_published_course_and_reads_the_enrollment():
    client = http()
    course_id, _, _, _ = author_course(client)
    enrolled = client.post(
        f"/api/v1/learning/courses/{course_id}/enrollments",
        json={},
        headers={**auth(LEARNING_STUDENT_A), "Idempotency-Key": key()},
    )
    assert enrolled.status_code == 200
    enrollment = enrolled.json()["enrollment"]
    # Server-derived facts: the verified identity, the effective tenant, ACTIVE.
    assert enrollment["student_identity_id"] == "idn_human_c"
    assert enrollment["tenant_id"] == "ten_a"
    assert enrollment["course_id"] == course_id
    assert enrollment["status"] == "ACTIVE"

    mine = client.get(
        f"/api/v1/learning/courses/{course_id}/enrollments/me",
        headers=auth(LEARNING_STUDENT_A),
    )
    assert mine.status_code == 200
    assert mine.json()["enrollment"]["enrollment_id"] == enrollment["enrollment_id"]


# ---------------------------------------------------------------- submissions
def test_student_submits_and_reads_own_submission():
    client = http()
    course_id, _, _, assignment_id = author_course(client)
    client.post(
        f"/api/v1/learning/courses/{course_id}/enrollments",
        json={},
        headers={**auth(LEARNING_STUDENT_A), "Idempotency-Key": key()},
    )
    submitted = client.post(
        f"/api/v1/learning/assignments/{assignment_id}/submissions",
        json={"attempt": 1, "content": "x = 5"},
        headers={**auth(LEARNING_STUDENT_A), "Idempotency-Key": key()},
    )
    assert submitted.status_code == 200
    submission = submitted.json()["submission"]
    assert submission["student_identity_id"] == "idn_human_c"
    assert submission["tenant_id"] == "ten_a"
    assert submission["assignment_id"] == assignment_id
    assert submission["attempt"] == 1
    assert submission["content"] == "x = 5"
    assert submission["status"] == "SUBMITTED"

    mine = client.get(
        f"/api/v1/learning/submissions/{submission['submission_id']}",
        headers=auth(LEARNING_STUDENT_A),
    )
    assert mine.status_code == 200
    assert mine.json()["submission"] == submission


def test_teacher_reads_the_students_submission():
    client = http()
    course_id, _, _, assignment_id = author_course(client)
    client.post(
        f"/api/v1/learning/courses/{course_id}/enrollments",
        json={},
        headers={**auth(LEARNING_STUDENT_A), "Idempotency-Key": key()},
    )
    submission_id = client.post(
        f"/api/v1/learning/assignments/{assignment_id}/submissions",
        json={"attempt": 1, "content": "x = 5"},
        headers={**auth(LEARNING_STUDENT_A), "Idempotency-Key": key()},
    ).json()["submission"]["submission_id"]

    served = client.get(
        f"/api/v1/learning/submissions/{submission_id}",
        headers=auth(LEARNING_TEACHER_A),
    )
    assert served.status_code == 200
    assert served.json()["submission"]["content"] == "x = 5"


def test_teacher_reads_assignment_anytime_student_reads_published():
    client = http()
    # A draft assignment (course not published yet) is readable by its teacher.
    created = client.post(
        "/api/v1/learning/courses",
        json={"title": "Draft course", "description": ""},
        headers={**auth(LEARNING_TEACHER_A), "Idempotency-Key": key()},
    )
    course_id = created.json()["course"]["course_id"]
    module_id = client.post(
        f"/api/v1/learning/courses/{course_id}/modules",
        json={"title": "M", "position": 1},
        headers={**auth(LEARNING_TEACHER_A), "Idempotency-Key": key()},
    ).json()["module"]["module_id"]
    lesson_id = client.post(
        f"/api/v1/learning/modules/{module_id}/lessons",
        json={"title": "L", "content": "C", "position": 1},
        headers={**auth(LEARNING_TEACHER_A), "Idempotency-Key": key()},
    ).json()["lesson"]["lesson_id"]
    assignment_id = client.post(
        f"/api/v1/learning/lessons/{lesson_id}/assignments",
        json={"title": "A", "instructions": "I"},
        headers={**auth(LEARNING_TEACHER_A), "Idempotency-Key": key()},
    ).json()["assignment"]["assignment_id"]

    assert (
        client.get(
            f"/api/v1/learning/assignments/{assignment_id}",
            headers=auth(LEARNING_TEACHER_A),
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/learning/assignments/{assignment_id}",
            headers=auth(LEARNING_STUDENT_A),
        ).status_code
        == 403
    )


# ------------------------------------------------------------ the Level-0 client
def test_the_published_client_serves_the_same_contract():
    """A consumer holding only the value-only client crosses the same chain."""
    instance = monolith_with_learning()
    client = instance.learning_client()
    course = client.create_course(
        LEARNING_TEACHER_A,
        title="Via the client",
        description="Same contract, in process",
        idempotency_key=key(),
    )
    assert course.tenant_id == "ten_a"
    assert course.status == "DRAFT"
    assert course.created_by == "idn_human_a"

    published = client.publish_course(
        LEARNING_TEACHER_A, course.course_id, idempotency_key=key()
    )
    assert published.status == "PUBLISHED"

    served = client.get_course(LEARNING_STUDENT_A, course.course_id)
    assert served.status == "PUBLISHED"
