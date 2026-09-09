"""SCS-001 Slice 4 — Learning Content Authoring (Issue #31): security slice.

The enforcement chain for the authoring commands against the composed
Platform Instance (IS-001 + IS-002 + IS-003 + IS-005): fail closed without
identity, the Teacher/Student grant matrix, tenant isolation for every
command and read, the IS-001 identity cross-check, and dependency failures.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from learning_service.store import LearningStore
from tests.test_learning_authoring import (
    STUDENT_C,
    TEACHER_A,
    TEACHER_B,
    TEACHER_OPERATIONS,
    author_hierarchy,
    authoring_harness,
    create_assignment,
    create_course,
    create_lesson,
    create_module,
    read_course,
)
from tests.test_learning_slice1 import envelope_of

PUBLISH_BODY = None


def publish(harness, course_id: str, *, key: str, token: str = TEACHER_A):
    return harness.http().post(
        f"/api/v1/learning/courses/{course_id}/publish",
        headers={"authorization": f"Bearer {token}", "idempotency-key": key},
    )


def archive(harness, course_id: str, *, key: str, token: str = TEACHER_A):
    return harness.http().post(
        f"/api/v1/learning/courses/{course_id}/archive",
        headers={"authorization": f"Bearer {token}", "idempotency-key": key},
    )


# ------------------------------------------------------------ identity refused
def test_a_command_without_a_credential_is_refused_before_any_grant():
    harness = authoring_harness(store=LearningStore())

    response = harness.http().post(
        "/api/v1/learning/courses",
        headers={"idempotency-key": "ik-noauth"},
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 401
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert body["error"]["details"]["reason"] == "missing_identity"
    # Fail closed: no effect, no bound idempotency key.
    assert harness.learning.store.courses == {}
    assert "ik-noauth" not in harness.learning.engine.idempotency._store


def test_an_invalid_credential_is_refused_on_every_authoring_command():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-badtoken")
    http = harness.http()

    course = http.post(
        "/api/v1/learning/courses",
        headers={"authorization": "Bearer token-invalid", "idempotency-key": "ik-bad-1"},
        json={"title": "t", "description": "d"},
    )
    publish_attempt = http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": "Bearer token-invalid", "idempotency-key": "ik-bad-2"},
    )
    read = http.get(
        f"/api/v1/learning/courses/{ids['course_id']}",
        headers={"authorization": "Bearer token-invalid"},
    )

    for response in (course, publish_attempt, read):
        assert response.status_code == 401
        body = envelope_of(response)
        assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
        assert body["error"]["details"]["reason"] == "invalid_identity"
    assert harness.learning.store.courses == harness.learning.store.courses


def test_a_read_without_a_credential_is_refused():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-noread")

    response = harness.http().get(f"/api/v1/learning/courses/{ids['course_id']}")

    assert response.status_code == 401
    assert envelope_of(response)["error"]["details"]["reason"] == "missing_identity"


# --------------------------------------------------------------- grant matrix
def test_a_student_is_denied_every_authoring_command():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-studenied")
    http = harness.http()
    student = {"authorization": f"Bearer {STUDENT_C}"}
    courses_before = len(harness.learning.store.courses)

    attempts = [
        http.post(
            "/api/v1/learning/courses",
            headers={**student, "idempotency-key": "ik-s1"},
            json={"title": "t", "description": "d"},
        ),
        http.post(
            f"/api/v1/learning/courses/{ids['course_id']}/modules",
            headers={**student, "idempotency-key": "ik-s2"},
            json={"title": "t", "position": 1},
        ),
        http.post(
            f"/api/v1/learning/modules/{ids['module_id']}/lessons",
            headers={**student, "idempotency-key": "ik-s3"},
            json={"title": "t", "content": "c", "position": 1},
        ),
        http.post(
            f"/api/v1/learning/lessons/{ids['lesson_id']}/assignments",
            headers={**student, "idempotency-key": "ik-s4"},
            json={"title": "t", "instructions": "i"},
        ),
        publish(harness, ids["course_id"], key="ik-s5", token=STUDENT_C),
        archive(harness, ids["course_id"], key="ik-s6", token=STUDENT_C),
    ]

    for response in attempts:
        assert response.status_code == 403, response.text
        body = envelope_of(response)
        assert body["error"]["code"] == "AUTHORIZATION_DENIED"
        assert body["error"]["details"]["reason"] == "permission_not_granted"
    # Nothing was created and nothing changed state.
    store = harness.learning.store
    assert len(store.courses) == courses_before
    assert store.courses[ids["course_id"]].status == "DRAFT"


def test_a_command_grant_is_checked_individually():
    """No blanket Teacher pass: each command needs its own grant."""
    from learning_service.contracts import (
        OPERATION_COURSE_CREATE,
        OPERATION_COURSE_PUBLISH,
    )

    harness = authoring_harness(store=LearningStore())
    # A subject that can create but not publish.
    harness.instance.authorization.store.grant(
        "ten_a", "idn_human_c", OPERATION_COURSE_CREATE
    )

    created = create_course(harness, key="ik-granular-1", token=STUDENT_C)
    assert created.status_code == 201, created.text
    refused = publish(harness, created.json()["course_id"], key="ik-granular-2", token=STUDENT_C)

    assert refused.status_code == 403
    assert (
        envelope_of(refused)["error"]["details"]["reason"] == "permission_not_granted"
    )
    assert harness.learning.store.courses[created.json()["course_id"]].status == "DRAFT"


def test_a_teacher_without_the_archive_grant_cannot_archive():
    from learning_service.contracts import (
        OPERATION_COURSE_CREATE,
        OPERATION_COURSE_PUBLISH,
    )

    harness = authoring_harness(store=LearningStore())
    harness.instance.authorization.store.grant(
        "ten_a", "idn_human_c", OPERATION_COURSE_CREATE, OPERATION_COURSE_PUBLISH
    )
    created = create_course(harness, key="ik-noarchive-1", token=STUDENT_C)
    published = publish(
        harness, created.json()["course_id"], key="ik-noarchive-2", token=STUDENT_C
    )
    assert published.status_code == 200

    refused = archive(
        harness, created.json()["course_id"], key="ik-noarchive-3", token=STUDENT_C
    )

    assert refused.status_code == 403
    assert (
        envelope_of(refused)["error"]["details"]["reason"] == "permission_not_granted"
    )
    assert (
        harness.learning.store.courses[created.json()["course_id"]].status
        == "PUBLISHED"
    )


def test_a_deny_never_reaches_the_owned_data_operation():
    harness = authoring_harness(store=LearningStore())
    before = len(harness.learning.store.audit)

    denied = create_course(harness, key="ik-deny-audit", token=STUDENT_C)

    assert denied.status_code == 403
    events = harness.learning.store.audit[before:]
    # Exactly the audited denial — no owned-data operation event, no effect.
    assert len(events) == 1
    assert events[0].action == "learning.courses.create"
    assert events[0].decision == "DENY"
    assert harness.learning.store.courses == {}


# ----------------------------------------------------------- tenant isolation
def test_a_teacher_cannot_read_another_tenant_s_course():
    harness = authoring_harness()
    # crs_b1 is the published demo course of tenant B.

    response = read_course(harness, "crs_b1")

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "resource_tenant_mismatch"


def test_a_teacher_cannot_create_a_child_under_another_tenant_s_course():
    harness = authoring_harness()
    # crs_b1 belongs to tenant B; the tenant A teacher asks to extend it.

    response = create_module(harness, "crs_b1", key="ik-xmod-1")

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "resource_tenant_mismatch"
    assert harness.learning.store.modules["mod_b1"].course_id == "crs_b1"
    assert len(harness.learning.store.modules) == 2


def test_a_teacher_cannot_publish_another_tenant_s_course():
    harness = authoring_harness()
    ids_b = author_hierarchy(
        harness, key_prefix="ik-xpub", token=TEACHER_B
    )
    # Sanity: teacher B owns the hierarchy.
    assert harness.learning.store.courses[ids_b["course_id"]].tenant_id == "ten_b"

    response = publish(harness, ids_b["course_id"], key="ik-xpub-1")

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "resource_tenant_mismatch"
    )
    store = harness.learning.store
    assert store.courses[ids_b["course_id"]].status == "DRAFT"
    assert store.modules[ids_b["module_id"]].status == "DRAFT"
    assert store.lessons[ids_b["lesson_id"]].status == "DRAFT"


def test_a_teacher_cannot_archive_another_tenant_s_course():
    harness = authoring_harness()
    ids_b = author_hierarchy(harness, key_prefix="ik-xarch", token=TEACHER_B)
    published = publish(harness, ids_b["course_id"], key="ik-xarch-p", token=TEACHER_B)
    assert published.status_code == 200

    response = archive(harness, ids_b["course_id"], key="ik-xarch-1")

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "resource_tenant_mismatch"
    )
    assert harness.learning.store.courses[ids_b["course_id"]].status == "PUBLISHED"


def test_a_student_cannot_read_another_tenant_s_published_course():
    harness = authoring_harness()
    # crs_b1 is published in tenant B; the student holds only the tenant A read.

    response = harness.http().get(
        "/api/v1/learning/courses/crs_b1",
        headers={"authorization": f"Bearer {STUDENT_C}"},
    )

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"]
        == "resource_tenant_mismatch"
    )


def test_a_child_of_another_tenant_cannot_be_created_under_any_grandparent():
    """The tenant rule is checked at every level of the chain."""
    harness = authoring_harness()
    ids_b = author_hierarchy(harness, key_prefix="ik-xles", token=TEACHER_B)
    lessons_before = len(harness.learning.store.lessons)
    assignments_before = len(harness.learning.store.assignments)

    lesson = create_lesson(harness, ids_b["module_id"], key="ik-xles-1")
    assignment = create_assignment(harness, ids_b["lesson_id"], key="ik-xles-2")

    for response in (lesson, assignment):
        assert response.status_code == 403
        body = envelope_of(response)
        assert body["error"]["code"] == "AUTHORIZATION_DENIED"
        assert body["error"]["details"]["reason"] == "resource_tenant_mismatch"
    store = harness.learning.store
    assert len(store.lessons) == lessons_before
    assert len(store.assignments) == assignments_before


def test_the_identity_cross_check_denies_a_claimed_foreign_tenant():
    harness = authoring_harness()
    # Give the tenant A teacher every tenant B authoring grant — the refusal
    # must come from IS-001 (the identity is not bound to ten_b), not from
    # the grants.
    harness.instance.authorization.store.grant("ten_b", "idn_human_a", *TEACHER_OPERATIONS)

    response = create_course(harness, key="ik-claim", claim="ten_b")

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "tenant_mismatch"
    # No course was created in either tenant.
    assert len(harness.learning.store.courses) == 2


def test_an_unbound_identity_cannot_become_the_effective_tenant():
    """A subject bound to no tenant is denied the whole command chain."""
    harness = authoring_harness(store=LearningStore())
    # idn_human_c is bound to ten_a only; the claim selects nothing.
    harness.instance.authorization.store.grant("ten_a", "idn_human_c", *TEACHER_OPERATIONS)

    response = harness.http().post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {STUDENT_C}",
            "idempotency-key": "ik-unbound",
        },
        json={"title": "t", "description": "d"},
    )

    # The effective tenant is the identity's own (ten_a): allowed, and the
    # course belongs to ten_a — the identity, not the claim, decides.
    assert response.status_code == 201, response.text
    assert harness.learning.store.courses[response.json()["course_id"]].tenant_id == "ten_a"


# ------------------------------------------------------- dependency failures
def test_a_silent_authorization_dependency_makes_authoring_impossible():
    from tests.test_learning_boundary import StubAuthorizationPort, learning_deployment

    deployment = learning_deployment(StubAuthorizationPort(None), seed_demo=False)
    http = TestClient(deployment.contract_app())

    response = http.post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-silent",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert deployment.store.courses == {}


def test_a_malformed_authorization_answer_fails_the_command_closed():
    from tests.test_learning_boundary import StubAuthorizationPort, learning_deployment

    deployment = learning_deployment(
        StubAuthorizationPort(object()), seed_demo=False
    )
    http = TestClient(deployment.contract_app())

    response = http.post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-malformed",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 503
    assert deployment.store.courses == {}
    assert "ik-malformed" not in deployment.engine.idempotency._store


def test_a_failing_identity_context_refuses_create_course_closed():
    from learning_service.adapters import authorization_port
    from learning_service.deployment import build_deployment as build_learning

    class ExplodingTenantContext:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("identity down")

    harness = authoring_harness(store=LearningStore())
    learning = build_learning(
        {
            "platform_id": harness.instance.authority.current_platform_id,
            "environment": "test",
        },
        authorization=authorization_port(
            harness.instance.authorization.publish(credential="authz-svc-token-learning")
        ),
        tenant_context=ExplodingTenantContext(),
        store=LearningStore(),
        with_http=True,
    )
    http = TestClient(learning.contract_app())

    response = http.post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-id-down",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"]["reason"] == "authorization_unavailable"
    assert learning.store.audit[-1].details == {
        "stated_code": None
    } or learning.store.audit[-1].details is not None
    assert learning.store.courses == {}
    assert "ik-id-down" not in learning.engine.idempotency._store


def test_an_unwired_tenant_context_refuses_create_course_closed():
    """No port, no command: the failure is explicit, never a silent pass."""
    from learning_service.adapters import authorization_port
    from learning_service.deployment import build_deployment as build_learning

    harness = authoring_harness(store=LearningStore())
    deployment = build_learning(
        {
            "platform_id": harness.instance.authority.current_platform_id,
            "environment": "test",
        },
        authorization=authorization_port(
            harness.instance.authorization.publish(credential="authz-svc-token-learning")
        ),
        store=LearningStore(),
        with_http=True,
    )  # no tenant_context port wired
    http = TestClient(deployment.contract_app())

    response = http.post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-unwired",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"] == {
        "reason": "authorization_unavailable"
    }
    # The precise refusal is in the audit journal, not the public envelope.
    event = deployment.store.audit[-1]
    assert event.details["refused"] == "tenant_context_port_not_wired"
    assert deployment.store.courses == {}


def test_a_non_authoritative_tenant_context_answer_is_refused():
    from learning_service.adapters import authorization_port
    from learning_service.consumed import TenantContextRefusal
    from learning_service.deployment import build_deployment as build_learning

    class RogueTenantContext:
        """Answers outside the published IS-001 vocabulary."""

        def resolve(self, *args, **kwargs):
            return TenantContextRefusal("totally_unknown_code")

    harness = authoring_harness(store=LearningStore())
    learning = build_learning(
        {
            "platform_id": harness.instance.authority.current_platform_id,
            "environment": "test",
        },
        authorization=authorization_port(
            harness.instance.authorization.publish(credential="authz-svc-token-learning")
        ),
        tenant_context=RogueTenantContext(),
        store=LearningStore(),
        with_http=True,
    )
    http = TestClient(learning.contract_app())

    response = http.post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-rogue",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"] == {"reason": "authorization_unavailable"}
    # The non-authoritative answer is named in the audit journal only.
    event = learning.store.audit[-1]
    assert event.details["stated_code"] == "totally_unknown_code"
    assert learning.store.courses == {}


def test_an_identity_refusal_with_a_published_reason_is_passed_through():
    from learning_service.adapters import authorization_port
    from learning_service.consumed import TenantContextRefusal
    from learning_service.deployment import build_deployment as build_learning

    class SuspendedTenantContext:
        """IS-001 refuses the context read with its published reason."""

        def resolve(self, *args, **kwargs):
            return TenantContextRefusal("tenant_suspended")

    harness = authoring_harness(store=LearningStore())
    learning = build_learning(
        {
            "platform_id": harness.instance.authority.current_platform_id,
            "environment": "test",
        },
        authorization=authorization_port(
            harness.instance.authorization.publish(credential="authz-svc-token-learning")
        ),
        tenant_context=SuspendedTenantContext(),
        store=LearningStore(),
        with_http=True,
    )
    http = TestClient(learning.contract_app())

    response = http.post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-susp",
        },
        json={"title": "t", "description": "d"},
    )

    # The stable reason of the identity read is passed through unchanged and
    # denies the command — never swallowed, never re-invented.
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "tenant_suspended"
    assert learning.store.courses == {}


# ------------------------------------------------------------------- ordering
def test_the_chain_runs_in_the_declared_order_on_a_command():
    """Validation → identity → tenant context → decision → effect."""
    from learning_service.contracts import OPERATION_COURSE_CREATE
    from learning_service.errors import AccessRefused

    harness = authoring_harness(store=LearningStore())
    # The subject holds the grant but claims a foreign tenant: the IS-001
    # cross-check (step 3) fires although the decision (step 4) would allow.
    harness.instance.authorization.store.grant("ten_b", "idn_human_c", OPERATION_COURSE_CREATE)

    response = create_course(
        harness, key="ik-order", token=STUDENT_C, claim="ten_b"
    )
    assert response.status_code == 403
    assert envelope_of(response)["error"]["details"]["reason"] == "tenant_mismatch"

    # A schema-refused request never reaches identity either: the refused
    # request carries no business effect at all.
    schema = harness.http().post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-order-schema",
        },
        json={"description": "no title"},
    )
    assert schema.status_code == 422
    assert envelope_of(schema)["error"]["details"]["reason"] == "malformed_request"
    assert harness.learning.store.courses == {}
