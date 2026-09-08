"""Ports through which Learning consumes the facts it does not own.

Learning may not read another component's storage, internal modules or
internal types (ARCHITECTURE.md §1.1, §6.1, LAW-04). It depends on exactly
two narrow ports — expressed entirely in this component's own vocabulary —
and on nothing else:

* :class:`IdentityContextPort` — who the caller is together with the Tenant
  effective for the request (IS-001). There is no identity verification, no
  token handling and no tenant derivation anywhere in this component: no
  answer, no operation (invariant 1 and 2 of the API contract);
* :class:`AuthorizationPort` — the access decision for one question (IS-003).
  There is no permission logic and no role model here: Teacher and Student
  are grants inside IS-003, and a decision that cannot be obtained is a
  denial (invariant 6).

Both ports are total: they answer with a value of
:mod:`learning_service.consumed`, including when the answer is "none". A
refusal of a dependency is data this component decides on, not an exception
vocabulary borrowed from a provider.

The implementations wired in by a composition root are the adapters of
:mod:`learning_service.adapters`, which sit over the published consumer
surfaces of IS-001 and IS-003. Nothing in this module — or anywhere else in
the component — imports those components.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from learning_service.consumed import ContextOutcome, DecisionOutcome


@runtime_checkable
class IdentityContextPort(Protocol):
    """Source of the verified identity and the effective tenant (IS-001).

    ``credential`` is what the caller presented; it is verified by IS-001 and
    nowhere else. There is no ``tenant_id`` argument that could select a
    tenant: the effective tenant is derived from the verified identity only
    (LAW-16a, invariant 1 of the API contract).
    """

    def resolve_context(
        self,
        credential: str | None,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextOutcome: ...


@runtime_checkable
class AuthorizationPort(Protocol):
    """Source of the access decision (IS-003).

    ``resource_tenant_id`` is the Tenant that owns the resource, stated by
    this data owner from its own store — never a caller claim. The default
    outcome of the authority is DENY; this component treats a dependency that
    does not answer exactly like a denial that cannot be questioned.
    """

    def decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DecisionOutcome: ...


__all__ = ["AuthorizationPort", "IdentityContextPort"]
