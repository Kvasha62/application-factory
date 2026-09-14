"""SCS-003 Booking — Availability and Reservation through the enforcement chain.

Public behaviour of the five interval operations against the composed
Platform Instance: declare/list Availability Windows, create/read/cancel
Reservations; the five creation conditions of ADR-0014 §8 with their
deterministic refusals; timezone-aware input and UTC responses; half-open
adjacency; the ``ACTIVE → CANCELLED`` lifecycle; tenant isolation;
authorization (grant required, ``booker_mismatch`` for another booker of
the same Tenant); IS-005 replay/conflict/binding; and audit coverage of
served accesses and refusals.
"""

from __future__ import annotations

import pytest

from booking_service.contracts import (
    OPERATION_AVAILABILITY_CREATE,
    OPERATION_RESERVATION_CANCEL,
    OPERATION_RESERVATION_CREATE,
)
from booking_service.errors import AccessRefused
from booking_service.store import BookingStore
from tests.test_booking_skeleton import (
    BASE,
    SUBJECT_A,
    SUBJECT_B,
    SUBJECT_C,
    WINDOW_END,
    WINDOW_START,
    auth,
    bookable_resource,
    booking_harness,
    cancel_reservation,
    create_availability,
    create_reservation,
    create_resource,
    envelope_of,
)

SLOT = ("2026-10-01T10:00:00+00:00", "2026-10-01T11:00:00+00:00")
NEXT_SLOT = ("2026-10-01T11:00:00+00:00", "2026-10-01T12:00:00+00:00")


def reserve(http, resource_id, key, **kw):
    start, end = kw.pop("slot", SLOT)
    return create_reservation(http, resource_id, key, start_at=start, end_at=end, **kw)


# ------------------------------------------------------------ availability
def test_declare_then_list_availability_in_utc_order():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = create_resource(http, "avl-res").json()["resource_id"]

    later = create_availability(
        http,
        resource_id,
        "avl-2",
        start_at="2026-10-02T09:00:00+02:00",
        end_at="2026-10-02T17:00:00+02:00",
    )
    assert later.status_code == 201, later.text
    earlier = create_availability(http, resource_id, "avl-1")
    assert earlier.status_code == 201
    body = later.json()
    assert set(body) == {
        "availability_id",
        "resource_id",
        "start_at",
        "end_at",
        "created_at",
    }
    # Persisted and served in UTC, not in the offset the caller used.
    assert body["start_at"] == "2026-10-02T07:00:00+00:00"
    assert body["end_at"] == "2026-10-02T15:00:00+00:00"

    listed = http.get(
        f"{BASE}/resources/{resource_id}/availability", headers=auth(SUBJECT_A)
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [i["availability_id"] for i in items] == [
        earlier.json()["availability_id"],
        body["availability_id"],
    ]
    assert listed.json()["resource_id"] == resource_id


def test_availability_of_a_resource_without_windows_is_an_empty_list():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = create_resource(http, "avl-empty").json()["resource_id"]
    listed = http.get(
        f"{BASE}/resources/{resource_id}/availability", headers=auth(SUBJECT_A)
    )
    assert listed.status_code == 200
    assert listed.json() == {"resource_id": resource_id, "items": []}


@pytest.mark.parametrize(
    ("start_at", "end_at"),
    [
        ("2026-10-01T08:00:00", "2026-10-01T18:00:00"),  # naive
        ("2026-10-01T08:00:00Z", "2026-10-01T18:00:00"),  # naive end
        ("2026-10-01T18:00:00Z", "2026-10-01T08:00:00Z"),  # inverted
        ("2026-10-01T08:00:00Z", "2026-10-01T08:00:00Z"),  # empty
        ("yesterday", "2026-10-01T08:00:00Z"),  # unreadable
    ],
)
def test_invalid_window_is_a_validation_error_and_nothing_is_persisted(
    start_at, end_at
):
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = create_resource(http, "avl-bad-res").json()["resource_id"]
    response = create_availability(
        http, resource_id, "avl-bad", start_at=start_at, end_at=end_at
    )
    assert response.status_code == 422, response.text
    body = envelope_of(response)
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "validation_error"
    assert harness.booking.store.availability == {}


def test_availability_replay_and_conflict():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = create_resource(http, "avl-idem-res").json()["resource_id"]
    first = create_availability(http, resource_id, "avl-idem")
    replay = create_availability(http, resource_id, "avl-idem")
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert len(harness.booking.store.availability) == 1
    # Same key, different window: refused by IS-005.
    changed = create_availability(
        http,
        resource_id,
        "avl-idem",
        start_at="2026-10-03T08:00:00Z",
        end_at="2026-10-03T18:00:00Z",
    )
    assert changed.status_code == 409
    assert envelope_of(changed)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.booking.store.availability) == 1


def test_availability_replay_is_the_instant_not_the_offset_spelling():
    """The command identity is the UTC-normalized interval."""
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = create_resource(http, "avl-off-res").json()["resource_id"]
    first = create_availability(
        http,
        resource_id,
        "avl-off",
        start_at="2026-10-01T10:00:00+02:00",
        end_at="2026-10-01T20:00:00+02:00",
    )
    same_instant = create_availability(
        http,
        resource_id,
        "avl-off",
        start_at="2026-10-01T08:00:00Z",
        end_at="2026-10-01T18:00:00Z",
    )
    assert first.status_code == 201
    assert same_instant.status_code == 201
    assert same_instant.json() == first.json()
    assert len(harness.booking.store.availability) == 1


def test_availability_of_foreign_or_unknown_resource():
    harness = booking_harness()
    http = harness.http()
    foreign = create_availability(http, "res_b1", "avl-foreign")
    assert foreign.status_code == 403
    assert envelope_of(foreign)["error"]["code"] == "AUTHORIZATION_DENIED"
    unknown = create_availability(http, "res_missing", "avl-unknown")
    assert unknown.status_code == 404
    assert envelope_of(unknown)["error"]["details"]["reason"] == "resource_not_found"
    listed = http.get(f"{BASE}/resources/res_b1/availability", headers=auth(SUBJECT_A))
    assert listed.status_code == 403


# ------------------------------------------------------------- reservation
def test_create_read_reservation_with_utc_response_and_opaque_booker():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-ok")
    created = reserve(
        http,
        resource_id,
        "rsv-ok-1",
        slot=("2026-10-01T12:00:00+02:00", "2026-10-01T13:00:00+02:00"),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {
        "reservation_id",
        "resource_id",
        "booker_identity_id",
        "start_at",
        "end_at",
        "status",
        "created_at",
        "cancelled_at",
    }
    assert body["booker_identity_id"] == "idn_human_a"
    assert body["status"] == "ACTIVE"
    assert body["cancelled_at"] is None
    assert body["start_at"] == "2026-10-01T10:00:00+00:00"
    assert body["end_at"] == "2026-10-01T11:00:00+00:00"
    assert "tenant_id" not in body

    read = http.get(
        f"{BASE}/reservations/{body['reservation_id']}", headers=auth(SUBJECT_A)
    )
    assert read.status_code == 200
    assert read.json() == body
    stored = harness.booking.store.reservations[body["reservation_id"]]
    assert stored.tenant_id == "ten_a"


def test_booker_cannot_be_supplied_in_the_payload():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-booker")
    response = http.post(
        f"{BASE}/reservations",
        headers=auth(SUBJECT_A, "rsv-booker-1"),
        json={
            "resource_id": resource_id,
            "start_at": SLOT[0],
            "end_at": SLOT[1],
            "booker_identity_id": "idn_human_b",
        },
    )
    assert response.status_code == 422
    assert envelope_of(response)["error"]["code"] == "INVALID_REQUEST"
    assert harness.booking.store.reservations == {}


def test_adjacent_reservations_are_accepted_and_overlap_is_a_409():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-adj")
    assert reserve(http, resource_id, "rsv-adj-1").status_code == 201
    assert reserve(http, resource_id, "rsv-adj-2", slot=NEXT_SLOT).status_code == 201
    overlap = reserve(
        http,
        resource_id,
        "rsv-adj-3",
        slot=("2026-10-01T10:30:00+00:00", "2026-10-01T11:30:00+00:00"),
    )
    assert overlap.status_code == 409
    body = envelope_of(overlap)
    assert body["error"]["code"] == "RESERVATION_CONFLICT"
    assert body["error"]["details"]["reason"] == "reservation_conflict"
    assert len(harness.booking.store.active_reservations_of(resource_id)) == 2


def test_conflict_is_detected_across_offsets():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-off")
    assert reserve(http, resource_id, "rsv-off-1").status_code == 201  # 10–11Z
    same_instant = reserve(
        http,
        resource_id,
        "rsv-off-2",
        slot=("2026-10-01T12:00:00+02:00", "2026-10-01T13:00:00+02:00"),
    )
    assert same_instant.status_code == 409
    assert envelope_of(same_instant)["error"]["details"]["reason"] == (
        "reservation_conflict"
    )


def test_second_booker_of_same_tenant_cannot_double_book():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-two")
    assert reserve(http, resource_id, "rsv-two-a").status_code == 201
    other = reserve(http, resource_id, "rsv-two-c", token=SUBJECT_C)
    assert other.status_code == 409
    assert envelope_of(other)["error"]["details"]["reason"] == "reservation_conflict"


@pytest.mark.parametrize(
    ("slot", "reason", "code"),
    [
        (
            ("2026-10-01T07:00:00+00:00", "2026-10-01T09:00:00+00:00"),
            "outside_availability",
            "OUTSIDE_AVAILABILITY",
        ),
        (
            ("2026-10-01T17:30:00+00:00", "2026-10-01T18:30:00+00:00"),
            "outside_availability",
            "OUTSIDE_AVAILABILITY",
        ),
        (
            ("2026-10-02T10:00:00+00:00", "2026-10-02T11:00:00+00:00"),
            "outside_availability",
            "OUTSIDE_AVAILABILITY",
        ),
    ],
)
def test_reservation_outside_the_window_is_refused(slot, reason, code):
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-out")
    response = reserve(http, resource_id, "rsv-out-1", slot=slot)
    assert response.status_code == 409, response.text
    body = envelope_of(response)
    assert body["error"]["code"] == code
    assert body["error"]["details"]["reason"] == reason
    assert harness.booking.store.reservations == {}


def test_reservation_exactly_filling_the_window_is_contained():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-fill")
    response = reserve(http, resource_id, "rsv-fill-1", slot=(WINDOW_START, WINDOW_END))
    assert response.status_code == 201, response.text


def test_resource_without_windows_is_availability_not_found():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = create_resource(http, "rsv-nowin").json()["resource_id"]
    response = reserve(http, resource_id, "rsv-nowin-1")
    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "OUTSIDE_AVAILABILITY"
    assert body["error"]["details"]["reason"] == "availability_not_found"


def test_inactive_resource_accepts_no_reservation():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = create_resource(http, "rsv-inactive", status="INACTIVE").json()[
        "resource_id"
    ]
    assert create_availability(http, resource_id, "rsv-inactive-avl").status_code == 201
    response = reserve(http, resource_id, "rsv-inactive-1")
    assert response.status_code == 409
    body = envelope_of(response)
    assert body["error"]["code"] == "RESOURCE_INACTIVE"
    assert body["error"]["details"]["reason"] == "resource_inactive"
    assert harness.booking.store.reservations == {}


def test_demo_inactive_resource_is_readable_but_not_bookable():
    harness = booking_harness()
    http = harness.http()
    read = http.get(f"{BASE}/resources/res_a2", headers=auth(SUBJECT_A))
    assert read.status_code == 200
    assert read.json()["status"] == "INACTIVE"
    response = reserve(http, "res_a2", "rsv-demo-inactive")
    assert response.status_code == 409
    assert envelope_of(response)["error"]["details"]["reason"] == "resource_inactive"


@pytest.mark.parametrize(
    "slot",
    [
        ("2026-10-01T10:00:00", "2026-10-01T11:00:00"),
        ("2026-10-01T10:00:00Z", "2026-10-01T11:00:00"),
        ("2026-10-01T11:00:00Z", "2026-10-01T10:00:00Z"),
        ("2026-10-01T10:00:00Z", "2026-10-01T10:00:00Z"),
        ("2026-10-01T10:00:00+02:00", "2026-10-01T08:00:00Z"),
    ],
)
def test_invalid_reservation_interval_is_refused_before_any_decision(slot):
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-bad")
    decisions_before = len(harness.instance.authorization.store.audit)
    response = reserve(http, resource_id, "rsv-bad-1", slot=slot)
    assert response.status_code == 422, response.text
    assert envelope_of(response)["error"]["code"] == "VALIDATION_ERROR"
    assert harness.booking.store.reservations == {}
    assert len(harness.instance.authorization.store.audit) == decisions_before


def test_unknown_resource_on_reservation_is_a_404():
    harness = booking_harness(store=BookingStore())
    response = reserve(harness.http(), "res_missing", "rsv-missing")
    assert response.status_code == 404
    assert envelope_of(response)["error"]["details"]["reason"] == "resource_not_found"


# ------------------------------------------------------------- lifecycle
def test_cancel_preserves_the_record_and_repeated_cancel_is_refused():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-cancel")
    created = reserve(http, resource_id, "rsv-cancel-1").json()
    reservation_id = created["reservation_id"]

    cancelled = cancel_reservation(http, reservation_id, "rsv-cancel-2")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "CANCELLED"
    assert body["cancelled_at"]
    assert {k: body[k] for k in body if k not in ("status", "cancelled_at")} == {
        k: created[k] for k in created if k not in ("status", "cancelled_at")
    }
    read = http.get(f"{BASE}/reservations/{reservation_id}", headers=auth(SUBJECT_A))
    assert read.status_code == 200
    assert read.json() == body

    again = cancel_reservation(http, reservation_id, "rsv-cancel-3")
    assert again.status_code == 409
    envelope = envelope_of(again)
    assert envelope["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert envelope["error"]["details"]["reason"] == "invalid_state_transition"
    assert reservation_id in harness.booking.store.reservations


def test_cancel_replay_returns_the_recorded_result_without_second_effect():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-crep")
    reservation_id = reserve(http, resource_id, "rsv-crep-1").json()["reservation_id"]
    first = cancel_reservation(http, reservation_id, "rsv-crep-2")
    replay = cancel_reservation(http, reservation_id, "rsv-crep-2")
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    # Both accesses were served (and audited), but the effect happened once:
    # the guard replayed the recorded result and the record is unchanged.
    replays = [
        e for e in harness.booking.store.audit if e.action == "idempotency_replay"
    ]
    assert len(replays) == 1
    assert replays[0].decision == "ALLOW"
    stored = harness.booking.store.reservations[reservation_id]
    assert stored.status == "CANCELLED"
    assert stored.cancelled_at == first.json()["cancelled_at"]


def test_cancelled_interval_is_bookable_again():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-free")
    first = reserve(http, resource_id, "rsv-free-1").json()
    assert (
        cancel_reservation(http, first["reservation_id"], "rsv-free-2").status_code
        == 200
    )
    second = reserve(http, resource_id, "rsv-free-3", token=SUBJECT_C)
    assert second.status_code == 201
    assert second.json()["booker_identity_id"] == "idn_human_c"


def test_no_delete_or_reactivate_route_exists():
    http = booking_harness().http()
    for path in (
        f"{BASE}/reservations/rsv_x",
        f"{BASE}/resources/res_a1",
        f"{BASE}/resources/res_a1/availability",
    ):
        assert http.delete(path, headers=auth(SUBJECT_A)).status_code == 405
        assert http.put(path, headers=auth(SUBJECT_A), json={}).status_code == 405
        assert http.patch(path, headers=auth(SUBJECT_A), json={}).status_code == 405
    for path in (
        f"{BASE}/reservations/rsv_x/activate",
        f"{BASE}/reservations/rsv_x/reactivate",
        f"{BASE}/reservations",
        f"{BASE}/resources",
    ):
        method = http.get if not path.endswith("ate") else http.post
        response = method(path, headers=auth(SUBJECT_A, "x"))
        assert response.status_code in (404, 405, 422), (path, response.status_code)


# ---------------------------------------------------------- authorization
def test_another_booker_cannot_cancel_even_with_the_grant():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-bm")
    reservation_id = reserve(http, resource_id, "rsv-bm-1").json()["reservation_id"]
    # Subject C holds booking.reservations.cancel in Tenant A, yet the
    # Reservation is A's: ownership is checked after the grant.
    response = cancel_reservation(http, reservation_id, "rsv-bm-2", token=SUBJECT_C)
    assert response.status_code == 403
    body = envelope_of(response)
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert body["error"]["details"]["reason"] == "booker_mismatch"
    assert harness.booking.store.reservations[reservation_id].status == "ACTIVE"


def test_booker_without_the_cancel_grant_is_denied_before_ownership():
    harness = booking_harness(store=BookingStore())
    harness.instance.authorization.store.revoke(
        "ten_a", "idn_human_a", OPERATION_RESERVATION_CANCEL
    )
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-grant")
    reservation_id = reserve(http, resource_id, "rsv-grant-1").json()["reservation_id"]
    response = cancel_reservation(http, reservation_id, "rsv-grant-2")
    assert response.status_code == 403
    assert envelope_of(response)["error"]["details"]["reason"] == (
        "permission_not_granted"
    )
    assert harness.booking.store.reservations[reservation_id].status == "ACTIVE"


def test_subject_without_create_grant_cannot_reserve():
    harness = booking_harness(store=BookingStore())
    harness.instance.authorization.store.revoke(
        "ten_a", "idn_human_c", OPERATION_RESERVATION_CREATE
    )
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-nog")
    response = reserve(http, resource_id, "rsv-nog-1", token=SUBJECT_C)
    assert response.status_code == 403
    assert envelope_of(response)["error"]["details"]["reason"] == (
        "permission_not_granted"
    )
    assert harness.booking.store.reservations == {}


def test_cross_tenant_reservation_paths_are_denied_or_hidden():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-xt")
    reservation_id = reserve(http, resource_id, "rsv-xt-1").json()["reservation_id"]
    # Tenant B: cannot reserve on A's Resource, read or cancel A's Reservation.
    denied = reserve(http, resource_id, "rsv-xt-b", token=SUBJECT_B, slot=NEXT_SLOT)
    assert denied.status_code == 403
    read = http.get(f"{BASE}/reservations/{reservation_id}", headers=auth(SUBJECT_B))
    assert read.status_code == 403
    cancel = cancel_reservation(http, reservation_id, "rsv-xt-c", token=SUBJECT_B)
    assert cancel.status_code == 403
    assert envelope_of(cancel)["error"]["details"]["reason"] != "booker_mismatch"
    assert harness.booking.store.reservations[reservation_id].status == "ACTIVE"
    assert len(harness.booking.store.reservations) == 1
    # And nothing of A's data appears in any refusal body.
    for response in (denied, read, cancel):
        assert reservation_id not in response.text
        assert "idn_human_a" not in response.text


def test_unknown_reservation_is_a_404_without_asking_the_decision():
    harness = booking_harness()
    before = len(harness.instance.authorization.store.audit)
    read = harness.http().get(f"{BASE}/reservations/missing", headers=auth(SUBJECT_A))
    assert read.status_code == 404
    assert envelope_of(read)["error"]["details"]["reason"] == "reservation_not_found"
    cancel = cancel_reservation(harness.http(), "missing", "rsv-404")
    assert cancel.status_code == 404
    assert len(harness.instance.authorization.store.audit) == before


# ------------------------------------------------------------ idempotency
def test_reservation_replay_and_conflict():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-idem")
    first = reserve(http, resource_id, "rsv-idem-1")
    replay = reserve(http, resource_id, "rsv-idem-1")
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert len(harness.booking.store.reservations) == 1
    changed = reserve(http, resource_id, "rsv-idem-1", slot=NEXT_SLOT)
    assert changed.status_code == 409
    assert envelope_of(changed)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.booking.store.reservations) == 1


def test_reservation_key_cannot_move_across_subjects_or_commands():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-bind")
    created = reserve(http, resource_id, "rsv-bind-1")
    assert created.status_code == 201
    # Same key, other subject of the same Tenant: a different binding.
    other = reserve(http, resource_id, "rsv-bind-1", token=SUBJECT_C, slot=NEXT_SLOT)
    assert other.status_code == 409
    assert envelope_of(other)["error"]["details"]["reason"] == "idempotency_conflict"
    # Same key, other command.
    cancel = cancel_reservation(http, created.json()["reservation_id"], "rsv-bind-1")
    assert cancel.status_code == 409
    assert envelope_of(cancel)["error"]["details"]["reason"] == "idempotency_conflict"
    assert harness.booking.store.reservations[
        created.json()["reservation_id"]
    ].status == ("ACTIVE")


def test_conflicting_reservation_records_no_idempotency_result():
    """A refused effect is not recorded: the same key can retry once the slot frees."""
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-retry")
    first = reserve(http, resource_id, "rsv-retry-1").json()
    refused = reserve(http, resource_id, "rsv-retry-2", token=SUBJECT_C)
    assert refused.status_code == 409
    assert (
        cancel_reservation(http, first["reservation_id"], "rsv-retry-3").status_code
        == 200
    )
    retried = reserve(http, resource_id, "rsv-retry-2", token=SUBJECT_C)
    assert retried.status_code == 201


def test_commands_without_key_are_refused_before_any_effect():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-nokey")
    reservation_id = reserve(http, resource_id, "rsv-nokey-1").json()["reservation_id"]
    for response in (
        http.post(
            f"{BASE}/resources/{resource_id}/availability",
            headers=auth(SUBJECT_A),
            json={"start_at": WINDOW_START, "end_at": WINDOW_END},
        ),
        http.post(
            f"{BASE}/reservations",
            headers=auth(SUBJECT_A),
            json={
                "resource_id": resource_id,
                "start_at": NEXT_SLOT[0],
                "end_at": NEXT_SLOT[1],
            },
        ),
        http.post(
            f"{BASE}/reservations/{reservation_id}/cancel", headers=auth(SUBJECT_A)
        ),
    ):
        assert response.status_code == 400, response.text
        assert envelope_of(response)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert len(harness.booking.store.availability) == 1
    assert len(harness.booking.store.reservations) == 1
    assert harness.booking.store.reservations[reservation_id].status == "ACTIVE"


# ------------------------------------------------------------------ client
def test_published_client_covers_the_reservation_flow():
    harness = booking_harness(store=BookingStore())
    client = harness.client()
    resource = client.create_resource(
        SUBJECT_A, name="Client room", idempotency_key="c-1"
    )
    window = client.create_availability(
        SUBJECT_A,
        resource.resource_id,
        start_at=WINDOW_START,
        end_at=WINDOW_END,
        idempotency_key="c-2",
    )
    assert client.list_availability(SUBJECT_A, resource.resource_id) == (window,)
    reservation = client.create_reservation(
        SUBJECT_A,
        resource_id=resource.resource_id,
        start_at=SLOT[0],
        end_at=SLOT[1],
        idempotency_key="c-3",
    )
    assert reservation.status == "ACTIVE"
    assert client.read_reservation(SUBJECT_A, reservation.reservation_id) == reservation
    with pytest.raises(AccessRefused) as exc:
        client.create_reservation(
            SUBJECT_C,
            resource_id=resource.resource_id,
            start_at=SLOT[0],
            end_at=SLOT[1],
            idempotency_key="c-4",
        )
    assert exc.value.reason == "reservation_conflict"
    assert exc.value.status_code == 409
    cancelled = client.cancel_reservation(
        SUBJECT_A, reservation.reservation_id, idempotency_key="c-5"
    )
    assert cancelled.status == "CANCELLED"
    with pytest.raises(AccessRefused) as exc:
        client.cancel_reservation(
            SUBJECT_A, reservation.reservation_id, idempotency_key="c-6"
        )
    assert exc.value.reason == "invalid_state_transition"


# ------------------------------------------------------------------- audit
def test_every_outcome_is_audited_with_context():
    harness = booking_harness(store=BookingStore())
    http = harness.http()
    resource_id = bookable_resource(harness, "rsv-audit")
    ok = reserve(
        http,
        resource_id,
        "rsv-audit-1",
        **{"x-request-id": "req-rsv-1", "x-correlation-id": "corr-rsv-1"},
    )
    assert ok.status_code == 201
    conflict = reserve(http, resource_id, "rsv-audit-2", token=SUBJECT_C)
    assert conflict.status_code == 409
    mismatch = cancel_reservation(
        http, ok.json()["reservation_id"], "rsv-audit-3", token=SUBJECT_C
    )
    assert mismatch.status_code == 403
    foreign = reserve(http, resource_id, "rsv-audit-4", token=SUBJECT_B, slot=NEXT_SLOT)
    assert foreign.status_code == 403
    naive = reserve(
        http,
        resource_id,
        "rsv-audit-5",
        slot=("2026-10-01T10:00:00", "2026-10-01T11:00:00"),
    )
    assert naive.status_code == 422

    journal = harness.booking.store.audit
    by_reason = {}
    for event in journal:
        by_reason.setdefault(event.reason, []).append(event)

    served = [
        e
        for e in journal
        if e.action == OPERATION_RESERVATION_CREATE and e.decision == "ALLOW"
    ]
    assert len(served) == 1
    assert served[0].request_id == "req-rsv-1"
    assert served[0].correlation_id == "corr-rsv-1"
    assert served[0].subject_id == "idn_human_a"
    assert served[0].tenant_id == "ten_a"
    assert served[0].resource_tenant_id == "ten_a"
    assert served[0].details == {"reservation_id": ok.json()["reservation_id"]}

    assert [e.subject_id for e in by_reason["reservation_conflict"]] == ["idn_human_c"]
    assert (
        by_reason["reservation_conflict"][0].request_id
        == envelope_of(conflict)["request_id"]
    )
    assert [e.subject_id for e in by_reason["booker_mismatch"]] == ["idn_human_c"]
    assert by_reason["booker_mismatch"][0].action == OPERATION_RESERVATION_CANCEL
    assert (
        by_reason["validation_error"][0].details["rule"]
        == "start_at must be timezone-aware"
    )
    foreign_reason = envelope_of(foreign)["error"]["details"]["reason"]
    assert foreign_reason in by_reason
    assert by_reason[foreign_reason][0].tenant_id == "ten_b"
    assert by_reason[foreign_reason][0].resource_tenant_id == "ten_a"
    windows = [e for e in journal if e.action == OPERATION_AVAILABILITY_CREATE]
    assert len(windows) == 1 and windows[0].decision == "ALLOW"
    # Every audit record carries the linkage identifiers.
    assert all(e.request_id and e.correlation_id for e in journal)
