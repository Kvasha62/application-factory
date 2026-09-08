"""Error surface of the Learning component.

A refusal of this component is data: a stable machine-readable
:class:`learning_service.contracts.ErrorCode`, an HTTP status consistent with
the foundation semantics, and an audited record with ``request_id`` and
``correlation_id``. The published envelope is
``{"error": {"code", "message", "details"}, "request_id", "correlation_id"}``;
``message`` never carries a stack trace, a secret or another tenant's data.

* :class:`AccessRefused` — the business operation did not happen: the request
  was not authenticated, was denied, addressed an unavailable resource,
  violated a lifecycle/precondition rule or was refused by IS-005;
* :class:`ContractViolation` — a consumed contract transport failed or
  answered outside its published shape on the *consumer* side of this
  component; a consumer of Learning must fail closed on it;
* :class:`ConfigurationError` — invalid or unknown configuration
  (ARCHITECTURE.md §20).

There is deliberately no permissive exception: nothing here can be caught as
a signal that a business effect happened.
"""

from __future__ import annotations

from typing import Any


class AccessRefused(Exception):
    """One refused Learning operation at this component's boundary.

    ``code`` is one of the published :class:`~learning_service.contracts.ErrorCode`
    values and ``status_code`` is the HTTP status the contract maps it to.
    The object is a value: it carries no handle on the store, the engine or a
    dependency, and it can never be mistaken for a served representation.
    """

    def __init__(
        self,
        code: str,
        *,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.details: dict[str, Any] = details or {}
        super().__init__(code)


class ContractViolation(Exception):
    """A consumed contract failed or answered outside its published shape.

    Not an :class:`AccessRefused`: it is not a decision about a Learning
    operation. A consumer of Learning must fail closed instead of assuming a
    representation was served or an effect happened.
    """


class ConfigurationError(Exception):
    """Invalid or unknown configuration of this component (ARCHITECTURE.md §20)."""


__all__ = ["AccessRefused", "ConfigurationError", "ContractViolation"]
