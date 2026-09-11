"""SCS-001 Enrollment Slice 2 — Student Enrollment (ADR-0012).

The two enrollment operations against the fully composed Platform Instance
(IS-001 + IS-002 + IS-003 + IS-005 + Learning): the student self-enrollment
``learning.enrollments.create`` and the read of the student's own Enrollment
``learning.enrollments.read``.

What the slice has to prove is narrow and each group below proves one part of
it: only the verified subject can enroll, and only in a ``PUBLISHED`` Course
of its own effective tenant; one student holds at most one ``ACTIVE``
Enrollment per Course; the command is delivered through IS-005; the read
reaches the caller's own record and nothing else — not even the fact that
another student's record exists; and every outcome, served or refused, is
audited in the approved envelope.

The composition root follows ``tests.test_learning_authoring``: grants are the
harness's job (IS-003 publishes no grant API), the IS-001 tenant-context port
is wired so the fixture Courses can be created and published through the real
commands, and the enrollment grants are given to the student only.
"""

from __future__ import annotations

import gc
import inspect
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient

from authorization_service.models import ServiceAccess
from authorization_service.store import CONSUMER_PERMISSIONS
from identity_service import transport as identity_transport
from learning_service.adapters import authorization_port, tenant_context_port
from learning_service.contracts import (
    OPERATION_COURSE_ARCHIVE,
    OPERATION_COURSE_CREATE,
    OPERATION_COURSE_PUBLISH,
    OPERATION_ENROLLMENT_CREATE,
    OPERATION_ENROLLMENT_READ,
)
from learning_service.deployment import LearningDeployment
from learning_service.deployment import build_deployment as build_learning
from learning_service.errors import AccessRefused
from tests.conftest import PLATFORM_ID, monolith
from tests.test_learning_slice1 import (
    LEARNING_CREDENTIAL,
    STUDENT_C,
    TEACHER_A,
    TEACHER_B,
    envelope_of,
)

#: The demo published Course of the student's own tenant (``ten_a``).
COURSE_A = "crs_a1"
#: The demo published Course of the foreign tenant (``ten_b``).
COURSE_B = "crs_b1"
#: An unknown credential of the demo identity store.
UNKNOWN_TOKEN = "token-unknown"

#: The Student grant vocabulary of this slice: the subject that holds these two
#: grants IS the Student — no role logic exists inside Learning (ADR-0012).
STUDENT_OPERATIONS = (OPERATION_ENROLLMENT_CREATE, OPERATION_ENROLLMENT_READ)


@dataclass
class EnrollmentHarness:
    instance: Any
    learning: LearningDeployment

    def http(self) -> TestClient:
        return TestClient(self.learning.contract_app())

    def client(self) -> Any:
        return self.learning.publish()


def enrollment_harness(*, foreign_grant: bool = True) -> EnrollmentHarness:
    """Compose IS-001/IS-002/IS-003/IS-005 plus Learning with the enrollment grants.

    The student (``idn_human_c``, ``ten_a``) holds exactly the two enrollment
    operations; Teacher A holds the authoring commands needed to build the
    fixture Courses the eligibility rules are proved on. With ``foreign_grant``
    the student also holds both operations in ``ten_b``, so a cross-tenant
    refusal can only come from the tenant boundary itself — a grant can never
    compensate for it.
    """
    instance = monolith()
    instance.authorization.store.services["svc_learning"] = ServiceAccess(
        "svc_learning", PLATFORM_ID, CONSUMER_PERMISSIONS
    )
    instance.authorization.store.service_tokens[LEARNING_CREDENTIAL] = "svc_learning"
    instance.authorization.store.grant(
        "ten_a",
        "idn_human_a",
        OPERATION_COURSE_CREATE,
        OPERATION_COURSE_PUBLISH,
        OPERATION_COURSE_ARCHIVE,
    )
    instance.authorization.store.grant("ten_a", "idn_human_c", *STUDENT_OPERATIONS)
    if foreign_grant:
        instance.authorization.store.grant("ten_b", "idn_human_c", *STUDENT_OPERATIONS)
    learning = build_learning(
        {"platform_id": instance.authority.current_platform_id, "environment": "test"},
        authorization=authorization_port(
            instance.authorization.publish(credential=LEARNING_CREDENTIAL)
        ),
        tenant_context=tenant_context_port(instance.identity_context_client),
        seed_demo=True,
        with_http=True,
    )
    harness = EnrollmentHarness(instance=instance, learning=learning)
    _COMPOSED.append(harness)
    return harness


def harness_with_only(operation: str) -> EnrollmentHarness:
    """The same composition, but the student holds exactly one enrollment grant.

    ``store.grant`` merges into an existing grant, so the seeded pair is
    dropped first: what remains is exactly the one operation under test.
    """
    harness = enrollment_harness()
    harness.instance.authorization.store.grants.pop(("ten_a", "idn_human_c"), None)
    harness.instance.authorization.store.grant("ten_a", "idn_human_c", operation)
    return harness


@pytest.fixture(autouse=True)
def release_the_composed_instances() -> Iterator[None]:
    """Leave the IS-001 transport table exactly as this module found it.

    Every composition opens a contract channel to IS-001 for the tenant-context
    port, and the channel is revoked only when the client that owns it is
    collected. ``tests.test_identity_context_contract`` counts the open
    channels of the whole run around one explicit collection, so a channel this
    module leaves behind is revoked inside that test's assertion window and
    falsifies its count. Each test therefore ends by dropping its own
    compositions and revoking the channels they opened.
    """
    yield
    while _COMPOSED:
        harness = _COMPOSED.pop()
        identity_transport.close_channel(
            harness.instance.identity_context_client._channel
        )
    gc.collect()


# ------------------------------------------------------------------ helpers
def create_enrollment(
    harness: EnrollmentHarness,
    *,
    key: str | None,
    token: str | None = STUDENT_C,
    course_id: str = COURSE_A,
    claim: str | None = None,
    body: dict | None = None,
    request_id: str | None = None,
) -> Any:
    """One ``POST /api/v1/learning/enrollments`` request."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if key is not None:
        headers["idempotency-key"] = key
    if claim is not None:
        headers["x-tenant-id"] = claim
    if request_id is not None:
        headers["x-request-id"] = request_id
        headers["x-correlation-id"] = request_id
    payload = body if body is not None else {"course_id": course_id}
    return harness.http().post(
        "/api/v1/learning/enrollments", headers=headers, json=payload
    )


def read_enrollment(
    harness: EnrollmentHarness,
    course_id: str = COURSE_A,
    *,
    token: str | None = STUDENT_C,
    request_id: str | None = None,
) -> Any:
    """One ``GET /api/v1/learning/enrollments/{course_id}`` request."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if request_id is not None:
        headers["x-request-id"] = request_id
        headers["x-correlation-id"] = request_id
    return harness.http().get(
        f"/api/v1/learning/enrollments/{course_id}", headers=headers
    )


def draft_course(harness: EnrollmentHarness, title: str = "Draft course") -> str:
    """One ``DRAFT`` Course in ``ten_a``, created through the real command."""
    view, _, _ = harness.learning.engine.create_course(
        TEACHER_A,
        title=title,
        description="A fixture course of the enrollment tests.",
        idempotency_key=f"create-{title}",
        request_id=f"req-create-{title}",
    )
    return view.course_id


def publish(harness: EnrollmentHarness, course_id: str) -> None:
    harness.learning.engine.publish_course(
        TEACHER_A, course_id, idempotency_key=f"publish-{course_id}"
    )


def archive(harness: EnrollmentHarness, course_id: str) -> None:
    harness.learning.engine.archive_course(
        TEACHER_A, course_id, idempotency_key=f"archive-{course_id}"
    )


def stored_enrollments(harness: EnrollmentHarness) -> list[Any]:
    return list(harness.learning.store.enrollments.values())


def audit_of(harness: EnrollmentHarness, request_id: str) -> list[Any]:
    return [
        event
        for event in harness.learning.store.audit
        if event.request_id == request_id
    ]


PUBLISHED_FIELDS = {
    "enrollment_id",
    "course_id",
    "student_identity_id",
    "status",
    "created_at",
    "updated_at",
}


#: The compositions a test has built, so its teardown can revoke their
#: IS-001 channels (see :func:`release_the_composed_instances`).
_COMPOSED: list[EnrollmentHarness] = []


# ------------------------------------------------------------- composition
def test_the_harness_composes_learning_over_the_published_contracts():
    harness = enrollment_harness()
    # The student holds exactly the two enrollment grants, in its own tenant.
    # (The IS-003 demo seed also gives it ``records.read`` — a Records
    # operation that says nothing about Learning: being a Student is not a
    # role, it is holding these two operations, ADR-0012.)
    grant = harness.instance.authorization.store.grants[("ten_a", "idn_human_c")]
    learning_operations = {op for op in grant.operations if op.startswith("learning.")}
    assert learning_operations == set(STUDENT_OPERATIONS)
    # The demo store states one published Course per tenant, and no enrollment.
    assert harness.learning.store.courses[COURSE_A].status == "PUBLISHED"
    assert harness.learning.store.courses[COURSE_A].tenant_id == "ten_a"
    assert harness.learning.store.courses[COURSE_B].tenant_id == "ten_b"
    assert harness.learning.store.enrollments == {}


# ------------------------------------------------------- CREATE: the success
def test_a_student_enrolls_in_a_published_course_of_its_own_tenant():
    harness = enrollment_harness()

    response = create_enrollment(harness, key="enroll-1", request_id="req-ok")

    assert response.status_code == 201
    body = response.json()
    assert body["course_id"] == COURSE_A
    assert body["student_identity_id"] == "idn_human_c"
    assert body["status"] == "ACTIVE"
    assert body["created_at"] == body["updated_at"]
    assert body["enrollment_id"].startswith("enr_")


def test_the_created_enrollment_is_learning_data_of_the_effective_tenant():
    harness = enrollment_harness()

    body = create_enrollment(harness, key="enroll-1").json()

    stored = harness.learning.store.get_enrollment(body["enrollment_id"])
    assert stored is not None
    assert stored.tenant_id == "ten_a"
    assert stored.course_id == COURSE_A
    assert stored.student_identity_id == "idn_human_c"
    assert stored.owner_component == "learning"
    assert len(stored_enrollments(harness)) == 1


def test_the_published_representation_is_exactly_the_six_business_fields():
    harness = enrollment_harness()

    created = create_enrollment(harness, key="enroll-1")
    read = read_enrollment(harness)

    assert set(created.json()) == PUBLISHED_FIELDS
    assert set(read.json()) == PUBLISHED_FIELDS
    # No tenant, no owner and no profile data crosses the boundary.
    for field in ("tenant_id", "owner_component", "email", "profile"):
        assert field not in created.json()


# --------------------------------------------------------- authentication
@pytest.mark.parametrize(
    "token, reason",
    [(None, "missing_identity"), (UNKNOWN_TOKEN, "unknown_identity")],
)
def test_create_without_a_verified_subject_is_refused(token, reason):
    harness = enrollment_harness()

    response = create_enrollment(harness, key="enroll-noauth", token=token)

    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == reason
    assert stored_enrollments(harness) == []


@pytest.mark.parametrize("token", [None, UNKNOWN_TOKEN])
def test_read_without_a_verified_subject_is_refused(token):
    harness = enrollment_harness()

    response = read_enrollment(harness, token=token)

    assert response.status_code == 401
    assert envelope_of(response)["error"]["code"] == "AUTHENTICATION_REQUIRED"


# ------------------------------------------------------------ authorization
def test_a_subject_without_the_grant_cannot_enroll():
    harness = enrollment_harness()
    harness.instance.authorization.store.grants.pop(("ten_a", "idn_human_c"))

    response = create_enrollment(harness, key="enroll-nogrant", request_id="req-ng")

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "permission_not_granted"
    assert stored_enrollments(harness) == []
    assert audit_of(harness, "req-ng")[-1].decision == "DENY"


def test_the_create_grant_does_not_imply_the_read():
    harness = harness_with_only(OPERATION_ENROLLMENT_CREATE)
    create_enrollment(harness, key="enroll-1")

    response = read_enrollment(harness, request_id="req-noread")

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"] == "permission_not_granted"
    )
    assert audit_of(harness, "req-noread")[-1].decision == "DENY"


def test_the_read_grant_does_not_imply_the_create():
    harness = harness_with_only(OPERATION_ENROLLMENT_READ)

    response = create_enrollment(harness, key="enroll-nocreate")

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"] == "permission_not_granted"
    )
    assert stored_enrollments(harness) == []


# ------------------------------------------------------- student ownership
def test_the_command_body_cannot_name_another_identity():
    harness = enrollment_harness()

    response = create_enrollment(
        harness,
        key="enroll-smuggle",
        body={"course_id": COURSE_A, "student_identity_id": "idn_human_a"},
        request_id="req-smuggle",
    )

    # The published schema refuses the request before any handler runs: there
    # is no identity field to supply, so nobody else can be enrolled.
    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["reason"] == "malformed_request"
    assert stored_enrollments(harness) == []
    assert audit_of(harness, "req-smuggle")[-1].decision == "DENY"


@pytest.mark.parametrize("field", ["status", "enrollment_id", "tenant_id", "attempt"])
def test_the_request_schema_forbids_every_field_but_the_course(field):
    harness = enrollment_harness()

    response = create_enrollment(
        harness, key=f"enroll-extra-{field}", body={"course_id": COURSE_A, field: "x"}
    )

    assert response.status_code == 422
    assert envelope_of(response)["error"]["details"]["reason"] == "malformed_request"
    assert stored_enrollments(harness) == []


def test_a_missing_course_field_is_refused_by_the_schema():
    harness = enrollment_harness()

    response = create_enrollment(harness, key="enroll-empty", body={})

    assert response.status_code == 422
    assert envelope_of(response)["error"]["details"]["reason"] == "malformed_request"


def test_each_subject_enrolls_as_itself_and_never_as_another():
    harness = enrollment_harness()
    harness.instance.authorization.store.grant(
        "ten_a", "idn_human_a", *STUDENT_OPERATIONS
    )

    mine = create_enrollment(harness, key="enroll-mine", token=STUDENT_C)
    theirs = create_enrollment(harness, key="enroll-theirs", token=TEACHER_A)

    assert mine.json()["student_identity_id"] == "idn_human_c"
    assert theirs.json()["student_identity_id"] == "idn_human_a"
    assert mine.json()["enrollment_id"] != theirs.json()["enrollment_id"]
    assert len(stored_enrollments(harness)) == 2


def test_neither_the_command_nor_the_client_accepts_an_identity():
    harness = enrollment_harness()

    for target in (
        harness.learning.engine.enroll,
        harness.learning.engine.read_enrollment,
        harness.client().enroll,
        harness.client().read_enrollment,
    ):
        names = set(inspect.signature(target).parameters)
        assert not {"student_identity_id", "student_id", "identity_id"} & names, names


# --------------------------------------------------------- course existence
def test_enrolling_in_an_unknown_course_is_not_found():
    harness = enrollment_harness()

    response = create_enrollment(
        harness, key="enroll-unknown", course_id="crs_missing", request_id="req-unk"
    )

    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "course_unknown"
    assert stored_enrollments(harness) == []
    assert audit_of(harness, "req-unk")[-1].decision == "DENY"


# -------------------------------------------------------- tenant isolation
def test_a_course_of_another_tenant_is_refused_even_with_a_grant_there():
    # The student holds both enrollment operations in ten_b as well, so the
    # refusal can only come from the tenant boundary: IS-003 checks the
    # resource tenant before it looks at any grant.
    harness = enrollment_harness(foreign_grant=True)

    response = create_enrollment(
        harness, key="enroll-foreign", course_id=COURSE_B, request_id="req-foreign"
    )

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "resource_tenant_mismatch"
    )
    assert stored_enrollments(harness) == []
    assert audit_of(harness, "req-foreign")[-1].decision == "DENY"


def test_reading_another_tenants_course_is_refused():
    harness = enrollment_harness(foreign_grant=True)

    response = read_enrollment(harness, COURSE_B, request_id="req-foreign-read")

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "resource_tenant_mismatch"
    )


def test_a_claimed_tenant_never_selects_the_effective_tenant():
    harness = enrollment_harness()

    # A matching claim changes nothing, a foreign one is refused as the
    # cross-check failure it is (LAW-16a) — the enrollment is never created.
    assert (
        create_enrollment(harness, key="enroll-claim-ok", claim="ten_a").status_code
        == 201
    )
    refused = create_enrollment(harness, key="enroll-claim-bad", claim="ten_b")

    assert refused.status_code == 403
    assert envelope_of(refused)["error"]["details"]["reason"] == "tenant_mismatch"
    assert len(stored_enrollments(harness)) == 1


def test_the_same_student_in_another_tenant_is_a_distinct_fact():
    # Tenant isolation of the duplicate key: Teacher B owns ten_b, and the
    # enrollment the student holds in ten_a says nothing about ten_b.
    harness = enrollment_harness()
    harness.instance.authorization.store.grant(
        "ten_b", "idn_human_b", *STUDENT_OPERATIONS
    )

    in_a = create_enrollment(harness, key="enroll-a", token=STUDENT_C)
    in_b = create_enrollment(
        harness, key="enroll-b", token=TEACHER_B, course_id=COURSE_B
    )

    assert in_a.status_code == 201 and in_b.status_code == 201
    tenants = {record.tenant_id for record in stored_enrollments(harness)}
    assert tenants == {"ten_a", "ten_b"}


# ------------------------------------------------------- PUBLISHED required
def test_a_draft_course_accepts_no_enrollment():
    harness = enrollment_harness()
    course_id = draft_course(harness, "Draft")

    response = create_enrollment(
        harness, key="enroll-draft", course_id=course_id, request_id="req-draft"
    )

    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert body["error"]["details"]["reason"] == "invalid_state_transition"
    assert stored_enrollments(harness) == []
    event = audit_of(harness, "req-draft")[-1]
    assert event.decision == "DENY"
    assert event.details["domain_rule"] == "course_not_published"
    assert event.details["course_status"] == "DRAFT"


def test_an_archived_course_accepts_no_enrollment():
    harness = enrollment_harness()
    course_id = draft_course(harness, "Archived")
    publish(harness, course_id)
    archive(harness, course_id)

    response = create_enrollment(
        harness, key="enroll-archived", course_id=course_id, request_id="req-archived"
    )

    assert response.status_code == 409
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "invalid_state_transition"
    )
    assert audit_of(harness, "req-archived")[-1].details["course_status"] == "ARCHIVED"
    assert stored_enrollments(harness) == []


def test_the_same_course_accepts_the_enrollment_once_it_is_published():
    harness = enrollment_harness()
    course_id = draft_course(harness, "Published later")

    assert (
        create_enrollment(harness, key="enroll-later", course_id=course_id).status_code
        == 409
    )
    publish(harness, course_id)

    assert (
        create_enrollment(harness, key="enroll-later", course_id=course_id).status_code
        == 201
    )


# ------------------------------------------------------------- idempotency
def test_the_command_requires_an_idempotency_key():
    harness = enrollment_harness()

    response = create_enrollment(harness, key=None, request_id="req-nokey")

    assert response.status_code == 400
    body = envelope_of(response)
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert body["error"]["details"]["reason"] == "idempotency_key_required"
    assert stored_enrollments(harness) == []
    assert audit_of(harness, "req-nokey")[-1].decision == "DENY"


def test_an_exact_replay_returns_the_recorded_enrollment_without_a_second_record():
    harness = enrollment_harness()

    first = create_enrollment(harness, key="enroll-replay", request_id="req-r1")
    second = create_enrollment(harness, key="enroll-replay", request_id="req-r2")

    assert first.status_code == 201 and second.status_code == 201
    assert second.json() == first.json()
    assert len(stored_enrollments(harness)) == 1
    # IS-005 reports the replay into this component's own audit journal.
    assert any(
        event.action == "idempotency_replay" for event in audit_of(harness, "req-r2")
    )


def test_an_exact_replay_still_answers_after_the_course_was_archived():
    # The state rule lives inside the guarded effect, so a recorded result
    # survives a later change of the Course it refers to.
    harness = enrollment_harness()
    first = create_enrollment(harness, key="enroll-stable")
    archive(harness, COURSE_A)

    second = create_enrollment(harness, key="enroll-stable")

    assert second.status_code == 201
    assert second.json()["enrollment_id"] == first.json()["enrollment_id"]
    assert len(stored_enrollments(harness)) == 1


def test_the_same_key_with_another_course_is_a_conflict():
    harness = enrollment_harness()
    other = draft_course(harness, "Other published")
    publish(harness, other)
    create_enrollment(harness, key="enroll-binding")

    response = create_enrollment(
        harness, key="enroll-binding", course_id=other, request_id="req-conflict"
    )

    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert body["error"]["details"]["reason"] == "idempotency_conflict"
    assert len(stored_enrollments(harness)) == 1


def test_a_refused_command_does_not_consume_its_key():
    harness = enrollment_harness()
    course_id = draft_course(harness, "Refused then published")

    refused = create_enrollment(harness, key="enroll-retry", course_id=course_id)
    publish(harness, course_id)
    retried = create_enrollment(harness, key="enroll-retry", course_id=course_id)

    assert refused.status_code == 409
    assert retried.status_code == 201
    assert len(stored_enrollments(harness)) == 1


# ------------------------------------------------- duplicate ACTIVE record
def test_a_second_command_with_another_key_is_a_business_conflict():
    harness = enrollment_harness()
    first = create_enrollment(harness, key="enroll-first")

    second = create_enrollment(harness, key="enroll-second", request_id="req-dup")

    assert first.status_code == 201
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert body["error"]["details"]["reason"] == "invalid_state_transition"
    event = audit_of(harness, "req-dup")[-1]
    assert event.decision == "DENY"
    assert event.details["domain_rule"] == "duplicate_active_enrollment"


def test_the_surviving_record_is_the_first_one():
    harness = enrollment_harness()
    first = create_enrollment(harness, key="enroll-first")

    create_enrollment(harness, key="enroll-second")

    active = harness.learning.store.find_active_enrollment(
        tenant_id="ten_a", student_identity_id="idn_human_c", course_id=COURSE_A
    )
    assert active is not None
    assert active.enrollment_id == first.json()["enrollment_id"]
    assert len(stored_enrollments(harness)) == 1


def test_concurrent_commands_store_exactly_one_active_enrollment():
    # Two commands, two different keys, one business fact: the duplicate check
    # and the insertion are one atomic decision inside the store.
    harness = enrollment_harness()
    attempts = 8
    barrier = threading.Barrier(attempts)
    statuses: list[int] = [0] * attempts

    def race(index: int) -> None:
        barrier.wait()
        statuses[index] = create_enrollment(
            harness, key=f"enroll-race-{index}"
        ).status_code

    threads = [
        threading.Thread(target=race, args=(index,)) for index in range(attempts)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(201) == 1
    assert statuses.count(409) == attempts - 1
    assert len(stored_enrollments(harness)) == 1


def test_another_student_in_the_same_course_is_a_distinct_fact():
    harness = enrollment_harness()
    harness.instance.authorization.store.grant(
        "ten_a", "idn_human_a", *STUDENT_OPERATIONS
    )

    create_enrollment(harness, key="enroll-c", token=STUDENT_C)
    other = create_enrollment(harness, key="enroll-a", token=TEACHER_A)

    assert other.status_code == 201
    assert len(stored_enrollments(harness)) == 2


# ------------------------------------------------------------------- READ
def test_the_student_reads_its_own_enrollment():
    harness = enrollment_harness()
    created = create_enrollment(harness, key="enroll-1")

    response = read_enrollment(harness, request_id="req-read")

    assert response.status_code == 200
    assert response.json() == created.json()
    assert audit_of(harness, "req-read")[-1].decision == "ALLOW"


def test_the_read_is_addressed_by_course_and_accepts_no_enrollment_identifier():
    harness = enrollment_harness()
    created = create_enrollment(harness, key="enroll-1")

    # The enrollment id is not an address of this operation: it names no Course,
    # so the ownership boundary refuses it before any decision is requested.
    response = read_enrollment(harness, created.json()["enrollment_id"])

    assert response.status_code == 404
    assert envelope_of(response)["error"]["details"]["reason"] == "course_unknown"
    assert "{enrollment_id}" not in str(
        [route.path for route in harness.learning.contract_app().routes]
    )


def test_reading_without_an_enrollment_is_not_found():
    harness = enrollment_harness()

    response = read_enrollment(harness, request_id="req-none")

    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "enrollment_unknown"
    assert audit_of(harness, "req-none")[-1].decision == "DENY"


def test_another_subjects_enrollment_is_never_served_nor_disclosed():
    harness = enrollment_harness()
    harness.instance.authorization.store.grant(
        "ten_a", "idn_human_a", *STUDENT_OPERATIONS
    )
    theirs = create_enrollment(harness, key="enroll-teacher", token=TEACHER_A)
    assert theirs.status_code == 201

    response = read_enrollment(harness, request_id="req-notmine")
    never_enrolled = enrollment_harness()
    baseline = read_enrollment(never_enrolled, request_id="req-baseline")

    # The student has no enrollment of its own here, and the answer is exactly
    # the one a Course with no enrollment at all produces: the existence of the
    # teacher's record is not disclosed.
    assert response.status_code == baseline.status_code == 404
    assert envelope_of(response)["error"] == envelope_of(baseline)["error"]
    assert envelope_of(response)["error"]["details"]["reason"] == "enrollment_unknown"
    assert len(stored_enrollments(harness)) == 1


def test_each_subject_reads_its_own_record_of_the_same_course():
    harness = enrollment_harness()
    harness.instance.authorization.store.grant(
        "ten_a", "idn_human_a", *STUDENT_OPERATIONS
    )
    mine = create_enrollment(harness, key="enroll-c", token=STUDENT_C)
    theirs = create_enrollment(harness, key="enroll-a", token=TEACHER_A)

    assert read_enrollment(harness, token=STUDENT_C).json()["enrollment_id"] == (
        mine.json()["enrollment_id"]
    )
    assert read_enrollment(harness, token=TEACHER_A).json()["enrollment_id"] == (
        theirs.json()["enrollment_id"]
    )


def test_the_own_enrollment_stays_readable_after_the_course_was_archived():
    # Enrollment is a participation fact, not a publication state: archiving
    # the Course does not withdraw the record.
    harness = enrollment_harness()
    created = create_enrollment(harness, key="enroll-1")
    archive(harness, COURSE_A)

    response = read_enrollment(harness)

    assert response.status_code == 200
    assert response.json()["enrollment_id"] == created.json()["enrollment_id"]


# ------------------------------------------------------------------- audit
def test_a_successful_create_is_audited_with_the_enrollment_as_the_resource():
    harness = enrollment_harness()

    body = create_enrollment(harness, key="enroll-1", request_id="req-audit").json()

    allowed = [
        event
        for event in audit_of(harness, "req-audit")
        if event.action == OPERATION_ENROLLMENT_CREATE and event.decision == "ALLOW"
    ]
    assert len(allowed) == 1
    event = allowed[0]
    assert event.reason == "permitted"
    assert event.resource_id == body["enrollment_id"]
    assert event.resource_tenant_id == "ten_a"
    assert event.subject_id == "idn_human_c"
    assert event.tenant_id == "ten_a"
    assert event.details == {"course_id": COURSE_A}
    assert event.correlation_id == "req-audit"


def test_a_successful_read_is_audited_with_the_enrollment_as_the_resource():
    harness = enrollment_harness()
    body = create_enrollment(harness, key="enroll-1").json()

    read_enrollment(harness, request_id="req-read-audit")

    allowed = [
        event
        for event in audit_of(harness, "req-read-audit")
        if event.action == OPERATION_ENROLLMENT_READ and event.decision == "ALLOW"
    ]
    assert len(allowed) == 1
    assert allowed[0].resource_id == body["enrollment_id"]
    assert allowed[0].subject_id == "idn_human_c"
    assert allowed[0].details == {"course_id": COURSE_A}


def test_every_refusal_is_audited_with_the_caller_request_context():
    harness = enrollment_harness()
    course_id = draft_course(harness, "Draft")

    create_enrollment(harness, key=None, request_id="req-deny-key")
    create_enrollment(harness, key="k-401", token=None, request_id="req-deny-authn")
    create_enrollment(
        harness, key="k-404", course_id="crs_missing", request_id="req-deny-404"
    )
    create_enrollment(
        harness, key="k-409", course_id=course_id, request_id="req-deny-409"
    )
    create_enrollment(harness, key="k-dup", request_id="req-deny-first")
    create_enrollment(harness, key="k-dup-2", request_id="req-deny-dup")
    # The one refused read: the student holds no Enrollment in this Course.
    read_enrollment(harness, course_id, request_id="req-deny-read")

    for request_id in (
        "req-deny-key",
        "req-deny-authn",
        "req-deny-404",
        "req-deny-409",
        "req-deny-dup",
        "req-deny-read",
    ):
        events = audit_of(harness, request_id)
        assert events, request_id
        assert events[-1].decision == "DENY", request_id
        assert events[-1].correlation_id == request_id, request_id
        assert events[-1].reason, request_id


def test_the_two_domain_refusals_stay_distinguishable_in_the_audit_journal():
    harness = enrollment_harness()
    course_id = draft_course(harness, "Draft")

    create_enrollment(
        harness, key="k-state", course_id=course_id, request_id="req-state"
    )
    create_enrollment(harness, key="k-one", request_id="req-one")
    create_enrollment(harness, key="k-two", request_id="req-two")

    assert audit_of(harness, "req-state")[-1].details["domain_rule"] == (
        "course_not_published"
    )
    assert audit_of(harness, "req-two")[-1].details["domain_rule"] == (
        "duplicate_active_enrollment"
    )
    # Both are the same published reason and status — the vocabulary is closed.
    assert audit_of(harness, "req-state")[-1].reason == "invalid_state_transition"
    assert audit_of(harness, "req-two")[-1].reason == "invalid_state_transition"


# ------------------------------------------------------- approved envelope
@pytest.mark.parametrize(
    "case, status, code, reason",
    [
        ("no-key", 400, "IDEMPOTENCY_KEY_REQUIRED", "idempotency_key_required"),
        ("no-authn", 401, "AUTHENTICATION_REQUIRED", "missing_identity"),
        ("no-grant", 403, "AUTHORIZATION_DENIED", "permission_not_granted"),
        ("no-course", 404, "NOT_FOUND", "course_unknown"),
        ("no-enrollment", 404, "NOT_FOUND", "enrollment_unknown"),
        ("draft-course", 409, "INVALID_STATE_TRANSITION", "invalid_state_transition"),
        ("duplicate", 409, "INVALID_STATE_TRANSITION", "invalid_state_transition"),
        ("extra-field", 422, "INVALID_REQUEST", "malformed_request"),
    ],
)
def test_every_documented_refusal_uses_the_approved_envelope(
    case, status, code, reason
):
    harness = enrollment_harness()
    if case == "no-enrollment":
        # A fresh composition where the student may read but holds no record:
        # the read of a Course it is not enrolled in.
        harness = harness_with_only(OPERATION_ENROLLMENT_READ)
        response = read_enrollment(harness)
        assert response.status_code == status, case
        body = envelope_of(response)
        assert body["error"]["code"] == code, case
        assert body["error"]["details"]["reason"] == reason, case
        assert body["request_id"] and body["correlation_id"]
        return

    if case == "no-grant":
        harness.instance.authorization.store.grants.pop(("ten_a", "idn_human_c"))
    course_id = draft_course(harness, "Draft")
    create_enrollment(harness, key="seed")

    if case == "no-key":
        response = create_enrollment(harness, key=None)
    elif case == "no-authn":
        response = create_enrollment(harness, key="k", token=None)
    elif case == "no-grant":
        response = create_enrollment(harness, key="k")
    elif case == "no-course":
        response = create_enrollment(harness, key="k", course_id="crs_missing")
    elif case == "draft-course":
        response = create_enrollment(harness, key="k", course_id=course_id)
    elif case == "duplicate":
        response = create_enrollment(harness, key="k-other")
    else:
        response = create_enrollment(
            harness, key="k", body={"course_id": COURSE_A, "status": "ACTIVE"}
        )

    assert response.status_code == status, case
    body = envelope_of(response)
    assert body["error"]["code"] == code, case
    assert body["error"]["details"]["reason"] == reason, case
    assert body["request_id"] and body["correlation_id"]


# --------------------------------------------------- published client (IS-004 style)
def test_the_published_client_enrolls_and_reads_its_own_enrollment():
    harness = enrollment_harness()
    client = harness.client()

    created = client.enroll(STUDENT_C, course_id=COURSE_A, idempotency_key="cli-1")
    read = client.read_enrollment(STUDENT_C, COURSE_A)

    assert created.course_id == COURSE_A
    assert created.student_identity_id == "idn_human_c"
    assert created.status == "ACTIVE"
    assert read.enrollment_id == created.enrollment_id
    assert len(stored_enrollments(harness)) == 1


def test_the_client_replay_returns_the_recorded_result():
    harness = enrollment_harness()
    client = harness.client()

    first = client.enroll(STUDENT_C, course_id=COURSE_A, idempotency_key="cli-1")
    second = client.enroll(STUDENT_C, course_id=COURSE_A, idempotency_key="cli-1")

    assert second == first
    assert len(stored_enrollments(harness)) == 1


@pytest.mark.parametrize(
    "operation, reason, status",
    [
        ("duplicate", "invalid_state_transition", 409),
        ("no-key", "idempotency_key_required", 400),
        ("unknown-course", "course_unknown", 404),
        ("foreign-course", "resource_tenant_mismatch", 403),
        ("no-enrollment", "enrollment_unknown", 404),
    ],
)
def test_client_refusals_arrive_as_published_access_refusals(operation, reason, status):
    harness = enrollment_harness()
    client = harness.client()

    if operation == "duplicate":
        client.enroll(STUDENT_C, course_id=COURSE_A, idempotency_key="cli-1")
        call = partial(
            client.enroll, STUDENT_C, course_id=COURSE_A, idempotency_key="cli-2"
        )
    elif operation == "no-key":
        call = partial(
            client.enroll, STUDENT_C, course_id=COURSE_A, idempotency_key=None
        )
    elif operation == "unknown-course":
        call = partial(
            client.enroll, STUDENT_C, course_id="crs_missing", idempotency_key="cli-1"
        )
    elif operation == "foreign-course":
        call = partial(client.read_enrollment, STUDENT_C, COURSE_B)
    else:
        call = partial(client.read_enrollment, STUDENT_C, COURSE_A)

    with pytest.raises(AccessRefused) as exc:
        call()
    assert exc.value.reason == reason
    assert exc.value.status_code == status


def test_the_published_client_stays_value_only():
    harness = enrollment_harness()
    client = harness.client()

    state = [
        getattr(client, name) for name in client.__slots__ if not name.startswith("__")
    ]
    assert len(state) == 1
    assert isinstance(state[0], str) and state[0]
    for forbidden in ("store", "engine", "app", "audit", "deployment"):
        assert not hasattr(client, forbidden), forbidden
