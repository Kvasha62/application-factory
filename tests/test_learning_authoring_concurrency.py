"""SCS-001 Slice 4 — Learning Content Authoring (Issue #31): concurrency slice.

Regression tests for the PR #32 review blocker: Course publication (and
archive) must be serialized with child creation even when the two commands
carry DIFFERENT ``Idempotency-Key`` values. IS-005 serializes only a single
key, so two commands under two keys execute concurrently — and without the
Level-0 domain/store critical section a child created between the validation
of a publication and its writes stays ``DRAFT`` under a ``PUBLISHED`` Course,
which Issue #31 forbids.

Every test below runs the two commands on two real threads through the real
IS-005 guard with two distinct keys, and then proves that the final stored
state can never be ``PUBLISHED Course + DRAFT descendant``. The orchestrated
tests park the threads deterministically at the validation seam (a
main-thread conductor with event rendezvous), so they reproduce the exact
review interleaving instead of hoping for a lucky schedule; every wait has a
timeout so a broken implementation fails loudly instead of hanging the suite.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import pytest

from learning_service.errors import AccessRefused
from learning_service.store import LearningStore
from tests.test_learning_authoring import (
    TEACHER_A,
    author_hierarchy,
    authoring_harness,
)

#: How long a thread waits for its orchestrated peer before the test fails
#: loudly instead of hanging the suite.
RENDEZVOUS_TIMEOUT = 10.0
JOIN_TIMEOUT = 30.0
#: The serialization probe: how long the conductor waits for a command that
#: must be blocked by the critical section. An uncontended in-memory write
#: takes microseconds; a blocked command can never finish, so the outcome is
#: deterministic on both sides of the fix.
PROBE_TIMEOUT = 3.0


# ------------------------------------------------------------------ mechanics
def _run_concurrently(
    first: Callable[[], Any], second: Callable[[], Any]
) -> tuple[tuple[str, Any], tuple[str, Any], bool]:
    """Run two commands on two threads behind one start gate.

    Returns the two outcomes — ``("ok", value)``, ``("refused",
    AccessRefused)`` or ``("error", exception)`` — plus whether both threads
    finished before the join timeout (a hang is a failure, never a pass).
    """
    gate = threading.Event()
    outcomes: dict[str, tuple[str, Any]] = {}

    def attempt(name: str, command: Callable[[], Any]) -> None:
        if not gate.wait(timeout=RENDEZVOUS_TIMEOUT):
            outcomes[name] = ("error", AssertionError("start gate never opened"))
            return
        try:
            outcomes[name] = ("ok", command())
        except AccessRefused as exc:
            outcomes[name] = ("refused", exc)
        except Exception as exc:  # pragma: no cover - must never happen
            outcomes[name] = ("error", exc)

    threads = [
        threading.Thread(target=attempt, args=("first", first)),
        threading.Thread(target=attempt, args=("second", second)),
    ]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    finished = all(not thread.is_alive() for thread in threads)
    assert "first" in outcomes and "second" in outcomes, outcomes
    return outcomes["first"], outcomes["second"], finished


def _assert_no_published_course_with_draft_descendant(
    store: LearningStore, course_id: str
) -> None:
    """The concurrency invariant of Issue #31, checked on the stored state."""
    course, modules, lessons, assignments = store.course_hierarchy(course_id)
    if course.status == "PUBLISHED":
        for module in modules:
            assert module.status == "PUBLISHED", (
                f"PUBLISHED course {course_id} with {module.status} module "
                f"{module.module_id}"
            )
        for lesson in lessons:
            assert lesson.status == "PUBLISHED", (
                f"PUBLISHED course {course_id} with {lesson.status} lesson "
                f"{lesson.lesson_id}"
            )
        for assignment in assignments:
            assert assignment.status == "PUBLISHED", (
                f"PUBLISHED course {course_id} with {assignment.status} "
                f"assignment {assignment.assignment_id}"
            )
    if course.status == "ARCHIVED":
        for module in modules:
            assert module.status == "ARCHIVED", (
                f"ARCHIVED course {course_id} with {module.status} module "
                f"{module.module_id}"
            )
        for lesson in lessons:
            assert lesson.status == "ARCHIVED", (
                f"ARCHIVED course {course_id} with {lesson.status} lesson "
                f"{lesson.lesson_id}"
            )
        for assignment in assignments:
            assert assignment.status == "ARCHIVED", (
                f"ARCHIVED course {course_id} with {assignment.status} "
                f"assignment {assignment.assignment_id}"
            )


def _child_command(
    engine: Any, level: str, ids: dict[str, str], *, key: str
) -> Callable[[], Any]:
    """The child-creation engine command of one hierarchy level."""
    if level == "module":
        parent_id = ids["course_id"]
        return lambda: engine.create_module(
            TEACHER_A, parent_id, title="Racer", position=99,
            idempotency_key=key,
        )
    if level == "lesson":
        parent_id = ids["module_id"]
        return lambda: engine.create_lesson(
            TEACHER_A, parent_id, title="Racer", content="race body",
            position=99, idempotency_key=key,
        )
    assert level == "assignment"
    parent_id = ids["lesson_id"]
    return lambda: engine.create_assignment(
        TEACHER_A, parent_id, title="Racer", instructions="race",
        idempotency_key=key,
    )


# ------------------------------------------- deterministic interleaving: publish
@pytest.mark.parametrize("level", ["module", "lesson", "assignment"])
def test_publication_is_serialized_with_concurrent_child_creation(level: str):
    """The exact interleaving of the review, forced deterministically.

    Schedule (driven by the main-thread conductor):

    1. the child command (key B) runs its full engine path and parks at the
       store gate, holding nothing;
    2. the publication (key A — a DIFFERENT key) captures its hierarchy
       snapshot and parks between validation and writes;
    3. the child is released: serialized, it blocks on the critical section
       (the probe observes it cannot finish); unserialized, it lands as a
       ``DRAFT`` record after the snapshot;
    4. the publication is released and writes.

    Without the critical section the final state is ``PUBLISHED`` Course
    with a ``DRAFT`` descendant and this test fails; with it the publication
    wins, the late child is refused against the published parent, and the
    hierarchy is published whole.
    """
    harness = authoring_harness(store=LearningStore())
    ids = author_hierarchy(harness, key_prefix=f"ik-cfix-det-{level}")
    store = harness.learning.store
    engine = harness.learning.engine
    course_id = ids["course_id"]
    create_child = _child_command(engine, level, ids, key="key-B-child")

    # --- the validation seam -------------------------------------------
    # The publication parks after its snapshot; the child parks at the store
    # gate before its effect. Both hooks are armed only while the conductor
    # drives the race; afterwards every call passes straight through.
    original_snapshot = store._hierarchy_of_course
    original_create = {
        "module": store.create_module,
        "lesson": store.create_lesson,
        "assignment": store.create_assignment,
    }[level]
    armed = threading.Event()
    armed.set()
    publication_snapped = threading.Event()
    release_publication = threading.Event()
    child_at_store = threading.Event()
    release_child = threading.Event()
    child_written = threading.Event()

    def hooked_snapshot(course: Any) -> Any:
        result = original_snapshot(course)
        if armed.is_set():
            publication_snapped.set()
            assert release_publication.wait(timeout=RENDEZVOUS_TIMEOUT), (
                "conductor never released the parked publication"
            )
        return result

    def hooked_create(*args: Any, **kwargs: Any) -> Any:
        if armed.is_set():
            child_at_store.set()
            assert release_child.wait(timeout=RENDEZVOUS_TIMEOUT), (
                "conductor never released the parked child"
            )
        created = original_create(*args, **kwargs)
        child_written.set()
        return created

    store._hierarchy_of_course = hooked_snapshot  # type: ignore[method-assign]
    setattr(store, f"create_{level}", hooked_create)

    outcomes: dict[str, tuple[str, Any]] = {}

    def publish_attempt() -> None:
        try:
            outcomes["publish"] = (
                "ok",
                engine.publish_course(
                    TEACHER_A, course_id, idempotency_key="key-A-publish"
                ),
            )
        except AccessRefused as exc:
            outcomes["publish"] = ("refused", exc)
        except Exception as exc:  # pragma: no cover - must never happen
            outcomes["publish"] = ("error", exc)

    def child_attempt() -> None:
        try:
            outcomes["child"] = ("ok", create_child())
        except AccessRefused as exc:
            outcomes["child"] = ("refused", exc)
        except Exception as exc:  # pragma: no cover - must never happen
            outcomes["child"] = ("error", exc)

    child_thread = threading.Thread(target=child_attempt)
    child_thread.start()
    assert child_at_store.wait(timeout=RENDEZVOUS_TIMEOUT), (
        "the child never reached the store gate"
    )
    publish_thread = threading.Thread(target=publish_attempt)
    publish_thread.start()
    assert publication_snapped.wait(timeout=RENDEZVOUS_TIMEOUT), (
        "the publication never reached validation"
    )
    # Both threads are parked: the child holds nothing, the publication holds
    # its snapshot. Release the child and probe: serialized, it cannot finish
    # while the publication is parked mid-critical-section.
    armed.clear()
    release_child.set()
    child_finished_while_parked = child_written.wait(timeout=PROBE_TIMEOUT)
    release_publication.set()
    child_thread.join(timeout=JOIN_TIMEOUT)
    publish_thread.join(timeout=JOIN_TIMEOUT)
    assert not child_thread.is_alive() and not publish_thread.is_alive(), (
        "threads hung"
    )

    assert not child_finished_while_parked, (
        "the child creation finished while the publication was parked "
        "between validation and writes: no serialization"
    )
    publish_outcome, child_outcome = outcomes["publish"], outcomes["child"]
    assert publish_outcome[0] == "ok", publish_outcome
    # The publication serialized first, so the late child meets a published
    # parent and is refused — it must never land as a DRAFT descendant.
    assert child_outcome[0] == "refused", child_outcome
    assert child_outcome[1].reason == "invalid_state_transition"

    stored_course, modules, lessons, assignments = store.course_hierarchy(course_id)
    assert stored_course.status == "PUBLISHED"
    _assert_no_published_course_with_draft_descendant(store, course_id)
    # Nothing was created by the refused race: the hierarchy still holds
    # exactly the sequentially authored records.
    assert [m.module_id for m in modules] == [ids["module_id"]]
    assert [lesson.lesson_id for lesson in lessons] == [ids["lesson_id"]]
    assert [a.assignment_id for a in assignments] == [ids["assignment_id"]]


# ------------------------------------------------- hammer: publish vs. child
@pytest.mark.parametrize("level", ["module", "lesson", "assignment"])
def test_concurrent_publish_and_child_creation_with_different_keys(level: str):
    """Twenty-five real races per level: publication (key A) against child
    creation (key B), both threads released by one gate.

    Either order is a correct serialization — the child is refused against
    the published parent, or it is created first and published together with
    the hierarchy — but ``PUBLISHED Course + DRAFT descendant`` must never
    occur.
    """
    harness = authoring_harness(store=LearningStore())
    engine = harness.learning.engine
    store = harness.learning.store
    for iteration in range(25):
        prefix = f"ik-cfix-ham-{level}-{iteration}"
        ids = author_hierarchy(harness, key_prefix=prefix)
        course_id = ids["course_id"]
        create_child = _child_command(engine, level, ids, key=f"{prefix}-B")

        def publish_now(cid: str = course_id, k: str = f"{prefix}-A") -> Any:
            return engine.publish_course(TEACHER_A, cid, idempotency_key=k)

        publish_outcome, child_outcome, finished = _run_concurrently(
            publish_now, create_child
        )
        assert finished, f"iteration {iteration}: threads hung"
        assert publish_outcome[0] == "ok", (iteration, publish_outcome)
        assert child_outcome[0] in ("ok", "refused"), (iteration, child_outcome)
        if child_outcome[0] == "refused":
            assert child_outcome[1].reason == "invalid_state_transition"
        assert "error" not in (publish_outcome[0], child_outcome[0])
        _assert_no_published_course_with_draft_descendant(store, course_id)
        assert store.courses[course_id].status == "PUBLISHED"


# -------------------------------------------- hammer: archive vs. child create
def test_concurrent_archive_and_child_creation_with_different_keys():
    """Archive (key A) races child creation (key B) on a PUBLISHED course.

    The child is refused whatever the order — its parent is never ``DRAFT``
    — while the archive flips the whole hierarchy ``PUBLISHED → ARCHIVED``
    atomically: no partial archive and no new child may exist afterwards.
    """
    harness = authoring_harness(store=LearningStore())
    engine = harness.learning.engine
    store = harness.learning.store
    for iteration in range(10):
        prefix = f"ik-cfix-arch-{iteration}"
        ids = author_hierarchy(harness, key_prefix=prefix)
        course_id = ids["course_id"]
        engine.publish_course(
            TEACHER_A, course_id, idempotency_key=f"{prefix}-pub"
        )

        def archive_now(cid: str = course_id, k: str = f"{prefix}-A") -> Any:
            return engine.archive_course(TEACHER_A, cid, idempotency_key=k)

        def create_now(cid: str = course_id, k: str = f"{prefix}-B") -> Any:
            return engine.create_module(
                TEACHER_A, cid, title="Racer", position=99, idempotency_key=k
            )

        archive_outcome, child_outcome, finished = _run_concurrently(
            archive_now, create_now
        )
        assert finished, f"iteration {iteration}: threads hung"
        assert archive_outcome[0] == "ok", (iteration, archive_outcome)
        assert child_outcome[0] == "refused", (iteration, child_outcome)
        assert child_outcome[1].reason == "invalid_state_transition"
        stored_course, modules, lessons, assignments = store.course_hierarchy(
            course_id
        )
        assert stored_course.status == "ARCHIVED"
        assert all(m.status == "ARCHIVED" for m in modules)
        assert all(lesson.status == "ARCHIVED" for lesson in lessons)
        assert all(a.status == "ARCHIVED" for a in assignments)
        assert [m.module_id for m in modules] == [ids["module_id"]]
        _assert_no_published_course_with_draft_descendant(store, course_id)


# --------------------------- deterministic serialization: twoCourse commands
def _run_double_course_command_race(
    engine: Any,
    store: LearningStore,
    course_id: str,
    command: Callable[..., Any],
    *,
    key_prefix: str,
) -> tuple[tuple[str, Any], tuple[str, Any], bool]:
    """Race one Course command (publish or archive) against itself under two
    distinct keys, both parked deterministically after their snapshots.

    The first command to validate parks holding its snapshot; the second
    either parks beside it (no serialization — both captured the same
    pre-transition hierarchy) or blocks on the critical section (the probe
    observes it never reaches validation). Returns the two outcomes plus the
    probe result: whether the second command reached validation while the
    first was parked.
    """
    original_snapshot = store._hierarchy_of_course
    armed = threading.Event()
    armed.set()
    parked_count = 0
    park_guard = threading.Lock()
    first_parked = threading.Event()
    second_parked = threading.Event()
    go = threading.Event()

    def hooked_snapshot(course: Any) -> Any:
        nonlocal parked_count
        result = original_snapshot(course)
        with park_guard:
            armed_now = armed.is_set()
            if armed_now:
                parked_count += 1
                call = parked_count
            else:
                call = 0
        if call == 1:
            first_parked.set()
            assert go.wait(timeout=RENDEZVOUS_TIMEOUT), (
                "conductor never released the parked commands"
            )
        elif call == 2:
            second_parked.set()
            assert go.wait(timeout=RENDEZVOUS_TIMEOUT), (
                "conductor never released the parked commands"
            )
        return result

    store._hierarchy_of_course = hooked_snapshot  # type: ignore[method-assign]
    outcomes: dict[str, tuple[str, Any]] = {}

    def attempt(name: str, key: str) -> None:
        try:
            outcomes[name] = (
                "ok", command(TEACHER_A, course_id, idempotency_key=key)
            )
        except AccessRefused as exc:
            outcomes[name] = ("refused", exc)
        except Exception as exc:  # pragma: no cover - must never happen
            outcomes[name] = ("error", exc)

    first_thread = threading.Thread(
        target=attempt, args=("first", f"{key_prefix}-A")
    )
    first_thread.start()
    assert first_parked.wait(timeout=RENDEZVOUS_TIMEOUT), (
        "the first command never reached validation"
    )
    second_thread = threading.Thread(
        target=attempt, args=("second", f"{key_prefix}-B")
    )
    second_thread.start()
    second_reached_validation = second_parked.wait(timeout=PROBE_TIMEOUT)
    armed.clear()
    go.set()
    first_thread.join(timeout=JOIN_TIMEOUT)
    second_thread.join(timeout=JOIN_TIMEOUT)
    assert not first_thread.is_alive() and not second_thread.is_alive(), (
        "threads hung"
    )
    return outcomes["first"], outcomes["second"], second_reached_validation


def test_concurrent_publishes_under_different_keys_serialize_to_one_success():
    """Two publications of one DRAFT course under two keys: exactly one wins.

    Serialized, the second publication blocks until the first completes and
    then meets a ``PUBLISHED`` course; unserialized, both capture the same
    ``DRAFT`` snapshot and both report success. The hierarchy is published
    exactly once, with no ``DRAFT`` descendant left behind.
    """
    harness = authoring_harness(store=LearningStore())
    engine = harness.learning.engine
    store = harness.learning.store
    ids = author_hierarchy(harness, key_prefix="ik-cfix-pp")
    course_id = ids["course_id"]

    first, second, second_reached = _run_double_course_command_race(
        engine, store, course_id, engine.publish_course, key_prefix="ik-cfix-pp"
    )

    assert not second_reached, (
        "the second publication validated while the first was parked "
        "between validation and writes: no serialization"
    )
    results = sorted([first[0], second[0]])
    assert results == ["ok", "refused"], (first, second)
    refused = second if first[0] == "ok" else first
    assert refused[1].reason == "invalid_state_transition"
    _assert_no_published_course_with_draft_descendant(store, course_id)
    assert store.courses[course_id].status == "PUBLISHED"


def test_concurrent_archives_under_different_keys_serialize_to_one_success():
    """Two archives of one PUBLISHED course under two keys: exactly one wins.

    Serialized, the second archive blocks until the first completes and then
    meets an ``ARCHIVED`` course; unserialized, both capture the same
    ``PUBLISHED`` snapshot and both report success. The hierarchy is archived
    exactly once, never partially.
    """
    harness = authoring_harness(store=LearningStore())
    engine = harness.learning.engine
    store = harness.learning.store
    ids = author_hierarchy(harness, key_prefix="ik-cfix-aa")
    course_id = ids["course_id"]
    engine.publish_course(TEACHER_A, course_id, idempotency_key="ik-cfix-aa-pub")

    first, second, second_reached = _run_double_course_command_race(
        engine, store, course_id, engine.archive_course, key_prefix="ik-cfix-aa"
    )

    assert not second_reached, (
        "the second archive validated while the first was parked between "
        "validation and writes: no serialization"
    )
    results = sorted([first[0], second[0]])
    assert results == ["ok", "refused"], (first, second)
    refused = second if first[0] == "ok" else first
    assert refused[1].reason == "invalid_state_transition"
    stored_course, modules, lessons, assignments = store.course_hierarchy(course_id)
    assert stored_course.status == "ARCHIVED"
    assert all(m.status == "ARCHIVED" for m in modules)
    assert all(lesson.status == "ARCHIVED" for lesson in lessons)
    assert all(a.status == "ARCHIVED" for a in assignments)
