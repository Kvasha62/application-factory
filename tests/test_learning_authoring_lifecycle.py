"""SCS-001 Slice 4 — Learning Content Authoring (Issue #31): lifecycle slice.

The Course lifecycle — publication and archive as atomic, whole-hierarchy,
whole-tenant effects with no unpublish and no unarchive — plus the immutable
published hierarchy and the idempotency of the authoring commands (IS-005):
exact replay, concurrent same-key, changed bindings.

The authorization and tenant-isolation matrices live in the sibling module.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from learning_service.store import LearningStore
from tests.test_learning_authoring import (
    STUDENT_C,
    TEACHER_A,
    TEACHER_B,
    authoring_harness,
    author_hierarchy,
    create_assignment,
    create_course,
    create_lesson,
    create_module,
    read_course,
)
from tests.test_learning_slice1 import envelope_of

# ---------------------------------------------------------------- publication
def test_a_teacher_publishes_a_valid_draft_hierarchy():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-pub")

    response = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-pub-1"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["course_id"] == ids["course_id"]
    assert body["status"] == "PUBLISHED"
    store = harness.learning.store
    assert store.courses[ids["course_id"]].status == "PUBLISHED"
    assert store.modules[ids["module_id"]].status == "PUBLISHED"
    assert store.lessons[ids["lesson_id"]].status == "PUBLISHED"
    assert store.assignments[ids["assignment_id"]].status == "PUBLISHED"


def test_publication_flips_the_whole_hierarchy_atomically():
    """A deep hierarchy crosses PUBLISHED together — or nothing does."""
    harness = authoring_harness()
    course = create_course(harness, key="ik-deep-crs").json()
    course_id = course["course_id"]
    module_a = create_module(harness, course_id, key="ik-deep-m1", position=1).json()
    module_b = create_module(harness, course_id, key="ik-deep-m2", position=2).json()
    lesson_a1 = create_lesson(harness, module_a["module_id"], key="ik-deep-l1").json()
    lesson_a2 = create_lesson(harness, module_a["module_id"], key="ik-deep-l2").json()
    lesson_b1 = create_lesson(harness, module_b["module_id"], key="ik-deep-l3").json()
    asg = create_assignment(harness, lesson_b1["lesson_id"], key="ik-deep-a1").json()

    response = harness.http().post(
        f"/api/v1/learning/courses/{course_id}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-deep-pub"},
    )

    assert response.status_code == 200, response.text
    store = harness.learning.store
    for module_id in (module_a["module_id"], module_b["module_id"]):
        assert store.modules[module_id].status == "PUBLISHED"
    for lesson_id in (lesson_a1["lesson_id"], lesson_a2["lesson_id"], lesson_b1["lesson_id"]):
        assert store.lessons[lesson_id].status == "PUBLISHED"
    assert store.assignments[asg["assignment_id"]].status == "PUBLISHED"


def test_publication_of_a_non_draft_course_is_refused():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-twice")

    first = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-twice-1"},
    )
    assert first.status_code == 200

    # A second publication under a different key is an invalid transition.
    second = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-twice-2"},
    )
    assert second.status_code == 409
    body = envelope_of(second)
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert body["error"]["details"]["reason"] == "invalid_state_transition"


def test_publication_refuses_an_invalid_hierarchy_and_leaves_nothing_published():
    """One non-DRAFT descendant: the whole course command fails closed."""
    from learning_service.models import OwnedLesson

    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-badtree")
    # A published lesson is planted under the DRAFT course (a corrupted state
    # that registration and creation cannot produce — defence in depth).
    harness.learning.store.lessons["les_ghost"] = OwnedLesson(
        lesson_id="les_ghost",
        module_id=ids["module_id"],
        tenant_id="ten_a",
        owner_component="learning",
        title="Ghost",
        content="",
        position=2,
        status="PUBLISHED",
    )

    response = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-badtree-1"},
    )

    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
    # Nothing at all was published: not the course, not the other descendants.
    store = harness.learning.store
    assert store.courses[ids["course_id"]].status == "DRAFT"
    assert store.modules[ids["module_id"]].status == "DRAFT"
    assert store.lessons[ids["lesson_id"]].status == "DRAFT"
    assert store.assignments[ids["assignment_id"]].status == "DRAFT"


def test_publication_refuses_a_cross_tenant_hierarchy_and_leaves_nothing_published():
    from learning_service.models import OwnedModule

    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-xmod")
    harness.learning.store.modules["mod_foreign"] = OwnedModule(
        module_id="mod_foreign",
        course_id=ids["course_id"],
        tenant_id="ten_b",
        owner_component="learning",
        title="Foreign",
        position=2,
        status="DRAFT",
    )

    response = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-xmod-1"},
    )

    assert response.status_code == 409
    assert (
        envelope_of(response)["error"]["details"]["reason"] == "invalid_state_transition"
    )
    store = harness.learning.store
    assert store.courses[ids["course_id"]].status == "DRAFT"
    assert store.modules[ids["module_id"]].status == "DRAFT"
    assert store.lessons[ids["lesson_id"]].status == "DRAFT"


def test_publication_is_idempotent_on_exact_replay():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-replay")
    http = harness.http()
    headers = {
        "authorization": f"Bearer {TEACHER_A}",
        "idempotency-key": "ik-replay-pub",
    }

    first = http.post(f"/api/v1/learning/courses/{ids['course_id']}/publish", headers=headers)
    assert first.status_code == 200
    published_at = first.json()["updated_at"]
    replay = http.post(f"/api/v1/learning/courses/{ids['course_id']}/publish", headers=headers)

    assert replay.status_code == 200
    assert replay.json() == first.json()
    # The effect ran exactly once; the replay is recorded, not re-executed
    # (the replayed request itself is still audited as a served request).
    actions = [event.action for event in harness.learning.store.audit]
    assert actions.count("learning.courses.publish") == 2
    assert actions.count("idempotency_replay") == 1
    assert (
        harness.learning.store.courses[ids["course_id"]].updated_at == published_at
    )


def test_concurrent_same_key_publication_executes_the_effect_once():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-race")
    http = TestClient(harness.learning.contract_app())
    headers = {
        "authorization": f"Bearer {TEACHER_A}",
        "idempotency-key": "ik-race-pub",
    }
    url = f"/api/v1/learning/courses/{ids['course_id']}/publish"

    barrier = threading.Barrier(4)
    results: list = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        response = http.post(url, headers=headers)
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [200, 200, 200, 200]
    actions = [event.action for event in harness.learning.store.audit]
    assert actions.count("learning.courses.publish") == 4
    assert actions.count("idempotency_replay") == 3


# --------------------------------------------------------------------- archive
def test_a_teacher_archives_a_published_hierarchy():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-arch")
    harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-arch-p"},
    )

    response = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/archive",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-arch-1"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ARCHIVED"
    store = harness.learning.store
    assert store.courses[ids["course_id"]].status == "ARCHIVED"
    assert store.modules[ids["module_id"]].status == "ARCHIVED"
    assert store.lessons[ids["lesson_id"]].status == "ARCHIVED"
    assert store.assignments[ids["assignment_id"]].status == "ARCHIVED"


def test_archiving_a_draft_course_is_refused():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-archdraft")

    response = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/archive",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-archdraft-1"},
    )

    assert response.status_code == 409
    assert envelope_of(response)["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert harness.learning.store.courses[ids["course_id"]].status == "DRAFT"


def test_archive_is_idempotent_on_exact_replay():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-archrep")
    http = harness.http()
    publish_headers = {
        "authorization": f"Bearer {TEACHER_A}",
        "idempotency-key": "ik-archrep-p",
    }
    http.post(f"/api/v1/learning/courses/{ids['course_id']}/publish", headers=publish_headers)
    headers = {
        "authorization": f"Bearer {TEACHER_A}",
        "idempotency-key": "ik-archrep-1",
    }
    first = http.post(f"/api/v1/learning/courses/{ids['course_id']}/archive", headers=headers)
    assert first.status_code == 200

    replay = http.post(f"/api/v1/learning/courses/{ids['course_id']}/archive", headers=headers)

    assert replay.status_code == 200
    assert replay.json() == first.json()
    actions = [event.action for event in harness.learning.store.audit]
    assert actions.count("learning.courses.archive") == 2
    assert actions.count("idempotency_replay") == 1


def test_repeated_archive_under_a_new_key_is_refused():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-arch2")
    http = harness.http()
    for key in ("ik-arch2-p", "ik-arch2-1"):
        response = http.post(
            f"/api/v1/learning/courses/{ids['course_id']}/publish"
            if key.endswith("p")
            else f"/api/v1/learning/courses/{ids['course_id']}/archive",
            headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": key},
        )
        assert response.status_code == 200

    again = http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/archive",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-arch2-2"},
    )

    assert again.status_code == 409
    assert envelope_of(again)["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_no_unpublish_and_no_unarchive_exist():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-no-undo")

    # The published surface offers no way back: no route, no store method.
    assert harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/unpublish"
    ).status_code == 404
    assert harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/unarchive"
    ).status_code == 404
    assert not hasattr(harness.learning.store, "unpublish_course")
    assert not hasattr(harness.learning.store, "unarchive_course")

    # And after archiving, publication is a refused transition, not a revival.
    http = harness.http()
    http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-no-undo-p"},
    )
    http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/archive",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-no-undo-a"},
    )
    revived = http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-no-undo-p2"},
    )
    assert revived.status_code == 409


# ------------------------------------------------- the immutable published tree
def test_a_published_hierarchy_is_immutable():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-immutable")
    harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-immutable-p"},
    )

    # No child creation under any published parent.
    module = create_module(harness, ids["course_id"], key="ik-immutable-m2")
    lesson = create_lesson(harness, ids["module_id"], key="ik-immutable-l2")
    assignment = create_assignment(harness, ids["lesson_id"], key="ik-immutable-a2")
    assert module.status_code == 409
    assert lesson.status_code == 409
    assert assignment.status_code == 409
    for response in (module, lesson, assignment):
        body = envelope_of(response)
        assert body["error"]["code"] == "INVALID_STATE_TRANSITION"
        assert body["error"]["details"]["reason"] == "invalid_state_transition"

    # And nothing was created: the course still owns exactly its one module.
    store = harness.learning.store
    assert [
        module_id
        for module_id, module in store.modules.items()
        if module.course_id == ids["course_id"]
    ] == [ids["module_id"]]
    assert [
        lesson_id
        for lesson_id, lesson in store.lessons.items()
        if lesson.module_id == ids["module_id"]
    ] == [ids["lesson_id"]]


# ------------------------------------------------------------- student reads
def test_a_student_reads_a_published_hierarchy_of_the_own_tenant():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-sturead")
    harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-sturead-p"},
    )

    response = harness.http().get(
        f"/api/v1/learning/courses/{ids['course_id']}",
        headers={"authorization": f"Bearer {STUDENT_C}"},
    )

    assert response.status_code == 200
    assert response.json()["course_id"] == ids["course_id"]
    assert response.json()["modules"][0]["module_id"] == ids["module_id"]


def test_a_student_cannot_read_a_draft_hierarchy():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-studraft")

    response = harness.http().get(
        f"/api/v1/learning/courses/{ids['course_id']}",
        headers={"authorization": f"Bearer {STUDENT_C}"},
    )

    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "permission_not_granted"


def test_a_student_cannot_read_an_archived_hierarchy():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-stuarch")
    http = harness.http()
    http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-stuarch-p"},
    )
    http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/archive",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-stuarch-a"},
    )

    response = harness.http().get(
        f"/api/v1/learning/courses/{ids['course_id']}",
        headers={"authorization": f"Bearer {STUDENT_C}"},
    )

    assert response.status_code == 403
    assert (
        envelope_of(response)["error"]["details"]["reason"] == "permission_not_granted"
    )


def test_the_teacher_keeps_reading_after_archive():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-teacharch")
    http = harness.http()
    http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-teacharch-p"},
    )
    http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/archive",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-teacharch-a"},
    )

    response = read_course(harness, ids["course_id"])

    assert response.status_code == 200
    assert response.json()["status"] == "ARCHIVED"


# ------------------------------------------------------------------ idempotency
def test_every_authoring_command_requires_an_idempotency_key():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-nokey")
    http = harness.http()
    headers = {"authorization": f"Bearer {TEACHER_A}"}
    expected = [
        ("post", "/api/v1/learning/courses", {"title": "t", "description": "d"}),
        ("post", f"/api/v1/learning/courses/{ids['course_id']}/modules", {"title": "t", "position": 1}),
        ("post", f"/api/v1/learning/modules/{ids['module_id']}/lessons", {"title": "t", "content": "c", "position": 1}),
        ("post", f"/api/v1/learning/lessons/{ids['lesson_id']}/assignments", {"title": "t", "instructions": "i"}),
        ("post", f"/api/v1/learning/courses/{ids['course_id']}/publish", None),
        ("post", f"/api/v1/learning/courses/{ids['course_id']}/archive", None),
    ]
    # The course under test is DRAFT and the children DRAFT, so every refusal
    # below is the missing key, not the state rule.
    draft_ids = author_hierarchy(harness, key_prefix="ik-nokey2")
    expected[1] = ("post", f"/api/v1/learning/courses/{draft_ids['course_id']}/modules", {"title": "t", "position": 1})
    expected[2] = ("post", f"/api/v1/learning/modules/{draft_ids['module_id']}/lessons", {"title": "t", "content": "c", "position": 1})
    expected[3] = ("post", f"/api/v1/learning/lessons/{draft_ids['lesson_id']}/assignments", {"title": "t", "instructions": "i"})
    expected[4] = ("post", f"/api/v1/learning/courses/{draft_ids['course_id']}/publish", None)
    expected[5] = ("post", f"/api/v1/learning/courses/{draft_ids['course_id']}/archive", None)

    for method, url, payload in expected:
        response = getattr(http, method)(url, headers=headers, json=payload)
        body = envelope_of(response)
        assert response.status_code == 400, (url, response.text)
        assert body["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED", url


def test_an_empty_idempotency_key_is_refused():
    harness = authoring_harness()

    response = harness.http().post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 400
    assert envelope_of(response)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_exact_replay_of_creation_returns_the_same_answer_without_a_second_effect():
    harness = authoring_harness(store=LearningStore())

    first = create_course(harness, key="ik-create-replay")
    replay = create_course(harness, key="ik-create-replay")

    assert first.status_code == 201 and replay.status_code == 201
    assert replay.json() == first.json()
    assert len(harness.learning.store.courses) == 1
    actions = [event.action for event in harness.learning.store.audit]
    assert actions.count("learning.courses.create") == 2
    assert actions.count("idempotency_replay") == 1


def test_exact_replay_of_every_child_creation_replays_without_a_second_effect():
    harness = authoring_harness(store=LearningStore())
    ids = author_hierarchy(harness, key_prefix="ik-childreplay")

    module_replay = create_module(harness, ids["course_id"], key="ik-childreplay-mod")
    lesson_replay = create_lesson(harness, ids["module_id"], key="ik-childreplay-les")
    assignment_replay = create_assignment(
        harness, ids["lesson_id"], key="ik-childreplay-asg"
    )

    assert module_replay.status_code == 201
    assert module_replay.json()["module_id"] == ids["module_id"]
    assert lesson_replay.status_code == 201
    assert lesson_replay.json()["lesson_id"] == ids["lesson_id"]
    assert assignment_replay.status_code == 201
    assert assignment_replay.json()["assignment_id"] == ids["assignment_id"]
    actions = [event.action for event in harness.learning.store.audit]
    assert actions.count("idempotency_replay") == 3
    store = harness.learning.store
    assert len(store.modules) == 1 and len(store.lessons) == 1 and len(store.assignments) == 1


def test_a_changed_payload_under_the_same_key_is_a_conflict():
    harness = authoring_harness(store=LearningStore())

    first = create_course(harness, key="ik-payload", title="Original")
    conflict = create_course(harness, key="ik-payload", title="Changed")

    assert first.status_code == 201
    assert conflict.status_code == 409
    body = envelope_of(conflict)
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.learning.store.courses) == 1


def test_a_changed_identity_under_the_same_key_is_a_conflict():
    harness = authoring_harness(store=LearningStore())
    # A second teacher of the SAME tenant: the tenant binding is identical,
    # the identity binding is not.
    harness.instance.authorization.store.grant(
        "ten_a", "idn_human_c", *TEACHER_GRANTS
    )

    first = create_course(harness, key="ik-identity", token=TEACHER_A)
    conflict = create_course(
        harness, key="ik-identity", token=STUDENT_C
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.learning.store.courses) == 1


def test_a_changed_tenant_under_the_same_key_is_a_conflict():
    harness = authoring_harness(store=LearningStore())

    first = create_course(harness, key="ik-tenant", token=TEACHER_A)
    conflict = create_course(harness, key="ik-tenant", token=TEACHER_B)

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # Only the tenant A course exists.
    assert [
        course.tenant_id for course in harness.learning.store.courses.values()
    ] == ["ten_a"]


def test_a_changed_target_under_the_same_key_is_a_conflict():
    harness = authoring_harness()
    first_course = create_course(harness, key="ik-target-a").json()
    second_course = create_course(harness, key="ik-target-b").json()

    first = create_module(harness, first_course["course_id"], key="ik-target")
    conflict = create_module(harness, second_course["course_id"], key="ik-target")

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # The second course received no module.
    body = read_course(harness, second_course["course_id"]).json()
    assert body["modules"] == []


def test_a_publish_key_is_not_reusable_for_a_different_operation():
    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-opmix")

    published = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-opmix-x"},
    )
    assert published.status_code == 200
    conflict = harness.http().post(
        f"/api/v1/learning/courses/{ids['course_id']}/archive",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-opmix-x"},
    )

    assert conflict.status_code == 409
    assert envelope_of(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert harness.learning.store.courses[ids["course_id"]].status == "PUBLISHED"


def test_denied_authorization_creates_no_idempotency_record_and_no_effect():
    harness = authoring_harness(store=LearningStore())

    response = create_course(harness, key="ik-denied", token=STUDENT_C)

    assert response.status_code == 403
    assert envelope_of(response)["error"]["details"]["reason"] == "permission_not_granted"
    # No business effect and no record: a DENY never binds a key.
    assert harness.learning.store.courses == {}
    assert "ik-denied" not in harness.learning.engine.idempotency._store


def test_a_failing_identity_dependency_fails_the_command_closed():
    class ExplodingTenantContext:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("identity unavailable")

    from learning_service.adapters import authorization_port
    from learning_service.deployment import build_deployment as build_learning

    harness = authoring_harness(store=LearningStore())
    learning = build_learning(
        {"platform_id": harness.instance.authority.current_platform_id, "environment": "test"},
        authorization=authorization_port(
            harness.instance.authorization.publish(credential=LEARNING_CREDENTIAL_REF)
        ),
        tenant_context=ExplodingTenantContext(),
        store=LearningStore(),
        with_http=True,
    )
    from fastapi.testclient import TestClient as Client

    response = Client(learning.contract_app()).post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-dep-identity",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"]["reason"] == "authorization_unavailable"
    assert learning.store.courses == {}
    assert "ik-dep-identity" not in learning.engine.idempotency._store


def test_a_failing_authorization_dependency_fails_the_command_closed():
    from tests.test_learning_boundary import StubAuthorizationPort, learning_deployment

    deployment = learning_deployment(
        StubAuthorizationPort(RuntimeError("boom")), seed_demo=False
    )
    http = TestClient(deployment.contract_app())

    response = http.post(
        "/api/v1/learning/courses",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-dep-authz",
        },
        json={"title": "t", "description": "d"},
    )

    assert response.status_code == 503
    body = envelope_of(response)
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert deployment.store.courses == {}
    assert "ik-dep-authz" not in deployment.engine.idempotency._store


# ----------------------------------------------------------------- regression
def test_the_submission_flow_keeps_working_alongside_authoring():
    from tests.test_learning_authoring import TEACHER_A as TEACHER  # noqa: F401

    harness = authoring_harness()
    ids = author_hierarchy(harness, key_prefix="ik-regression")
    http = harness.http()
    http.post(
        f"/api/v1/learning/courses/{ids['course_id']}/publish",
        headers={"authorization": f"Bearer {TEACHER_A}", "idempotency-key": "ik-regression-p"},
    )

    review = http.post(
        "/api/v1/learning/submissions/sub_a1_1/review",
        headers={
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": "ik-regression-review",
        },
        json={"decision": "approved"},
    )
    assert review.status_code == 200, review.text

    submission = http.get(
        "/api/v1/learning/submissions/sub_a1_1",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert submission.status_code == 200
    # The declared submission states stay DRAFT/SUBMITTED: a review records
    # reviewed_by / reviewed_at, it introduces no new state.
    assert submission.json()["status"] == "SUBMITTED"
    assert submission.json()["reviewed_by"] == "idn_human_a"

    listing = http.get(
        f"/api/v1/learning/assignments/{ids['assignment_id']}/submissions",
        headers={"authorization": f"Bearer {TEACHER_A}"},
    )
    assert listing.status_code == 200
    assert listing.json()["items"] == []


# --------------------------------------------------------------------- helpers
from learning_service.contracts import (  # noqa: E402
    OPERATION_ASSIGNMENT_CREATE as _A_CREATE,
    OPERATION_COURSE_ARCHIVE as _C_ARCHIVE,
    OPERATION_COURSE_CREATE as _C_CREATE,
    OPERATION_COURSE_PUBLISH as _C_PUBLISH,
    OPERATION_LESSON_CREATE as _L_CREATE,
    OPERATION_MODULE_CREATE as _M_CREATE,
)

#: The six mutation grants of the authoring capability (no reads).
TEACHER_GRANTS = (
    _C_CREATE,
    _M_CREATE,
    _L_CREATE,
    _A_CREATE,
    _C_PUBLISH,
    _C_ARCHIVE,
)
LEARNING_CREDENTIAL_REF = "authz-svc-token-learning"
