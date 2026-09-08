"""Error surface of the Identity / Tenant Context component.

``AccessDenied`` names why a request was refused; the reason vocabulary is the
published one (:class:`identity_service.contracts.DenyReason`).
``ContractViolation`` is not a decision: it means the component answered outside
its published contract, and a consumer must fail closed on it instead of
assuming that a subject is verified.
"""

from __future__ import annotations

from identity_service.contracts import DenyReason


class AccessDenied(Exception):
    def __init__(self, reason: DenyReason, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason.value)


class ContractViolation(Exception):
    """The component answered outside its published contract (transport fault)."""
