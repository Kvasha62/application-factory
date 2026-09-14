"""SCS-003 Booking — domain invariants at the interval and store level.

Pure proofs of ADR-0014 §5–§8 without HTTP: timezone-aware input and UTC
normalization (including DST offsets), refusal of naive timestamps,
``start_at < end_at``, half-open ``[start_at, end_at)`` semantics with
adjacent intervals free, full containment in an Availability Window,
occupancy by ACTIVE Reservations only, the ``ACTIVE → CANCELLED``
lifecycle with history preserved, and the structural rules of
registration (same-tenant children, single owner, UTC-only persistence).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from booking_service import OWNER_COMPONENT
from booking_service.intervals import (
    Interval,
    TimeProblem,
    contains,
    iso_utc,
    overlaps,
    parse_interval,
    to_utc,
)
from booking_service.models import (
    OwnedAvailabilityWindow,
    OwnedReservation,
    OwnedResource,
)
from booking_service.store import BookingStore, DomainRefusal

WHEN = "2026-09-13T00:00:00+00:00"
T = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def at(hours: float) -> datetime:
    return T + timedelta(hours=hours)


def interval(start: float, end: float) -> Interval:
    return Interval(start_at=at(start), end_at=at(end))


def store_with_resource(
    *, status: str = "ACTIVE", windows: tuple[tuple[float, float], ...] = ((0, 10),)
) -> BookingStore:
    store = BookingStore()
    store.register_resource(
        OwnedResource(
            resource_id="res_1",
            tenant_id="ten_a",
            owner_component=OWNER_COMPONENT,
            name="Room",
            status=status,
            created_by="idn_human_a",
            created_at=WHEN,
            updated_at=WHEN,
        )
    )
    for index, (start, end) in enumerate(windows):
        store.register_availability(
            OwnedAvailabilityWindow(
                availability_id=f"avl_{index}",
                resource_id="res_1",
                tenant_id="ten_a",
                owner_component=OWNER_COMPONENT,
                start_at=at(start),
                end_at=at(end),
                created_by="idn_human_a",
                created_at=WHEN,
            )
        )
    return store


def reserve(store: BookingStore, start: float, end: float, booker="idn_human_a"):
    return store.create_reservation(
        resource_id="res_1",
        tenant_id="ten_a",
        booker_identity_id=booker,
        interval=interval(start, end),
        now=WHEN,
    )


# ------------------------------------------------------------- time rules
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-01T10:00:00+02:00", datetime(2026, 10, 1, 8, 0, tzinfo=UTC)),
        ("2026-10-01T08:00:00Z", datetime(2026, 10, 1, 8, 0, tzinfo=UTC)),
        ("2026-10-01T03:00:00-05:00", datetime(2026, 10, 1, 8, 0, tzinfo=UTC)),
        # DST: the same wall-clock hour in Europe/Berlin is +02:00 in summer
        # and +01:00 in winter — both are valid aware input and normalize to
        # the instant they actually denote.
        ("2026-07-01T10:00:00+02:00", datetime(2026, 7, 1, 8, 0, tzinfo=UTC)),
        ("2026-12-01T10:00:00+01:00", datetime(2026, 12, 1, 9, 0, tzinfo=UTC)),
        (
            datetime(2026, 10, 1, 10, 0, tzinfo=timezone(timedelta(hours=2))),
            datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        ),
    ],
)
def test_aware_input_normalizes_to_utc(raw, expected):
    value = to_utc(raw, what="start_at")
    assert value == expected
    assert value.tzinfo is UTC
    assert iso_utc(value).endswith("+00:00")


@pytest.mark.parametrize(
    "raw",
    [
        "2026-10-01T10:00:00",  # naive
        datetime(2026, 10, 1, 10, 0),  # noqa: DTZ001 — naive on purpose
        "2026-10-01",  # date only, naive
        "not a timestamp",
        "",
        None,
        1700000000,
    ],
)
def test_naive_or_unreadable_timestamps_are_not_valid_input(raw):
    with pytest.raises(TimeProblem):
        to_utc(raw, what="start_at")


def test_interval_requires_start_strictly_before_end():
    with pytest.raises(TimeProblem):
        parse_interval("2026-10-01T10:00:00Z", "2026-10-01T10:00:00Z")
    with pytest.raises(TimeProblem):
        parse_interval("2026-10-01T11:00:00Z", "2026-10-01T10:00:00Z")
    parsed = parse_interval("2026-10-01T10:00:00+02:00", "2026-10-01T09:00:00Z")
    assert parsed.start_at < parsed.end_at
    assert parsed.start_at == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def test_same_instant_in_different_offsets_is_an_empty_interval():
    # 10:00+02:00 is exactly 08:00Z: comparison happens on the instant.
    with pytest.raises(TimeProblem):
        parse_interval("2026-10-01T10:00:00+02:00", "2026-10-01T08:00:00Z")


def test_half_open_overlap_semantics():
    assert overlaps(interval(0, 1), interval(0.5, 1.5))
    assert overlaps(interval(0, 2), interval(0.5, 1))  # containment
    assert overlaps(interval(0, 1), interval(0, 1))  # identical
    assert not overlaps(interval(0, 1), interval(1, 2))  # adjacent
    assert not overlaps(interval(1, 2), interval(0, 1))  # adjacent, reversed
    assert not overlaps(interval(0, 1), interval(2, 3))


def test_containment_is_bound_inclusive():
    window = interval(0, 10)
    assert contains(window, interval(0, 10))
    assert contains(window, interval(0, 1))
    assert contains(window, interval(9, 10))
    assert not contains(window, interval(-0.5, 1))
    assert not contains(window, interval(9, 10.5))
    assert not contains(window, interval(-1, 11))


# ---------------------------------------------------------- store: booking
def test_reservation_persists_utc_aware_bounds():
    store = store_with_resource()
    created = reserve(store, 1, 2)
    assert created.status == "ACTIVE"
    assert created.start_at.tzinfo is UTC
    assert created.end_at.tzinfo is UTC
    assert created.start_at < created.end_at
    for record in list(store.reservations.values()) + list(store.availability.values()):
        assert record.start_at.utcoffset() == timedelta(0)
        assert record.end_at.utcoffset() == timedelta(0)


def test_adjacent_reservations_do_not_overlap():
    store = store_with_resource()
    reserve(store, 2, 3)  # 10:00–11:00
    reserve(store, 3, 4)  # 11:00–12:00
    reserve(store, 1, 2)  # 09:00–10:00
    assert len(store.active_reservations_of("res_1")) == 3


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (2, 3),  # identical
        (2.5, 3.5),  # overlaps the end
        (1.5, 2.5),  # overlaps the start
        (2.25, 2.75),  # inside
        (1, 4),  # encloses
    ],
)
def test_overlapping_active_reservation_is_a_conflict(start, end):
    store = store_with_resource()
    reserve(store, 2, 3)
    with pytest.raises(DomainRefusal) as exc:
        reserve(store, start, end)
    assert exc.value.reason == "reservation_conflict"
    assert len(store.reservations) == 1


def test_cancelled_reservation_frees_its_interval():
    store = store_with_resource()
    first = reserve(store, 2, 3)
    cancelled = store.cancel_reservation(first.reservation_id, now=WHEN)
    assert cancelled.status == "CANCELLED"
    second = reserve(store, 2, 3)
    assert second.status == "ACTIVE"
    # The cancelled record is preserved as a historical fact.
    assert first.reservation_id in store.reservations
    assert store.reservations[first.reservation_id].status == "CANCELLED"
    assert len(store.reservations) == 2
    assert store.active_reservations_of("res_1") == [second]


def test_reservation_must_be_fully_inside_one_window():
    store = store_with_resource(windows=((0, 4), (6, 10)))
    with pytest.raises(DomainRefusal) as exc:
        reserve(store, 3, 5)  # spills out of the first window
    assert exc.value.reason == "outside_availability"
    with pytest.raises(DomainRefusal) as exc:
        reserve(store, 3, 7)  # bridges two windows: contained in neither
    assert exc.value.reason == "outside_availability"
    with pytest.raises(DomainRefusal) as exc:
        reserve(store, 4, 6)  # the gap
    assert exc.value.reason == "outside_availability"
    assert reserve(store, 0, 4).status == "ACTIVE"  # exactly the window
    assert reserve(store, 6, 10).status == "ACTIVE"


def test_resource_without_any_window_answers_availability_not_found():
    store = store_with_resource(windows=())
    with pytest.raises(DomainRefusal) as exc:
        reserve(store, 1, 2)
    assert exc.value.reason == "availability_not_found"


def test_inactive_resource_receives_no_reservation():
    store = store_with_resource(status="INACTIVE")
    with pytest.raises(DomainRefusal) as exc:
        reserve(store, 1, 2)
    assert exc.value.reason == "resource_inactive"
    assert store.reservations == {}


def test_availability_is_a_permission_window_not_occupancy():
    """Two windows may overlap each other freely: they are not slots."""
    store = store_with_resource(windows=((0, 10), (2, 4)))
    assert len(store.availability_of("res_1")) == 2
    reserve(store, 2, 3)
    # Occupancy is the ACTIVE Reservation, and the windows are untouched.
    assert len(store.availability_of("res_1")) == 2
    assert len(store.active_reservations_of("res_1")) == 1


def test_unknown_or_foreign_tenant_resource_is_not_found():
    store = store_with_resource()
    with pytest.raises(DomainRefusal) as exc:
        store.create_reservation(
            resource_id="res_1",
            tenant_id="ten_b",
            booker_identity_id="idn_human_b",
            interval=interval(1, 2),
            now=WHEN,
        )
    assert exc.value.reason == "resource_not_found"
    with pytest.raises(DomainRefusal) as exc:
        store.create_reservation(
            resource_id="res_missing",
            tenant_id="ten_a",
            booker_identity_id="idn_human_a",
            interval=interval(1, 2),
            now=WHEN,
        )
    assert exc.value.reason == "resource_not_found"


# --------------------------------------------------------- store: lifecycle
def test_lifecycle_is_exactly_active_to_cancelled():
    store = store_with_resource()
    created = reserve(store, 1, 2)
    cancelled = store.cancel_reservation(
        created.reservation_id, now="2026-09-14T00:00:00+00:00"
    )
    assert cancelled.status == "CANCELLED"
    assert cancelled.cancelled_at == "2026-09-14T00:00:00+00:00"
    # History preserved: everything but status/cancelled_at is unchanged.
    assert cancelled.reservation_id == created.reservation_id
    assert cancelled.start_at == created.start_at
    assert cancelled.end_at == created.end_at
    assert cancelled.booker_identity_id == created.booker_identity_id
    assert cancelled.created_at == created.created_at
    # Repeated cancellation is refused; the record still exists.
    with pytest.raises(DomainRefusal) as exc:
        store.cancel_reservation(created.reservation_id, now=WHEN)
    assert exc.value.reason == "invalid_state_transition"
    assert store.reservations[created.reservation_id] == cancelled


def test_store_offers_no_reactivation_and_no_deletion():
    names = {name for name in dir(BookingStore) if not name.startswith("_")}
    for forbidden in (
        "delete_reservation",
        "remove_reservation",
        "reactivate_reservation",
        "activate_reservation",
        "update_reservation",
        "delete_resource",
    ):
        assert forbidden not in names, forbidden


def test_cancelled_records_are_frozen_values():
    store = store_with_resource()
    created = reserve(store, 1, 2)
    with pytest.raises(AttributeError):
        created.status = "CANCELLED"  # type: ignore[misc]


# ------------------------------------------------------ store: registration
def _reservation(**overrides) -> OwnedReservation:
    base = {
        "reservation_id": "rsv_x",
        "tenant_id": "ten_a",
        "resource_id": "res_1",
        "owner_component": OWNER_COMPONENT,
        "booker_identity_id": "idn_human_a",
        "start_at": at(1),
        "end_at": at(2),
        "status": "ACTIVE",
        "created_at": WHEN,
    }
    base.update(overrides)
    return OwnedReservation(**base)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"start_at": datetime(2026, 10, 1, 9, 0)}, "naive_start_at"),  # noqa: DTZ001
        ({"end_at": datetime(2026, 10, 1, 10, 0)}, "naive_end_at"),  # noqa: DTZ001
        (
            {
                "start_at": datetime(
                    2026, 10, 1, 11, 0, tzinfo=timezone(timedelta(hours=2))
                )
            },
            "non_utc_start_at",
        ),
        ({"start_at": at(2), "end_at": at(1)}, "invalid_interval"),
        ({"start_at": at(2), "end_at": at(2)}, "invalid_interval"),
        ({"status": "PENDING"}, "unknown_reservation_status"),
        ({"owner_component": "commerce"}, "foreign_owner"),
        ({"tenant_id": "ten_b"}, "resource_not_found"),
        ({"resource_id": "res_missing"}, "resource_not_found"),
        ({"booker_identity_id": ""}, "missing_booker"),
    ],
)
def test_registration_refuses_structural_violations(overrides, reason):
    store = store_with_resource()
    with pytest.raises(DomainRefusal) as exc:
        store.register_reservation(_reservation(**overrides))
    assert exc.value.reason == reason
    assert store.reservations == {}


def test_registration_refuses_naive_window_bounds():
    store = store_with_resource(windows=())
    with pytest.raises(DomainRefusal) as exc:
        store.register_availability(
            OwnedAvailabilityWindow(
                availability_id="avl_naive",
                resource_id="res_1",
                tenant_id="ten_a",
                owner_component=OWNER_COMPONENT,
                start_at=datetime(2026, 10, 1, 8, 0),  # noqa: DTZ001 — naive
                end_at=at(10),
                created_by="idn_human_a",
                created_at=WHEN,
            )
        )
    assert exc.value.reason == "naive_start_at"
    assert store.availability == {}


def test_resource_status_vocabulary_is_closed_at_registration():
    store = BookingStore()
    with pytest.raises(DomainRefusal) as exc:
        store.register_resource(
            OwnedResource(
                resource_id="res_x",
                tenant_id="ten_a",
                owner_component=OWNER_COMPONENT,
                name="Room",
                status="ARCHIVED",
                created_by="idn_human_a",
                created_at=WHEN,
                updated_at=WHEN,
            )
        )
    assert exc.value.reason == "unknown_resource_status"


def test_resource_record_carries_no_foreign_domain_fields():
    fields = set(OwnedResource.__dataclass_fields__)
    assert fields == {
        "resource_id",
        "tenant_id",
        "owner_component",
        "name",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    }
    reservation_fields = set(OwnedReservation.__dataclass_fields__)
    assert reservation_fields == {
        "reservation_id",
        "tenant_id",
        "resource_id",
        "owner_component",
        "booker_identity_id",
        "start_at",
        "end_at",
        "status",
        "created_at",
        "cancelled_at",
    }
    for stem in (
        "price",
        "payment",
        "product",
        "course",
        "lesson",
        "enroll",
        "capacity",
    ):
        assert not any(stem in f for f in fields | reservation_fields), stem
