"""Errors raised by the Component Catalog.

Slice B mirrors the Slice A error discipline: validation collects *every*
violation instead of failing on the first one, so a diverged catalog produces
one deterministic, complete report.
"""

from __future__ import annotations

from collections.abc import Sequence


class CatalogError(Exception):
    """Base class for every Component Catalog failure."""


class CatalogNotFoundError(CatalogError):
    """The canonical catalog document cannot be found, read or decoded."""


class CatalogValidationError(CatalogError):
    """The catalog document failed validation.

    The message lists every violation, one per line, sorted, so that the same
    diverged document always produces the same report.
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        joined = "\n".join(f"  - {item}" for item in self.errors)
        super().__init__(
            f"invalid component catalog ({len(self.errors)} errors):\n{joined}"
        )
