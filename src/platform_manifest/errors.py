"""Errors for Platform Manifest (Slice C)."""

from __future__ import annotations


class ManifestError(Exception):
    """Base class for manifest errors."""


class ManifestNotFoundError(ManifestError):
    """Raised when a manifest document cannot be found or read."""


class ManifestValidationError(ManifestError):
    """Raised when a manifest document is invalid."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors
