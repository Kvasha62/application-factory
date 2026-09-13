"""SCS-003 Booking — the double-booking invariant under concurrency (Issue #57).

ADR-0014 §8 requires the "no active overlapping Reservation" check and the
insert to be atomic per Resource. These tests race REAL threads from one
barrier through the complete HTTP chain (identity → authorization →
idempotency → store) with DIFFERENT ``Idempotency-Key`` values — so
IS-005 cannot collapse the race — and prove that exactly one Reservation
wins each contested interval, the loser receives ``409
reservation_conflict``, and no interleaving ever produces two ACTIVE
overlapping Reservations. Same-key concurrency is proven separately: one
effect, identical replies. Cancellation racing a booking of the freed
interval is serialized in the same section. No sleeps: the critical
section is exclusive, so a blocked worker is observable deterministically.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from booking_service.intervals import overlaps
from booking_service.store import BookingStore
from tests.test_booking_skeleton import (
    SUBJECT_A,
    SUBJECT_C,
    bookable_resource,
    booking_harness,
    cancel_reservation,
    create_reservation,
    envelope_of,
)

JOIN_TIMEOUT = 30.0
ROUNDS = 25

SLOT = ("2026-10-01T10:00:00+00:00", "2026-10-01T11:00:00+00:00")
OVERLAPPING = ("2026-10-01T10:30:00+00:00", "2026-10-01T11:30:00+00:00")
SAME_INSTANT_OTHER_OFFSET = ("2026-10-01T12:00:00+02:00", "2026-10-01T13:00:00+02:00")


def race(attempts: list[Callable[[], Any]]) -> list[Any]:
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
        thread.join(JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), "a racer hung"
    return results


def assert_no_active_overlap(store: BookingStore, resource_id: str) -> None:
    active = store.active_reservations_of(resource_id)
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            assert not overlaps(left, right), (left, right)


def reservation_attempt(harness, resource_id: str, key: str, token: str, slot):
    http = harness.http()  # one client per racer: no shared transport state
    return lambda: create_reservation(
        http, resource_id, key, token=token, start_at=slot[0], end_at=slot[1]
    )


# ------------------------------------------------------- different keys
def test_two_racers_for_one_interval_yield_exactly_one_reservation():
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    for round_no in range(ROUNDS):
        resource_id = bookable_resource(harness, f"race-{round_no}")
        results = race(
            [
                reservation_attempt(
                    harness, resource_id, f"r{round_no}-a", SUBJECT_A, SLOT
                ),
                reservation_attempt(
                    harness, resource_id, f"r{round_no}-c", SUBJECT_C, SLOT
                ),
            ]
        )
        statuses = sorted(r.status_code for r in results)
        assert statuses == [201, 409], [(r.status_code, r.text) for r in results]
        loser = next(r for r in results if r.status_code == 409)
        body = envelope_of(loser)
        assert body["error"]["code"] == "RESERVATION_CONFLICT"
        assert body["error"]["details"]["reason"] == "reservation_conflict"
        assert len(store.active_reservations_of(resource_id)) == 1
        assert (
            len(
                [r for r in store.reservations.values() if r.resource_id == resource_id]
            )
            == 1
        )
        assert_no_active_overlap(store, resource_id)


def test_many_racers_with_overlapping_and_offset_spellings():
    """Six racers, three distinct spellings of overlapping intervals: one winner."""
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    for round_no in range(ROUNDS // 5):
        resource_id = bookable_resource(harness, f"many-{round_no}")
        attempts = []
        for index, (token, slot) in enumerate(
            [
                (SUBJECT_A, SLOT),
                (SUBJECT_C, SLOT),
                (SUBJECT_A, OVERLAPPING),
                (SUBJECT_C, OVERLAPPING),
                (SUBJECT_A, SAME_INSTANT_OTHER_OFFSET),
                (SUBJECT_C, SAME_INSTANT_OTHER_OFFSET),
            ]
        ):
            attempts.append(
                reservation_attempt(
                    harness, resource_id, f"m{round_no}-{index}", token, slot
                )
            )
        results = race(attempts)
        codes = sorted(r.status_code for r in results)
        assert codes == [201, 409, 409, 409, 409, 409], codes
        assert all(
            envelope_of(r)["error"]["details"]["reason"] == "reservation_conflict"
            for r in results
            if r.status_code == 409
        )
        assert len(store.active_reservations_of(resource_id)) == 1
        assert_no_active_overlap(store, resource_id)


def test_adjacent_racers_both_win():
    """Adjacency is not a conflict even under contention: [10,11) and [11,12)."""
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    next_slot = ("2026-10-01T11:00:00+00:00", "2026-10-01T12:00:00+00:00")
    for round_no in range(ROUNDS // 5):
        resource_id = bookable_resource(harness, f"adj-{round_no}")
        results = race(
            [
                reservation_attempt(
                    harness, resource_id, f"adj{round_no}-a", SUBJECT_A, SLOT
                ),
                reservation_attempt(
                    harness, resource_id, f"adj{round_no}-c", SUBJECT_C, next_slot
                ),
            ]
        )
        assert [r.status_code for r in results] == [201, 201]
        assert len(store.active_reservations_of(resource_id)) == 2
        assert_no_active_overlap(store, resource_id)


def test_racers_on_different_resources_do_not_block_each_other_into_errors():
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    first = bookable_resource(harness, "iso-1")
    second = bookable_resource(harness, "iso-2")
    results = race(
        [
            reservation_attempt(harness, first, "iso-a", SUBJECT_A, SLOT),
            reservation_attempt(harness, second, "iso-c", SUBJECT_C, SLOT),
        ]
    )
    assert [r.status_code for r in results] == [201, 201]
    assert len(store.active_reservations_of(first)) == 1
    assert len(store.active_reservations_of(second)) == 1


# ------------------------------------------------------------ same key
def test_same_key_racers_produce_one_effect_and_identical_replies():
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    for round_no in range(ROUNDS // 5):
        resource_id = bookable_resource(harness, f"key-{round_no}")
        key = f"same-key-{round_no}"
        results = race(
            [
                reservation_attempt(harness, resource_id, key, SUBJECT_A, SLOT)
                for _ in range(4)
            ]
        )
        assert [r.status_code for r in results] == [201, 201, 201, 201], [
            (r.status_code, r.text) for r in results
        ]
        bodies = [r.json() for r in results]
        assert all(body == bodies[0] for body in bodies)
        assert len(store.active_reservations_of(resource_id)) == 1
        replays = [
            e
            for e in store.audit
            if e.action == "idempotency_replay" and e.details.get("key") == key
        ]
        assert len(replays) == 3


# ------------------------------------------------------- cancel vs. book
def test_cancel_and_rebook_of_the_same_interval_are_serialized():
    """Whatever the order, the end state has at most one ACTIVE Reservation."""
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    outcomes = set()
    for round_no in range(ROUNDS // 5):
        resource_id = bookable_resource(harness, f"cxb-{round_no}")
        http = harness.http()
        created = create_reservation(
            http, resource_id, f"cxb{round_no}-0", start_at=SLOT[0], end_at=SLOT[1]
        )
        assert created.status_code == 201
        reservation_id = created.json()["reservation_id"]
        cancel_http = harness.http()

        def cancel_attempt(
            http=cancel_http, rid=reservation_id, key=f"cxb{round_no}-1"
        ):
            return cancel_reservation(http, rid, key)

        results = race(
            [
                cancel_attempt,
                reservation_attempt(
                    harness, resource_id, f"cxb{round_no}-2", SUBJECT_C, SLOT
                ),
            ]
        )
        cancel, rebook = results
        assert cancel.status_code == 200
        assert rebook.status_code in (201, 409), rebook.text
        outcomes.add(rebook.status_code)
        active = store.active_reservations_of(resource_id)
        assert len(active) <= 1
        assert store.reservations[reservation_id].status == "CANCELLED"
        if rebook.status_code == 201:
            assert active[0].booker_identity_id == "idn_human_c"
        else:
            assert envelope_of(rebook)["error"]["details"]["reason"] == (
                "reservation_conflict"
            )
            assert active == []
        assert_no_active_overlap(store, resource_id)
    # Both orders are legitimate; neither may leave two ACTIVE records.
    assert outcomes <= {201, 409}


# ------------------------------------------------ the section is exclusive
def test_reservation_waits_outside_a_held_resource_section():
    """The whole check-and-insert runs inside the Resource's critical section."""
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    resource_id = bookable_resource(harness, "held")
    results: list[Any] = []
    attempt = reservation_attempt(harness, resource_id, "held-1", SUBJECT_A, SLOT)

    with store.resource_section(resource_id):
        worker = threading.Thread(target=lambda: results.append(attempt()), daemon=True)
        worker.start()
        worker.join(0.5)
        # Exclusive section: the command cannot have completed, failed or
        # written anything while another thread holds the section.
        assert worker.is_alive(), "reservation completed while the section was held"
        assert results == []
        assert store.active_reservations_of(resource_id) == []
    worker.join(JOIN_TIMEOUT)
    assert not worker.is_alive(), "reservation never completed after the release"
    assert results[0].status_code == 201
    assert len(store.active_reservations_of(resource_id)) == 1


def test_cancellation_waits_outside_a_held_resource_section():
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    resource_id = bookable_resource(harness, "held-cancel")
    http = harness.http()
    reservation_id = create_reservation(
        http, resource_id, "hc-0", start_at=SLOT[0], end_at=SLOT[1]
    ).json()["reservation_id"]
    results: list[Any] = []

    with store.resource_section(resource_id):
        worker = threading.Thread(
            target=lambda: results.append(
                cancel_reservation(http, reservation_id, "hc-1")
            ),
            daemon=True,
        )
        worker.start()
        worker.join(0.5)
        assert worker.is_alive(), "cancel completed while the section was held"
        assert store.reservations[reservation_id].status == "ACTIVE"
    worker.join(JOIN_TIMEOUT)
    assert not worker.is_alive()
    assert results[0].status_code == 200
    assert store.reservations[reservation_id].status == "CANCELLED"


def test_read_paths_are_not_blocked_by_the_section():
    """Reads never enter the write section: they must complete while it is held."""
    harness = booking_harness(store=BookingStore())
    store = harness.booking.store
    resource_id = bookable_resource(harness, "held-read")
    http = harness.http()
    results: list[Any] = []
    with store.resource_section(resource_id):
        worker = threading.Thread(
            target=lambda: results.append(
                http.get(
                    f"/api/v1/booking/resources/{resource_id}",
                    headers={"authorization": f"Bearer {SUBJECT_A}"},
                )
            ),
            daemon=True,
        )
        worker.start()
        worker.join(JOIN_TIMEOUT)
        assert not worker.is_alive(), "a read blocked on the write section"
    assert results[0].status_code == 200
