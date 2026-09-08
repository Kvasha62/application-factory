"""Error surface of the Tenant Authority component.

Two groups of codes are deliberately separated:

* :class:`TenantAuthorityDenial` — why an operation was denied (identity,
  authorization, ownership, idempotency) and is recorded in the audit journal;
* :class:`TenantAuthorityLifecycle` — why a lifecycle state was rejected.

Both are part of the published contract of this component: consumers may catch
the exception classes re-exported here, but never the internal engine types.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class DenyReason(StrEnum):
    """Machine-readable reasons a Tenant Authority operation is rejected."""

    MISSING_SERVICE_IDENTITY = "missing_service_identity"
    INVALID_SERVICE_IDENTITY = "invalid_service_identity"
    UNKNOWN_SERVICE = "unknown_service"
    INSUFFICIENT_AUTHORIZATION = "insufficient_authorization"
    PLATFORM_MISMATCH = "platform_mismatch"
    TENANT_EXISTS = "tenant_exists"
    TENANT_NOT_FOUND = "tenant_not_found"
    FOREIGN_TENANT = "foreign_tenant"
    INVALID_TRANSITION = "invalid_transition"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class TenantAuthorityError(Exception):
    """Base class for every rejection produced by this component."""

    status_code: int = 400

    def __init__(
        self,
        reason: DenyReason,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.details: dict[str, Any] = details or {}
        super().__init__(message or reason.value)


class AuthenticationDenied(TenantAuthorityError):
    """The caller could not be verified as a service identity of this platform."""

    status_code = 401


class AuthorizationDenied(TenantAuthorityError):
    """The verified caller is not allowed to perform this operation."""

    status_code = 403


class OwnershipDenied(TenantAuthorityError):
    """The Tenant exists but belongs to another Platform Instance.

    Reported with 404 semantics: a caller inside one Platform Instance must not
    learn that a given identifier resolves in a different Platform Instance.
    """

    status_code = 404


class TenantNotFound(TenantAuthorityError):
    status_code = 404


class TenantConflict(TenantAuthorityError):
    status_code = 409


class InvalidTransition(TenantAuthorityError):
    """A lifecycle transition outside the state machine published by this component."""

    status_code = 409


class ConfigurationError(Exception):
    """Invalid or unknown configuration of the Tenant Authority component."""
