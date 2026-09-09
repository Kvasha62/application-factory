"""Ports through which Learning consumes the Authorization Boundary.

SCS-001 may not read another component's storage, internal modules or
internal types (ARCHITECTURE.md §1.1, §6.1, LAW-04). It depends on exactly
one narrow port — expressed entirely in this component's own vocabulary —
and on nothing else:

* :class:`AuthorizationPort` — the decision of IS-003 for one access
  question. This is why Learning contains no permission logic, no grant
  lookup, no identity verification and no tenant derivation of its own:
  there is no second authorization or tenant-context mechanism, because
  there is no mechanism here at all.

The port declares what this component *needs*, not what IS-003 happens to
offer: nothing here can grant a permission, read a grant, enumerate
subjects or read an audit journal. The operation is total — it answers
with a value of :mod:`learning_service.consumed`, including when the
answer is "no" or "none": a refusal of the dependency is data this
component decides on, not an exception vocabulary borrowed from the
provider.

The implementation wired in by a composition root is the adapter of
:mod:`learning_service.adapters`, which sits over the published consumer
surface of IS-003. Nothing in this module — or anywhere else in the
component — imports that component.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from learning_service.consumed import DecisionOutcome


@runtime_checkable
class AuthorizationPort(Protocol):
    """Source of the access decision (IS-003).

    ``resource_tenant_id`` is the Tenant that owns the resource, stated by
    this data owner from its own store — never a caller claim.
    ``claimed_tenant_id`` is the caller-supplied cross-check, forwarded
    as-is: it can never select the effective tenant (LAW-16a).
    """

    def decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DecisionOutcome: ...


__all__ = [
    "AuthorizationPort",
    "DecisionOutcome",
]
