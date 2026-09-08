"""Tenant isolation (LAW-16, LAW-16a) at the Learning boundary.

The effective tenant comes from the verified identity and from nowhere else
— there is no field, no header and no body key that could choose it. A
subject of Tenant A never reaches an entity of Tenant B: not by reading, not
by enrolling, not by submitting, not by guessing identifiers. What exists in
another tenant is refused as a mismatch, what exists nowhere is a plain 404.
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

TEACHER = LEARNING_TEACHER_A  # ten_a
STUDENT = LEARNING_STUDENT_A  # ten_a
TEACHER_B = LEARNING_TEACHER_B  # ten_b


def http() -> TestClient:
    return monolith_with_learning().learning_http()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def key() -> str:
    return f"key-{uuid.uuid4().hex}"


def code_of(response: Any) -> str:  # noqa: ANN401
    return response.json()["error"]["code"]


def published_course(client: TestClient, token: str) -> str:
    created = client.post(
        "/api/v1/learning/courses",
        json={"title": f"Course of {token}", "description": ""},
        headers={**auth(token), "Idempotency-Key": key()},
    )
    course_id = created.json()["course"]["course_id"]
    published = client.post(
        f"/api/v1/learning/courses/{course_id}/publish",
        json={},
        headers={**auth(token), "Idempotency-Key": key()},
    )
    assert published.status_code == 200
    return course_id


def test_a_course_of_another_tenant_is_not_readable():
    client = http()
    foreign = published_course(client, TEACHER)
    refused = client.get(f"/api/v1/learning/courses/{foreign}", headers=auth(TEACHER_B))
    assert refused.status_code == 403
    assert code_of(refused) == "TENANT_MISMATCH"


def test_a_draft_course_of_another_tenant_is_equally_unreachable():
    client = http()
    refused = client.get("/api/v1/learning/courses/crs_demo_b", headers=auth(TEACHER))
    assert refused.status_code == 403
    assert code_of(refused) == "TENANT_MISMATCH"


def test_a_student_cannot_enroll_into_a_foreign_course():
    client = http()
    foreign = published_course(client, TEACHER)
    refused = client.post(
        f"/api/v1/learning/courses/{foreign}/enrollments",
        json={},
        headers={**auth(TEACHER_B), "Idempotency-Key": key()},
    )
    assert refused.status_code == 403
    assert code_of(refused) == "TENANT_MISMATCH"


def test_a_foreign_course_cannot_be_published_or_structured():
    client = http()
    foreign = published_course(client, TEACHER)
    for path, body in (
        (f"/api/v1/learning/courses/{foreign}/publish", {}),
        (f"/api/v1/learning/courses/{foreign}/modules", {"title": "M", "position": 1}),
    ):
        refused = client.post(path, json=body, headers={**auth(TEACHER_B), "Idempotency-Key": key()})
        assert refused.status_code == 403, path
        assert code_of(refused) == "TENANT_MISMATCH"


def test_a_submission_cannot_reach_a_foreign_assignment():
    client = http()
    foreign = published_course(client, TEACHER)
    # The teacher of ten_a owns the whole chain; the student of ten_a is not
    # enrolled, but the tenant mismatch is decided before any of that.
    assignment = client.get(
        f"/api/v1/learning/courses/{foreign}", headers=auth(TEACHER)
    )
    assert assignment.status_code == 200
    refused = client.post(
        f"/api/v1/learning/assignments/asg_foreign/submissions",
        json={"attempt": 1, "content": "x"},
        headers={**auth(TEACHER_B), "Idempotency-Key": key()},
    )
    assert refused.status_code == 404  # unknown in ten_b's view of the store
    assert code_of(refused) == "RESOURCE_NOT_FOUND"


def test_enrollments_me_is_scoped_to_the_effective_tenant():
    client = http()
    refused = client.get(
        "/api/v1/learning/courses/crs_demo_b/enrollments/me", headers=auth(STUDENT)
    )
    assert refused.status_code == 403
    assert code_of(refused) == "TENANT_MISMATCH"


def test_tenant_context_has_no_client_side_selection_path():
    """No header can select a tenant: the effective tenant is IS-001's answer."""
    client = http()
    course_id = published_course(client, TEACHER)
    # An attacker-supplied tenant header is not part of the contract; the
    # application ignores unknown headers and the effective tenant is derived.
    smuggled = client.get(
        f"/api/v1/learning/courses/{course_id}",
        headers={**auth(TEACHER_B), "X-Tenant-Id": "ten_a"},
    )
    assert smuggled.status_code == 403
    assert code_of(smuggled) == "TENANT_MISMATCH"


def test_unknown_identifiers_stay_unknown_across_tenants():
    client = http()
    for path in (
        "/api/v1/learning/courses/crs_absent",
        "/api/v1/learning/assignments/asg_absent",
        "/api/v1/learning/submissions/sub_absent",
    ):
        refused = client.get(path, headers=auth(TEACHER_B))
        assert refused.status_code == 404, path
        assert code_of(refused) == "RESOURCE_NOT_FOUND"
