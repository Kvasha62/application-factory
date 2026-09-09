"""The additive context contract of IS-001 (version 0.3.0, Issue #12).

The Authorization Boundary must not invent identity verification or tenant
derivation, so IS-001 publishes one read that answers both questions. These
tests check that the addition behaves like the rest of the component: the
effective tenant still comes from the verified identity, a caller-supplied
``tenant_id`` is still only a cross-check, denials are still audited, and the
read grants nothing.
"""

from __future__ import annotations

import gc

import pytest

from identity_service import COMPONENT_VERSION
from identity_service import transport as identity_transport
from identity_service.contracts import (
    TENANT_CONTEXT_SOURCE,
    IdentityKind,
    VerifiedContext,
)
from identity_service.errors import AccessDenied, ContractViolation
from identity_service.models import Decision, DenyReason
from identity_service.reader import IdentityContextClient, build_client
from tenant_authority.contracts import TenantState
from tests.conftest import PLATFORM_ID, monolith


def test_the_context_read_returns_the_verified_subject_and_effective_tenant():
    instance = monolith()
    context = instance.identity_context_client.resolve_context("token-human-a")
    assert isinstance(context, VerifiedContext)
    assert context.identity_id == "idn_human_a"
    assert context.kind is IdentityKind.HUMAN
    assert context.tenant_id == "ten_a"
    assert context.platform_id == PLATFORM_ID
    assert context.source == TENANT_CONTEXT_SOURCE == "verified_identity"


def test_the_claimed_tenant_is_a_cross_check_only():
    instance = monolith()
    client = instance.identity_context_client
    assert (
        client.resolve_context("token-human-a", claimed_tenant_id="ten_a").tenant_id
        == "ten_a"
    )
    with pytest.raises(AccessDenied) as exc:
        client.resolve_context("token-human-a", claimed_tenant_id="ten_b")
    assert exc.value.reason is DenyReason.TENANT_MISMATCH


@pytest.mark.parametrize(
    "credential, reason",
    [
        (None, DenyReason.MISSING_IDENTITY),
        ("", DenyReason.MISSING_IDENTITY),
        ("token-invalid", DenyReason.INVALID_IDENTITY),
        ("token-unknown", DenyReason.UNKNOWN_IDENTITY),
    ],
)
def test_an_unverifiable_credential_gets_no_context(credential, reason):
    instance = monolith()
    with pytest.raises(AccessDenied) as exc:
        instance.identity_context_client.resolve_context(credential)
    assert exc.value.reason is reason


def test_the_context_read_is_audited_with_the_caller_request_context():
    instance = monolith()
    instance.identity_context_client.resolve_context(
        "token-human-a", request_id="req-ctx", correlation_id="cor-ctx"
    )
    allowed = [
        event
        for event in instance.identity.store.audit
        if event.action == "identity.context" and event.request_id == "req-ctx"
    ]
    assert allowed and allowed[-1].decision is Decision.ALLOW
    assert allowed[-1].correlation_id == "cor-ctx"

    with pytest.raises(AccessDenied):
        instance.identity_context_client.resolve_context(
            None, request_id="req-ctx-deny"
        )
    denied = [
        event
        for event in instance.identity.store.audit
        if event.action == "identity.context" and event.request_id == "req-ctx-deny"
    ]
    assert denied and denied[-1].decision is Decision.DENY
    assert denied[-1].reason == DenyReason.MISSING_IDENTITY.value


def test_the_context_read_grants_nothing_and_states_no_tenant_state():
    """It answers "who and where", never "may they".

    Lifecycle state is deliberately absent from the answer: the Tenant Authority
    owns it (T-004) and a consumer must read it there, live.
    """
    instance = monolith()
    instance.authority.engine.transition_tenant(
        "svc-token-admin", "ten_a", TenantState.SUSPENDED
    )
    context = instance.identity_context_client.resolve_context("token-human-a")
    # A suspended Tenant still has a context: refusing to serve it is a decision
    # taken at an authorization boundary, not at context resolution.
    assert context.tenant_id == "ten_a"
    assert not hasattr(context, "state")
    assert not hasattr(context, "status")
    assert not hasattr(context, "permissions")


def test_the_published_client_is_value_only():
    instance = monolith()
    client = instance.identity_context_client
    assert {name for name in dir(client) if not name.startswith("_")} == {
        "resolve_context"
    }
    state = [
        getattr(client, slot) for slot in client.__slots__ if not slot.startswith("__")
    ]
    assert state == [client._channel]
    assert all(isinstance(value, str) for value in state)
    for forbidden in ("store", "engine", "app", "audit", "read_record", "write_record"):
        assert not hasattr(client, forbidden), forbidden


def test_a_dropped_context_client_revokes_its_channel():
    instance = monolith()
    before = identity_transport.channel_count()
    client = build_client(instance.identity_app)
    assert identity_transport.channel_count() == before + 1
    handle = client._channel
    del client
    gc.collect()
    assert identity_transport.channel_count() == before
    with pytest.raises(ContractViolation):
        identity_transport.call_contract(handle, "GET", "/api/v1/context", (), None)


def test_a_broken_identity_answer_fails_closed():
    async def not_json(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"idn_human_a"})

    channel = identity_transport.open_channel(not_json)
    try:
        with pytest.raises(ContractViolation):
            IdentityContextClient(channel).resolve_context("token-human-a")
    finally:
        identity_transport.close_channel(channel)


def test_the_addition_is_additive_and_versioned():
    """0.2.0 -> 0.3.0: a new operation, no change to the existing ones."""
    instance = monolith()
    http = instance.identity_client()
    assert COMPONENT_VERSION == "0.3.0"
    assert (
        http.get(
            "/api/v1/me", headers={"Authorization": "Bearer token-human-a"}
        ).status_code
        == 200
    )
    assert (
        http.get(
            "/api/v1/records/rec_a1", headers={"Authorization": "Bearer token-human-a"}
        ).status_code
        == 200
    )
    context = http.get(
        "/api/v1/context", headers={"Authorization": "Bearer token-human-a"}
    )
    assert context.status_code == 200
    assert set(context.json()) == {
        "identity_id",
        "kind",
        "subject",
        "tenant_id",
        "platform_id",
        "source",
    }
    unauthenticated = http.get("/api/v1/context")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["reason"] == "missing_identity"
    mismatch = http.get(
        "/api/v1/context",
        headers={"Authorization": "Bearer token-human-a", "X-Tenant-Id": "ten_b"},
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["detail"]["reason"] == "tenant_mismatch"
