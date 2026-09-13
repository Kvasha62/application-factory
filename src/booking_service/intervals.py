"""Time semantics of Booking (ADR-0014 §5) — one place, one rule set.

* every Booking interval is half-open: ``[start_at, end_at)``;
* the invariant ``start_at < end_at`` holds for every persisted interval;
* API/domain input must be timezone-aware; a naive timestamp is not valid
  Booking input and is refused before anything is persisted;
* persistence and every comparison use the UTC-normalized instant, so two
  timestamps that denote the same instant in different offsets (including
  across a DST transition) compare equal here;
* adjacent intervals do not overlap: ``[10:00, 11:00)`` and
  ``[11:00, 12:00)`` may both exist for one Resource.

The helpers are pure functions over :class:`datetime`; nothing here reads
a clock, a store or a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "Interval",
    "TimeProblem",
    "contains",
    "iso_utc",
    "overlaps",
    "parse_interval",
    "to_utc",
]


class TimeProblem(ValueError):
    """A timestamp or interval that violates the Booking time rules.

    ``problem`` is a stable, value-free description of the rule violated;
    the engine maps it to the published ``validation_error`` refusal.
    """

    def __init__(self, problem: str) -> None:
        self.problem = problem
        super().__init__(problem)


def to_utc(value: Any, *, what: str) -> datetime:
    """Normalize one timezone-aware timestamp to UTC.

    Accepts a :class:`datetime` or an ISO 8601 string (``Z`` and explicit
    offsets are both accepted, as Python 3.11+ ``fromisoformat`` does). A value without UTC offset information is a
    naive local timestamp and is refused: it cannot be normalized and must
    never be persisted (ADR-0014 §5).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise TimeProblem(f"{what} must be an ISO 8601 timestamp") from exc
    else:
        raise TimeProblem(f"{what} must be an ISO 8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimeProblem(f"{what} must be timezone-aware")
    return parsed.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    """The published string form of a UTC instant: ISO 8601 with ``+00:00``."""
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Interval:
    """One half-open UTC interval ``[start_at, end_at)`` with ``start < end``."""

    start_at: datetime
    end_at: datetime


def parse_interval(start_at: Any, end_at: Any) -> Interval:
    """Read two timezone-aware bounds into one UTC half-open interval.

    Raises :class:`TimeProblem` for a naive or unparsable bound and for an
    empty or inverted interval (``start_at >= end_at``).
    """
    start = to_utc(start_at, what="start_at")
    end = to_utc(end_at, what="end_at")
    if not start < end:
        raise TimeProblem("start_at must be strictly before end_at")
    return Interval(start_at=start, end_at=end)


def overlaps(left: Interval, right: Interval) -> bool:
    """Whether two half-open intervals intersect.

    ``[a, b)`` and ``[c, d)`` intersect iff ``a < d and c < b``; touching
    bounds (``b == c``) do not intersect — adjacent intervals are free.
    """
    return left.start_at < right.end_at and right.start_at < left.end_at


def contains(outer: Interval, inner: Interval) -> bool:
    """Whether ``inner`` lies completely inside ``outer`` (bounds inclusive).

    ``[c, d)`` is contained in ``[a, b)`` iff ``a <= c and d <= b``: a
    reservation that ends exactly where its window ends is contained.
    """
    return outer.start_at <= inner.start_at and inner.end_at <= outer.end_at
