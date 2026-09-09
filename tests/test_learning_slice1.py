"""SCS-001 Slice 1 — Submission Read (prerequisite for Slice 2 / Issue #20).

Single-submission read against the fully composed Platform Instance
(IS-001 + IS-002 + IS-003 + Learning): success, tenant isolation, the
approved error envelope, and audit. This module also defines the shared
composition helper used by the Slice 2, contract and boundary tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from authorization_service.models import ServiceAccess
from authorization_service.store import CONSUMER_PERMISSIONS
from learning_service.adapters import authorization_port
from learning_service.contracts import OPERATION_LIST, OPERATION_READ
from learning_service.deployment import LearningDeployment, build_deployment as build_learning
from learning_service.store import LearningStore
from tests.conftest import PLATFORM_ID, monolith

TEACHER_A = "token-human-a"  # idn_human_a / ten_a
TEACHER_B = "token-human-b"  # idn_human_b / ten_b
STUDENT_C = "token-human-c"  # idn_human_c / ten_a

LEARNING_CREDENTIAL = "authz-svc-token-learning"


@dataclass
class LearningHarness:
    instance: Any
    learning: LearningDeployment

    def http(self) -> TestClient:
        return TestClient(self.learning.contract_app())

    def client(self) -> Any:
        return self.learning.publish()


def learning_harness(store: LearningStore | None = None) -> LearningHarness:
    """Compose IS-001/IS-002/IS-003 plus Learning for one test.

    Grants are the composition root's job (IS-003 publishes no grant API).
    Teachers hold both read grants; the student holds the single-read grant
    only, so the Slice 2 list denial is exercisable.
    """
    instance = monolith()
    # Learning's own service identity for asking IS-003.
    instance.authorization.store.services["svc_learning"] = ServiceAccess(
        "svc_learning", PLATFORM_ID, CONSUMER_PERMISSIONS
    )
    instance.authorization.store.service_tokens[LEARNING_CREDENTIAL] = "svc_learning"
    instance.authorization.store.grant("ten_a", "idn_human_a", OPERATION_READ)
    instance.authorization.store.grant("ten_b", "idn_human_b", OPERATION_READ)
    instance.authorization.store.grant("ten_a", "idn_human_c", OPERATION_READ)
    instance.authorization.store.grant("ten_a", "idn_human_a", OPERATION_LIST)
    instance.authorization.store.grant("ten_b", "idn_human_b", OPERATION_LIST)
    learning = build_learning(
        {"platform_id": instance.authority.current_platform_id, "environment": "test"},
        authorization=authorization_port(
            instance.authorization.publish(credential=LEARNING_CREDENTIAL)
        ),
        store=store,
        seed_demo=store is None,
        with_http=True,
    )
    return LearningHarness(instance=instance, learning=learning)


def envelope_of(response: Any) -> dict:
    """The approved error envelope — and nothing else.

    Exact key sets prove there is no parallel legacy envelope: no top-level
    ``detail`` key exists anywhere in the refusal representation.
    """
    body = response.json()
    assert set(body) == {"error", "request_id", "correlation_id"}, body
    assert set(body["error"]) == {"code", "message", "details"}, body
    assert set(body["error"]["details"]) == {"reason"}, body
    assert body["request_id"] and body["correlation_id"]
    return body


# ------------------------------------------------------------------ success
def test_identity_a_reads_its_own_tenant_submission():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_a1_1",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "submission_id": "sub_a1_1",
        "assignment_id": "asg_a1",
        "student_identity_id": "idn_human_c",
        "attempt": 1,
        "content": {},
        "status": "SUBMITTED",
        "created_at": "2026-09-08T00:00:00+00:00",
        "updated_at": "2026-09-08T00:00:00+00:00",
        "reviewed_by": None,
        "reviewed_at": None,
    }


def test_identity_b_reads_its_own_tenant_submission():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_b1_1",
        headers={"authorization": f"Bearer {TEACHER_B}"},
    )

    assert response.status_code == 200
    assert response.json()["submission_id"] == "sub_b1_1"


def test_the_same_read_is_served_through_the_published_client():
    harness = learning_harness()
    consumer = harness.client()

    view = consumer.read_submission(TEACHER_A, "sub_a1_2")

    assert view.submission_id == "sub_a1_2"
    assert view.assignment_id == "asg_a1"
    assert view.status == "DRAFT"


# ---------------------------------------------------------------- isolation
def test_cross_tenant_read_is_denied_in_the_approved_envelope():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_b1_1",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["message"] == "Operation is not permitted."
    assert body["error"]["details"]["reason"] == "resource_tenant_mismatch"


def test_caller_supplied_tenant_id_cannot_select_the_tenant():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_a1_1",
        headers={"authorization": f"Bearer {TEACHER_A}", "x-tenant-id": "ten_b"},
    )

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "tenant_mismatch"


def test_unknown_submission_is_not_found():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_missing",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "submission_unknown"


def test_missing_identity_is_unauthorized():
    harness = learning_harness()
    client = harness.http()

    response = client.get("/api/v1/learning/submissions/sub_a1_1")

    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == "missing_identity"


# -------------------------------------------------------------------- audit
def test_allowed_read_is_audited_with_the_caller_request_context():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_a1_1",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "x-request-id": "req-learning-1",
            "x-correlation-id": "corr-learning-1",
        },
    )
    assert response.status_code == 200

    audit = harness.learning.store.audit
    assert len(audit) == 1
    event = audit[0]
    assert event.action == OPERATION_READ
    assert event.decision == "ALLOW"
    assert event.reason == "permitted"
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_id == "sub_a1_1"
    assert event.resource_tenant_id == "ten_a"
    assert event.assignment_id == "asg_a1"
    assert event.submission_id == "sub_a1_1"
    assert event.request_id == "req-learning-1"
    assert event.correlation_id == "corr-learning-1"


def test_denied_read_envelope_matches_its_audit_record():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_b1_1",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "x-request-id": "req-learning-2",
            "x-correlation-id": "corr-learning-2",
        },
    )
    assert response.status_code == 403
    body = envelope_of(response)

    event = harness.learning.store.audit[-1]
    assert event.decision == "DENY"
    assert event.reason == "resource_tenant_mismatch"
    assert event.subject_id == "idn_human_a"
    assert event.tenant_id == "ten_a"
    assert event.resource_id == "sub_b1_1"
    assert event.resource_tenant_id == "ten_b"
    # The envelope context is the audit context.
    assert body["request_id"] == event.request_id == "req-learning-2"
    assert body["correlation_id"] == event.correlation_id == "corr-learning-2"


def test_generated_request_ids_stay_traceable_when_the_caller_sends_none():
    harness = learning_harness()
    client = harness.http()

    response = client.get(
        "/api/v1/learning/submissions/sub_b1_1",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert response.status_code == 403
    body = envelope_of(response)

    event = harness.learning.store.audit[-1]
    assert body["request_id"] == event.request_id
    assert body["correlation_id"] == event.correlation_id
