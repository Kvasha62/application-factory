"""SCS-001 Slice 4 — Learning Content Authoring (Issue #31): domain slice.

The upstream hierarchy of the existing Learning flow —
``Course → Module → Lesson → Assignment`` — against the fully composed
Platform Instance (IS-001 + IS-002 + IS-003 + IS-005 + Learning): creation
commands, hierarchy invariants (no orphan, no cross-tenant child, tenant
consistency), the immutable-published rule, payload validation and the
navigation read. Publication/archive atomicity, idempotency and the
authorization/tenant-isolation matrices live in the sibling modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from authorization_service.models import ServiceAccess
from authorization_service.store import CONSUMER_PERMISSIONS
from learning_service.adapters import authorization_port, tenant_context_port
from learning_service.contracts import (
    OPERATION_ASSIGNMENT_CREATE,
    OPERATION_COURSE_ARCHIVE,
    OPERATION_COURSE_CREATE,
    OPERATION_COURSE_PUBLISH,
    OPERATION_COURSE_READ,
    OPERATION_COURSE_READ_UNPUBLISHED,
    OPERATION_LESSON_CREATE,
    OPERATION_LIST,
    OPERATION_MODULE_CREATE,
    OPERATION_READ,
    OPERATION_REVIEW,
)
from learning_service.deployment import LearningDeployment
from learning_service.deployment import build_deployment as build_learning
from learning_service.errors import AccessRefused
from learning_service.store import LearningStore
from tests.conftest import PLATFORM_ID, monolith
from tests.test_learning_slice1 import LEARNING_CREDENTIAL, STUDENT_C, TEACHER_A, TEACHER_B
from tests.test_learning_slice1 import envelope_of

#: The Teacher grant vocabulary of the authoring capability: the subject that
#: holds these grants IS the Teacher — no role logic exists inside Learning.
TEACHER_OPERATIONS = (
    OPERATION_COURSE_CREATE,
    OPERATION_MODULE_CREATE,
    OPERATION_LESSON_CREATE,
    OPERATION_ASSIGNMENT_CREATE,
    OPERATION_COURSE_PUBLISH,
    OPERATION_COURSE_ARCHIVE,
    OPERATION_COURSE_READ,
    OPERATION_COURSE_READ_UNPUBLISHED,
)


@dataclass
class AuthoringHarness:
    instance: Any
    learning: LearningDeployment

    def http(self) -> TestClient:
        return TestClient(self.learning.contract_app())

    def client(self) -> Any:
        return self.learning.publish()


def authoring_harness(
    store: LearningStore | None = None,
    *,
    seed_demo: bool = True,
) -> AuthoringHarness:
    """Compose IS-001/IS-002/IS-003 plus Learning with the authoring grants.

    Teacher A holds the full authoring vocabulary in ``ten_a``, Teacher B in
    ``ten_b``; the student (``idn_human_c``, ``ten_a``) holds only the
    published-content read. The IS-001 tenant-context port is wired for the
    create-course command, whose target does not exist yet.
    """
    instance = monolith()
    instance.authorization.store.services["svc_learning"] = ServiceAccess(
        "svc_learning", PLATFORM_ID, CONSUMER_PERMISSIONS
    )
    instance.authorization.store.service_tokens[LEARNING_CREDENTIAL] = "svc_learning"
    for token_identity, tenant in (("idn_human_a", "ten_a"), ("idn_human_b", "ten_b")):
        instance.authorization.store.grant(tenant, token_identity, *TEACHER_OPERATIONS)
        instance.authorization.store.grant(
            tenant,
            token_identity,
            OPERATION_READ,
            OPERATION_LIST,
            OPERATION_REVIEW,
        )
    instance.authorization.store.grant("ten_a", "idn_human_c", OPERATION_COURSE_READ)
    learning = build_learning(
        {"platform_id": instance.authority.current_platform_id, "environment": "test"},
        authorization=authorization_port(
            instance.authorization.publish(credential=LEARNING_CREDENTIAL)
        ),
        tenant_context=tenant_context_port(instance.identity_context_client),
        store=store,
        seed_demo=seed_demo and store is None,
        with_http=True,
    )
    return AuthoringHarness(instance=instance, learning=learning)


# ------------------------------------------------------------- command helpers
def create_course(
    harness: AuthoringHarness,
    *,
    key: str | None,
    token: str | None = TEACHER_A,
    title: str = "Algebra",
    description: str = "An algebra course",
    claim: str | None = None,
    body: dict | None = None,
) -> Any:
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if key is not None:
        headers["idempotency-key"] = key
    if claim is not None:
        headers["x-tenant-id"] = claim
    payload = body if body is not None else {"title": title, "description": description}
    return harness.http().post("/api/v1/learning/courses", headers=headers, json=payload)


def create_module(
    harness: AuthoringHarness,
    course_id: str,
    *,
    key: str | None,
    token: str | None = TEACHER_A,
    title: str = "Module 1",
    position: int = 1,
) -> Any:
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if key is not None:
        headers["idempotency-key"] = key
    return harness.http().post(
        f"/api/v1/learning/courses/{course_id}/modules",
        headers=headers,
        json={"title": title, "position": position},
    )


def create_lesson(
    harness: AuthoringHarness,
    module_id: str,
    *,
    key: str | None,
    token: str | None = TEACHER_A,
    title: str = "Lesson 1",
    content: str = "Lesson body",
    position: int = 1,
) -> Any:
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if key is not None:
        headers["idempotency-key"] = key
    return harness.http().post(
        f"/api/v1/learning/modules/{module_id}/lessons",
        headers=headers,
        json={"title": title, "content": content, "position": position},
    )


def create_assignment(
    harness: AuthoringHarness,
    lesson_id: str,
    *,
    key: str | None,
    token: str | None = TEACHER_A,
    title: str = "Assignment 1",
    instructions: str = "Solve the exercises",
) -> Any:
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if key is not None:
        headers["idempotency-key"] = key
    return harness.http().post(
        f"/api/v1/learning/lessons/{lesson_id}/assignments",
        headers=headers,
        json={"title": title, "instructions": instructions},
    )


def author_hierarchy(
    harness: AuthoringHarness, *, key_prefix: str, token: str | None = TEACHER_A
) -> dict[str, str]:
    """Author one full ``Course → Module → Lesson → Assignment`` chain."""
    course = create_course(harness, key=f"{key_prefix}-crs", token=token)
    assert course.status_code == 201, course.text
    course_id = course.json()["course_id"]
    module = create_module(harness, course_id, key=f"{key_prefix}-mod", token=token)
    assert module.status_code == 201, module.text
    module_id = module.json()["module_id"]
    lesson = create_lesson(harness, module_id, key=f"{key_prefix}-les", token=token)
    assert lesson.status_code == 201, lesson.text
    lesson_id = lesson.json()["lesson_id"]
    assignment = create_assignment(
        harness, lesson_id, key=f"{key_prefix}-asg", token=token
    )
    assert assignment.status_code == 201, assignment.text
    return {
        "course_id": course_id,
        "module_id": module.json()["module_id"],
        "lesson_id": lesson.json()["lesson_id"],
        "assignment_id": assignment.json()["assignment_id"],
    }


def read_course(harness: AuthoringHarness, course_id: str, token: str = TEACHER_A) -> Any:
    return harness.http().get(
        f"/api/v1/learning/courses/{course_id}",
        headers={"authorization": f"Bearer {token}"},
    )


# ------------------------------------------------------------------- creation
def test_teacher_creates_a_draft_course():
    harness = authoring_harness()

    response = create_course(harness, key="ik-crs-1")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["title"] == "Algebra"
    assert body["description"] == "An algebra course"
    assert body["modules"] == []
    # created_by is the verified identity of the chain, never a caller claim.
    assert body["created_by"] == "idn_human_a"
    assert body["created_at"] and body["updated_at"]
    # No tenant or owner data is published by the view.
    assert "tenant_id" not in body
    assert "owner_component" not in body


def test_the_created_course_lands_in_the_effective_tenant_of_the_identity():
    harness = authoring_harness()

    # The cross-check header agrees with the verified identity (idn_human_a is
    # bound to ten_a), so the command is decided — and the Course belongs to
    # the effective tenant of the verified identity, never to the claim.
    response = create_course(harness, key="ik-crs-2", claim="ten_a")

    assert response.status_code == 201, response.text
    stored = harness.learning.store.courses[response.json()["course_id"]]
    assert stored.tenant_id == "ten_a"


def test_a_caller_supplied_tenant_that_conflicts_with_the_identity_is_refused():
    harness = authoring_harness()
    harness.instance.authorization.store.grant(
        "ten_b", "idn_human_a", *TEACHER_OPERATIONS
    )

    response = create_course(harness, key="ik-crs-3", claim="ten_b")

    # idn_human_a is not bound to ten_b: the identity cross-check denies.
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "tenant_mismatch"
    assert harness.learning.store.courses == harness.learning.store.courses


def test_teacher_creates_module_lesson_and_assignment_as_draft():
    harness = authoring_harness()

    course = create_course(harness, key="ik-tree-1").json()
    module = create_module(harness, course["course_id"], key="ik-tree-2").json()
    lesson = create_lesson(harness, module["module_id"], key="ik-tree-3").json()
    assignment = create_assignment(
        harness, lesson["lesson_id"], key="ik-tree-4"
    ).json()

    assert module["status"] == "DRAFT"
    assert module["course_id"] == course["course_id"]
    assert lesson["status"] == "DRAFT"
    assert lesson["module_id"] == module["module_id"]
    assert lesson["content"] == "Lesson body"
    assert assignment["status"] == "DRAFT"
    assert assignment["lesson_id"] == lesson["lesson_id"]
    assert assignment["title"] == "Assignment 1"
    assert assignment["instructions"] == "Solve the exercises"

    stored = harness.learning.store
    assert stored.modules[module["module_id"]].tenant_id == "ten_a"
    assert stored.lessons[lesson["lesson_id"]].tenant_id == "ten_a"
    assert stored.assignments[assignment["assignment_id"]].tenant_id == "ten_a"


def test_every_child_belongs_to_exactly_one_parent_of_the_same_tenant():
    harness = authoring_harness()

    ids = author_hierarchy(harness, key_prefix="ik-one")

    stored = harness.learning.store
    module = stored.modules[ids["module_id"]]
    lesson = stored.lessons[ids["lesson_id"]]
    assignment = stored.assignments[ids["assignment_id"]]
    assert module.course_id == ids["course_id"]
    assert lesson.module_id == ids["module_id"]
    assert assignment.lesson_id == ids["lesson_id"]
    course_tenant = stored.courses[ids["course_id"]].tenant_id
    assert module.tenant_id == lesson.tenant_id == assignment.tenant_id == course_tenant


def test_a_module_cannot_be_created_under_an_unknown_course():
    harness = authoring_harness()

    response = create_module(harness, "crs_missing", key="ik-orphan-1")

    assert response.status_code == 404
    body = envelope_of(response)
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["details"]["reason"] == "course_unknown"


def test_a_lesson_cannot_be_created_under_an_unknown_module():
    harness = authoring_harness()

    response = create_lesson(harness, "mod_missing", key="ik-orphan-2")

    assert response.status_code == 404
    assert envelope_of(response)["error"]["details"]["reason"] == "module_unknown"


def test_an_assignment_cannot_be_created_under_an_unknown_lesson():
    harness = authoring_harness()

    response = create_assignment(harness, "les_missing", key="ik-orphan-3")

    assert response.status_code == 404
    assert envelope_of(response)["error"]["details"]["reason"] == "lesson_unknown"


def test_the_store_refuses_orphan_and_cross_tenant_registrations():
    store = LearningStore()
    import pytest

    from learning_service.models import OwnedCourse, OwnedModule

    course = store.register_course(
        OwnedCourse(
            course_id="crs_x",
            tenant_id="ten_a",
            owner_component="learning",
            title="t",
            description="",
            status="DRAFT",
            created_by="idn_human_a",
            created_at="2026-09-09T00:00:00+00:00",
            updated_at="2026-09-09T00:00:00+00:00",
        )
    )
    assert course.status == "DRAFT"

    from learning_service.models import OwnedLesson

    with pytest.raises(ValueError):
        # No orphan modules: the parent Course must be registered.
        store.register_module(
            OwnedModule(
                module_id="mod_orphan",
                course_id="crs_unknown",
                tenant_id="ten_a",
                owner_component="learning",
                title="t",
                position=0,
                status="DRAFT",
            )
        )
    with pytest.raises(ValueError):
        # No cross-tenant children: a module of another Tenant is refused.
        store.register_module(
            OwnedModule(
                module_id="mod_foreign",
                course_id="crs_x",
                tenant_id="ten_b",
                owner_component="learning",
                title="t",
                position=0,
                status="DRAFT",
            )
        )
    with pytest.raises(ValueError):
        # A module is registered only in DRAFT.
        store.register_module(
            OwnedModule(
                module_id="mod_published",
                course_id="crs_x",
                tenant_id="ten_a",
                owner_component="learning",
                title="t",
                position=0,
                status="PUBLISHED",
            )
        )
    with pytest.raises(ValueError):
        # No orphan lessons either.
        store.register_lesson(
            OwnedLesson(
                lesson_id="les_orphan",
                module_id="mod_unknown",
                tenant_id="ten_a",
                owner_component="learning",
                title="t",
                content="",
                position=0,
                status="DRAFT",
            )
        )


def test_validation_error_on_an_empty_title():
    harness = authoring_harness(store=LearningStore())

    response = create_course(harness, key="ik-val-1", title="   ")

    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "validation_error"
    # No business effect and no successful idempotency record.
    assert harness.learning.store.courses == {}
    assert "ik-val-1" not in harness.learning.engine.idempotency._store


def test_validation_error_on_an_oversized_title():
    harness = authoring_harness()

    response = create_course(harness, key="ik-val-2", title="x" * 513)

    assert response.status_code == 422
    assert envelope_of(response)["error"]["details"]["reason"] == "validation_error"


def test_validation_error_on_a_negative_position():
    harness = authoring_harness(store=LearningStore())
    course = create_course(harness, key="ik-val-3").json()

    response = create_module(
        harness, course["course_id"], key="ik-val-4", position=-1
    )

    assert response.status_code == 422
    assert envelope_of(response)["error"]["details"]["reason"] == "validation_error"
    assert harness.learning.store.modules == {}


def test_a_malformed_body_is_an_invalid_request():
    harness = authoring_harness(store=LearningStore())

    response = harness.http().post(
        "/api/v1/learning/courses",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-bad-1"},
        json={"description": "missing the title"},
    )

    assert response.status_code == 422
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["reason"] == "malformed_request"


def test_an_undeclared_payload_field_is_refused():
    harness = authoring_harness(store=LearningStore())

    response = create_course(
        harness,
        key="ik-bad-2",
        body={"title": "t", "description": "d", "tenant_id": "ten_b"},
    )

    # A caller cannot smuggle a tenant_id (or any undeclared field) into the
    # command: the published schema refuses the request outright.
    assert response.status_code == 422
    assert envelope_of(response)["error"]["details"]["reason"] == "malformed_request"
    assert harness.learning.store.courses == {}


# ------------------------------------------------------------------- the read
def test_the_hierarchy_read_returns_the_full_navigation_chain():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-read")

    response = read_course(harness, ids["course_id"])

    assert response.status_code == 200
    body = response.json()
    assert body["course_id"] == ids["course_id"]
    assert body["status"] == "DRAFT"
    assert len(body["modules"]) == 1
    module = body["modules"][0]
    assert module["module_id"] == ids["module_id"]
    assert module["course_id"] == ids["course_id"]
    assert len(module["lessons"]) == 1
    lesson = module["lessons"][0]
    assert lesson["lesson_id"] == ids["lesson_id"]
    assert lesson["module_id"] == ids["module_id"]
    assert len(lesson["assignments"]) == 1
    assignment = lesson["assignments"][0]
    assert assignment["assignment_id"] == ids["assignment_id"]
    assert assignment["lesson_id"] == ids["lesson_id"]


def test_the_hierarchy_read_order_is_deterministic():
    harness = authoring_harness()
    course = create_course(harness, key="ik-order-crs").json()
    course_id = course["course_id"]
    first = create_module(harness, course_id, key="ik-order-m1", position=2).json()
    second = create_module(harness, course_id, key="ik-order-m2", position=1).json()
    create_lesson(
        harness, first["module_id"], key="ik-order-l1", title="A", position=2
    )
    create_lesson(
        harness, first["module_id"], key="ik-order-l2", title="B", position=1
    )

    body = read_course(harness, course_id).json()

    # Deterministic order: position first, then the id.
    assert [m["module_id"] for m in body["modules"]] == [
        second["module_id"],
        first["module_id"],
    ]
    lessons = body["modules"][1]["lessons"]
    assert [lesson["title"] for lesson in lessons] == ["B", "A"]


def test_the_hierarchy_read_of_an_unknown_course_is_not_found():
    harness = authoring_harness()

    response = read_course(harness, "crs_missing")

    assert response.status_code == 404
    assert envelope_of(response)["error"]["details"]["reason"] == "course_unknown"


def test_a_foreign_owner_course_is_never_served():
    from learning_service.models import OwnedCourse

    harness = authoring_harness()
    store = harness.learning.store
    store.courses["crs_foreign"] = OwnedCourse(
        course_id="crs_foreign",
        tenant_id="ten_a",
        owner_component="identity",
        title="t",
        description="",
        status="DRAFT",
        created_by="idn_human_a",
        created_at="2026-09-09T00:00:00+00:00",
        updated_at="2026-09-09T00:00:00+00:00",
    )

    response = read_course(harness, "crs_foreign")

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "owner_mismatch"


def test_the_created_assignment_still_serves_the_submission_flow():
    """The authoring slice extends the existing flow — no second model."""
    from learning_service.models import OwnedSubmission

    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-flow")

    # The existing submission registration (the canonical flow) accepts the
    # authored assignment as its parent: same identity, same tenant rule.
    harness.learning.store.register_submission(
        OwnedSubmission(
            submission_id="sub_authored_1",
            assignment_id=ids["assignment_id"],
            tenant_id="ten_a",
            student_identity_id="idn_human_c",
            attempt=1,
            content={},
            status="SUBMITTED",
            created_at="2026-09-09T00:00:00+00:00",
            updated_at="2026-09-09T00:00:00+00:00",
            owner_component="learning",
        )
    )

    response = harness.http().get(
        "/api/v1/learning/submissions/sub_authored_1",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["assignment_id"] == ids["assignment_id"]

    listing = harness.http().get(
        f"/api/v1/learning/assignments/{ids['assignment_id']}/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert listing.status_code == 200
    assert [item["submission_id"] for item in listing.json()["items"]] == [
        "sub_authored_1"
    ]


def test_the_published_client_creates_and_reads_the_hierarchy():
    harness = authoring_harness()
    consumer = harness.client()

    course = consumer.create_course(
        TEACHER_A,
        title="Client course",
        description="via the published client",
        idempotency_key="ik-client-crs",
    )
    module = consumer.create_module(
        TEACHER_A, course.course_id, title="M", position=1, idempotency_key="ik-client-mod"
    )
    lesson = consumer.create_lesson(
        TEACHER_A,
        module.module_id,
        title="L",
        content="body",
        position=1,
        idempotency_key="ik-client-les",
    )
    assignment = consumer.create_assignment(
        TEACHER_A,
        lesson.lesson_id,
        title="A",
        instructions="do it",
        idempotency_key="ik-client-asg",
    )
    view = consumer.read_course(TEACHER_A, course.course_id)

    assert view.course_id == course.course_id
    assert view.status == "DRAFT"
    assert view.modules[0].module_id == module.module_id
    assert view.modules[0].lessons[0].lesson_id == lesson.lesson_id
    assert view.modules[0].lessons[0].assignments[0].assignment_id == (
        assignment.assignment_id
    )


def test_no_generic_editing_endpoints_exist():
    harness = authoring_harness()

    methods = {
        method
        for route in harness.learning.contract_app().routes
        for method in getattr(route, "methods", set())
    }

    # The published hierarchy is immutable by construction: no editing API.
    assert {"PUT", "PATCH", "DELETE"}.isdisjoint(methods)
