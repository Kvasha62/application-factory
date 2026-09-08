"""Behavioral proof of the Tenant Authority component boundary (IS-002 review).

A consumer is handed exactly one object: ``TenantAuthorityClient``, published by
the composition root from the component's contract application and speaking that
contract in process (Level 0). These tests attack the real object at runtime —
enumerating its attributes, calling everything it offers, walking its state,
dropping the objects it was built from, holding a second credential — and assert
that no store handle, audit journal, mutation operation, lifecycle or idempotency
machinery and no engine reference becomes reachable.

No test here reads or searches source text: every assertion is about live
objects, their types, their attributes and their behavior.
"""

from __future__ import annotations

import dataclasses
import gc
import importlib
import importlib.util
import inspect
import pytest
from fastapi import FastAPI

from identity_service.ports import TenantAuthorityPort
from tenant_authority import contracts, errors, reader, transport
from tenant_authority.deployment import TenantAuthorityDeployment, build_deployment
from tenant_authority.engine import TenantAuthorityEngine
from tenant_authority.models import AuditEvent, Decision, TenantRecord
from tenant_authority.reader import TenantAuthorityClient
from tenant_authority.store import TenantAuthorityStore

from tests.conftest import DEMO_CONFIG, PLATFORM_ID, monolith

CLIENT = TenantAuthorityClient

#: Names that belong to the component's internals and must not resolve on anything
#: a consumer holds — neither under their own name nor under a private alias.
FORBIDDEN_NAMES = (
    "store",
    "engine",
    "config",
    "http_app",
    "app",
    "deployment",
    "runtime",
    "tenants",
    "transitions",
    "audit",
    "audit_events",
    "idempotency",
    "services",
    "service_tokens",
    "clock",
    "create_tenant",
    "transition_tenant",
    "list_tenants",
    "get_tenant",
    "lifecycle_status",
    "transition_history",
    "verify_service_identity",
    "observability",
    "_source",
    "_engine",
    "_store",
    "_runtime",
    "_registry",
    "_audit",
    "_deployment",
    "_authority",
)

#: Actions a published read is allowed to leave in the audit journal.
READ_ACTIONS = frozenset({"tenant.read", "tenant.lifecycle"})


def published_authority() -> tuple[TenantAuthorityDeployment, TenantAuthorityClient]:
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    client = deployment.publish(
        credential="svc-token-identity", expected_platform_id=PLATFORM_ID
    )
    return deployment, client


def client_state(client: TenantAuthorityClient) -> list[object]:
    return [getattr(client, name) for name in client.__slots__]


def registry_state(deployment: TenantAuthorityDeployment) -> dict:
    store = deployment.store
    return {
        "tenants": {
            key: (record.platform_id, record.state, record.created_at, record.updated_at)
            for key, record in store.tenants.items()
        },
        "transitions": list(store.transitions),
        "idempotency": dict(store.idempotency),
        "services": dict(store.services),
        "service_tokens": dict(store.service_tokens),
    }


# --------------------------------------------------------------- the published surface
def test_published_surface_is_exactly_the_two_contract_reads():
    _, client = published_authority()

    published = {name for name in dir(client) if not name.startswith("_")}
    assert published == {"lookup", "lifecycle_decision"}

    # Each operation takes a Tenant id and optional tracing/binding parameters —
    # no actor, no permission, no state, no options object that could ask for
    # more than the one read it performs.
    for name in published:
        parameters = inspect.signature(getattr(client, name)).parameters
        assert set(parameters) == {
            "tenant_id",
            "expected_platform_id",
            "request_id",
            "correlation_id",
        }, name
        assert parameters["tenant_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_client_exposes_no_mutation_operation_by_any_route():
    """Nothing reachable on the client is callable in a way that writes."""
    _, client = published_authority()

    # Every callable the published object offers is one of the two reads plus the
    # client's own request helper; nothing else is even reachable by name.
    surface = {
        name: getattr(CLIENT, name)
        for name in dir(CLIENT)
        if not name.startswith("__") and callable(getattr(CLIENT, name))
    }
    assert set(surface) == {"lookup", "lifecycle_decision", "_get", "_error"}
    for name, attribute in surface.items():
        bound_to = getattr(getattr(client, name), "__self__", None)
        # A method bound to the engine or the store would hand out that object.
        assert bound_to is None or type(bound_to) is TenantAuthorityClient, name
        assert not hasattr(attribute, "store") and not hasattr(attribute, "tenants")
    assert {n for n in surface if not n.startswith("_")} == {"lookup", "lifecycle_decision"}


def test_client_object_has_no_writable_state_at_all():
    """Slots only: a consumer cannot attach an internal to the published object."""
    _, client = published_authority()

    assert not hasattr(client, "__dict__")
    assert set(client.__slots__) == {"_transport", "_credential", "_expected_platform_id"}
    with pytest.raises(AttributeError):
        client.store = object()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        client.lookup = lambda *a, **k: None  # type: ignore[method-assign]


def test_no_attribute_of_the_client_names_an_internal():
    _, client = published_authority()

    for name in FORBIDDEN_NAMES:
        with pytest.raises(AttributeError, match=name):
            getattr(client, name)

    # The client is not built from a source object: it takes a contract transport,
    # and no constructor argument accepts an engine, store or deployment.
    for smuggled in ("engine", "store", "source", "deployment", "runtime", "app"):
        with pytest.raises(TypeError):
            CLIENT(**{smuggled: object()})  # type: ignore[call-arg]


def test_client_state_holds_values_and_one_opaque_callable():
    _, client = published_authority()
    values = client_state(client)

    for value in values:
        assert isinstance(value, (str, type(None))) or inspect.isroutine(value), type(value)
        assert not isinstance(value, (TenantAuthorityEngine, TenantAuthorityStore))
        assert not isinstance(value, TenantAuthorityDeployment)
    assert not any(hasattr(value, "__self__") for value in values)
    # The transport is the published factory's product and carries no attributes
    # of its own beyond the call itself.
    callables = [value for value in values if callable(value)]
    assert len(callables) == 1
    assert {n for n in dir(callables[0]) if not n.startswith("__")} == set()


def test_internals_are_unreachable_through_every_attribute_path():
    """Walk every non-dunder attribute path reachable from the consumer object."""
    deployment, client = published_authority()
    internal_types = (TenantAuthorityEngine, TenantAuthorityStore, TenantAuthorityDeployment)
    owned_types = (TenantRecord, AuditEvent)

    frontier: list[tuple[tuple[str, ...], object]] = [
        ((), value) for value in client_state(client)
    ]
    visited = 0
    while frontier:
        path, value = frontier.pop(0)
        visited += 1
        assert not isinstance(value, internal_types), f"{type(value).__name__} at {path}"
        assert not isinstance(value, owned_types), f"{type(value).__name__} at {path}"
        if len(path) >= 3 or isinstance(value, (str, int, float, bytes, type(None), type)):
            continue
        for name in dir(value):
            if name.startswith("_"):
                continue
            try:
                child = getattr(value, name)
            except Exception:  # pragma: no cover - a broken attribute is no door
                continue
            assert name not in {
                "tenants",
                "audit",
                "idempotency",
                "service_tokens",
                "transitions",
            }, (path, name)
            if isinstance(child, (TenantAuthorityEngine, TenantAuthorityStore)):
                pytest.fail(f"internal reachable at {path + (name,)}")
            if not inspect.isroutine(child) and not isinstance(
                child, (str, int, float, bytes, list, dict, tuple, set, type(None))
            ):
                frontier.append((path + (name,), child))
    assert visited > 0


def test_contract_reads_work_from_inside_a_running_event_loop():
    """The transport must not depend on being called from a synchronous thread."""
    import asyncio

    _, client = published_authority()

    async def main() -> list[str]:
        first = await asyncio.to_thread(client.lookup, "ten_a")
        second = await asyncio.to_thread(client.lifecycle_decision, "ten_b")
        return [first.tenant_id, second.tenant_id]

    assert asyncio.run(main()) == ["ten_a", "ten_b"]


def test_calling_everything_the_surface_offers_changes_no_registry_state():
    deployment, client = published_authority()
    before = registry_state(deployment)

    attempts = ((), ("ten_a",), ("ten_a", "active"))
    keyword_attempts = (
        {},
        {"state": "active"},
        {"to_state": "deleted"},
        {"expected_platform_id": PLATFORM_ID},
        {"tenant_id": "ten_a", "idempotency_key": "ik-1"},
        {"reason": "promote me"},
    )
    for name in sorted(n for n in dir(client) if not n.startswith("__")):
        attribute = getattr(client, name)
        if not callable(attribute):
            continue
        for args in attempts:
            for kwargs in keyword_attempts:
                try:
                    attribute(*args, **kwargs)
                except Exception:
                    pass

    assert registry_state(deployment) == before
    actions = {event.action for event in deployment.store.audit}
    assert actions and actions <= READ_ACTIONS
    # Reads never invent a lifecycle history either.
    assert deployment.store.transitions == []
    assert deployment.store.idempotency == {}


def test_values_cross_the_boundary_not_live_records():
    deployment, client = published_authority()

    first = client.lookup("ten_a")
    second = client.lookup("ten_a")
    assert first == second and first is not second
    assert type(first) is contracts.TenantSnapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.state = contracts.TenantState.DELETED  # type: ignore[misc]
    assert registry_state(deployment)["tenants"]["ten_a"][1] is contracts.TenantState.ACTIVE

    decision = client.lifecycle_decision("ten_suspended")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.permitted = True  # type: ignore[misc]
    assert decision.permitted is False and decision.reason == "tenant_suspended"


def test_client_keeps_working_after_the_composition_root_is_gone():
    """The consumer depends on the contract, not on a live component object."""
    deployment, client = published_authority()
    expected = client.lookup("ten_a")
    del deployment
    gc.collect()

    assert client.lookup("ten_a") == expected


# ------------------------------------------------------------------ per-hop identity
def test_each_consumer_is_authenticated_and_authorized_for_itself():
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    identity_client = deployment.publish(
        credential="svc-token-identity", expected_platform_id=PLATFORM_ID
    )
    admin_client = deployment.publish(
        credential="svc-token-admin", expected_platform_id=PLATFORM_ID
    )
    revoked_client = deployment.publish(credential="svc-token-unknown")

    # Same Tenant, same values — but each hop is attributed to its own identity.
    assert admin_client.lookup("ten_a") == identity_client.lookup("ten_a")
    with pytest.raises(errors.AuthenticationDenied):
        revoked_client.lookup("ten_a")

    reads = [event for event in deployment.store.audit if event.action in READ_ACTIONS]
    assert {event.actor_id for event in reads if event.decision is Decision.ALLOW} == {
        "svc_identity",
        "svc_tenant_admin",
    }
    # Attribution comes from the checked service identity, never from a
    # caller-supplied consumer id, so a consumer cannot pick its own audit trail.
    assert all("consumer_id" not in event.details for event in reads)
    for client in (identity_client, admin_client, revoked_client):
        assert not hasattr(client, "actor") and not hasattr(client, "service_id")
        assert not hasattr(client, "token")
    # The credential is not readable back off the published surface.
    assert not hasattr(identity_client, "credential")


def test_a_full_registry_credential_gains_no_further_reach_here():
    """Even the admin credential sees only the two reads through this surface."""
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    admin_client = deployment.publish(credential="svc-token-admin")

    assert {n for n in dir(admin_client) if not n.startswith("_")} == {
        "lookup",
        "lifecycle_decision",
    }
    before = registry_state(deployment)
    with pytest.raises(TypeError):
        admin_client.lookup("ten_a", state="deleted")  # type: ignore[call-arg]
    assert registry_state(deployment) == before
    # The registry itself is still mutable — but only through the component's own
    # audited contract operation, which this surface does not offer.
    assert callable(deployment.engine.transition_tenant)


def test_foreign_tenant_is_not_disclosed_through_the_surface():
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    client = deployment.publish(credential="svc-token-identity", expected_platform_id=PLATFORM_ID)

    with pytest.raises(errors.TenantNotFound) as exc_info:
        client.lookup("ten_foreign")
    # The consumer learns only "no such Tenant of mine"; the precise ownership
    # denial stays in the authority's journal.
    assert exc_info.value.reason is errors.DenyReason.TENANT_NOT_FOUND
    assert exc_info.value.details.get("reason") != "foreign_tenant"
    with pytest.raises(errors.TenantAuthorityError):
        client.lifecycle_decision("ten_foreign")

    denials = [event for event in deployment.store.audit if event.decision is Decision.DENY]
    assert denials and {event.reason for event in denials} == {"foreign_tenant"}
    assert {event.tenant_id for event in denials} == {"ten_foreign"}
    for event in denials:
        assert event.platform_id == PLATFORM_ID


def test_platform_mismatch_is_denied_not_ignored():
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    wrong_wiring = deployment.publish(
        credential="svc-token-identity", expected_platform_id="plt_other"
    )

    with pytest.raises(errors.AuthorizationDenied) as exc_info:
        wrong_wiring.lookup("ten_a")
    assert exc_info.value.reason is errors.DenyReason.PLATFORM_MISMATCH
    assert not hasattr(exc_info.value, "record")


# ------------------------------------------------------------- the module boundary
def test_no_consumer_facing_runtime_object_exists():
    """The old runtime API is gone, not merely renamed and re-exported."""
    assert importlib.util.find_spec("tenant_authority.runtime") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("tenant_authority.runtime")

    package = importlib.import_module("tenant_authority")
    for forbidden in ("build_runtime", "TenantAuthorityRuntime", "TenantAuthorityReader"):
        assert not hasattr(package, forbidden), forbidden
    module = importlib.import_module("tenant_authority.deployment")
    assert not hasattr(module, "build_runtime")
    assert not hasattr(module, "TenantAuthorityRuntime")

    # The deployment type keeps its internals non-published: it is built by the
    # composition root and hands consumers exactly one thing — a client.
    assert "engine" in TenantAuthorityDeployment.__dataclass_fields__
    assert callable(TenantAuthorityDeployment.publish)
    assert not hasattr(TenantAuthorityDeployment, "reader")
    assert not hasattr(TenantAuthorityDeployment, "publish_reader")


def test_the_only_published_consumer_type_is_the_client():
    exported = {name for name in dir(reader) if not name.startswith("_")}
    assert "TenantAuthorityClient" in exported and "build_client" in exported
    for internal in (
        "TenantAuthorityEngine",
        "TenantAuthorityStore",
        "create_app",
        "build_deployment",
    ):
        assert internal not in exported, internal
    assert not hasattr(reader, "build_client_from_engine")
    assert not hasattr(reader, "TenantAuthorityReader")
    # A client cannot be constructed without a credential...
    with pytest.raises(ValueError):
        CLIENT(transport.asgi_transport(object()), "")
    # `build_client` speaks to the published contract application. Handing it a
    # component internal instead of the app yields no access at all: the boundary
    # fails closed rather than adapting to whatever object it was given.
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    with pytest.raises(TypeError):
        reader.build_client(deployment.engine, credential="svc-token-identity").lookup("ten_a")
    with pytest.raises(TypeError):
        reader.build_client(deployment.store, credential="svc-token-identity").lifecycle_decision(
            "ten_a"
        )


def test_identity_api_module_publishes_only_its_own_api():
    """BLOCKER-04: identity's HTTP module is not a window into Tenant Authority.

    It exports no ASGI application of any component, no deployment, no engine, no
    store and no client instance — the module owns routes and a factory that binds
    them to an engine supplied by a composition root.
    """
    identity_api = importlib.import_module("identity_service.api")
    published = {
        name: value for name, value in vars(identity_api).items() if not name.startswith("_")
    }

    assert "create_app" in published
    parameters = inspect.signature(identity_api.create_app).parameters
    assert list(parameters) == ["engine"]

    for name, value in published.items():
        assert not isinstance(value, FastAPI), f"{name} is an ASGI application"
        assert not isinstance(
            value,
            (
                TenantAuthorityEngine,
                TenantAuthorityStore,
                TenantAuthorityDeployment,
                TenantAuthorityClient,
            ),
        ), name
        assert getattr(value, "__module__", "") != "tenant_authority.deployment", name
        module = getattr(getattr(value, "__class__", None), "__module__", "") or ""
        assert not module.startswith("tenant_authority"), (name, module)

    for forbidden in (
        "tenant_authority_contract_app",
        "tenant_authority_app",
        "authority_app",
        "tenant_authority_deployment",
        "tenant_authority_client",
        "tenant_authority_runtime",
        "build_deployment",
    ):
        assert not hasattr(identity_api, forbidden), forbidden

    # The factory needs an engine from the caller: the module cannot be used to
    # obtain an application of this — or of any other — component on its own.
    with pytest.raises(TypeError):
        identity_api.create_app()  # type: ignore[call-arg]
    # No published name is a re-export from Tenant Authority, and none carries that
    # component's mutation or registry machinery, whatever module it comes from.
    authority_only = {"create_tenant", "transition_tenant", "verify_service_identity"}
    for name, value in published.items():
        origin = str(getattr(value, "__module__", "") or "")
        assert not origin.startswith("tenant_authority"), (name, origin)
        attributes = set() if isinstance(value, str) else {a for a in dir(value)}
        assert not attributes & authority_only, (name, attributes & authority_only)


def test_composed_identity_application_carries_no_tenant_authority_object():
    """The identity application's own state knows nothing about the authority app."""
    instance = monolith()
    identity_app = instance.identity_app

    # Two published APIs, two applications: identity neither owns nor re-exports
    # the authority's one.
    assert identity_app is not instance.authority_app
    assert not hasattr(identity_app.state, "tenant_authority")
    assert not hasattr(identity_app.state, "tenant_authority_deployment")

    forbidden_types = (
        TenantAuthorityEngine,
        TenantAuthorityStore,
        TenantAuthorityDeployment,
        TenantAuthorityClient,
    )
    internal_app_names = {id(instance.authority_app), id(instance.authority)}

    frontier: list[tuple[tuple[str, ...], object]] = [
        ((), value) for value in vars(identity_app).values()
    ]
    walked = 0
    while frontier:
        path, value = frontier.pop(0)
        walked += 1
        assert not isinstance(value, forbidden_types), f"{type(value).__name__} at {path}"
        assert id(value) not in internal_app_names, f"authority handle at {path}"
        if len(path) >= 2:
            continue
        for name, child in list(vars(value).items()) if hasattr(value, "__dict__") else []:
            if name.startswith("_"):
                continue
            assert not hasattr(child, "service_tokens"), path + (name,)
            assert not hasattr(child, "create_tenant"), path + (name,)
            assert not hasattr(child, "transition_tenant"), path + (name,)
            frontier.append((path + (name,), child))
    assert walked > 0

    # Routes stay inside identity: no Tenant Authority operation is served here.
    identity_paths = {r.path for r in identity_app.routes if hasattr(r, "path")}
    authority_paths = {r.path for r in instance.authority_app.routes if hasattr(r, "path")}
    identity_v1 = {path for path in identity_paths if path.startswith("/api/v1")}
    authority_v1 = {path for path in authority_paths if path.startswith("/api/v1")}
    assert identity_v1 & authority_v1 == set()
    assert identity_v1 == {"/api/v1/me", "/api/v1/records/{record_id}"}
    assert authority_v1
    assert all("tenants" in path or path == "/api/v1/lifecycle" for path in authority_v1)


def test_identity_openapi_declares_no_tenant_authority_surface():
    """What the published API contract of identity offers — and no more."""
    instance = monolith()
    client = instance.identity_client()

    document = client.get("/openapi.json").json()
    paths = set(document["paths"])
    assert paths == {"/health", "/ready", "/api/v1/me", "/api/v1/records/{record_id}"}
    schemas = set(document.get("components", {}).get("schemas", {}))
    for foreign in ("TenantOut", "TransitionOut", "LifecycleOut", "CreateIn", "TransitionIn"):
        assert foreign not in schemas, foreign
    # Nothing can be proxied through identity: the authority's paths are absent.
    response = client.get("/api/v1/tenants/ten_a")
    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"


def test_identity_port_is_satisfied_by_the_client_and_nothing_more():
    _, client = published_authority()
    assert isinstance(client, TenantAuthorityPort)
    port_methods = {
        name for name in dir(TenantAuthorityPort) if not name.startswith("_")
    }
    assert port_methods == {"lookup", "lifecycle_decision"}
    # The port asks for nothing the client does not already know at composition:
    # no consumer id, no actor, no permission parameter.
    for name in port_methods:
        parameters = inspect.signature(getattr(TenantAuthorityPort, name)).parameters
        assert set(parameters) <= {
            "self",
            "tenant_id",
            "expected_platform_id",
            "request_id",
            "correlation_id",
        }, name


def test_a_broken_contract_answer_fails_closed_instead_of_granting_access():
    """A transport fault is never read as "tenant is fine"."""

    async def not_json(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"active"})

    async def silent(scope, receive, send):
        return

    with pytest.raises(transport.ContractViolation, match="non-JSON"):
        transport.asgi_transport(not_json)("GET", "/api/v1/tenants/ten_a", [], None)

    client = CLIENT(transport.asgi_transport(silent), "svc-token-identity")
    with pytest.raises(transport.ContractViolation):
        client.lookup("ten_a")
    with pytest.raises(transport.ContractViolation):
        client.lifecycle_decision("ten_a")


def test_reader_module_reexports_no_internal_type():
    """Importing the consumer surface must not drag an internal type into scope."""
    assert not any(
        name in vars(reader) for name in ("TenantRecord", "AuditEvent", "IdempotencyRecord")
    )
    assert contracts.TENANT_STATE_SOURCE == "tenant_authority"
    # The decision a consumer receives is the component's own lifecycle verdict.
    _, client = published_authority()
    assert client.lifecycle_decision("ten_a").permitted is True
    assert client.lifecycle_decision("ten_a").reason == "permitted"
    assert errors.DenyReason.FOREIGN_TENANT.value == "foreign_tenant"
