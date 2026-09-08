"""Local value types for what Learning consumes from other components.

Learning depends on facts it does not own and obtains each of them through a
published contract, never through another component's internals
(ARCHITECTURE.md §1.1, §6.1, LAW-04):

* *who is the caller and which Tenant is effective* — IS-001
  (:class:`SubjectContext`, answered per request);
* *whether this verified subject may perform this operation on this
  tenant-scoped resource* — IS-003 (:class:`DecisionAnswer`, answered per
  access);
* *whether the command already happened* — IS-005, consumed as the published
  :class:`idempotency.guard.IdempotencyGuard` itself, exactly like IS-006
  does; there is no second idempotency mechanism.

Why local types at all: a consumed answer that kept the provider's own
classes would make the provider's internals part of this component's code.
The boundary is expressed in this module's own vocabulary, and a dependency
that does not answer — or answers outside the published vocabulary — is a
:class:`DependencyRefusal`, which fails closed (a dependency that cannot
answer is never an implicit yes).

The reason vocabularies below mirror the published contracts of IS-001 and
IS-003 (``components/identity/contract``,
``components/authorization/contract``). They are mirrors, not imports: no
module of this component imports ``identity_service`` or
``authorization_service``, so a composition root may equally wire a remote
client, a stub or a future implementation, as long as it answers the
documented contract.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ALLOW",
    "DENY",
    "PERMITTED",
    "KNOWN_DECISIONS",
    "KNOWN_IDENTITY_REASONS",
    "KNOWN_REASONS",
    "TENANT_CONTEXT_SOURCE",
    "ContextOutcome",
    "DecisionAnswer",
    "DecisionOutcome",
    "DependencyRefusal",
    "SubjectContext",
]

#: Where the effective tenant of a request must come from — the published
#: constant of IS-001. An answer claiming any other source is not a context.
TENANT_CONTEXT_SOURCE = "verified_identity"

#: Decision values published by IS-003.
ALLOW = "ALLOW"
DENY = "DENY"

#: The only reason an ALLOW may carry, as published by IS-003.
PERMITTED = "permitted"

KNOWN_DECISIONS: frozenset[str] = frozenset({ALLOW, DENY})

#: The published denial reasons of IS-001, mirrored from its contract.
KNOWN_IDENTITY_REASONS: frozenset[str] = frozenset(
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

#: The published reason set of IS-003, mirrored from its component contract.
#: An answer carrying anything else is non-authoritative and fails closed.
KNOWN_REASONS: frozenset[str] = frozenset(
    {
        "permitted",
        "missing_identity",
        "invalid_identity",
        "unknown_identity",
        "missing_tenant_context",
        "tenant_mismatch",
        "tenant_unknown",
        "platform_ownership_mismatch",
        "resource_tenant_unknown",
        "resource_tenant_mismatch",
        "tenant_not_active",
        "tenant_suspended",
        "tenant_deletion_requested",
        "tenant_deleted",
        "permission_not_granted",
        "authority_unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class SubjectContext:
    """The verified subject of a request and the tenant effective for it.

    Produced from the answer of IS-001 for exactly one request. It is a
    value: holding one grants nothing, and no operation of this component
    accepts one from a caller.
    """

    identity_id: str
    tenant_id: str
    kind: str | None = None
    subject: str | None = None
    platform_id: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    """The decision of IS-003 reduced to values of this component.

    Produced by the adapter for exactly one access; holding one grants
    nothing, and no operation of this component accepts one from a caller.
    """

    decision: str
    reason: str
    subject_id: str | None = None
    tenant_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyRefusal:
    """A consumed dependency refused, or failed, to answer in its contract.

    Also used for answers outside the published vocabulary: a
    non-authoritative answer is the same fact as no answer — the operation is
    refused either way, and never with a success.
    """

    reason_code: str | None = None


#: What the identity port returns: a context, or the reason there is none.
ContextOutcome = SubjectContext | DependencyRefusal

#: What the authorization port returns: an answer, or the reason there is none.
DecisionOutcome = DecisionAnswer | DependencyRefusal
