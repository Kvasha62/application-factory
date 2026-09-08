from __future__ import annotations

from identity_service.models import DenyReason


class AccessDenied(Exception):
    def __init__(self, reason: DenyReason, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason.value)
