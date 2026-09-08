"""Local value types for what IS-006 consumes from the Identity component.

A workflow needs exactly one fact it does not own: *which verified subject acts
and which Tenant is effective for this workflow*. IS-006 obtains that answer
through the published consumer surface of IS-001 and stores nothing of it
beyond the immutable :class:`saga.models.SagaContext`, so this component
contains no identity verification, no token handling, no tenant registry and no
tenant derivation of its own: there is no second identity or tenant-context
mechanism here, because there is no mechanism at all (invariants 9 and 10).

Why local types at all: a consumed answer that kept the provider's own classes
would make the provider's internals part of this component's code. The boundary
is therefore expressed in this module's own vocabulary:

* :class:`TenantContextAnswer` — the verified subject and its effective tenant,
  reduced to plain strings;
* :class:`TenantContextRefusal` — IS-001 did not establish a context;
  ``reason_code`` is the published code it stated, or ``None`` when it stated
  nothing this component can read. Either way no saga is created: a missing
  tenant context is never filled in by a guess.

The reason vocabulary mirrors the published contract of IS-001
(``components/identity/contract``). It is a mirror, not an import: no module of
this component imports ``identity_service``, so the dependency is on the
published contract and a composition root may wire a remote client, a stub or a
future implementation, as long as it answers the documented contract.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "KNOWN_TENANT_CONTEXT_DENIALS",
    "TenantContextAnswer",
    "TenantContextOutcome",
    "TenantContextRefusal",
]

#: The published denial reasons of IS-001, mirrored from its component contract.
KNOWN_TENANT_CONTEXT_DENIALS: frozenset[str] = frozenset(
    {
        "missing_identity",
        "invalid_identity",
        "unknown_identity",
        "missing_tenant_context",
        "tenant_mismatch",
        "tenant_suspended",
        "tenant_deleted",
        "tenant_deletion_requested",
        "tenant_not_active",
        "tenant_unknown",
        "platform_ownership_mismatch",
        "insufficient_authorization",
        "unknown_resource",
        "idempotency_conflict",
    }
)


@dataclass(frozen=True, slots=True)
class TenantContextAnswer:
    """The verified subject and the effective Tenant of one delivery.

    ``tenant_id`` is derived from the verified identity by IS-001; a
    caller-supplied tenant is a cross-check that can only agree with it or be
    refused. Holding this value grants nothing: it is not an authorization and
    it does not entitle the holder to any business operation.
    """

    identity_id: str
    tenant_id: str
    platform_id: str
    kind: str = "HUMAN"


@dataclass(frozen=True, slots=True)
class TenantContextRefusal:
    """IS-001 did not establish a tenant context for the presented credential.

    Also used for an answer outside the published contract: a non-authoritative
    answer is the same fact as no answer — no saga is created either way.
    """

    reason_code: str | None = None


#: What the tenant-context port returns: an answer, or the reason there is none.
TenantContextOutcome = TenantContextAnswer | TenantContextRefusal
