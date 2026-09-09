"""SCS-001 Slice 2 — Teacher Submission Discovery (Issue #20).

List one Assignment's submissions against the fully composed Platform
Instance (IS-001 + IS-002 + IS-003 + Learning), on top of the Slice 1
prerequisite: success with exact filtering, the empty list, tenant
isolation, the approved error envelope, and audit.

Canonicalized onto the Slice 3 baseline: the published submission
representation carries the Slice 3 review fact (``reviewed_by`` /
``reviewed_at``, ``None`` until the first review), and the two slices are
proven not to disturb each other at the end of this module.
"""

from __future__ import annotations

import pytest

from learning_service.contracts import OPERATION_LIST
from tests.test_learning_slice1 import (
    STUDENT_C,
    TEACHER_A,
    TEACHER_B,
    envelope_of,
    learning_harness,
)


def _review_harness(store=None):
    """The Slice 3 composition root: the Slice 2 grants plus the review grants."""
    from tests.test_learning_slice3 import review_harness as compose

    return compose(store)


def _review(harness, submission_id, *, key):
    """Record one review through the Slice 3 command of the published API."""
    from tests.test_learning_slice3 import review as post_review

    return post_review(harness, submission_id, key=key)


def _submission(
    submission_id: str,
    attempt: int,
    status: str,
    *,
    reviewed_by: str | None = None,
    reviewed_at: str | None = None,
) -> dict:
    return {
        "submission_id": submission_id,
        "assignment_id": "asg_a1",
        "student_identity_id": "idn_human_c",
        "attempt": attempt,
        "content": {},
        "status": status,
        "created_at": "2026-09-08T00:00:00+00:00",
        "updated_at": "2026-09-08T00:00:00+00:00",
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
    }


# ------------------------------------------------------------------ success
def test_teacher_lists_the_full_set_of_the_own_assignment():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_a1/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items"}
    assert body["items"] == [
        _submission("sub_a1_1", 1, "SUBMITTED"),
        _submission("sub_a1_2", 2, "DRAFT"),
    ]


def test_list_contains_only_the_requested_assignment_submissions():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_a2/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["submission_id"] for item in items] == ["sub_a2_1"]
    assert all(item["assignment_id"] == "asg_a2" for item in items)


def test_empty_assignment_answers_an_empty_list():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_a_empty/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_teacher_b_lists_only_the_own_tenant_assignment():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_b1/submissions",
        headers={"authorization": f"Bearer {TEACHER_B}"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["submission_id"] for item in items] == ["sub_b1_1"]


def test_the_same_list_is_served_through_the_published_client():
    harness = learning_harness()
    consumer = harness.client()

    views = consumer.list_submissions(TEACHER_A, "asg_a1")

    assert [view.submission_id for view in views] == ["sub_a1_1", "sub_a1_2"]
    assert all(view.assignment_id == "asg_a1" for view in views)


# ---------------------------------------------------------------- isolation
def test_cross_tenant_list_is_denied_without_a_partial_set():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_b1/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "resource_tenant_mismatch"
    assert "items" not in response.json()


def test_student_without_the_list_grant_is_denied():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_a1/submissions",
        headers={"authorization": f"Bearer {STUDENT_C}"},
    )

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "permission_not_granted"


def test_caller_supplied_tenant_id_cannot_select_the_tenant_for_list():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_a1/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}", "x-tenant-id": "ten_b"},
    )

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "tenant_mismatch"


def test_unknown_assignment_is_not_found():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_missing/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "assignment_unknown"


def test_missing_identity_cannot_list():
    harness = learning_harness()
    client = harness.http()

    response = client.get("/api/v1/learning/assignments/asg_a1/submissions")

    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == "missing_identity"


@pytest.mark.parametrize(
    ("credential", "reason"),
    [
        ("Bearer ", "missing_identity"),
        ("Bearer token-does-not-exist", "invalid_identity"),
        ("Bearer !!!", "invalid_identity"),
        ("not-a-bearer-token", "invalid_identity"),
    ],
)
def test_an_invalid_identity_cannot_list(credential, reason):
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_a1/submissions",
        headers={"authorization": credential},
    )

    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == reason


# -------------------------------------------------------------------- audit
def test_allowed_list_is_audited_with_the_caller_request_context():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_a1/submissions",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "x-request-id": "req-learning-list-1",
            "x-correlation-id": "corr-learning-list-1",
        },
    )
    assert response.status_code == 200

    audit = harness.learning.store.audit
    assert len(audit) == 1
    event = audit[0]
    assert event.action == OPERATION_LIST
    assert event.decision == "ALLOW"
    assert event.reason == "permitted"
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_id == "asg_a1"
    assert event.resource_tenant_id == "ten_a"
    assert event.assignment_id == "asg_a1"
    assert event.submission_id is None
    assert event.request_id == "req-learning-list-1"
    assert event.correlation_id == "corr-learning-list-1"


def test_denied_list_envelope_matches_its_audit_record():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/assignments/asg_b1/submissions",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "x-request-id": "req-learning-list-2",
            "x-correlation-id": "corr-learning-list-2",
        },
    )
    assert response.status_code == 403
    body = envelope_of(response)

    event = harness.learning.store.audit[-1]
    assert event.action == OPERATION_LIST
    assert event.decision == "DENY"
    assert event.reason == "resource_tenant_mismatch"
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_id == "asg_b1"
    assert event.resource_tenant_id == "ten_b"
    assert body["request_id"] == event.request_id == "req-learning-list-2"
    assert body["correlation_id"] == event.correlation_id == "corr-learning-list-2"


# ----------------------------------------------------- slice 1 / 3 compatibility
def test_the_slice_1_read_still_answers_alongside_the_list():
    harness = _review_harness()
    http = harness.http()
    headers = {"authorization": f"Bearer {TEACHER_A}"}

    read = http.get("/api/v1/learning/submissions/sub_a1_1", headers=headers)
    listed = http.get("/api/v1/learning/assignments/asg_a1/submissions", headers=headers)

    assert read.status_code == 200
    assert listed.status_code == 200
    assert read.json() == listed.json()["items"][0]


def test_a_reviewed_submission_is_listed_with_its_review_fact():
    """Slice 3 compatibility: the review fact is visible in the teacher list."""
    harness = _review_harness()
    headers = {"authorization": f"Bearer {TEACHER_A}"}

    reviewed = _review(harness, "sub_a1_1", key="ik-slice2-compat")
    assert reviewed.status_code == 200
    fact = reviewed.json()

    listed = harness.http().get(
        "/api/v1/learning/assignments/asg_a1/submissions", headers=headers
    )
    assert listed.status_code == 200
    items = {item["submission_id"]: item for item in listed.json()["items"]}

    assert items["sub_a1_1"]["reviewed_by"] == fact["reviewed_by"] == "idn_human_a"
    assert items["sub_a1_1"]["reviewed_at"] == fact["reviewed_at"]
    assert items["sub_a1_1"]["reviewed_at"] is not None
    # review is not a lifecycle transition, and the list does not apply it
    assert items["sub_a1_1"]["status"] == "SUBMITTED"
    assert items["sub_a1_2"]["reviewed_by"] is None
    assert items["sub_a1_2"]["reviewed_at"] is None


def test_the_list_is_a_read_and_changes_no_submission_state():
    harness = _review_harness()
    headers = {"authorization": f"Bearer {TEACHER_A}"}

    first = harness.http().get(
        "/api/v1/learning/assignments/asg_a1/submissions", headers=headers
    )
    second = harness.http().get(
        "/api/v1/learning/assignments/asg_a1/submissions", headers=headers
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["items"] == [
        _submission("sub_a1_1", 1, "SUBMITTED"),
        _submission("sub_a1_2", 2, "DRAFT"),
    ]
    # DRAFT → SUBMITTED only: no review state appears through the list
    assert [item["status"] for item in first.json()["items"]] == ["SUBMITTED", "DRAFT"]


def test_the_read_operation_requires_no_idempotency_key():
    """Discovery is a read: idempotency is natural, no Idempotency-Key is required."""
    harness = _review_harness()

    response = harness.http().get(
        "/api/v1/learning/assignments/asg_a1/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 200
    assert response.json()["items"]
