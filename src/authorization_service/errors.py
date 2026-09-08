"""Error surface of the Authorization Boundary component (IS-003).

These errors are about the **caller** — the component that asks for a decision —
and never about the subject of the decision:

* :class:`CallerNotAuthenticated` — the asking component did not present a
  verifiable service identity;
* :class:`CallerNotAuthorized` — it is verified, but it may not ask this
  authority for decisions (or it belongs to another Platform Instance);
* :class:`ContractViolation` — this component answered outside its published
  contract, so a consumer must fail closed instead of assuming ``ALLOW``.

A subject that must not be served is never reported as an error: it is a normal
``DENY`` decision with a stable reason (see
:mod:`authorization_service.contracts`). Keeping the two apart is the point of
the component: "your question was refused" and "the answer is no" are different
facts, and only the second one is an authorization decision.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class CallerDenyReason(StrEnum):
    """Machine-readable reasons a request for a decision is refused outright."""

    MISSING_SERVICE_IDENTITY = "missing_service_identity"
    INVALID_SERVICE_IDENTITY = "invalid_service_identity"
    UNKNOWN_SERVICE = "unknown_service"
    INSUFFICIENT_AUTHORIZATION = "insufficient_authorization"
    PLATFORM_MISMATCH = "platform_mismatch"
    MALFORMED_REQUEST = "malformed_request"


class AuthorizationServiceError(Exception):
    """Base class for every refusal to answer produced by this component."""

    status_code: int = 400

    def __init__(
        self,
        reason: CallerDenyReason,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.details: dict[str, Any] = details or {}
        super().__init__(message or reason.value)


class CallerNotAuthenticated(AuthorizationServiceError):
    """The asking component could not be verified as a service identity."""

    status_code = 401


class CallerNotAuthorized(AuthorizationServiceError):
    """The verified caller may not ask this authority for decisions."""

    status_code = 403


class MalformedDecisionRequest(AuthorizationServiceError):
    """The decision request itself is not answerable (e.g. an empty operation).

    It is refused rather than answered ``DENY``: a malformed question has no
    authorization meaning, and silently returning ``DENY`` would hide a defect
    in the consuming component behind a security-looking answer.
    """

    status_code = 422


class ContractViolation(Exception):
    """The component answered outside its published contract (transport fault).

    Deliberately not an :class:`AuthorizationServiceError`: it is not a
    decision, and a consumer must fail closed on it instead of treating a
    missing answer as permission.
    """


class ConfigurationError(Exception):
    """Invalid or unknown configuration of this component (ARCHITECTURE.md §20)."""
