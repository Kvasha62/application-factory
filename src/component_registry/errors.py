"""Errors raised by the Component Registry.

Slice A keeps the error surface deliberately small: validation collects
*every* violation instead of failing on the first one, so a broken registry
produces one deterministic, complete report.
"""

from __future__ import annotations

from collections.abc import Sequence


class RegistryError(Exception):
    """Base class for every Component Registry failure."""


class RegistryNotFoundError(RegistryError):
    """The canonical registry document or the repository root cannot be found."""


class RegistryValidationError(RegistryError):
    """The registry document failed validation.

    The message lists every violation, one per line, sorted, so that the same
    broken document always produces the same report.
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        joined = "\n".join(f"  - {item}" for item in self.errors)
        super().__init__(
            f"invalid component registry ({len(self.errors)} errors):\n{joined}"
        )
