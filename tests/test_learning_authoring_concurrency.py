"""SCS-001 Slice 4 — regression: the hierarchy critical section (Issue #31).

Course publication and archive are serialized against child creation by the
Level-0 domain critical section of the store: one Course hierarchy, one
section. These tests race REAL threads with DIFFERENT ``Idempotency-Key``
values — the interleaving identified in the PR #32 review: a child creation
with key B must never be able to wedge between a publication's validation
(key A) and its writes, i.e. the state ``PUBLISHED Course + DRAFT
descendant`` is unreachable, and the same holds for archive.

IS-005 is untouched and keeps its exactly-once semantics per key (replay,
conflict, binding and same-key concurrency are covered in
``test_learning_authoring_lifecycle.py``); the critical section is domain
serialization of the business operation across different keys.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from tests.test_learning_authoring import (
    TEACHER_A,
    authoring_harness,
    create_course,
    create_lesson,
    create_module,
)
from tests.test_learning_slice1 import envelope_of

PUBLISH_TIMEOUT = 30.0


def publish(harness, course_id: str, key: str, token: str = TEACHER_A) -> Any:
    return harness.http().post(
        f"/api/v1/learning/courses/{course_id}/publish",
        headers={"authorization": f"Bearer {token}", "idempotency-key": key},
    )


def archive(harness, course_id: str, key: str, token: str = TEACHER_A) -> Any:
    return harness.http().post(
        f"/api/v1/learning/courses/{course_id}/archive",
        headers={"authorization": f"Bearer {token}", "idempotency-key": key},
    )


def hierarchy_statuses(store, course_id: str) -> list[str]:
    """The status of the Course and of every anchored descendant."""
    course = store.courses[course_id]
    statuses = [course.status]
    module_ids: set[str] = set()
    lesson_ids: set[str] = set()
    for module in store.modules.values():
        if module.course_id == course_id:
            statuses.append(module.status)
            module_ids.add(module.module_id)
    for lesson in store.lessons.values():
        if lesson.module_id in module_ids:
            statuses.append(lesson.status)
            lesson_ids.add(lesson.lesson_id)
    for assignment in store.assignments.values():
        if assignment.lesson_id in lesson_ids:
            statuses.append(assignment.status)
    return statuses


def assert_uniform_hierarchy(store, course_id: str) -> None:
    """The forbidden state, negatively: no mixed-status tree exists.

    Publication and archive flip the whole hierarchy together, and a child
    is created only in DRAFT under a DRAFT parent — so every record of one
    hierarchy always carries the Course's own status. A mixed tree
    (``PUBLISHED`` Course + ``DRAFT`` descendant, or an archived tree with a
    leftover ``PUBLISHED``/``DRAFT`` record) is exactly the defect.
    """
    statuses = hierarchy_statuses(store, course_id)
    assert set(statuses) == {
        statuses[0]
    }, f"hierarchy of {course_id} is mixed: {statuses}"


def race(harness, attempts: list[Callable[[], Any]]) -> list[Any]:
    """Run the attempt callables concurrently from one barrier."""
    barrier = threading.Barrier(len(attempts))
    results: list[Any] = [None] * len(attempts)

    def attempt(index: int, fn: Callable[[], Any]) -> None:
        barrier.wait()
        results[index] = fn()

    threads = [
        threading.Thread(target=attempt, args=(index, fn), daemon=True)
        for index, fn in enumerate(attempts)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(PUBLISH_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), "a racer hung"
    return results


def blocked_while_section_held(harness, course_id: str, attempt: Callable[[], Any]):
    """Start ``attempt`` while the critical section is held by this thread.

    Returns the worker thread and its result slot. The worker must still be
    blocked when the section is released — deterministically, because the
    section is exclusive: the worker cannot complete, fail or write
    anything until the holder leaves the section.
    """
    store = harness.learning.store
    results: list[Any] = []

    with store.hierarchy_section(course_id):
        worker = threading.Thread(target=lambda: results.append(attempt()), daemon=True)
        worker.start()
        worker.join(0.5)
        # The section is exclusive: the attempt cannot have finished — it is
        # waiting to enter the critical sequence of its command.
        assert worker.is_alive(), (
            "the command completed while another thread held the "
            "Course's critical section"
        )
        yield worker, results
    worker.join(PUBLISH_TIMEOUT)
    assert not worker.is_alive(), "the command never completed after the release"


# ------------------------------------------------------- the required regression
def test_concurrent_publication_and_module_creation_cannot_mix_states():
    """Publication (key A) races child creation (key B), A != B.

    Whatever the interleaving, the tree is never ``PUBLISHED`` with a
    ``DRAFT`` descendant: the module is either refused (publication won the
    critical section) or created and published together with the Course
    (creation won it).
    """
    harness = authoring_harness()
    http = TestClient(harness.learning.contract_app())
    store = harness.learning.store

    for i in range(40):
        course = create_course(harness, key=f"ik-mix-crs-{i}").json()
        course_id = course["course_id"]
        create_module(harness, course_id, key=f"ik-mix-m1-{i}")

        publish_response, child_response = race(
            harness,
            [
                lambda course_id=course_id, i=i: http.post(
                    f"/api/v1/learning/courses/{course_id}/publish",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-mix-A-{i}",
                    },
                ),
                lambda course_id=course_id, i=i: http.post(
                    f"/api/v1/learning/courses/{course_id}/modules",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-mix-B-{i}",
                    },
                    json={"title": f"Racer {i}", "position": 2},
                ),
            ],
        )

        # The Course was a valid DRAFT tree: publication always succeeds.
        assert publish_response.status_code == 200, (i, publish_response.text)
        assert store.courses[course_id].status == "PUBLISHED"

        # THE forbidden state, negatively: no DRAFT module under the
        # PUBLISHED course, whatever key ordering won.
        drafts = [
            module
            for module in store.modules.values()
            if module.course_id == course_id and module.status != "PUBLISHED"
        ]
        assert drafts == [], (i, publish_response.text, child_response.text)

        # The racing child has exactly two possible outcomes: refused
        # (creation lost the section) or created (then publication, which
        # could only run after it, flipped it together with the tree). The
        # 201 view is the creation effect's result and may therefore still
        # say DRAFT — the store, asserted uniform below, is the truth.
        if child_response.status_code != 201:
            assert child_response.status_code == 409, (i, child_response.text)
            body = envelope_of(child_response)
            assert body["error"]["code"] == "INVALID_STATE_TRANSITION"

        assert_uniform_hierarchy(store, course_id)


def test_concurrent_publication_and_lesson_creation_cannot_mix_states():
    harness = authoring_harness()
    http = TestClient(harness.learning.contract_app())
    store = harness.learning.store

    for i in range(25):
        course = create_course(harness, key=f"ik-lesrace-crs-{i}").json()
        module = create_module(
            harness, course["course_id"], key=f"ik-lesrace-mod-{i}"
        ).json()

        _, lesson_response = race(
            harness,
            [
                lambda course=course, i=i: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/publish",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-lesrace-A-{i}",
                    },
                ),
                lambda course=course, module=module, i=i: http.post(
                    f"/api/v1/learning/modules/{module['module_id']}/lessons",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-lesrace-B-{i}",
                    },
                    json={"title": "Racer", "content": "c", "position": 1},
                ),
            ],
        )

        assert store.courses[course["course_id"]].status == "PUBLISHED"
        if lesson_response.status_code != 201:
            assert lesson_response.status_code == 409, (i, lesson_response.text)
            assert (
                envelope_of(lesson_response)["error"]["code"]
                == "INVALID_STATE_TRANSITION"
            )
        assert_uniform_hierarchy(store, course["course_id"])


def test_concurrent_publication_and_assignment_creation_cannot_mix_states():
    harness = authoring_harness()
    http = TestClient(harness.learning.contract_app())
    store = harness.learning.store

    for i in range(25):
        course = create_course(harness, key=f"ik-asgrace-crs-{i}").json()
        module = create_module(
            harness, course["course_id"], key=f"ik-asgrace-mod-{i}"
        ).json()
        lesson = create_lesson(
            harness, module["module_id"], key=f"ik-asgrace-les-{i}"
        ).json()

        _, assignment_response = race(
            harness,
            [
                lambda course=course, i=i: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/publish",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-asgrace-A-{i}",
                    },
                ),
                lambda course=course, lesson=lesson, i=i: http.post(
                    f"/api/v1/learning/lessons/{lesson['lesson_id']}/assignments",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-asgrace-B-{i}",
                    },
                    json={"title": "Racer", "instructions": "i"},
                ),
            ],
        )

        assert store.courses[course["course_id"]].status == "PUBLISHED"
        if assignment_response.status_code != 201:
            assert assignment_response.status_code == 409, (i, assignment_response.text)
            assert (
                envelope_of(assignment_response)["error"]["code"]
                == "INVALID_STATE_TRANSITION"
            )
        assert_uniform_hierarchy(store, course["course_id"])


# ------------------------------------------------ archive against mutations
def test_concurrent_archive_and_child_creation_stay_serialized():
    """Archive (key C) races child creation (key B) on a PUBLISHED course.

    The child can only be refused (a published hierarchy is immutable), and
    the archive flips the whole tree — never an ``ARCHIVED`` Course with a
    leftover ``DRAFT`` descendant.
    """
    harness = authoring_harness()
    http = TestClient(harness.learning.contract_app())
    store = harness.learning.store

    for i in range(25):
        course = create_course(harness, key=f"ik-arcrace-crs-{i}").json()
        create_module(harness, course["course_id"], key=f"ik-arcrace-mod-{i}")
        published = publish(harness, course["course_id"], key=f"ik-arcrace-P-{i}")
        assert published.status_code == 200

        archive_response, child_response = race(
            harness,
            [
                lambda course=course, i=i: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/archive",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-arcrace-C-{i}",
                    },
                ),
                lambda course=course, i=i: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/modules",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-arcrace-B-{i}",
                    },
                    json={"title": "Racer", "position": 2},
                ),
            ],
        )

        assert store.courses[course["course_id"]].status == "ARCHIVED", i
        assert archive_response.status_code == 200, (i, archive_response.text)
        # A published hierarchy is immutable in every interleaving.
        assert child_response.status_code == 409, (i, child_response.text)
        assert (
            envelope_of(child_response)["error"]["code"] == "INVALID_STATE_TRANSITION"
        )
        assert_uniform_hierarchy(store, course["course_id"])


def test_concurrent_publication_and_archive_stay_serialized():
    """Publication (key A) races archive (key C) on a DRAFT course.

    The commands serialize: either publication wins and the archive then
    succeeds (``ARCHIVED`` tree), or the archive is refused first and the
    publication then succeeds (``PUBLISHED`` tree). No mixed or revived
    state exists.
    """
    harness = authoring_harness()
    http = TestClient(harness.learning.contract_app())
    store = harness.learning.store

    for i in range(25):
        course = create_course(harness, key=f"ik-parcrace-crs-{i}").json()
        create_module(harness, course["course_id"], key=f"ik-parcrace-mod-{i}")

        publish_response, archive_response = race(
            harness,
            [
                lambda course=course, i=i: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/publish",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-parcrace-A-{i}",
                    },
                ),
                lambda course=course, i=i: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/archive",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-parcrace-C-{i}",
                    },
                ),
            ],
        )

        assert publish_response.status_code == 200, (i, publish_response.text)
        final_status = store.courses[course["course_id"]].status
        assert final_status in {"PUBLISHED", "ARCHIVED"}, (i, final_status)
        # The two commands happened in a strict order: archive succeeded
        # exactly when publication's tree is already archived.
        if archive_response.status_code == 200:
            assert final_status == "ARCHIVED", (i, final_status)
        else:
            assert archive_response.status_code == 409, (i, archive_response.text)
            assert final_status == "PUBLISHED", (i, final_status)
        assert_uniform_hierarchy(store, course["course_id"])


# ------------------------------------------------- deterministic exclusivity
def test_child_creation_waits_outside_the_publication_critical_section():
    """While the critical section is held, a child creation is excluded.

    Deterministic: the holder keeps the section; the racing creation must
    not complete, must not write anything, and must complete right after
    the release — into a consistent outcome.
    """
    harness = authoring_harness()
    course = create_course(harness, key="ik-det-child-crs").json()
    store = harness.learning.store

    for worker, results in blocked_while_section_held(
        harness,
        course["course_id"],
        lambda: create_module(
            harness, course["course_id"], key="ik-det-child-B", title="Blocked"
        ),
    ):
        # Nothing was written while the section was held.
        assert [
            module
            for module in store.modules.values()
            if module.course_id == course["course_id"]
        ] == []

    response = results[0]
    assert response.status_code == 201, response.text
    # The creation completed after the release, against the still-DRAFT course.
    assert store.courses[course["course_id"]].status == "DRAFT"
    assert response.json()["status"] == "DRAFT"
    assert_uniform_hierarchy(store, course["course_id"])


def test_publication_serializes_validation_and_writes_against_child_creation():
    """Publication and child creation serialize inside one critical section.

    Both are started while the section is held: neither can complete. After
    the release they run one after the other — so no child creation can
    wedge between the publication's validation and its writes in any order.
    """
    harness = authoring_harness()
    course = create_course(harness, key="ik-det-pub-crs").json()
    create_module(harness, course["course_id"], key="ik-det-pub-m1")
    store = harness.learning.store
    http = TestClient(harness.learning.contract_app())
    results: list[Any] = [None, None]  # slots: [publication, child creation]

    with store.hierarchy_section(course["course_id"]):
        publisher = threading.Thread(
            target=lambda: results.__setitem__(
                0,
                http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/publish",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": "ik-det-pub-A",
                    },
                ),
            ),
            daemon=True,
        )
        child = threading.Thread(
            target=lambda: results.__setitem__(
                1,
                http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/modules",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": "ik-det-pub-B",
                    },
                    json={"title": "Racer", "position": 2},
                ),
            ),
            daemon=True,
        )
        publisher.start()
        child.start()
        publisher.join(0.5)
        child.join(0.5)
        # Both are excluded by the held section.
        assert publisher.is_alive() and child.is_alive()
        # And neither has produced a state change yet.
        assert store.courses[course["course_id"]].status == "DRAFT"

    publisher.join(PUBLISH_TIMEOUT)
    child.join(PUBLISH_TIMEOUT)
    assert not publisher.is_alive() and not child.is_alive()
    assert len(results) == 2

    publish_response, child_response = results
    assert publish_response.status_code == 200, publish_response.text
    assert store.courses[course["course_id"]].status == "PUBLISHED"
    # The forbidden state does not exist in either serialization order.
    drafts = [
        module
        for module in store.modules.values()
        if module.course_id == course["course_id"] and module.status != "PUBLISHED"
    ]
    assert drafts == [], (publish_response.text, child_response.text)
    if child_response.status_code != 201:
        assert child_response.status_code == 409, child_response.text
        assert (
            envelope_of(child_response)["error"]["code"] == "INVALID_STATE_TRANSITION"
        )
    assert_uniform_hierarchy(store, course["course_id"])


def test_archive_holds_the_section_against_child_creation():
    harness = authoring_harness()
    course = create_course(harness, key="ik-det-arc-crs").json()
    create_module(harness, course["course_id"], key="ik-det-arc-mod")
    published = publish(harness, course["course_id"], key="ik-det-arc-P")
    assert published.status_code == 200
    store = harness.learning.store

    for worker, results in blocked_while_section_held(
        harness,
        course["course_id"],
        lambda: create_module(
            harness, course["course_id"], key="ik-det-arc-B", title="Blocked"
        ),
    ):
        # The archive has not run yet: the tree is intact and PUBLISHED.
        assert store.courses[course["course_id"]].status == "PUBLISHED"

    # The section holder here never archives; the child completes after the
    # release and is refused — a published hierarchy is immutable.
    response = results[0]
    assert response.status_code == 409, response.text
    assert store.courses[course["course_id"]].status == "PUBLISHED"
    assert_uniform_hierarchy(store, course["course_id"])


def test_publication_records_the_hierarchy_it_transitioned():
    """The recorded publication result is the tree as publication transitioned it.

    The critical section spans the transition AND the recorded snapshot, so
    an archive racing in between cannot leak into the recorded result: the
    served publication answer (and every exact replay of it) always shows
    the ``PUBLISHED`` hierarchy — never the state a later archive produced.
    """
    harness = authoring_harness()
    http = TestClient(harness.learning.contract_app())
    store = harness.learning.store

    for i in range(20):
        course = create_course(harness, key=f"ik-snap-crs-{i}").json()
        create_module(harness, course["course_id"], key=f"ik-snap-mod-{i}")
        headers = {
            "authorization": f"Bearer {TEACHER_A}",
            "idempotency-key": f"ik-snap-A-{i}",
        }

        publish_response, _archive_response = race(
            harness,
            [
                lambda course=course, headers=headers: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/publish",
                    headers=headers,
                ),
                lambda course=course, i=i: http.post(
                    f"/api/v1/learning/courses/{course['course_id']}/archive",
                    headers={
                        "authorization": f"Bearer {TEACHER_A}",
                        "idempotency-key": f"ik-snap-C-{i}",
                    },
                ),
            ],
        )

        assert publish_response.status_code == 200, (i, publish_response.text)
        # The publication answer is the PUBLISHED tree as transitioned.
        assert publish_response.json()["status"] == "PUBLISHED"
        assert all(
            module["status"] == "PUBLISHED"
            for module in publish_response.json()["modules"]
        )

        # An exact replay after whatever archive won the race later still
        # returns exactly the recorded publication result.
        replay = http.post(
            f"/api/v1/learning/courses/{course['course_id']}/publish",
            headers=headers,
        )
        assert replay.status_code == 200, (i, replay.text)
        assert replay.json() == publish_response.json()

        assert_uniform_hierarchy(store, course["course_id"])
