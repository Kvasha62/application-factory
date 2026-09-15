"""Errors raised by the Golden Bundle (Slice D).

Slice D mirrors the Slice A/B/C error discipline: validation collects *every*
violation instead of failing on the first one, so a broken bundle produces one
deterministic, complete report.
"""

from __future__ import annotations

from collections.abc import Sequence


class GoldenBundleError(Exception):
    """Base class for every Golden Bundle failure."""


class GoldenBundleNotFoundError(GoldenBundleError):
    """A bundle document or the repository root cannot be found or read."""


class GoldenBundleValidationError(GoldenBundleError):
    """A bundle document failed validation.

    The message lists every violation, one per line, sorted, so that the same
    broken document always produces the same report.
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        joined = "\n".join(f"  - {item}" for item in self.errors)
        super().__init__(
            f"invalid golden bundle ({len(self.errors)} errors):\n{joined}"
        )
