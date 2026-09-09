"""Acceptance proof of IS-003: what the boundary decides and why (Issue #12).

Every test here drives live objects through the published surfaces: a demo data
owner asks the Authorization Boundary through its contract client, and the
boundary asks IS-001 and IS-002 through theirs. No test asserts on source text.

The matrix required by the Issue is covered by this module:

* authorized subject + matching tenant + permission => ALLOW;
* tenant A -> resource of tenant B => DENY;
* authenticated subject without permission => DENY;
* missing / invalid identity => DENY;
* inactive tenant state => DENY.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from authorization_service.contracts import Decision, Reason, ResourceRef
from tenant_authority.contracts import TenantState
from tests.conftest import Refused, monolith, provision_tenant


def decision_for(instance, subject, operation, resource, **kwargs):
    return instance.authorization_client.decide(
        subject, operation=operation, resource=resource, **kwargs
    )


# --------------------------------------------------------------------- ALLOW
def test_authorized_subject_with_matching_tenant_and_permission_is_allowed():
    instance = monolith()
    answer = decision_for(
        instance,
        "token-human-a",
        "records.read",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert answer.decision is Decision.ALLOW
    assert answer.reason is Reason.PERMITTED
    assert answer.allowed is True
    # The subject and the tenant in the answer are the ones IS-001 established,
    # not the ones anybody asserted.
    assert answer.subject_id == "idn_human_a"
    assert answer.tenant_id == "ten_a"
    assert answer.decided_by == "authorization"


def test_the_same_subject_is_allowed_only_for_the_operations_it_was_granted():
    instance = monolith()
    write = decision_for(
        instance,
        "token-human-a",
        "records.write",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert write.decision is Decision.ALLOW

    # `records.admin` was granted to nobody: an operation nobody holds is denied
    # for everybody, including a subject with other permissions in that Tenant.
    admin = decision_for(
        instance,
        "token-human-a",
        "records.admin",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert admin.decision is Decision.DENY
    assert admin.reason is Reason.PERMISSION_NOT_GRANTED


# ---------------------------------------------------------- tenant isolation
def test_a_subject_of_one_tenant_never_reaches_a_resource_of_another():
    """A-004: identity tenant != resource tenant => DENY."""
    instance = monolith()
    answer = decision_for(
        instance,
        "token-human-b",  # effective tenant ten_b
        "records.read",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is Reason.RESOURCE_TENANT_MISMATCH
    # The subject holds the very same permission — in its own Tenant.
    assert (
        decision_for(
            instance,
            "token-human-b",
            "records.read",
            ResourceRef("record", "rec_b1", "ten_b"),
        ).decision
        is Decision.ALLOW
    )


def test_a_resource_without_a_stated_tenant_cannot_be_authorized():
    """An uncheckable tenant-scoped resource is denied, never treated as a wildcard."""
    instance = monolith()
    for tenant_id in (None, "", "   "):
        answer = decision_for(
            instance,
            "token-human-a",
            "records.read",
            ResourceRef("record", "rec_a1", tenant_id),
        )
        assert answer.decision is Decision.DENY, tenant_id
        assert answer.reason is Reason.RESOURCE_TENANT_UNKNOWN, tenant_id


# ------------------------------------------------- authentication != authz
def test_an_authenticated_subject_without_permission_is_denied():
    """A-005 and A-006: a verified subject in the right Tenant still needs a grant."""
    instance = monolith()
    read = decision_for(
        instance,
        "token-human-c",
        "records.read",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert read.decision is Decision.ALLOW  # this one was granted

    write = decision_for(
        instance,
        "token-human-c",
        "records.write",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert write.decision is Decision.DENY
    assert write.reason is Reason.PERMISSION_NOT_GRANTED
    assert write.subject_id == "idn_human_c"  # authenticated, and still denied


def test_a_verified_subject_with_no_grant_at_all_is_denied():
    instance = monolith()
    # A subject verified by IS-001 whose grants were never provisioned here.
    instance.authorization.store.grants.pop(("ten_a", "idn_human_a"), None)
    answer = decision_for(
        instance,
        "token-human-a",
        "records.read",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is Reason.PERMISSION_NOT_GRANTED


def test_a_service_subject_is_treated_exactly_like_a_human_one():
    """§6.2: a service uses its own checked identity; it is not privileged here."""
    instance = monolith()
    allowed = decision_for(
        instance,
        "token-service",
        "records.read",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert allowed.decision is Decision.ALLOW
    assert allowed.subject_id == "idn_service_jobs"

    denied = decision_for(
        instance,
        "token-service",
        "records.write",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert denied.decision is Decision.DENY
    assert denied.reason is Reason.PERMISSION_NOT_GRANTED


# ------------------------------------------------------- identity is required
@pytest.mark.parametrize(
    "credential, reason",
    [
        (None, Reason.MISSING_IDENTITY),
        ("", Reason.MISSING_IDENTITY),
        ("   ", Reason.MISSING_IDENTITY),
        ("token-invalid", Reason.INVALID_IDENTITY),
        ("not-a-token", Reason.INVALID_IDENTITY),
        ("token-nobody", Reason.INVALID_IDENTITY),
        ("token-unknown", Reason.UNKNOWN_IDENTITY),
    ],
)
def test_missing_or_invalid_identity_is_denied(credential, reason):
    """A-001: without a verified identity nothing is authorized."""
    instance = monolith()
    answer = decision_for(
        instance, credential, "records.read", ResourceRef("record", "rec_a1", "ten_a")
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is reason
    assert answer.subject_id is None
    assert answer.tenant_id is None


def test_a_subject_without_any_tenant_association_has_no_effective_tenant():
    instance = monolith()
    store = instance.identity.store
    lonely = next(iter(store.identities.values()))
    store.associations = {
        key: value
        for key, value in store.associations.items()
        if value.identity_id != lonely.identity_id
    }
    token = next(
        token for token, ident in store.tokens.items() if ident == lonely.identity_id
    )
    answer = decision_for(
        instance, token, "records.read", ResourceRef("record", "rec_a1", "ten_a")
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is Reason.MISSING_TENANT_CONTEXT


# --------------------------------------------------------- tenant lifecycle
@pytest.mark.parametrize(
    "state, reason",
    [
        (TenantState.PROVISIONING, Reason.TENANT_NOT_ACTIVE),
        (TenantState.SUSPENDED, Reason.TENANT_SUSPENDED),
        (TenantState.DELETION_REQUESTED, Reason.TENANT_DELETION_REQUESTED),
        (TenantState.DELETED, Reason.TENANT_DELETED),
    ],
)
def test_a_tenant_that_must_not_be_served_denies_every_ordinary_operation(
    state, reason
):
    """A-007: the lifecycle verdict of IS-002 stops the decision before the grant."""
    instance = monolith()
    tenant_id = provision_tenant(instance.authority, f"authz_{state.value}", state)

    # A subject bound to that Tenant, with the permission explicitly granted:
    # only the lifecycle state can be the cause of the denial.
    identity = instance.identity.store.identities["idn_human_a"]
    from identity_service.models import TenantAssociation

    instance.identity.store.associations[(identity.identity_id, tenant_id)] = (
        TenantAssociation(identity.identity_id, tenant_id, frozenset({"records.read"}))
    )
    instance.authorization.store.grant(tenant_id, identity.identity_id, "records.read")

    answer = decision_for(
        instance,
        "token-human-a",
        "records.read",
        ResourceRef("record", "rec_x", tenant_id),
        claimed_tenant_id=tenant_id,
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is reason


def test_an_active_tenant_that_becomes_suspended_is_denied_on_the_next_decision():
    """The verdict is read live: no snapshot survives a lifecycle transition."""
    instance = monolith()
    resource = ResourceRef("record", "rec_a1", "ten_a")
    assert decision_for(instance, "token-human-a", "records.read", resource).allowed

    instance.authority.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED
    )
    after = decision_for(instance, "token-human-a", "records.read", resource)
    assert after.decision is Decision.DENY
    assert after.reason is Reason.TENANT_SUSPENDED

    instance.authority.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.ACTIVE
    )
    assert decision_for(instance, "token-human-a", "records.read", resource).allowed


def test_a_tenant_of_another_platform_instance_is_not_served():
    """The authority answers a foreign Tenant like a missing one; the boundary denies."""
    instance = monolith()
    identity = instance.identity.store.identities["idn_human_a"]
    from identity_service.models import TenantAssociation

    instance.identity.store.associations[(identity.identity_id, "ten_foreign")] = (
        TenantAssociation(
            identity.identity_id, "ten_foreign", frozenset({"records.read"})
        )
    )
    instance.authorization.store.grant(
        "ten_foreign", identity.identity_id, "records.read"
    )

    answer = decision_for(
        instance,
        "token-human-a",
        "records.read",
        ResourceRef("record", "rec_f", "ten_foreign"),
        claimed_tenant_id="ten_foreign",
    )
    assert answer.decision is Decision.DENY
    assert answer.reason in {Reason.TENANT_UNKNOWN, Reason.PLATFORM_OWNERSHIP_MISMATCH}


# ------------------------------------------------------------- enforcement
def test_the_data_owner_enforces_the_decision_at_its_own_boundary():
    """A-008: the authority answers; the component that owns the data refuses."""
    instance = monolith()
    owner = instance.data_owner()

    assert owner.read("token-human-a", "rec_a1") == "rec_a1:ten_a"

    with pytest.raises(Refused) as exc:
        owner.read("token-human-b", "rec_a1")
    assert exc.value.decision.reason is Reason.RESOURCE_TENANT_MISMATCH

    # Nothing the authority returns can serve the record: the value carries no
    # data of the resource and no handle on the data owner's storage.
    denied = owner.decision_for("token-human-b", "rec_a1")
    assert not denied.allowed
    assert {name for name in dir(denied) if not name.startswith("_")} == {
        "allowed",
        "correlation_id",
        "decided_by",
        "decision",
        "operation",
        "reason",
        "request_id",
        "resource",
        "subject_id",
        "tenant_id",
    }


def test_a_decision_is_a_value_and_grants_nothing_by_existing():
    instance = monolith()
    answer = decision_for(
        instance,
        "token-human-a",
        "records.read",
        ResourceRef("record", "rec_a1", "ten_a"),
    )
    with pytest.raises(FrozenInstanceError):
        answer.decision = Decision.ALLOW  # type: ignore[misc]
    # A denial cannot be turned into an allowance by rebuilding it either: what a
    # consumer forges locally is not what the authority audited.
    forged = type(answer)(
        decision=Decision.ALLOW,
        reason=Reason.PERMITTED,
        operation="records.write",
        resource=ResourceRef("record", "rec_b1", "ten_b"),
    )
    assert forged.request_id is None
    audited = [
        event
        for event in instance.authorization.store.audit
        if event.reason is not None
    ]
    assert all(event.operation != "records.write" for event in audited)
