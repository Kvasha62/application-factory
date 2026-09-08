"""Behavioural proof of the IS-003 consumer boundary (Issue #12, A-008/A-010).

A consumer is handed exactly one object: ``AuthorizationClient``, published by
the composition root from the component's contract application and speaking that
contract in process (Level 0). These tests attack the real object at runtime —
enumerating its attributes, calling everything it offers, walking its state and
its closures, dropping the objects it was built from — and assert that no grant
store, audit journal, engine reference, policy object or grant mutation becomes
reachable.

No test here reads or searches source text: every assertion is about live
objects, their types, their attributes and their behaviour.
"""

from __future__ import annotations

import gc
import importlib
import inspect
import types

import pytest
from fastapi import FastAPI

from authorization_service import contracts, errors, policy, reader, transport
from authorization_service.adapters import identity_port, tenant_authority_port
from authorization_service.contracts import Decision, Reason, ResourceRef
from authorization_service.deployment import AuthorizationDeployment, build_deployment
from authorization_service.engine import AuthorizationEngine
from authorization_service.ports import IdentityContextPort, TenantAuthorityPort
from authorization_service.reader import AuthorizationClient
from authorization_service.store import AuthorizationStore
from tests.conftest import DATA_OWNER_CREDENTIAL, PLATFORM_ID, monolith

CLIENT = AuthorizationClient

STATE_DUNDERS = {"__closure__", "__func__", "__self__", "__defaults__", "__dict__"}

#: Names of this component's internals: none may resolve on anything a consumer
#: holds, neither under their own name nor under a private alias.
FORBIDDEN_NAMES = (
    "store",
    "engine",
    "config",
    "http_app",
    "app",
    "deployment",
    "grants",
    "grant",
    "revoke",
    "is_granted",
    "services",
    "service_tokens",
    "audit",
    "policy",
    "identity",
    "tenant_authority",
    "verify_service_identity",
    "observability",
    "clock",
    "_engine",
    "_store",
    "_deployment",
    "_grants",
    "_audit",
    "_policy",
    "_app",
    "_transport",
)

INTERNAL_TYPES = (
    FastAPI,
    AuthorizationDeployment,
    AuthorizationEngine,
    AuthorizationStore,
    transport.ASGIContractTransport,
)


def walk_state(root: object, *, depth: int = 5) -> set[int]:
    """ids of everything reachable from ``root`` through attributes and closures."""
    seen: set[int] = set()
    frontier: list[tuple[int, object]] = [(0, root)]
    while frontier:
        level, value = frontier.pop(0)
        if level > depth or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, types.ModuleType) or isinstance(
            value, (str, bytes, int, float, complex, bool, type(None))
        ):
            continue
        members: list[object] = []
        if hasattr(value, "__dict__"):
            members.extend(vars(value).values())
        for slot in getattr(type(value), "__slots__", ()) or ():
            try:
                members.append(getattr(value, slot))
            except AttributeError:
                continue
        for dunder in STATE_DUNDERS:
            member = getattr(value, dunder, None)
            if member is not None and dunder != "__dict__":
                members.append(member)
        for cell in getattr(value, "__closure__", None) or ():
            try:
                members.append(cell.cell_contents)
            except ValueError:
                continue
        if isinstance(value, dict):
            members.extend(value.keys())
            members.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            members.extend(value)
        for member in members:
            frontier.append((level + 1, member))
    return seen


def published() -> tuple[AuthorizationDeployment, AuthorizationClient]:
    instance = monolith()
    return instance.authorization, instance.authorization_client


# ------------------------------------------------------------------- surface
def test_the_published_surface_is_exactly_one_operation():
    _, client = published()
    assert {name for name in dir(client) if not name.startswith("_")} == {"decide"}
    parameters = inspect.signature(client.decide).parameters
    assert set(parameters) == {
        "subject_credential",
        "operation",
        "resource",
        "claimed_tenant_id",
        "request_id",
        "correlation_id",
    }


def test_the_client_offers_no_way_to_grant_read_or_revoke_a_permission():
    _, client = published()
    for name in FORBIDDEN_NAMES:
        assert not hasattr(client, name), name
        assert not hasattr(type(client), name), name
    for name in ("grants", "list_grants", "read_audit", "decisions", "subjects"):
        assert not hasattr(client, name), name


def test_the_client_holds_values_only_and_nothing_reaches_the_application():
    deployment, client = published()
    state = [getattr(client, slot) for slot in client.__slots__ if not slot.startswith("__")]
    assert state and all(isinstance(value, str) for value in state)

    reached = walk_state(client)
    for label, target in (
        ("contract application", deployment.http_app),
        ("deployment", deployment),
        ("engine", deployment.engine),
        ("store", deployment.store),
    ):
        assert id(target) not in reached, f"{label} reachable from the published client"


def test_no_attribute_path_of_the_client_leads_to_an_internal_type():
    deployment, client = published()
    frontier: list[tuple[tuple[str, ...], object]] = [((), client)]
    walked = 0
    while frontier:
        path, value = frontier.pop(0)
        walked += 1
        assert not isinstance(value, INTERNAL_TYPES), f"{type(value).__name__} at {path}"
        if len(path) >= 3:
            continue
        members: list[tuple[str, object]] = []
        if hasattr(value, "__dict__"):
            members.extend(vars(value).items())
        for slot in getattr(type(value), "__slots__", ()) or ():
            try:
                members.append((slot, getattr(value, slot)))
            except AttributeError:
                continue
        for index, cell in enumerate(getattr(value, "__closure__", None) or ()):
            try:
                members.append((f"__closure__[{index}]", cell.cell_contents))
            except ValueError:
                continue
        for name, child in members:
            if isinstance(child, (str, bytes, int, float, type(None), type)):
                continue
            frontier.append((path + (name,), child))
    assert walked > 0
    assert deployment.store.grants  # the store exists; it is simply not reachable


def test_asking_for_a_decision_changes_no_grant():
    deployment, client = published()
    before = {key: value.operations for key, value in deployment.store.grants.items()}
    client.decide(
        "token-human-a", operation="records.read", resource=ResourceRef("record", "rec_a1", "ten_a")
    )
    client.decide(
        "token-human-b", operation="records.write", resource=ResourceRef("record", "rec_a1", "ten_a")
    )
    after = {key: value.operations for key, value in deployment.store.grants.items()}
    assert after == before


def test_a_dropped_client_revokes_its_channel_on_the_provider_side():
    deployment, _ = published()
    before = transport.channel_count()
    client = deployment.publish(credential=DATA_OWNER_CREDENTIAL)
    assert transport.channel_count() == before + 1
    handle = client._channel
    del client
    gc.collect()
    assert transport.channel_count() == before
    with pytest.raises(errors.ContractViolation):
        transport.call_contract(handle, "POST", "/api/v1/decisions", (), None)


def test_a_broken_contract_answer_fails_closed_instead_of_allowing():
    async def not_json(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ALLOW"})

    async def silent(scope, receive, send):
        return

    async def wrong_shape(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"decision": "MAYBE"}'})

    for app in (not_json, silent, wrong_shape):
        channel = transport.open_channel(app)
        try:
            client = CLIENT(channel, DATA_OWNER_CREDENTIAL)
            with pytest.raises(errors.ContractViolation):
                client.decide(
                    "token-human-a",
                    operation="records.read",
                    resource=ResourceRef("record", "rec_a1", "ten_a"),
                )
        finally:
            transport.close_channel(channel)

    # A handle that was never issued grants nothing either.
    with pytest.raises(errors.ContractViolation):
        transport.call_contract("azc-not-a-channel", "POST", "/api/v1/decisions", (), None)


def test_each_consumer_is_authenticated_and_authorized_for_itself():
    deployment, _ = published()
    app = deployment.contract_app()

    allowed = reader.build_client(app, credential="authz-svc-token-records")
    assert allowed.decide(
        "token-human-a", operation="records.read", resource=ResourceRef("record", "rec_a1", "ten_a")
    ).decision is Decision.ALLOW

    for credential, expected in (
        ("authz-svc-token-reporting", errors.CallerNotAuthorized),
        ("authz-svc-token-foreign", errors.CallerNotAuthorized),
        ("authz-svc-token-unknown", errors.CallerNotAuthenticated),
        ("not-a-service-token", errors.CallerNotAuthenticated),
    ):
        client = reader.build_client(app, credential=credential)
        with pytest.raises(expected):
            client.decide(
                "token-human-a",
                operation="records.read",
                resource=ResourceRef("record", "rec_a1", "ten_a"),
            )


def test_a_client_cannot_be_built_without_a_credential_or_a_handle():
    with pytest.raises(ValueError):
        CLIENT("azc-handle", "")
    with pytest.raises(ValueError):
        CLIENT("", DATA_OWNER_CREDENTIAL)
    with pytest.raises(ValueError):
        CLIENT(42, DATA_OWNER_CREDENTIAL)  # type: ignore[arg-type]
    # Handing the builder an internal object instead of the contract app yields
    # no access at all: the boundary fails closed rather than adapting.
    instance = monolith()
    deployment = instance.authorization
    with pytest.raises(TypeError):
        reader.build_client(deployment.engine, credential=DATA_OWNER_CREDENTIAL).decide(
            "token-human-a",
            operation="records.read",
            resource=ResourceRef("record", "rec_a1", "ten_a"),
        )


# ------------------------------------------------------------------ modules
def test_the_published_module_reexports_no_internal_type():
    exported = {name for name in dir(reader) if not name.startswith("_")}
    assert "AuthorizationClient" in exported and "build_client" in exported
    for internal in (
        "AuthorizationEngine",
        "AuthorizationStore",
        "AuthorizationDeployment",
        "create_app",
        "build_deployment",
        "open_channel",
        "call_contract",
        "close_channel",
        "ASGIContractTransport",
        "transport",
        "PermissionGrant",
        "AuditEvent",
    ):
        assert internal not in exported, internal

    for name, value in vars(reader).items():
        assert not isinstance(value, INTERNAL_TYPES), name


def test_a_fresh_import_of_the_published_surface_reaches_no_application():
    instance = monolith()
    fresh = importlib.reload(importlib.import_module("authorization_service.reader"))
    namespace = types.SimpleNamespace(**{k: v for k, v in vars(fresh).items()})
    reached = walk_state(namespace)
    for label, target in (
        ("contract application", instance.authorization.http_app),
        ("deployment", instance.authorization),
        ("engine", instance.authorization.engine),
        ("store", instance.authorization.store),
    ):
        assert id(target) not in reached, f"{label} reachable from a fresh reader import"
    # Control: the channel table does lead to the application, so the walk above
    # is capable of finding one.
    table = types.SimpleNamespace(_CHANNELS=vars(transport)["_CHANNELS"])
    assert id(instance.authorization.http_app) in walk_state(table)


def test_the_component_composes_no_other_component():
    """IS-003 receives clients; it never assembles IS-001 or IS-002."""
    api = importlib.import_module("authorization_service.api")
    for name, value in vars(api).items():
        if name.startswith("_"):
            continue
        origin = str(getattr(value, "__module__", "") or "")
        assert not origin.startswith("identity_service"), (name, origin)
        assert not origin.startswith("tenant_authority"), (name, origin)
        assert not isinstance(value, FastAPI), name
    # No module-level demo application exists: a demo needs a composed Platform
    # Instance, and composing one is the job of a composition root.
    assert not hasattr(api, "app")
    deployment_module = importlib.import_module("authorization_service.deployment")
    assert not hasattr(deployment_module, "build_demo_deployment")
    parameters = inspect.signature(deployment_module.build_deployment).parameters
    assert "identity" in parameters and "tenant_authority" in parameters


def test_the_ports_ask_for_reads_and_nothing_else():
    identity_methods = {n for n in dir(IdentityContextPort) if not n.startswith("_")}
    authority_methods = {n for n in dir(TenantAuthorityPort) if not n.startswith("_")}
    assert identity_methods == {"resolve_context"}
    assert authority_methods == {"lifecycle_decision"}

    instance = monolith()
    # The engine holds this component's own adapters over the published clients:
    # one read each, and no way back into the component that answered.
    engine = instance.authorization.engine
    assert type(engine.identity).__name__ == "IdentityContextAdapter"
    assert type(engine.tenant_authority).__name__ == "TenantAuthorityAdapter"
    assert isinstance(engine.identity, IdentityContextPort)
    assert isinstance(engine.tenant_authority, TenantAuthorityPort)
    reached = walk_state(engine.identity) | walk_state(engine.tenant_authority)
    for label, target in (
        ("identity engine", instance.identity),
        ("identity store", instance.identity.store),
        ("authority deployment", instance.authority),
        ("authority store", instance.authority.store),
    ):
        assert id(target) not in reached, label


def test_the_published_vocabulary_is_closed_and_self_describing():
    assert contracts.DECISION_SOURCE == "authorization"
    assert {member.value for member in Decision} == {"ALLOW", "DENY"}
    assert Reason.PERMITTED not in contracts.DENY_REASONS
    assert len(contracts.DENY_REASONS) == len(list(Reason)) - 1
    # The chain is published, so a reordering is a visible change.
    assert policy.DECISION_CHAIN == (
        "verified_subject",
        "effective_tenant_context",
        "resource_tenant_match",
        "tenant_lifecycle",
        "explicit_permission",
    )


def test_two_platform_instances_do_not_share_a_boundary():
    instance = monolith()
    other = build_deployment(
        {"platform_id": "plt_other", "environment": "test"},
        identity=identity_port(instance.identity_context_client),
        tenant_authority=tenant_authority_port(instance.tenant_authority_client),
        seed_demo=True,
        with_http=True,
    )
    # Same subject, same resource, another Platform Instance: the Tenant belongs
    # to `plt_demo`, so a boundary serving `plt_other` cannot decide about it.
    client = other.publish(credential=DATA_OWNER_CREDENTIAL)
    answer = client.decide(
        "token-human-a",
        operation="records.read",
        resource=ResourceRef("record", "rec_a1", "ten_a"),
    )
    assert answer.decision is Decision.DENY
    assert answer.reason in {Reason.PLATFORM_OWNERSHIP_MISMATCH, Reason.TENANT_UNKNOWN}
    # Grants of one instance are not grants of the other, either.
    assert other.store is not instance.authorization.store
    assert PLATFORM_ID != other.current_platform_id
    assert (
        instance.authorization_client.decide(
            "token-human-a",
            operation="records.read",
            resource=ResourceRef("record", "rec_a1", "ten_a"),
        ).decision
        is Decision.ALLOW
    )
