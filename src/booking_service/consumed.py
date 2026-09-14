"""Local value types for what Booking consumes from the platform boundaries.

Booking depends on exactly one fact it does not own: *whether this
verified subject may perform this operation on this tenant-scoped
resource* (IS-003). It obtains the answer through the published consumer
surface of IS-003 and stores nothing of it beyond the audit journal, so
this component contains no second authorization mechanism and no
tenant-context mechanism at all: the effective tenant of a request is
whatever the decision carries, and it exists here only for the duration
of one access.

Why local types at all: a consumed answer that kept the provider's own
classes would make the provider's internals part of this component's
code. The boundary is therefore expressed in this module's own
vocabulary:

* :class:`DecisionAnswer` — the decision of IS-003 reduced to plain strings;
* :class:`DependencyRefusal` — the dependency did not produce a usable
  answer; ``reason_code`` is the published code it stated, or ``None``
  when it stated nothing this component can read. Either way the access is
  denied: a dependency that cannot answer is never an implicit yes.

The decision vocabulary below mirrors the published decision model of
IS-003 (``components/authorization/contract``). It is a mirror, not an
import: no module of this component imports ``authorization_service``, so
the dependency is on the published contract, and a composition root may
equally wire a remote client, a stub or a future implementation, as long
as it answers the documented contract. An answer outside the mirrored
vocabulary is treated as non-authoritative and fails closed
(:mod:`booking_service.engine`).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ALLOW",
    "DENY",
    "DENY_REASONS",
    "KNOWN_DECISIONS",
    "KNOWN_REASONS",
    "PERMITTED",
    "DecisionAnswer",
    "DecisionOutcome",
    "DependencyRefusal",
    "TenantContextAnswer",
    "TenantContextOutcome",
    "TenantContextRefusal",
]

#: Decision values published by IS-003.
ALLOW = "ALLOW"
DENY = "DENY"

#: The only reason an ALLOW may carry, as published by IS-003.
PERMITTED = "permitted"

KNOWN_DECISIONS: frozenset[str] = frozenset({ALLOW, DENY})

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

DENY_REASONS: frozenset[str] = KNOWN_REASONS - {PERMITTED}


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    """The decision of IS-003 reduced to values of this component.

    Produced by the adapter for exactly one access; holding one grants
    nothing, and no operation of this component accepts one from a consumer.
    """

    decision: str
    reason: str
    subject_id: str | None = None
    tenant_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyRefusal:
    """The authorization dependency refused, or failed, to answer.

    Also used for answers outside the published contract: a non-authoritative
    answer is the same fact as no answer — the access is denied either way.
    """

    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class TenantContextAnswer:
    """The effective tenant context of one verified subject (IS-001).

    Consumed for exactly one purpose: the create-resource command has no
    pre-existing target, so the resource Tenant of its authorization question is
    the effective tenant of the verified identity — resolved here, from the
    published identity contract, and from nowhere else. The answer is a
    value of this component; it never outlives the command and is never read
    from a caller-supplied field.
    """

    identity_id: str
    tenant_id: str
    platform_id: str | None = None
    kind: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class TenantContextRefusal:
    """The identity dependency refused, or failed, to answer.

    A subject whose tenant context cannot be resolved has no effective
    tenant, and a command without an effective tenant is denied — a missing
    answer is never an implicit yes.
    """

    reason_code: str | None = None


#: What the tenant-context port returns: an answer, or the reason there is none.
TenantContextOutcome = TenantContextAnswer | TenantContextRefusal


#: What the authorization port returns: an answer, or the reason there is none.
DecisionOutcome = DecisionAnswer | DependencyRefusal
