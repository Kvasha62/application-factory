"""Errors raised by the Composer (Slice E).

Slice E mirrors the Slice A/B/C/D error discipline: validation and composition
collect *every* violation instead of failing on the first one, so a broken
request or an impossible composition always produces one deterministic,
complete report (ADR-0015 §8: invalid composition must fail deterministically
and explain the compatibility or contract violation).
"""

from __future__ import annotations

from collections.abc import Sequence


class ComposerError(Exception):
    """Base class for every Composer failure."""


class ComposerNotFoundError(ComposerError):
    """A composition request document or the repository root cannot be found."""


class ComposerValidationError(ComposerError):
    """A composition request document failed validation.

    The message lists every violation, one per line, sorted, so the same broken
    request always produces the same report.
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        joined = "\n".join(f"  - {item}" for item in self.errors)
        super().__init__(
            f"invalid composition request ({len(self.errors)} errors):\n{joined}"
        )


class CompositionRejectedError(ComposerError):
    """The platform composition cannot be produced.

    Composition is rejected deterministically: every incompatibility or
    contract violation is reported, sorted, and no manifest is produced. The
    Composer never substitutes a version, never drops a declared dependency and
    never repairs a composition silently (ARCHITECTURE.md §17; ADR-0015 §8).
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        joined = "\n".join(f"  - {item}" for item in self.errors)
        super().__init__(f"composition rejected ({len(self.errors)} errors):\n{joined}")
