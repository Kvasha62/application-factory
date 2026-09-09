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
* :class:`CommandSafetyPort` — the exactly-once execution of one
  state-changing command, answered by IS-005. This is why Learning owns no
  second idempotency mechanism: the review command is delivered to the
  IS-005 guard, which decides between executing, replaying and refusing a
  conflict.

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

from collections.abc import Callable
from typing import Protocol, TypeVar, runtime_checkable

from learning_service.consumed import DecisionOutcome

T = TypeVar("T")


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


@runtime_checkable
class CommandSafetyPort(Protocol):
    """The IS-005 command-safety boundary, as this component consumes it.

    The shape is exactly the published surface of IS-005
    (``idempotency.guard.IdempotencyGuard.execute``): one logical command
    identity executes at most once, an exact replay returns the recorded
    result without executing the effect again, and a differing context or
    command for the same key raises ``IdempotencyConflict``. Learning
    supplies the command identity of a review (operation, target,
    fingerprint) and nothing else — no second key scheme, no second store,
    no second replay rule.
    """

    def execute(
        self,
        key: str | None,
        *,
        identity: str | None,
        tenant_id: str | None,
        operation: str,
        resource: str | None,
        fingerprint: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
        effect: Callable[[], T],
    ) -> T: ...


__all__ = [
    "AuthorizationPort",
    "CommandSafetyPort",
    "DecisionOutcome",
]
