"""The decision rules of IS-003 — a fixed chain, not a policy engine.

There is no policy DSL, no external policy engine and no rule storage here: the
chain below is the whole model, and it is deliberately short enough to read.

```text
Verified Identity            (IS-001 — verified subject + effective tenant)
      ↓
Tenant Authority             (IS-002 — may this Tenant be served right now?)
      ↓
Authorization Boundary       (this component)
   ├─ subject verified?           else DENY
   ├─ effective tenant known?     else DENY
   ├─ resource tenant == subject tenant?  else DENY
   ├─ tenant state servable?      else DENY
   └─ explicit permission?        else DENY
      ↓
ALLOW  →  enforcement by the component that owns the resource
```

Every step can only deny; nothing in the chain can turn a denial into access,
and the end of the chain is the only place that produces ``ALLOW``. The order
matters and is part of the published behaviour: a subject that is not verified
never reaches a grant lookup, and a Tenant that must not be served never
reaches one either — so a grant can never compensate for a broken precondition.

The mappings below translate the vocabularies of IS-001 and IS-002 into this
component's reason codes. They are translations, not second sources: the fact
itself is decided by the component that owns it.
"""

from __future__ import annotations

from authorization_service.contracts import Reason

#: The published order of checks. Also used by the contract tests: a change of
#: order is a change of behaviour and must be visible.
DECISION_CHAIN: tuple[str, ...] = (
    "verified_subject",
    "effective_tenant_context",
    "resource_tenant_match",
    "tenant_lifecycle",
    "explicit_permission",
)

#: IS-001 denial reason -> reason of this component.
IDENTITY_DENIALS: dict[str, Reason] = {
    "missing_identity": Reason.MISSING_IDENTITY,
    "invalid_identity": Reason.INVALID_IDENTITY,
    "unknown_identity": Reason.UNKNOWN_IDENTITY,
    "missing_tenant_context": Reason.MISSING_TENANT_CONTEXT,
    "tenant_mismatch": Reason.TENANT_MISMATCH,
    "tenant_unknown": Reason.TENANT_UNKNOWN,
    "platform_ownership_mismatch": Reason.PLATFORM_OWNERSHIP_MISMATCH,
    "tenant_not_active": Reason.TENANT_NOT_ACTIVE,
    "tenant_suspended": Reason.TENANT_SUSPENDED,
    "tenant_deletion_requested": Reason.TENANT_DELETION_REQUESTED,
    "tenant_deleted": Reason.TENANT_DELETED,
}

#: IS-002 lifecycle verdict reason -> reason of this component.
LIFECYCLE_DENIALS: dict[str, Reason] = {
    "provisioning_not_served": Reason.TENANT_NOT_ACTIVE,
    "tenant_suspended": Reason.TENANT_SUSPENDED,
    "tenant_deletion_requested": Reason.TENANT_DELETION_REQUESTED,
    "tenant_deleted": Reason.TENANT_DELETED,
}

#: IS-002 refusal reason -> reason of this component. A Tenant of another
#: Platform Instance is answered by the authority exactly like a missing one, so
#: both are reported here as a tenant that this boundary cannot serve.
TENANT_AUTHORITY_DENIALS: dict[str, Reason] = {
    "tenant_not_found": Reason.TENANT_UNKNOWN,
    "foreign_tenant": Reason.PLATFORM_OWNERSHIP_MISMATCH,
    "platform_mismatch": Reason.PLATFORM_OWNERSHIP_MISMATCH,
}


def identity_denial(reason_value: str | None) -> Reason:
    """Translate an IS-001 denial; an unknown answer denies as unavailable.

    Fail closed: a reason this component does not understand is never read as
    "the subject is fine".
    """
    if reason_value is None:
        return Reason.AUTHORITY_UNAVAILABLE
    return IDENTITY_DENIALS.get(reason_value, Reason.AUTHORITY_UNAVAILABLE)


def lifecycle_denial(reason_value: str | None) -> Reason:
    """Translate a lifecycle verdict of IS-002 into a deny reason.

    A verdict outside the published vocabulary denies as `tenant_not_active`:
    an unrecognised state is not a servable state.
    """
    if reason_value is None:
        return Reason.TENANT_NOT_ACTIVE
    return LIFECYCLE_DENIALS.get(reason_value, Reason.TENANT_NOT_ACTIVE)


def tenant_authority_denial(reason_value: str | None) -> Reason:
    """Translate a refusal of IS-002; anything unexpected fails closed."""
    if reason_value is None:
        return Reason.AUTHORITY_UNAVAILABLE
    return TENANT_AUTHORITY_DENIALS.get(reason_value, Reason.AUTHORITY_UNAVAILABLE)
