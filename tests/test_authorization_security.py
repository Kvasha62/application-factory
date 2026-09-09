"""Adversarial proofs: the Authorization Boundary fails closed (Issue #12).

The other files show that the boundary answers correctly when it is used
correctly. These check the opposite direction: what happens when someone lies,
when a dependency disappears, when a permission is taken away and when a value
is used as if it were a capability. Everything here must end in ``DENY``, in a
refusal to answer, or in an error — never in access.
"""

from __future__ import annotations

import dataclasses

import pytest

from authorization_service.contracts import (
    AuthorizationDecision,
    Decision,
    Reason,
    ResourceRef,
)
from authorization_service.errors import (
    CallerDenyReason,
    CallerNotAuthenticated,
    CallerNotAuthorized,
)
from authorization_service.store import PERM_DECIDE
from tenant_authority import transport as tenant_authority_transport
from tests.conftest import DATA_OWNER_CREDENTIAL, monolith

READ = "records.read"
WRITE = "records.write"
RECORD_A = ResourceRef("record", "rec_a1", "ten_a")


def decide(instance, credential, *, operation=READ, resource=RECORD_A, **kwargs):
    return instance.authorization_client.decide(
        credential, operation=operation, resource=resource, **kwargs
    )


def test_the_asking_component_cannot_assert_who_the_subject_is():
    """Only the credential decides the subject; a claim next to it is ignored."""
    instance = monolith()
    http = instance.authorization_http()
    response = http.post(
        "/api/v1/decisions",
        headers={
            "Authorization": f"Bearer {DATA_OWNER_CREDENTIAL}",
            # token-human-c may read, not write; the headers claim to be someone
            # who may do both.
            "X-Subject-Authorization": "Bearer token-human-c",
            "X-Subject-Id": "idn_human_a",
            "X-Identity-Id": "idn_human_a",
        },
        json={
            "operation": WRITE,
            "resource": {
                "resource_type": "record",
                "resource_id": "rec_a1",
                "tenant_id": "ten_a",
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["subject_id"] == "idn_human_c"
    assert body["decision"] == "DENY"
    assert body["reason"] == "permission_not_granted"


@pytest.mark.parametrize(
    "credential",
    [
        "TOKEN-HUMAN-A",
        "Bearer token-human-a",
        "token-human-a; token-human-b",
        "token-human-b\ttoken-human-a",
        "token-human-*",
        "token-human-a" * 100,
        "token-human-a\x00",
        "'; select 1 --",
    ],
)
def test_a_credential_that_is_not_exactly_a_valid_one_never_yields_allow(credential):
    instance = monolith()
    decision = decide(instance, credential)
    assert decision.decision is Decision.DENY
    assert not decision.allowed
    assert decision.subject_id is None


@pytest.mark.parametrize(
    "credential", ["token-human-a ", " token-human-a", "token-human-a\n"]
)
def test_padding_a_credential_with_whitespace_changes_nothing(credential):
    """Whitespace around a bearer value is HTTP framing, not part of the secret.

    The transport normalises it away, so such a request is the ordinary one from
    the same subject — it grants no more than the unpadded credential does.
    """
    instance = monolith()
    decision = decide(instance, credential)
    assert decision.subject_id == "idn_human_a"
    assert decision.tenant_id == "ten_a"
    assert decide(instance, credential, operation="records.delete").reason is (
        Reason.PERMISSION_NOT_GRANTED
    )


@pytest.mark.parametrize(
    "operation",
    [
        "records.READ",
        "records.read ",
        "records",
        "records.*",
        "*",
        "records.read.extra",
        "read",
    ],
)
def test_an_operation_is_matched_exactly_and_never_by_pattern(operation):
    """No wildcard, no prefix, no case folding: a grant covers what it names."""
    instance = monolith()
    decision = decide(instance, "token-human-a", operation=operation)
    assert decision.decision is Decision.DENY
    assert decision.reason is Reason.PERMISSION_NOT_GRANTED
    # The same subject, asking for the operation that was actually granted, passes.
    assert decide(instance, "token-human-a", operation=READ).allowed


def test_a_revoked_grant_stops_the_very_next_decision():
    """Grants are read per decision: nothing is cached and nothing is stale."""
    instance = monolith()
    assert decide(instance, "token-human-a", operation=WRITE).allowed

    instance.authorization.store.revoke("ten_a", "idn_human_a", WRITE)

    after = decide(instance, "token-human-a", operation=WRITE)
    assert after.reason is Reason.PERMISSION_NOT_GRANTED
    # Revoking one operation leaves the other grant intact.
    assert decide(instance, "token-human-a", operation=READ).allowed


def test_a_revoked_service_identity_can_no_longer_ask():
    """Losing the right to ask is a refusal to answer, not a DENY decision."""
    instance = monolith()
    assert decide(instance, "token-human-a").allowed

    instance.authorization.store.service_tokens.pop(DATA_OWNER_CREDENTIAL)
    with pytest.raises(CallerNotAuthenticated) as exc:
        decide(instance, "token-human-a")
    assert exc.value.reason is CallerDenyReason.INVALID_SERVICE_IDENTITY


def test_a_service_that_loses_its_permission_can_no_longer_ask():
    instance = monolith()
    services = instance.authorization.store.services
    services["svc_records"] = dataclasses.replace(
        services["svc_records"], permissions=frozenset()
    )

    with pytest.raises(CallerNotAuthorized) as exc:
        decide(instance, "token-human-a")
    assert exc.value.reason is CallerDenyReason.INSUFFICIENT_AUTHORIZATION
    # The refusal states the reason and nothing about the internals: which
    # permission was missing (``PERM_DECIDE``) stays inside the component.
    assert set(exc.value.details) == {"decision", "reason", "request_id"}
    assert PERM_DECIDE not in str(exc.value.details)


def test_deciding_never_changes_who_is_allowed_what():
    """Asking is a read: the grant table is identical after any sequence of asks."""
    instance = monolith()
    before = {
        key: value.operations
        for key, value in instance.authorization.store.grants.items()
    }

    for credential in (
        "token-human-a",
        "token-human-b",
        "token-human-c",
        "token-unknown",
        None,
    ):
        for operation in (READ, WRITE, "records.delete"):
            try:
                decide(instance, credential, operation=operation)
            except Exception:  # pragma: no cover - a refusal is not a mutation either
                pass

    after = {
        key: value.operations
        for key, value in instance.authorization.store.grants.items()
    }
    assert after == before


def test_a_decision_is_a_value_and_never_a_capability():
    """An ALLOW cannot be edited into a broader one, nor replayed as access."""
    instance = monolith()
    decision = decide(instance, "token-human-a")
    assert decision.allowed

    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.decision = Decision.DENY  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.tenant_id = "ten_b"  # type: ignore[misc]

    # A hand-made ALLOW is just a local object: it carries no credential and
    # opens nothing, because the data owner asks the authority, not the value.
    forged = AuthorizationDecision(
        decision=Decision.ALLOW,
        reason=Reason.PERMITTED,
        operation=WRITE,
        resource=ResourceRef("record", "rec_b1", "ten_b"),
        subject_id="idn_human_a",
        tenant_id="ten_b",
    )
    assert forged.allowed
    owner = instance.data_owner()
    real = owner.decision_for("token-human-a", "rec_b1", operation=WRITE)
    assert real.decision is Decision.DENY
    assert real.reason is Reason.RESOURCE_TENANT_MISMATCH


def test_no_credential_value_is_echoed_into_a_decision_or_the_journal():
    """Tokens travel in, never out: an audit journal is not a credential store."""
    instance = monolith()
    decide(instance, "token-human-a", request_id="req-secret")
    with pytest.raises(CallerNotAuthenticated):
        instance.authorization.engine.decide(
            "authz-svc-token-unknown-and-secret",
            operation=READ,
            resource=RECORD_A,
            subject_credential="token-human-a",
        )

    secrets = (
        "token-human-a",
        DATA_OWNER_CREDENTIAL,
        "authz-svc-token-unknown-and-secret",
    )
    for event in instance.authorization.store.audit:
        rendered = repr(
            dataclasses.asdict(event)
            if dataclasses.is_dataclass(event)
            else vars(event)
        )
        for secret in secrets:
            assert secret not in rendered, event.action

    decision = decide(instance, "token-human-a")
    assert "token" not in repr(decision)


def test_an_unreachable_tenant_authority_denies_instead_of_allowing():
    """A dependency that cannot answer is not an implicit yes (invariant 7)."""
    instance = monolith()
    assert decide(instance, "token-human-a").allowed

    # Break the published contract channel underneath the adapter: from this
    # component's side the dependency simply stops answering.
    tenant_authority_transport.close_channel(
        instance.authorization.engine.tenant_authority._client._channel
    )

    decision = decide(instance, "token-human-a", request_id="req-authority-down")
    assert decision.decision is Decision.DENY
    assert decision.reason is Reason.AUTHORITY_UNAVAILABLE
    # The outage is visible where a security-relevant denial has to be visible.
    event = instance.authorization.store.audit[-1]
    assert event.request_id == "req-authority-down"
    assert event.decision is Decision.DENY
    assert event.reason == Reason.AUTHORITY_UNAVAILABLE.value


def test_a_resource_whose_tenant_is_unstated_is_not_a_wildcard():
    instance = monolith()
    for tenant_id in (None, "", "   "):
        decision = decide(
            instance,
            "token-human-a",
            resource=ResourceRef("record", "rec_a1", tenant_id),
        )
        assert decision.decision is Decision.DENY
        assert decision.reason is Reason.RESOURCE_TENANT_UNKNOWN


def test_a_data_owner_that_misstates_its_resource_deceives_only_itself():
    """The authority answers about the resource it was told about.

    Which Tenant owns ``rec_b1`` is a fact of the data owner's own store, so
    IS-003 cannot check it — and that is exactly why enforcement stays with the
    owner (invariant 8). What the boundary guarantees instead: the statement is
    recorded, so a wrong one is attributable to the component that made it.
    """
    instance = monolith()
    lying = instance.data_owner(
        records={"rec_b1": "ten_a"}
    )  # rec_b1 really belongs to ten_b

    decision = lying.decision_for("token-human-a", "rec_b1", request_id="req-misstated")
    assert decision.allowed
    assert decision.resource.tenant_id == "ten_a"

    event = next(
        item
        for item in instance.authorization.store.audit
        if item.request_id == "req-misstated"
    )
    assert event.resource_id == "rec_b1"
    assert event.resource_tenant_id == "ten_a"
    assert event.actor_id == "svc_records"

    # And the honest owner of the same record is still refused it.
    honest = instance.data_owner()
    assert (
        honest.decision_for("token-human-a", "rec_b1").reason
        is Reason.RESOURCE_TENANT_MISMATCH
    )
