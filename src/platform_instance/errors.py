"""Errors for Platform Instance assembly (Slice F)."""

from __future__ import annotations


class PlatformInstanceError(Exception):
    """Base class for Platform Instance errors."""


class InstanceNotFoundError(PlatformInstanceError):
    """Raised when an instance document cannot be found or read."""


class InstanceValidationError(PlatformInstanceError):
    """Raised when an instance document is invalid."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


class AssemblyRejectedError(PlatformInstanceError):
    """Raised when a Platform Instance cannot be assembled from a manifest.

    Every violation is reported together, deterministically sorted: assembly
    is never partial and never repairs its inputs (ADR-0015 §8 discipline,
    applied to instance assembly).
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors
