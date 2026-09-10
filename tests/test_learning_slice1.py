"""SCS-001 Slice 1 — Submission Read (prerequisite for Slice 2 / Issue #20).

Single-submission read against the fully composed Platform Instance
(IS-001 + IS-002 + IS-003 + Learning): success, tenant isolation, the
approved error envelope, and audit. This module also defines the shared
composition helper used by the Slice 2, contract and boundary tests.

Enrollment Slice 1 (ADR-0012) extends this module with the owned Enrollment
data foundation: the ``OwnedEnrollment`` model and the store-level
registration, reads, tenant isolation and duplicate ACTIVE invariant. The
enrollment tests below touch only the model and the store — no HTTP surface,
no engine and no contract participates, because exposing enrollment commands
through the published contract is a later slice.
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from authorization_service.models import ServiceAccess
from authorization_service.store import CONSUMER_PERMISSIONS
from learning_service import OWNER_COMPONENT
from learning_service.adapters import authorization_port
from learning_service.contracts import OPERATION_LIST, OPERATION_READ
from learning_service.deployment import LearningDeployment
from learning_service.deployment import build_deployment as build_learning
from learning_service.models import OwnedEnrollment
from learning_service.store import ENROLLMENT_STATES, LearningStore
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


# ------------------------------------------------------------- enrollment
# ADR-0012, Enrollment Slice 1 — the data foundation of Student Enrollment:
# the OwnedEnrollment model and the store-level registration, reads and
# invariants. Every test below runs directly against LearningStore: the
# store is a data owner, not an authorization layer, and these tests assert
# exactly what the store owes — nothing more.

_ENROLLMENT_BASE: dict[str, Any] = {
    "enrollment_id": "enr_a1_c1",
    "course_id": "crs_a1",
    "tenant_id": "ten_a",
    "student_identity_id": "idn_human_c",
    "status": "ACTIVE",
    "created_at": "2026-09-10T00:00:00+00:00",
    "updated_at": "2026-09-10T00:00:01+00:00",
    "owner_component": "learning",
}


def enrollment_record(**overrides: Any) -> OwnedEnrollment:
    """One complete ACTIVE enrollment, with the named fields replaced."""
    return OwnedEnrollment(**{**_ENROLLMENT_BASE, **overrides})


# ------------------------------------------------------------------- model
def test_enrollment_carries_exactly_the_owned_fields():
    names = {field.name for field in dataclasses.fields(OwnedEnrollment)}
    assert names == {
        "enrollment_id",
        "course_id",
        "tenant_id",
        "student_identity_id",
        "status",
        "created_at",
        "updated_at",
        "owner_component",
    }
    enrollment = enrollment_record()
    for name, value in _ENROLLMENT_BASE.items():
        assert getattr(enrollment, name) == value


def test_enrollment_is_immutable_by_construction():
    enrollment = enrollment_record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        enrollment.status = "ACTIVE"  # type: ignore[misc]
    updated = dataclasses.replace(enrollment, updated_at="2026-09-11T00:00:00+00:00")
    assert updated.updated_at == "2026-09-11T00:00:00+00:00"
    assert enrollment.updated_at == _ENROLLMENT_BASE["updated_at"]


def test_enrollment_owner_component_is_learning():
    store = LearningStore()
    stored = store.register_enrollment(enrollment_record())
    assert stored.owner_component == "learning" == OWNER_COMPONENT
    assert store.get_enrollment(stored.enrollment_id) is stored


def test_enrollment_status_vocabulary_is_only_active():
    assert ENROLLMENT_STATES == ("ACTIVE",)
    store = LearningStore()
    for status in ("INVITED", "SUSPENDED", "COMPLETED", "CANCELLED", "DRAFT", ""):
        with pytest.raises(ValueError):
            store.register_enrollment(enrollment_record(status=status))
    assert store.enrollments == {}


def test_student_identity_is_stored_as_an_opaque_value():
    # Whatever shape an identity reference has outside Learning, the store
    # keeps the exact value and interprets nothing: no normalization, no
    # parsing, no second identity meaning attached to it.
    opaque = "idn_OPAQUE/ünïcode ∮ opaque-value 7"
    store = LearningStore()
    stored = store.register_enrollment(enrollment_record(student_identity_id=opaque))
    assert stored.student_identity_id == opaque
    assert store.get_enrollment(stored.enrollment_id) is stored
    assert (
        store.find_active_enrollment(
            tenant_id="ten_a", student_identity_id=opaque, course_id="crs_a1"
        )
        is stored
    )


# ------------------------------------------------------------------- store
def test_register_enrollment_stores_the_record():
    store = LearningStore()
    enrollment = enrollment_record()
    stored = store.register_enrollment(enrollment)
    assert stored is enrollment
    assert store.enrollments[enrollment.enrollment_id] is enrollment
    assert (
        store.find_active_enrollment(
            tenant_id="ten_a",
            student_identity_id="idn_human_c",
            course_id="crs_a1",
        )
        is enrollment
    )


def test_get_enrollment_returns_the_record_by_id():
    store = LearningStore()
    enrollment = enrollment_record()
    store.register_enrollment(enrollment)
    assert store.get_enrollment("enr_a1_c1") is enrollment


def test_get_enrollment_of_an_unknown_id_returns_none():
    store = LearningStore()
    store.register_enrollment(enrollment_record())
    assert store.get_enrollment("enr_missing") is None
    assert LearningStore().get_enrollment("enr_a1_c1") is None


def test_find_active_enrollment_miss_is_no_record():
    store = LearningStore()
    store.register_enrollment(enrollment_record())
    # Every component of the triple selects: no component is a wildcard and
    # a combination without a record is answered as absence, never as a hit.
    assert (
        store.find_active_enrollment(
            tenant_id="ten_b",
            student_identity_id="idn_human_c",
            course_id="crs_a1",
        )
        is None
    )
    assert (
        store.find_active_enrollment(
            tenant_id="ten_a",
            student_identity_id="idn_human_b",
            course_id="crs_a1",
        )
        is None
    )
    assert (
        store.find_active_enrollment(
            tenant_id="ten_a",
            student_identity_id="idn_human_c",
            course_id="crs_a2",
        )
        is None
    )


# ------------------------------------------------------- tenant isolation
def test_same_student_and_course_in_one_tenant_is_a_duplicate():
    store = LearningStore()
    first = store.register_enrollment(enrollment_record())
    with pytest.raises(ValueError, match="duplicate enrollment"):
        store.register_enrollment(
            enrollment_record(enrollment_id="enr_a1_c1_again", status="ACTIVE")
        )
    assert store.get_enrollment("enr_a1_c1_again") is None
    # The refused second registration changed nothing: the first record
    # remains the ACTIVE fact of this tenant, student and course.
    assert (
        store.find_active_enrollment(
            tenant_id="ten_a",
            student_identity_id="idn_human_c",
            course_id="crs_a1",
        )
        is first
    )


def test_same_student_and_course_in_another_tenant_is_allowed():
    # Tenant isolation: the duplicate key is scoped by tenant, so the same
    # student and course in a different tenant is a different fact.
    store = LearningStore()
    in_a = store.register_enrollment(enrollment_record())
    in_b = store.register_enrollment(
        enrollment_record(enrollment_id="enr_b1_c1", tenant_id="ten_b")
    )
    assert store.enrollments == {in_a.enrollment_id: in_a, in_b.enrollment_id: in_b}
    found_a = store.find_active_enrollment(
        tenant_id="ten_a",
        student_identity_id="idn_human_c",
        course_id="crs_a1",
    )
    found_b = store.find_active_enrollment(
        tenant_id="ten_b",
        student_identity_id="idn_human_c",
        course_id="crs_a1",
    )
    assert found_a is in_a and found_b is in_b


# ------------------------------------------------- registration invariants
def test_distinct_students_or_courses_are_distinct_facts():
    store = LearningStore()
    store.register_enrollment(enrollment_record())
    other_student = store.register_enrollment(
        enrollment_record(enrollment_id="enr_a1_c2", student_identity_id="idn_human_d")
    )
    other_course = store.register_enrollment(
        enrollment_record(enrollment_id="enr_a2_c1", course_id="crs_a2")
    )
    assert other_student.enrollment_id in store.enrollments
    assert other_course.enrollment_id in store.enrollments
    assert len(store.enrollments) == 3


def test_enrollment_id_is_owned_once():
    store = LearningStore()
    store.register_enrollment(enrollment_record())
    # A different business combination under an already-owned id must not
    # redefine the existing record — ids are unique and immutable (§11).
    with pytest.raises(ValueError, match="already owned"):
        store.register_enrollment(
            enrollment_record(tenant_id="ten_b", student_identity_id="idn_human_d")
        )
    assert len(store.enrollments) == 1


@pytest.mark.parametrize("blank", ["", "   "])
def test_registration_refuses_missing_key_fields(blank: str):
    for field in ("enrollment_id", "tenant_id", "course_id", "student_identity_id"):
        store = LearningStore()
        with pytest.raises(ValueError):
            store.register_enrollment(enrollment_record(**{field: blank}))
        assert store.enrollments == {}


def test_foreign_owner_component_never_enters_the_store():
    # An enrollment is Learning data by model invariant: a record claiming
    # another owner is refused at registration, so get_enrollment can never
    # return a wrong-owner enrollment — there is nothing to return.
    store = LearningStore()
    for owner in ("records", "identity", ""):
        with pytest.raises(ValueError, match="ownership is singular"):
            store.register_enrollment(
                enrollment_record(
                    enrollment_id=f"enr_{owner or 'empty'}", owner_component=owner
                )
            )
    assert store.enrollments == {}
    assert store.get_enrollment("enr_records") is None


def test_timestamps_are_stored_verbatim():
    # Slice 1 keeps timestamps as the opaque store strings the component
    # uses everywhere: registration preserves exactly what it was given and
    # invents nothing.
    store = LearningStore()
    enrollment = enrollment_record(
        created_at="2026-09-10T08:00:00+00:00",
        updated_at="2026-09-10T09:30:00+00:00",
    )
    stored = store.register_enrollment(enrollment)
    assert stored.created_at == "2026-09-10T08:00:00+00:00"
    assert stored.updated_at == "2026-09-10T09:30:00+00:00"
    fetched = store.get_enrollment(stored.enrollment_id)
    assert (fetched.created_at, fetched.updated_at) == (
        "2026-09-10T08:00:00+00:00",
        "2026-09-10T09:30:00+00:00",
    )


def test_seed_demo_keeps_the_enrollment_store_empty():
    # The fixed Level-0 demo data describes the existing submission flow;
    # enrollment in this slice establishes the store foundation only, so a
    # seeded store carries no enrollment fact — and re-seeding resets one.
    store = LearningStore()
    store.register_enrollment(enrollment_record())
    store.seed_demo()
    assert store.enrollments == {}
    assert (
        store.find_active_enrollment(
            tenant_id="ten_a",
            student_identity_id="idn_human_c",
            course_id="crs_a1",
        )
        is None
    )


def test_concurrent_registrations_store_exactly_one_active_record():
    # The duplicate invariant is decided atomically: concurrent commands —
    # different business registrations, each with its own id — cannot both
    # pass the check. One wins; the rest are refused conflicts (ADR-0012).
    store = LearningStore()
    attempts = 8
    barrier = threading.Barrier(attempts)
    outcomes: list[str] = [""] * attempts

    def enroll(index: int) -> None:
        record = enrollment_record(enrollment_id=f"enr_race_{index}")
        barrier.wait()
        try:
            store.register_enrollment(record)
            outcomes[index] = "stored"
        except ValueError:
            outcomes[index] = "refused"

    threads = [
        threading.Thread(target=enroll, args=(index,)) for index in range(attempts)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("stored") == 1
    assert outcomes.count("refused") == attempts - 1
    assert len(store.enrollments) == 1
    active = store.find_active_enrollment(
        tenant_id="ten_a",
        student_identity_id="idn_human_c",
        course_id="crs_a1",
    )
    assert active is next(iter(store.enrollments.values()))
