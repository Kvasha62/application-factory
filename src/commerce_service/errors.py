"""Error surface of the Commerce component (SCS-002).

These errors are the component's own refusals at its enforcement boundary. A
refusal is data with a stable machine-readable reason, and every refusal is
audited with ``request_id`` and ``correlation_id``.

* :class:`AccessRefused` — the access did not happen: either IS-003 decided
  ``DENY`` (the reason is passed through from its published set), or this
  component's own boundary refused (unknown product, foreign owner,
  dependency failed closed). The refusal carries the ``request_id`` and
  ``correlation_id`` it was audited with, so the HTTP contract can expose
  them in the approved error envelope;
* :class:`ContractViolation` — this component's contract transport failed or
  answered outside its published shape; a consumer must fail closed instead
  of assuming a product was served;
* :class:`ConfigurationError` — invalid or unknown configuration
  (ARCHITECTURE.md §20).

There is deliberately no permissive exception: nothing in this module can be
caught as a signal that an access succeeded.
"""

from __future__ import annotations

from typing import Any


class AccessRefused(Exception):
    """One refused access at the Commerce enforcement boundary.

    ``reason`` is a stable machine-readable code: one of this component's
    own reasons or one of the published denial reasons of IS-003 passed
    through unchanged. ``status_code`` is the HTTP status the published
    contract maps the refusal to. ``request_id`` and ``correlation_id`` are
    the observability context the refusal was audited with. The object is a
    value: it carries no handle on the store, the engine or the dependency,
    and it can never be mistaken for a served product.
    """

    def __init__(
        self,
        reason: str,
        *,
        status_code: int,
        decision: str = "DENY",
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.status_code = status_code
        self.decision = decision
        self.details: dict[str, Any] = details or {}
        self.request_id = request_id
        self.correlation_id = correlation_id
        super().__init__(reason)


class ContractViolation(Exception):
    """The contract transport failed or answered outside the published shape.

    Deliberately not an :class:`AccessRefused`: it is not a decision, and a
    consumer must fail closed on it instead of treating a missing answer as
    a served product.
    """


class ConfigurationError(Exception):
    """Invalid or unknown configuration of this component (ARCHITECTURE.md §20)."""
