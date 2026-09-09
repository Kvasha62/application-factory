"""SCS-001 Slice 3 — Teacher Submission Review (Issue #24).

Review against the fully composed Platform Instance (IS-001 + IS-002 +
IS-003 + IS-005 + Learning): the positive review, the exact idempotent
replay, the negative and boundary refusals, and the audit trail. The
review command is the only state-changing operation of this slice, it
records ``reviewed_by`` / ``reviewed_at`` and leaves the lifecycle
``SUBMITTED``.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from idempotency.guard import IdempotencyGuard
from learning_service.consumed import DependencyRefusal
from learning_service.contracts import OPERATION_REVIEW, OwnDenyReason
from learning_service.errors import AccessRefused
from learning_service.models import OwnedSubmission
from learning_service.store import SUBMISSION_STATES, DomainRefusal, LearningStore
from tests.test_learning_boundary import (
    ALLOW,
    StubAuthorizationPort,
    learning_deployment,
)
from tests.test_learning_slice1 import (
    STUDENT_C,
    TEACHER_A,
    TEACHER_B,
    envelope_of,
    learning_harness,
)

REVIEW_HEADERS = {"authorization": f"Bearer {TEACHER_A}"}


class CountingLearningStore(LearningStore):
    """A learning store that counts the review effect.

    The count is the behavioral proof that an exact replay — and every
    refused or failed attempt — applies the review effect at most once.
    """

    def __init__(self) -> None:
        super().__init__()
        self.seed_demo()
        self.applied_reviews = 0

    def apply_review(
        self, submission_id: str, reviewed_by: str, reviewed_at: str
    ) -> Any:
        updated = super().apply_review(submission_id, reviewed_by, reviewed_at)
        self.applied_reviews += 1
        return updated


def review_harness(store: LearningStore | None = None) -> Any:
    """Compose IS-001/IS-002/IS-003 + Learning with the review grants.

    Teacher A holds the review permission in ``ten_a``, Teacher B in
    ``ten_b``; the student (``idn_human_c``) holds the read permission only.
    """
    harness = learning_harness(store)
    harness.instance.authorization.store.grant("ten_a", "idn_human_a", OPERATION_REVIEW)
    harness.instance.authorization.store.grant("ten_b", "idn_human_b", OPERATION_REVIEW)
    return harness


def review(
    harness: Any, submission_id: str, *, key: str | None, token: str | None = TEACHER_A
):
    headers = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if key is not None:
        headers["idempotency-key"] = key
    return harness.http().post(
        f"/api/v1/learning/submissions/{submission_id}/review", headers=headers
    )


# ------------------------------------------------------------------ positive
def test_teacher_reviews_a_submitted_submission():
    harness = review_harness()

    response = review(harness, "sub_a1_1", key="ik-pos-1")

    assert response.status_code == 200
    body = response.json()
    assert body["submission_id"] == "sub_a1_1"
    assert body["status"] == "SUBMITTED"
    assert body["reviewed_by"] == "idn_human_a"
    assert isinstance(body["reviewed_at"], str) and body["reviewed_at"]


def test_reviewed_by_is_the_verified_teacher_identity():
    harness = review_harness()

    response = review(harness, "sub_a1_1", key="ik-pos-2")

    assert response.status_code == 200
    # reviewed_by is the verified identity carried by the IS-003 decision,
    # never a caller claim: the caller only presented a credential.
    assert response.json()["reviewed_by"] == "idn_human_a"


def test_reviewed_at_is_recorded_at_review_time():
    harness = review_harness()
    fixed = "2026-09-09T00:00:00+00:00"
    harness.learning.engine.clock = lambda: fixed

    response = review(harness, "sub_a1_1", key="ik-pos-3")

    assert response.status_code == 200
    assert response.json()["reviewed_at"] == fixed


def test_submission_stays_submitted_after_review():
    harness = review_harness()

    response = review(harness, "sub_a1_1", key="ik-pos-4")

    assert response.status_code == 200
    assert response.json()["status"] == "SUBMITTED"
    # The lifecycle vocabulary is unchanged: exactly DRAFT → SUBMITTED.
    assert SUBMISSION_STATES == ("DRAFT", "SUBMITTED")
    assert "REVIEWED" not in SUBMISSION_STATES


def test_existing_get_returns_the_review_fields_after_review():
    harness = review_harness()

    reviewed = review(harness, "sub_a1_1", key="ik-pos-5")
    assert reviewed.status_code == 200

    response = harness.http().get(
        "/api/v1/learning/submissions/sub_a1_1",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert response.status_code == 200
    assert (
        response.json()["reviewed_by"]
        == reviewed.json()["reviewed_by"]
        == "idn_human_a"
    )
    assert response.json()["reviewed_at"] == reviewed.json()["reviewed_at"]


def test_exact_idempotent_replay_has_no_second_effect():
    store = CountingLearningStore()
    harness = review_harness(store=store)

    first = review(harness, "sub_a1_1", key="ik-replay")
    second = review(harness, "sub_a1_1", key="ik-replay")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert store.applied_reviews == 1
    # The replay is observable in the audit journal.
    actions = [event.action for event in harness.learning.store.audit]
    assert actions.count("idempotency_replay") == 1
    assert actions.count(OPERATION_REVIEW) == 2


def test_the_published_client_records_the_review():
    harness = review_harness()
    consumer = harness.client()

    view = consumer.review_submission(
        TEACHER_A, "sub_a1_1", idempotency_key="ik-client"
    )

    assert view.submission_id == "sub_a1_1"
    assert view.reviewed_by == "idn_human_a"
    assert view.reviewed_at
    assert view.status == "SUBMITTED"


# --------------------------------------------------------------- immutability
def test_review_fact_is_immutable_across_different_keys():
    harness = review_harness()
    harness.learning.engine.clock = lambda: "2026-09-09T10:00:00+00:00"

    # 1. Review with Idempotency-Key A.
    first = review(harness, "sub_a1_1", key="ik-immutable-a")
    # 2. Success.
    assert first.status_code == 200
    # 3. Save the review fact.
    first_by = first.json()["reviewed_by"]
    first_at = first.json()["reviewed_at"]
    assert first_by == "idn_human_a"
    assert first_at == "2026-09-09T10:00:00+00:00"

    # A second command with another key would write a different timestamp if
    # the fact were mutable: advance the clock to prove it cannot.
    harness.learning.engine.clock = lambda: "2026-09-09T11:00:00+00:00"

    # 4. Review the same submission with Idempotency-Key B.
    second = review(harness, "sub_a1_1", key="ik-immutable-b")

    # 5. The first review fact is NOT overwritten: the command is refused.
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "ALREADY_REVIEWED"
    assert body["error"]["details"]["reason"] == "already_reviewed"

    stored = harness.learning.store.submissions["sub_a1_1"]
    # 6. reviewed_by remains the first teacher.
    assert stored.reviewed_by == first_by == "idn_human_a"
    # 7. reviewed_at remains the original timestamp.
    assert stored.reviewed_at == first_at == "2026-09-09T10:00:00+00:00"
    # The lifecycle stays SUBMITTED.
    assert stored.status == "SUBMITTED"


def test_a_different_key_creates_no_second_effect():
    store = CountingLearningStore()
    harness = review_harness(store=store)

    first = review(harness, "sub_a1_1", key="ik-effect-a")
    second = review(harness, "sub_a1_1", key="ik-effect-b")

    assert first.status_code == 200
    assert second.status_code == 409
    # The review effect ran exactly once: the second command applied nothing.
    assert store.applied_reviews == 1
    # The second command saved no successful idempotency record (IS-005 saves
    # only after the effect returned).
    assert "ik-effect-b" not in harness.learning.engine.idempotency._store


def test_second_review_is_audited_as_a_denial():
    harness = review_harness()

    assert review(harness, "sub_a1_1", key="ik-audit-a").status_code == 200
    response = review(harness, "sub_a1_1", key="ik-audit-b")
    assert response.status_code == 409

    event = harness.learning.store.audit[-1]
    assert event.action == OPERATION_REVIEW
    assert event.decision == "DENY"
    assert event.reason == "already_reviewed"
    assert event.subject_id == "idn_human_a"
    assert event.submission_id == "sub_a1_1"


def test_apply_review_is_immutable_at_the_store_boundary():
    store = LearningStore()
    store.seed_demo()

    first = store.apply_review("sub_a1_1", "idn_human_a", "2026-09-09T10:00:00+00:00")
    assert first.reviewed_by == "idn_human_a"
    assert first.reviewed_at == "2026-09-09T10:00:00+00:00"

    # Defence in depth: the store itself refuses a second review and never
    # overwrites the first fact, whatever identity/timestamp is passed.
    try:
        store.apply_review("sub_a1_1", "idn_human_c", "2026-09-09T11:00:00+00:00")
        raise AssertionError("expected a domain refusal")
    except DomainRefusal as refused:
        assert refused.reason == "already_reviewed"

    stored = store.submissions["sub_a1_1"]
    assert stored.reviewed_by == "idn_human_a"
    assert stored.reviewed_at == "2026-09-09T10:00:00+00:00"


# ------------------------------------------------------------------ negative
def test_student_is_denied():
    harness = review_harness()

    response = review(harness, "sub_a1_1", key="ik-neg-1", token=STUDENT_C)

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "permission_not_granted"


def test_teacher_without_the_review_permission_is_denied():
    # No review grants at all: the read permission is not the review permission.
    harness = learning_harness()

    response = review(harness, "sub_a1_1", key="ik-neg-2")

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "permission_not_granted"


def test_cross_tenant_review_is_denied():
    harness = review_harness()

    # Teacher A (ten_a) cannot review a submission of ten_b.
    response = review(harness, "sub_b1_1", key="ik-neg-3")

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "resource_tenant_mismatch"

    # Teacher B (ten_b) cannot review a submission of ten_a either.
    response = review(harness, "sub_a1_1", key="ik-neg-4", token=TEACHER_B)
    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "resource_tenant_mismatch"
    )


def test_caller_supplied_tenant_id_cannot_select_the_tenant_for_review():
    harness = review_harness()

    response = harness.http().post(
        "/api/v1/learning/submissions/sub_a1_1/review",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "x-tenant-id": "ten_b",
            "idempotency-key": "ik-neg-5",
        },
    )

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "tenant_mismatch"


def test_draft_submission_cannot_be_reviewed():
    harness = review_harness()

    response = review(harness, "sub_a1_2", key="ik-neg-6")

    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert body["error"]["details"]["reason"] == "invalid_state_transition"


def test_missing_idempotency_key_is_rejected():
    harness = review_harness()

    response = review(harness, "sub_a1_1", key=None)

    assert response.status_code == 400
    body = envelope_of(response)
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert body["error"]["details"]["reason"] == "idempotency_key_required"


def test_changed_idempotency_binding_is_a_conflict():
    harness = review_harness()

    first = review(harness, "sub_a1_1", key="ik-conflict")
    assert first.status_code == 200

    # Same key, different target: a different command binding.
    response = review(harness, "sub_a2_1", key="ik-conflict")
    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert body["error"]["details"]["reason"] == "idempotency_conflict"


def test_changed_idempotency_identity_binding_is_a_conflict():
    harness = review_harness()
    # A second verified identity of ten_a is given the review permission for
    # this test only, so the conflict is about the identity binding, not role.
    harness.instance.authorization.store.grant("ten_a", "idn_human_c", OPERATION_REVIEW)

    first = review(harness, "sub_a1_1", key="ik-identity")
    assert first.status_code == 200

    # Same key, same target, another verified identity: conflict.
    response = review(harness, "sub_a1_1", key="ik-identity", token=STUDENT_C)
    assert response.status_code == 409
    assert envelope_of(response)["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_unknown_submission_review_is_not_found():
    harness = review_harness()

    response = review(harness, "sub_missing", key="ik-neg-7")

    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "submission_unknown"


def test_missing_identity_review_is_unauthorized():
    harness = review_harness()

    response = review(harness, "sub_a1_1", key="ik-neg-8", token=None)

    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == "missing_identity"


def test_authentication_failure_has_no_effect():
    harness = review_harness()

    response = review(harness, "sub_a1_1", key="ik-neg-9", token=None)

    assert response.status_code == 401
    assert harness.learning.store.submissions["sub_a1_1"].reviewed_by is None
    assert harness.learning.store.submissions["sub_a1_1"].reviewed_at is None


def test_authorization_failure_has_no_effect():
    store = CountingLearningStore()
    harness = review_harness(store=store)

    response = review(harness, "sub_a1_1", key="ik-neg-10", token=STUDENT_C)

    assert response.status_code == 403
    assert store.submissions["sub_a1_1"].reviewed_by is None
    assert store.applied_reviews == 0


def test_dependency_failure_has_no_effect():
    deployment = learning_deployment(StubAuthorizationPort(RuntimeError("boom")))

    response = TestClient(deployment.contract_app()).post(
        "/api/v1/learning/submissions/sub_a1_1/review",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-neg-11",
        },
    )

    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"]["reason"] == "authorization_unavailable"
    assert deployment.store.submissions["sub_a1_1"].reviewed_by is None
    assert deployment.store.submissions["sub_a1_1"].reviewed_at is None


# ------------------------------------------------------------------ boundary
def test_a_foreign_owner_submission_is_never_reviewed():
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

    response = TestClient(deployment.contract_app()).post(
        "/api/v1/learning/submissions/sub_foreign/review",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-neg-12",
        },
    )

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == OwnDenyReason.OWNER_MISMATCH
    )
    assert store.submissions["sub_foreign"].reviewed_by is None


def test_review_uses_the_documented_grant_vocabulary():
    port = StubAuthorizationPort(ALLOW)
    deployment = learning_deployment(port)
    deployment.engine.clock = lambda: "2026-09-09T00:00:00+00:00"

    deployment.engine.review_submission(
        TEACHER_A, "sub_a1_1", idempotency_key="ik-grant"
    )

    assert [call["operation"] for call in port.calls] == [OPERATION_REVIEW]
    assert OPERATION_REVIEW == "learning.submissions.review"


def test_review_asks_the_authority_with_the_store_tenant_not_the_claim():
    port = StubAuthorizationPort(DependencyRefusal(None))
    deployment = learning_deployment(port)

    try:
        deployment.engine.review_submission(
            TEACHER_A, "sub_b1_1", claimed_tenant_id="ten_a", idempotency_key="ik-claim"
        )
        raise AssertionError("expected a refusal")
    except AccessRefused as refused:
        assert refused.reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE

    question = port.calls[0]
    assert question["resource_tenant_id"] == "ten_b"
    assert question["claimed_tenant_id"] == "ten_a"
    assert question["operation"] == OPERATION_REVIEW


def test_review_owns_no_second_idempotency_mechanism():
    harness = review_harness()

    engine = harness.learning.engine
    # The command-safety boundary is the IS-005 guard itself.
    assert isinstance(engine.idempotency, IdempotencyGuard)
    # The learning store owns no idempotency table of its own.
    assert not hasattr(harness.learning.store, "idempotency")


def test_allowed_review_is_audited_with_the_caller_request_context():
    harness = review_harness()

    response = harness.http().post(
        "/api/v1/learning/submissions/sub_a1_1/review",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-audit",
            "x-request-id": "req-review-1",
            "x-correlation-id": "corr-review-1",
        },
    )
    assert response.status_code == 200

    event = harness.learning.store.audit[-1]
    assert event.action == OPERATION_REVIEW
    assert event.decision == "ALLOW"
    assert event.reason == "permitted"
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_id == "sub_a1_1"
    assert event.resource_tenant_id == "ten_a"
    assert event.assignment_id == "asg_a1"
    assert event.submission_id == "sub_a1_1"
    assert event.request_id == "req-review-1"
    assert event.correlation_id == "corr-review-1"


def test_denied_review_envelope_matches_its_audit_record():
    harness = review_harness()

    response = review(harness, "sub_a1_2", key="ik-audit-denied")
    assert response.status_code == 409
    body = envelope_of(response)

    event = harness.learning.store.audit[-1]
    assert event.decision == "DENY"
    assert event.reason == "invalid_state_transition"
    assert body["error"]["details"]["reason"] == event.reason
