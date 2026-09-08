"""Acceptance proof E — bypass resistance (IS-004, Issue #14).

No public access path may allow the owned resource to be obtained without the
enforcement boundary. The proof is three attacks on the live objects:

* every route the published application serves refuses when the enforcement
  chain cannot produce an ALLOW — there is no path that returns resource data
  without going through the chain;
* the published consumer surface is walked like an object graph: no store,
  engine, deployment, application or transport is reachable from what a
  consumer holds, and the surface offers no operation besides the two
  enforced ones;
* the component contains no second tenant-context or authorization mechanism:
  no module of ``records_service`` imports another component, and a
  dependency that answers nothing makes every access impossible — nothing
  local can derive a tenant or a permission in its place.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi import FastAPI

from records_service import transport
from records_service.consumed import DecisionAnswer, DependencyRefusal
from records_service.deployment import RecordsDeployment
from records_service.engine import RecordsEngine
from records_service.errors import AccessRefused, ContractViolation
from records_service.reader import RecordsClient
from records_service.store import RecordsStore
from tests.conftest import CountingRecordsStore, StubAuthorizationPort, monolith, records_deployment

TOKEN_A = "token-human-a"

STATE_DUNDERS = {"__closure__", "__func__", "__self__", "__defaults__", "__dict__"}

FORBIDDEN_NAMES = (
    "store",
    "engine",
    "config",
    "http_app",
    "app",
    "deployment",
    "resources",
    "audit",
    "_store",
    "_engine",
    "_deployment",
    "_app",
    "_resources",
    "_audit",
)

INTERNAL_TYPES = (
    FastAPI,
    RecordsDeployment,
    RecordsEngine,
    RecordsStore,
    transport.ASGIContractTransport,
)


def walk_state(root: object, *, depth: int = 5) -> list[object]:
    """Every object reachable from ``root`` through attributes, containers and
    closures — the same traversal a hostile consumer could attempt."""
    seen: set[int] = set()
    found: list[object] = []
    frontier: list[tuple[int, object]] = [(0, root)]
    while frontier:
        level, value = frontier.pop(0)
        if level > depth or id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        if isinstance(value, (str, bytes, int, float, complex, type(None))):
            continue
        members: list[object] = []
        for name in dir(value):
            if name.startswith("__") and name not in STATE_DUNDERS:
                continue
            try:
                members.append(getattr(value, name))
            except Exception:
                continue
        for cell in getattr(value, "__closure__", None) or ():
            try:
                members.append(cell.cell_contents)
            except ValueError:
                continue
        if isinstance(value, Mapping) or isinstance(value, (list, tuple, set, frozenset)):
            if isinstance(value, Mapping):
                members.extend(list(value.keys()) + list(value.values()))
            else:
                members.extend(value)
        for member in members:
            if isinstance(member, type) and member is not type(None):
                continue
            frontier.append((level + 1, member))
    return found


# ---------------------------------------------------------------- route surface
def test_every_published_route_refuses_without_an_allow():
    """Walk every route of the published application: none of them can return
    resource data when the chain has no ALLOW to give (no credential here)."""
    instance = monolith()
    app = instance.records_app
    client = instance.records_http()

    resource_routes = [
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if route.path.startswith("/api/") and method in {"GET", "POST"}
    ]
    assert resource_routes, "the published surface must have resource operations"

    for method, path in resource_routes:
        concrete = path.replace("{resource_id}", "rec_a1")
        if method == "GET":
            response = client.get(concrete)
        else:
            response = client.post(concrete, json={"transition": "activate"})
        assert response.status_code in {401, 403, 404, 422}, (method, path)
        assert "resource_id" not in response.json(), (method, path)


def test_published_surface_is_exactly_the_contract_operations():
    instance = monolith()
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (route.path, method)
        for route in instance.records_app.routes
        for method in getattr(route, "methods", set())
        if route.path not in framework_routes and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    assert implemented == {
        ("/health", "GET"),
        ("/ready", "GET"),
        ("/api/v1/resources/{resource_id}", "GET"),
        ("/api/v1/resources/{resource_id}/transitions", "POST"),
    }
    # No listing, no bulk export, no store path: nothing else exists to bypass
    # the boundary with.
    assert not any(path.endswith("resources") for path, _ in implemented)


# ------------------------------------------------------------ consumer surface
def test_client_state_is_a_single_opaque_value():
    instance = monolith()
    client = instance.records_client()

    state = [getattr(client, name) for name in client.__slots__ if not name.startswith("__")]
    assert len(state) == 1
    assert isinstance(state[0], str) and state[0]

    published = {name for name in dir(client) if not name.startswith("_")}
    assert published == {"read_resource", "transition_resource"}


def test_client_object_graph_reaches_nothing_internal():
    instance = monolith()
    client = instance.records_client()

    reachable = walk_state(client) + walk_state(RecordsClient)
    for value in reachable:
        assert not isinstance(value, INTERNAL_TYPES), type(value)
    for value in reachable:
        for name in FORBIDDEN_NAMES:
            assert not (
                isinstance(value, dict) and name in value
            ), f"internal {name} reachable through the published client"


def test_revoked_channel_fails_closed():
    instance = monolith()
    client = instance.records_client()
    handle = client._channel
    transport.close_channel(handle)

    with pytest.raises(ContractViolation):
        client.read_resource(TOKEN_A, "rec_a1")


def test_an_unknown_channel_handle_is_a_closed_channel():
    client = RecordsClient("rbc-forged-handle")
    with pytest.raises(ContractViolation):
        client.read_resource(TOKEN_A, "rec_a1")


def test_importing_the_published_surface_binds_no_transport_reference():
    module = importlib.import_module("records_service.reader")
    namespace = dict(vars(module))
    assert "transport" not in namespace
    assert not any(
        value is transport or value is transport.call_contract
        for value in namespace.values()
    )


# ------------------------------------------------------------ isolation of code
def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # importlib.import_module("identity_service...") and friends
            for name in ("identity_service", "tenant_authority", "authorization_service"):
                if node.value == name or node.value.startswith(name + "."):
                    modules.add(name)
    return modules


def test_records_service_imports_no_other_component():
    """The dependency on IS-003 is on the published contract, not on code."""
    foreign = {"identity_service", "tenant_authority", "authorization_service"}
    package = Path("src/records_service")
    offenders = {}
    for path in sorted(package.glob("*.py")):
        imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        hit = foreign & imported
        if hit:
            offenders[path.name] = sorted(hit)
    assert offenders == {}

    # Control: the same scanner finds foreign imports where they legitimately
    # exist — the composition root — so the check is not trivially satisfied.
    conftest = _imported_modules(
        ast.parse(Path("tests/conftest.py").read_text(encoding="utf-8"))
    )
    assert foreign & conftest


def test_a_fresh_interpreter_imports_no_other_component():
    """Importing the whole published surface drags no other component along."""
    code = (
        "import sys, records_service.api, records_service.reader, "
        "records_service.engine, records_service.deployment, "
        "records_service.adapters, records_service.transport, "
        "records_service.store, records_service.contracts, "
        "records_service.consumed, records_service.ports, "
        "records_service.models, records_service.config, records_service.errors;"
        "foreign = [m for m in sys.modules if m.split('.')[0] in "
        "('identity_service', 'tenant_authority', 'authorization_service')];"
        "assert not foreign, foreign"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------- no second tenant mechanism
def test_a_silent_dependency_makes_every_access_impossible():
    """There is nothing local that could derive a tenant or a permission:
    when the port answers nothing, the component serves nothing — not even
    'its own' resources."""
    silent = StubAuthorizationPort(DependencyRefusal(None))
    store = CountingRecordsStore()
    deployment = records_deployment(silent, store=store)

    for resource_id in ("rec_a1", "rec_a2"):
        with pytest.raises(AccessRefused):
            deployment.engine.read_resource(TOKEN_A, resource_id)

    assert store.served_reads == 0
    assert silent.calls, "every access attempt still went to the boundary"


def test_the_resource_tenant_is_never_taken_from_the_request():
    """The question the owner asks states the store's tenant; the caller's
    claim travels separately and changes nothing about it."""
    port = StubAuthorizationPort(DependencyRefusal(None))
    deployment = records_deployment(port, store=CountingRecordsStore())

    with pytest.raises(AccessRefused):
        deployment.engine.read_resource(
            TOKEN_A, "rec_b1", claimed_tenant_id="ten_a"
        )

    question = port.calls[0]
    assert question["resource_tenant_id"] == "ten_b"  # the store's fact
    assert question["claimed_tenant_id"] == "ten_a"  # forwarded, never adopted


def test_a_foreign_owner_resource_is_never_served():
    """Defense in depth: even a resource that somehow carries another
    owner's name is refused at this boundary — and the authority is not
    even asked about it."""
    from records_service.models import OwnedResource

    port = StubAuthorizationPort(
        DecisionAnswer(
            decision="ALLOW", reason="permitted", subject_id="idn_human_a", tenant_id="ten_a"
        )
    )
    store = CountingRecordsStore()
    deployment = records_deployment(port, store=store)
    store.resources["rec_foreign"] = OwnedResource(
        resource_id="rec_foreign",
        resource_type="record",
        owner_component="identity",
        tenant_id="ten_a",
        state="active",
    )

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_foreign")

    assert refused.value.reason == "owner_mismatch"
    assert store.served_reads == 0
    assert port.calls == []
