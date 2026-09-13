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
import types
from collections.abc import Mapping
from pathlib import Path

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

#: Dunder attributes that belong to an object's reachable state, not to metadata.
STATE_DUNDERS = {"__closure__", "__func__", "__self__", "__defaults__", "__dict__"}

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
    """Every value the published object carries: its own slots, dunders excluded."""
    return [
        getattr(client, name) for name in client.__slots__ if not name.startswith("__")
    ]


#: Types whose instances a consumer must never reach through the published client.
INTERNAL_TYPES = (
    FastAPI,
    TenantAuthorityDeployment,
    TenantAuthorityEngine,
    TenantAuthorityStore,
    transport.ASGIContractTransport,
)


def walk_state(root: object, *, depth: int = 5) -> set[int]:
    """ids of everything reachable from ``root`` through attributes and closures.

    Slots, dict/typing members, bound objects, containers, ``__closure__`` cells
    (``cell_contents``), ``__func__`` and ``__self__`` are all followed. Module
    namespaces are deliberately not traversed: any code can import any module, and
    that door is shut by the consuming component's import guard plus the contract's
    ``internal_modules``, not by this object graph.
    """
    seen: set[int] = set()
    frontier: list[tuple[int, object]] = [(0, root)]
    while frontier:
        level, value = frontier.pop(0)
        if level > depth or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, (str, bytes, int, float, complex, type(None))):
            continue
        members: list[object] = []
        for name in dir(value):
            if name.startswith("__") and name not in STATE_DUNDERS:
                continue
            try:
                members.append(getattr(value, name))
            except Exception:  # a broken attribute is no door
                continue
        for cell in getattr(value, "__closure__", None) or ():
            try:
                members.append(cell.cell_contents)
            except ValueError:  # an empty cell carries nothing
                continue
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            if isinstance(value, Mapping):
                members.extend(list(value.keys()) + list(value.values()))
            else:
                members.extend(value)
        for member in members:
            if isinstance(member, type) and member is not type(None):
                continue
            frontier.append((level + 1, member))
    return seen


def registry_state(deployment: TenantAuthorityDeployment) -> dict:
    store = deployment.store
    return {
        "tenants": {
            key: (
                record.platform_id,
                record.state,
                record.created_at,
                record.updated_at,
            )
            for key, record in store.tenants.items()
        },
        "transitions": list(store.transitions),
        "idempotency": dict(store.idempotency),
        "services": dict(store.services),
        "service_tokens": dict(store.service_tokens),
    }


def published_values(root: object) -> list[object]:
    """Every attribute value on the published object and on its class."""
    values: list[object] = []
    for name in dir(root):
        if name.startswith("__") and name != "__closure__":
            continue
        try:
            values.append(getattr(root, name))
        except Exception:  # pragma: no cover - a broken attribute is no door
            continue
    return values


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
    assert {n for n in surface if not n.startswith("_")} == {
        "lookup",
        "lifecycle_decision",
    }


def test_client_object_has_no_writable_state_at_all():
    """Slots only: a consumer cannot attach an internal to the published object."""
    _, client = published_authority()

    assert not hasattr(client, "__dict__")
    # BLOCKER-05: `_transport` (an application-capturing closure) is gone; a handle
    # is all the client carries. `__weakref__` lets the provider revoke the channel
    # when the consumer drops the client, and points at the client alone.
    assert set(client.__slots__) == {
        "_channel",
        "_credential",
        "_expected_platform_id",
        "__weakref__",
    }
    reference = getattr(client, "__weakref__", None)
    assert reference is None or reference() is client
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


def test_client_state_holds_values_only_and_nothing_captures_the_application():
    """BLOCKER-05: the published client carries values, never a bound transport.

    The client used to hold the closure produced by ``asgi_transport(app)``; the
    component's application sat one ``__closure__[0].cell_contents._app`` away, so
    the boundary was a naming convention again. Its state is three strings now, and
    a string has no attributes to walk.
    """
    deployment, client = published_authority()
    app = deployment.contract_app()
    values = client_state(client)

    assert len(values) == 3
    for value in values:
        assert isinstance(value, str), type(value)
        assert not callable(value), value
        assert not hasattr(value, "__closure__")
        assert not hasattr(value, "__self__")
        assert not hasattr(value, "__func__")
        assert not hasattr(value, "cell_contents")
        assert value is not app
    assert all(not isinstance(value, INTERNAL_TYPES) for value in values)


def test_no_closure_or_cell_path_leads_to_the_application_or_deployment():
    """The leak path itself: ``__closure__`` and ``cell_contents`` are boundary too.

    Everything the consumer can hold — the client instance, its slots, its class and
    every attribute of that class — is expanded through closures, cells, bound
    objects and containers. Neither the component's ASGI application nor its
    deployment, engine or store is reachable by identity, and no value the client
    returns drags one along either.
    """
    deployment, client = published_authority()
    app = deployment.contract_app()
    forbidden_ids = {
        "contract application": id(app),
        "deployment": id(deployment),
        "engine": id(deployment.engine),
        "store": id(deployment.store),
    }
    # The walk detects what it claims to detect: rooted at the transport object the
    # client used to hold, the application is reachable — this is the path that had
    # to be removed, and removing it is what makes the assertions above mean
    # something rather than being an artefact of a blind traversal.
    assert forbidden_ids["contract application"] in walk_state(
        transport.asgi_transport(app)
    )
    for root_name, root in (("client", client), ("published class", CLIENT)):
        reached = walk_state(root)
        for label, target in forbidden_ids.items():
            assert target not in reached, f"{label} reachable from the {root_name}"
    for root in (client, CLIENT):
        for value in published_values(root):
            assert not isinstance(value, INTERNAL_TYPES), type(value).__name__
    # Read results are values of the published contract, not provider objects.
    for snapshot in (client.lookup("ten_a"), client.lifecycle_decision("ten_a")):
        assert not isinstance(snapshot, INTERNAL_TYPES)
        for field in dataclasses.fields(snapshot):
            value = getattr(snapshot, field.name)
            assert not isinstance(value, INTERNAL_TYPES), field.name
            assert id(value) not in set(forbidden_ids.values()), field.name
    # And nothing on the client is a function object any more, so there is no
    # closure to inspect in the first place.
    assert not any(callable(value) for value in client_state(client))
    assert not any(
        getattr(value, "__closure__", None) for value in published_values(CLIENT)
    )


def test_internals_are_unreachable_through_every_attribute_path():
    """Walk every attribute path the consumer object exposes — closure cells included.

    Before BLOCKER-05 this walk stopped at the client's single callable and treated
    routines as opaque, which is exactly where the component's application hid: one
    `__closure__[0].cell_contents._app` further. Private slots, `__closure__` cells,
    bound objects and container members are traversed now, so a leak cannot take
    refuge behind a leading underscore or behind a function object.
    """
    deployment, client = published_authority()
    internals = (
        FastAPI,
        TenantAuthorityDeployment,
        TenantAuthorityEngine,
        TenantAuthorityStore,
        transport.ASGIContractTransport,
    )
    owned = (TenantRecord, AuditEvent)
    targets = {
        "contract application": deployment.contract_app(),
        "deployment": deployment,
        "engine": deployment.engine,
        "store": deployment.store,
    }

    frontier: list[tuple[tuple[str, ...], object]] = [
        ((), value) for value in client_state(client)
    ]
    frontier += [
        ((f"<{name}>",), getattr(CLIENT, name))
        for name in dir(CLIENT)
        if not name.startswith("__")
    ]
    visited = 0
    while frontier:
        path, value = frontier.pop(0)
        visited += 1
        assert not isinstance(value, internals), f"{type(value).__name__} at {path}"
        assert not isinstance(value, owned), f"{type(value).__name__} at {path}"
        for label, target in targets.items():
            assert value is not target, f"{label} reachable at {path}"
        if len(path) >= 5 or isinstance(
            value, (str, bytes, int, float, complex, type(None), type)
        ):
            continue
        children: list[tuple[tuple[str, ...], object]] = []
        for name in dir(value):
            if name.startswith("_") and name not in STATE_DUNDERS:
                continue
            try:
                children.append((path + (name,), getattr(value, name)))
            except Exception:  # pragma: no cover - a broken attribute is no door
                continue
        for index, cell in enumerate(getattr(value, "__closure__", None) or ()):
            try:
                children.append((path + (f"__closure__[{index}]",), cell.cell_contents))
            except ValueError:  # an empty cell carries nothing
                continue
        if isinstance(value, Mapping):
            children += [(path + (f"[{key!r}]",), item) for key, item in value.items()]
            children += [(path + (f"key[{key!r}]",), key) for key in value]
        elif isinstance(value, (list, tuple, set, frozenset)):
            children += [
                (path + (f"[{index}]",), item) for index, item in enumerate(value)
            ]
        for child_path, child in children:
            if child is type(None):
                continue
            if isinstance(child, type):
                continue
            if isinstance(child, internals):
                pytest.fail(f"{type(child).__name__} reachable at {child_path}")
            frontier.append((child_path, child))
    assert visited > 20


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
    assert (
        registry_state(deployment)["tenants"]["ten_a"][1]
        is contracts.TenantState.ACTIVE
    )

    decision = client.lifecycle_decision("ten_suspended")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.permitted = True  # type: ignore[misc]
    assert decision.permitted is False and decision.reason == "tenant_suspended"


def test_a_dropped_client_revokes_its_channel_on_the_provider_side():
    """The provider-side channel table does not accumulate applications.

    Publishing keeps the application alive exactly as long as the consumer holds a
    client: the handle in the client's state is the only thing that crosses the
    boundary, and when the client is gone the channel is closed, so no leaked
    reference to another component's ASGI application survives the hand-off.
    """
    before = transport.channel_count()
    deployment, client = published_authority()
    assert transport.channel_count() == before + 1

    handle = client_state(client)[0]
    assert isinstance(handle, str) and handle
    del client
    gc.collect()

    # The revocation assertion is about this client's own channel — never a
    # global count: earlier tests in the suite leave dead clients behind in
    # layered cycles, so a global count cannot be exact. Per-client
    # revocation is the property that keeps the table from accumulating.
    with pytest.raises(transport.ContractViolation, match="closed"):
        transport.call_contract(handle, "GET", "/api/v1/tenants/ten_a", (), None)
    # The deployment is untouched by the revocation: it owns the application.
    assert deployment.store.tenants["ten_a"].state is contracts.TenantState.ACTIVE


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
    client = deployment.publish(
        credential="svc-token-identity", expected_platform_id=PLATFORM_ID
    )

    with pytest.raises(errors.TenantNotFound) as exc_info:
        client.lookup("ten_foreign")
    # The consumer learns only "no such Tenant of mine"; the precise ownership
    # denial stays in the authority's journal.
    assert exc_info.value.reason is errors.DenyReason.TENANT_NOT_FOUND
    assert exc_info.value.details.get("reason") != "foreign_tenant"
    with pytest.raises(errors.TenantAuthorityError):
        client.lifecycle_decision("ten_foreign")

    denials = [
        event for event in deployment.store.audit if event.decision is Decision.DENY
    ]
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
    for forbidden in (
        "build_runtime",
        "TenantAuthorityRuntime",
        "TenantAuthorityReader",
    ):
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


# ------------------------------------------------- the published module namespace
def _fresh_reader_module() -> types.ModuleType:
    """`tenant_authority.reader` imported again into a namespace of its own."""
    spec = importlib.util.spec_from_file_location(
        "tenant_authority.reader.probe", Path(reader.__file__)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reader_namespace_holds_no_transport_module_table_or_application():
    """BLOCKER-06: importing the consumer surface must not import the internals.

    The published module used to bind the internal transport module (`_transport`),
    which made `reader._transport._CHANNELS[...].._app` a working path to another
    component's ASGI application. The namespace is checked as a graph, not as a
    list of names: no module object, no table, no transport type, no application —
    and the control below shows that a namespace which did point at the transport
    would fail this test.
    """
    namespace = dict(vars(reader))
    public = {name for name in namespace if not name.startswith("_")}
    # What the module declares is exactly what a consumer may use — the declared
    # surface is a client and a builder, nothing else.
    assert set(reader.__all__) <= public, set(reader.__all__) - public
    assert {"TenantAuthorityClient", "build_client"} <= set(reader.__all__)
    # Nothing executor-shaped is declared for use: the published names are the
    # client, its builder, and the contract's values and errors.
    for name in reader.__all__:
        lowered = name.lower()
        assert "transport" not in lowered and "channel" not in lowered, name
        assert not lowered.startswith("asgi"), name
    for name, value in namespace.items():
        assert not isinstance(value, types.ModuleType), f"{name} is a module"
        assert not isinstance(
            value,
            (
                FastAPI,
                TenantAuthorityDeployment,
                TenantAuthorityEngine,
                TenantAuthorityStore,
            ),
        ), name
        origin = str(getattr(value, "__module__", "") or "")
        assert not origin.startswith("tenant_authority.transport"), (name, origin)
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            members = list(value.values()) if isinstance(value, dict) else list(value)
            for member in members + list(value):
                assert not isinstance(member, types.ModuleType), name
                assert not isinstance(member, transport.ASGIContractTransport), name
                assert not isinstance(member, FastAPI), name

    for door in ("_transport", "transport", "_CHANNELS", "ASGIContractTransport"):
        assert not hasattr(reader, door), door
    # A consumer cannot ask the published module for a channel either: opening,
    # calling, closing and enumerating channels live on the internal module only.
    for plumbing in ("open_channel", "call_contract", "close_channel", "channel_count"):
        assert not hasattr(reader, plumbing), plumbing

    # Control: the door really does live in the internal module, so the checks above
    # are a statement about `reader`, not about an empty-by-construction namespace.
    for door in (
        "_CHANNELS",
        "ASGIContractTransport",
        "asgi_transport",
        "call_contract",
    ):
        assert hasattr(transport, door), door
    assert isinstance(vars(transport)["_CHANNELS"], dict)


def test_a_fresh_import_of_the_reader_surface_leaves_no_path_to_the_application():
    """The same proof on a pristine import, plus the channel table stays private.

    The freshly imported module is exercised end to end: a client it publishes must
    really read through the contract, so a namespace cleaned by removing
    functionality could not pass.
    """
    probe = _fresh_reader_module()
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    app = deployment.contract_app()

    assert not any(
        isinstance(value, types.ModuleType) for value in vars(probe).values()
    ), sorted(n for n, v in vars(probe).items() if isinstance(v, types.ModuleType))
    for door in (
        "_transport",
        "transport",
        "_CHANNELS",
        "ASGIContractTransport",
        "asgi_transport",
    ):
        assert not hasattr(probe, door), door

    client = probe.build_client(
        app, credential="svc-token-identity", expected_platform_id=PLATFORM_ID
    )
    assert client.lookup("ten_a").tenant_id == "ten_a"
    assert client.lifecycle_decision("ten_a").permitted is True
    assert {n for n in dir(client) if not n.startswith("_")} == {
        "lookup",
        "lifecycle_decision",
    }
    assert all(
        isinstance(getattr(client, name), (str, type(None)))
        for name in client.__slots__
        if not name.startswith("__")
    )

    # And the application is not reachable through anything the published module
    # exposes — its classes, its functions, their closures or their containers.
    reached: set[int] = set()
    for value in vars(probe).values():
        if isinstance(value, types.ModuleType):  # pragma: no cover - asserted above
            continue
        reached |= walk_state(value)
    for label, target in (
        ("contract application", app),
        ("deployment", deployment),
        ("engine", deployment.engine),
        ("store", deployment.store),
    ):
        assert (
            id(target) not in reached
        ), f"{label} reachable from a fresh reader import"
    # Control: the channel table leads to the application, so a namespace that binds
    # it — which is what `reader` did before this fix — is caught by the walk above
    # rather than by a lucky choice of names to look at.
    table = types.SimpleNamespace(_CHANNELS=vars(transport)["_CHANNELS"])
    assert id(app) in walk_state(table)


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
    # The channel machinery is not re-exported by the published module either: a
    # consumer that holds the client holds values, and nothing that opens channels.
    for plumbing in (
        "open_channel",
        "call_contract",
        "close_channel",
        "asgi_transport",
        "ASGIContractTransport",
        "ContractTransport",
        "transport",
    ):
        assert not hasattr(reader, plumbing), plumbing
    # The published module holds no live object of the component at all: no
    # application, deployment, engine or store — not even inside a container.
    for name, value in vars(reader).items():
        assert not isinstance(
            value,
            (
                FastAPI,
                TenantAuthorityDeployment,
                TenantAuthorityEngine,
                TenantAuthorityStore,
            ),
        ), name
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            container = list(value.values()) if isinstance(value, dict) else list(value)
            for member in list(value) + container:
                assert not isinstance(
                    member,
                    (
                        FastAPI,
                        TenantAuthorityDeployment,
                        TenantAuthorityEngine,
                        TenantAuthorityStore,
                    ),
                ), (name, member)
    # A client cannot be constructed without a credential, and a handle must be a
    # handle: an object that merely looks like the old transport is rejected.
    with pytest.raises(ValueError):
        CLIENT(transport.asgi_transport(object()), "")
    with pytest.raises(ValueError):
        CLIENT(42, "svc-token-identity")
    with pytest.raises(ValueError):
        CLIENT("", "svc-token-identity")
    # `build_client` speaks to the published contract application. Handing it a
    # component internal instead of the app yields no access at all: the boundary
    # fails closed rather than adapting to whatever object it was given.
    deployment = build_deployment(DEMO_CONFIG, seed_demo=True, with_http=True)
    with pytest.raises(TypeError):
        reader.build_client(deployment.engine, credential="svc-token-identity").lookup(
            "ten_a"
        )
    with pytest.raises(TypeError):
        reader.build_client(
            deployment.store, credential="svc-token-identity"
        ).lifecycle_decision("ten_a")


def test_identity_api_module_publishes_only_its_own_api():
    """BLOCKER-04: identity's HTTP module is not a window into Tenant Authority.

    It exports no ASGI application of any component, no deployment, no engine, no
    store and no client instance — the module owns routes and a factory that binds
    them to an engine supplied by a composition root.
    """
    identity_api = importlib.import_module("identity_service.api")
    published = {
        name: value
        for name, value in vars(identity_api).items()
        if not name.startswith("_")
    }

    assert "create_app" in published
    parameters = inspect.signature(identity_api.create_app).parameters
    assert list(parameters) == ["engine"]

    # The module declares what it publishes, and everything it declares belongs to
    # this component: a name of another component can be neither imported for use
    # nor re-exported out.
    declared = list(identity_api.__all__)
    assert set(declared) <= set(published), set(declared) - set(published)
    for name in declared:
        value = getattr(identity_api, name)
        origin = str(getattr(value, "__module__", "") or "")
        if inspect.isclass(value) or inspect.isfunction(value):
            assert origin.startswith("identity_service."), (name, origin)
        assert not origin.startswith("tenant_authority"), (name, origin)
        assert not isinstance(value, FastAPI), (name, "declared ASGI application")

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

    # The client is the one Tenant Authority object identity is allowed to hold;
    # anything else belonging to that component — its application, deployment,
    # engine, store or transport object — is not.
    forbidden_types = (
        TenantAuthorityEngine,
        TenantAuthorityStore,
        TenantAuthorityDeployment,
        transport.ASGIContractTransport,
    )
    authority_objects = {
        "contract application": instance.authority_app,
        "deployment": instance.authority,
        "engine": instance.authority.engine,
        "store": instance.authority.store,
    }
    internal_app_names = {id(value) for value in authority_objects.values()}

    frontier: list[tuple[tuple[str, ...], object]] = [
        ((), value) for value in vars(identity_app).values()
    ]
    walked = 0
    while frontier:
        path, value = frontier.pop(0)
        walked += 1
        assert not isinstance(
            value, forbidden_types
        ), f"{type(value).__name__} at {path}"
        assert id(value) not in internal_app_names, f"authority handle at {path}"
        # No ASGI application of another component is reachable, and the published
        # client stays exactly the published client: two reads, value-only state.
        assert not (
            isinstance(value, FastAPI) and value is not identity_app
        ), f"foreign ASGI application at {path}"
        if isinstance(value, TenantAuthorityClient):
            assert {n for n in dir(value) if not n.startswith("_")} == {
                "lookup",
                "lifecycle_decision",
            }, path
            assert all(
                isinstance(getattr(value, name), (str, type(None)))
                for name in value.__slots__
                if not name.startswith("__")
            ), path
        if len(path) >= 2:
            continue
        # Private state is walked too: a `_`-prefixed attribute is where a leaked
        # handle hides, and a closure cell is where it is stored.
        own = vars(value) if hasattr(value, "__dict__") else {}
        members: list[tuple[str, object]] = list(own.items())
        for name in getattr(type(value), "__slots__", ()) or ():
            try:
                members.append((name, getattr(value, name)))
            except AttributeError:  # an unset slot carries nothing
                continue
        for name, child in members:
            assert not hasattr(child, "service_tokens"), path + (name,)
            assert not hasattr(child, "create_tenant"), path + (name,)
            assert not hasattr(child, "transition_tenant"), path + (name,)
            if name.startswith("__") and name not in STATE_DUNDERS:
                continue
            if isinstance(child, (type, str, bytes, int, float, complex, type(None))):
                continue
            frontier.append((path + (name,), child))
        for index, cell in enumerate(getattr(value, "__closure__", None) or ()):
            try:
                contents = cell.cell_contents
            except ValueError:
                continue
            if (
                isinstance(contents, forbidden_types)
                or id(contents) in internal_app_names
            ):
                pytest.fail(
                    f"authority reachable at {path + (f'__closure__[{index}]',)}"
                )
            frontier.append((path + (f"__closure__[{index}]",), contents))
    assert walked > 0

    # Where a leak would really live: every published route closes over the engine,
    # so the closures of the handlers are unpacked and searched cell by cell. The
    # walk does reach the composed state — the published client is inside it, and
    # that is the one allowed handle — and it finds nothing else of the authority.
    endpoints = [
        route.endpoint for route in identity_app.routes if hasattr(route, "endpoint")
    ]
    client_reached = 0
    for endpoint in endpoints:
        reached = walk_state(endpoint)
        if id(instance.tenant_authority_client) in reached:
            client_reached += 1
        for label, target in authority_objects.items():
            assert (
                id(target) not in reached
            ), f"{label} reachable from {endpoint.__name__}"
    assert (
        client_reached >= 1
    ), "the walk must reach the composed state to prove anything"

    # And from the handle identity holds, the same is true one level further in.
    reached = walk_state(instance.identity.tenant_authority)
    for label, target in authority_objects.items():
        assert id(target) not in reached, label

    # Routes stay inside identity: no Tenant Authority operation is served here.
    identity_paths = {r.path for r in identity_app.routes if hasattr(r, "path")}
    authority_paths = {
        r.path for r in instance.authority_app.routes if hasattr(r, "path")
    }
    identity_v1 = {path for path in identity_paths if path.startswith("/api/v1")}
    authority_v1 = {path for path in authority_paths if path.startswith("/api/v1")}
    assert identity_v1 & authority_v1 == set()
    # `/api/v1/context` is identity's own additive operation (0.3.0): it publishes
    # the verified subject and the effective tenant, and nothing of any other
    # component — no tenant registry, no lifecycle state, no authority route.
    assert identity_v1 == {
        "/api/v1/me",
        "/api/v1/context",
        "/api/v1/records/{record_id}",
    }
    assert authority_v1
    assert all(
        "tenants" in path or path == "/api/v1/lifecycle" for path in authority_v1
    )


def test_identity_openapi_declares_no_tenant_authority_surface():
    """What the published API contract of identity offers — and no more."""
    instance = monolith()
    client = instance.identity_client()

    document = client.get("/openapi.json").json()
    paths = set(document["paths"])
    assert paths == {
        "/health",
        "/ready",
        "/api/v1/me",
        "/api/v1/context",
        "/api/v1/records/{record_id}",
    }
    schemas = set(document.get("components", {}).get("schemas", {}))
    for foreign in (
        "TenantOut",
        "TransitionOut",
        "LifecycleOut",
        "CreateIn",
        "TransitionIn",
    ):
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

    # The same faults, reached through the published client instead of a transport
    # object: a closed channel and a non-answer both deny access, never allow it.
    broken = transport.open_channel(not_json)
    silent_channel = transport.open_channel(silent)
    try:
        with pytest.raises(transport.ContractViolation, match="non-JSON"):
            CLIENT(broken, "svc-token-identity").lookup("ten_a")
        client = CLIENT(silent_channel, "svc-token-identity")
        with pytest.raises(transport.ContractViolation):
            client.lookup("ten_a")
        with pytest.raises(transport.ContractViolation):
            client.lifecycle_decision("ten_a")
    finally:
        transport.close_channel(broken)
        transport.close_channel(silent_channel)

    # A revoked handle is not a fallback path either.
    with pytest.raises(transport.ContractViolation, match="closed"):
        transport.call_contract(
            silent_channel, "GET", "/api/v1/tenants/ten_a", (), None
        )
    with pytest.raises(transport.ContractViolation, match="closed"):
        CLIENT(silent_channel, "svc-token-identity").lookup("ten_a")
    # And a handle that was never issued grants nothing.
    with pytest.raises(transport.ContractViolation, match="closed"):
        transport.call_contract(
            "tac-not-a-channel", "GET", "/api/v1/tenants/ten_a", (), None
        )


def test_reader_module_reexports_no_internal_type():
    """Importing the consumer surface must not drag an internal type into scope."""
    assert not any(
        name in vars(reader)
        for name in ("TenantRecord", "AuditEvent", "IdempotencyRecord")
    )
    assert contracts.TENANT_STATE_SOURCE == "tenant_authority"
    # The decision a consumer receives is the component's own lifecycle verdict.
    _, client = published_authority()
    assert client.lifecycle_decision("ten_a").permitted is True
    assert client.lifecycle_decision("ten_a").reason == "permitted"
    assert errors.DenyReason.FOREIGN_TENANT.value == "foreign_tenant"
