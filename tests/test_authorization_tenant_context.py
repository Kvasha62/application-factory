"""IS-003 does not create a second tenant-context mechanism (Issue #12, A-002/A-003).

The proof is behavioural and structural at once:

* behaviourally, the effective tenant of every decision is the one IS-001
  derives, and a caller-supplied ``tenant_id`` can only agree with it or be
  rejected — never select it;
* structurally, this component owns no tenant registry, no tenant state and no
  code path that could derive a tenant: remove IS-001 from the composition and
  no decision can be made at all.
"""

from __future__ import annotations

import pytest

from authorization_service.adapters import identity_port, tenant_authority_port
from authorization_service.contracts import Decision, Reason, ResourceRef
from authorization_service.deployment import build_deployment
from authorization_service.store import AuthorizationStore
from identity_service.contracts import TENANT_CONTEXT_SOURCE, VerifiedContext
from identity_service.errors import AccessDenied, ContractViolation
from identity_service.models import DenyReason
from tests.conftest import PLATFORM_ID, monolith


def resource(
    tenant_id: str | None = "ten_a", resource_id: str = "rec_a1"
) -> ResourceRef:
    return ResourceRef("record", resource_id, tenant_id)


def test_the_effective_tenant_of_a_decision_is_the_one_identity_derived():
    instance = monolith()
    context = instance.identity_context_client.resolve_context("token-human-a")
    assert context.source == TENANT_CONTEXT_SOURCE

    answer = instance.authorization_client.decide(
        "token-human-a", operation="records.read", resource=resource()
    )
    assert answer.tenant_id == context.tenant_id == "ten_a"
    assert answer.subject_id == context.identity_id


def test_a_caller_supplied_tenant_id_can_only_cross_check_never_select():
    """A-003: rewriting the claimed tenant does not move the subject anywhere."""
    instance = monolith()

    matching = instance.authorization_client.decide(
        "token-human-a",
        operation="records.read",
        resource=resource(),
        claimed_tenant_id="ten_a",
    )
    assert matching.decision is Decision.ALLOW
    assert matching.tenant_id == "ten_a"

    # A tenant the subject is not bound to: IS-001 refuses the cross-check, and
    # the boundary denies. The subject does not become a member of `ten_b`.
    foreign_claim = instance.authorization_client.decide(
        "token-human-a",
        operation="records.read",
        resource=resource("ten_b", "rec_b1"),
        claimed_tenant_id="ten_b",
    )
    assert foreign_claim.decision is Decision.DENY
    assert foreign_claim.reason is Reason.TENANT_MISMATCH
    assert foreign_claim.tenant_id is None


def test_claiming_the_resource_tenant_does_not_move_the_subject_into_it():
    """Both halves of the claim are useless: neither header nor resource decides."""
    instance = monolith()
    answer = instance.authorization_client.decide(
        "token-human-b",
        operation="records.read",
        resource=resource("ten_a", "rec_a1"),
        claimed_tenant_id="ten_a",
    )
    assert answer.decision is Decision.DENY
    # IS-001 rejects the claim first: the subject of `ten_b` never gets a context
    # in `ten_a`, so the resource check is not even the thing that saves us here.
    assert answer.reason is Reason.TENANT_MISMATCH


def test_a_subject_bound_to_several_tenants_is_decided_per_effective_tenant():
    """The claim selects among tenants the *identity* is bound to — nothing more."""
    instance = monolith()
    # `idn_human_a` is bound to ten_a and ten_suspended in the demo data.
    home = instance.authorization_client.decide(
        "token-human-a", operation="records.read", resource=resource()
    )
    assert home.tenant_id == "ten_a"

    other = instance.authorization_client.decide(
        "token-human-a",
        operation="records.read",
        resource=resource("ten_suspended", "rec_s"),
        claimed_tenant_id="ten_suspended",
    )
    # The context moved, and the lifecycle of that Tenant denies it: the decision
    # is still made against a tenant IS-001 established, not one that was claimed.
    assert other.reason is Reason.TENANT_SUSPENDED


def test_this_component_owns_no_tenant_state_and_no_tenant_registry():
    """A-002, structurally: there is nothing here that could be a second source."""
    instance = monolith()
    store = instance.authorization.store
    assert set(vars(store)) == {"grants", "services", "service_tokens", "audit"}
    # Grants mention tenant ids; they do not define which Tenants exist, nor in
    # which state they are: a grant for an unknown Tenant grants nothing.
    store.grant("ten_never_created", "idn_human_a", "records.read")
    answer = instance.authorization_client.decide(
        "token-human-a",
        operation="records.read",
        resource=resource("ten_never_created", "rec_ghost"),
        claimed_tenant_id="ten_never_created",
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is Reason.TENANT_MISMATCH


def test_without_the_identity_component_no_decision_can_be_made():
    """No fallback path exists: the tenant is not derivable in this component."""

    class SilentIdentity:
        def resolve_context(self, credential, **_):
            raise ContractViolation("identity did not answer")

    instance = monolith()
    deployment = build_deployment(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        identity=identity_port(SilentIdentity()),
        tenant_authority=tenant_authority_port(instance.tenant_authority_client),
        seed_demo=True,
        with_http=True,
    )
    client = deployment.publish(credential="authz-svc-token-records")
    answer = client.decide(
        "token-human-a", operation="records.read", resource=resource()
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is Reason.AUTHORITY_UNAVAILABLE
    assert answer.tenant_id is None


def test_an_identity_denial_is_translated_and_never_swallowed():
    """Each published IS-001 denial maps to a stable reason of this component."""

    expected = {
        DenyReason.MISSING_IDENTITY: Reason.MISSING_IDENTITY,
        DenyReason.INVALID_IDENTITY: Reason.INVALID_IDENTITY,
        DenyReason.UNKNOWN_IDENTITY: Reason.UNKNOWN_IDENTITY,
        DenyReason.MISSING_TENANT_CONTEXT: Reason.MISSING_TENANT_CONTEXT,
        DenyReason.TENANT_MISMATCH: Reason.TENANT_MISMATCH,
        DenyReason.TENANT_UNKNOWN: Reason.TENANT_UNKNOWN,
        DenyReason.PLATFORM_OWNERSHIP_MISMATCH: Reason.PLATFORM_OWNERSHIP_MISMATCH,
        DenyReason.TENANT_SUSPENDED: Reason.TENANT_SUSPENDED,
        DenyReason.TENANT_DELETED: Reason.TENANT_DELETED,
        DenyReason.TENANT_DELETION_REQUESTED: Reason.TENANT_DELETION_REQUESTED,
        DenyReason.TENANT_NOT_ACTIVE: Reason.TENANT_NOT_ACTIVE,
    }
    instance = monolith()

    for denial, reason in expected.items():

        class Denying:
            def __init__(self, denial):
                self.denial = denial

            def resolve_context(self, credential, **_):
                raise AccessDenied(self.denial)

        deployment = build_deployment(
            {"platform_id": PLATFORM_ID, "environment": "test"},
            identity=identity_port(Denying(denial)),
            tenant_authority=tenant_authority_port(instance.tenant_authority_client),
            store=AuthorizationStore(),
            seed_demo=True,
            with_http=True,
        )
        client = deployment.publish(credential="authz-svc-token-records")
        answer = client.decide(
            "token-human-a", operation="records.read", resource=resource()
        )
        assert answer.decision is Decision.DENY, denial
        assert answer.reason is reason, denial


def test_an_unknown_identity_answer_fails_closed():
    """A reason this component does not understand is never read as "fine"."""

    class Surprising:
        def resolve_context(self, credential, **_):
            raise AccessDenied(DenyReason.IDEMPOTENCY_CONFLICT)

    instance = monolith()
    deployment = build_deployment(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        identity=identity_port(Surprising()),
        tenant_authority=tenant_authority_port(instance.tenant_authority_client),
        seed_demo=True,
        with_http=True,
    )
    answer = deployment.publish(credential="authz-svc-token-records").decide(
        "token-human-a", operation="records.read", resource=resource()
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is Reason.AUTHORITY_UNAVAILABLE


def test_a_forged_context_value_does_not_come_from_the_boundary():
    """The context is obtained, never accepted: a consumer cannot hand one in."""
    instance = monolith()
    forged = VerifiedContext(
        identity_id="idn_human_b",
        kind=instance.identity.store.identities["idn_human_b"].kind,
        subject="user-b@example.test",
        tenant_id="ten_a",
        platform_id=PLATFORM_ID,
    )
    # There is no operation on the published surface that accepts a context: the
    # only way in is a credential, which is verified by IS-001.
    with pytest.raises(TypeError):
        instance.authorization_client.decide(
            "token-human-b",
            operation="records.read",
            resource=resource(),
            context=forged,  # type: ignore[call-arg]
        )
    denied = instance.authorization_client.decide(
        "token-human-b", operation="records.read", resource=resource()
    )
    assert denied.decision is Decision.DENY
