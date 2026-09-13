"""Behavioral proof of the Booking component boundary (SCS-003, Issue #57).

No module of ``booking_service`` imports another component's service
package — in particular neither ``commerce_service`` nor
``learning_service`` (ADR-0014 §11): the published clients of IS-001 and
IS-003 are adapted at the composition boundary and only values owned by
this component reach the enforcement logic. This module proves the
property by importing the whole package in a fresh interpreter and
inspecting what came with it — ``idempotency`` is the one allowed library
surface (IS-005 guard), everything else of another component must be
absent. It also proves the vocabulary gates: no policy/grant logic inside
Booking, no non-goal concept (pricing, payment, capacity, recurrence,
waitlist, notification, calendar sync), a published client of exactly the
seven declared operations, and no sleeps in the Booking tests.

This module also defines the stub decision port and the standalone
deployment helper used by the contract tests.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from booking_service.consumed import DecisionAnswer
from booking_service.deployment import BookingDeployment
from booking_service.deployment import build_deployment as build_booking
from booking_service.reader import BookingClient
from booking_service.store import BookingStore
from tests.conftest import PLATFORM_ID

#: Service packages of other components: importing any of them from
#: booking_service would be a boundary violation (LAW-04, ADR-0014 §11).
#: ``idempotency`` is deliberately absent: it is the library surface of
#: IS-005, consumed by direct import like every other component does.
FORBIDDEN_PACKAGES = frozenset(
    {
        "identity_service",
        "authorization_service",
        "tenant_authority",
        "learning_service",
        "commerce_service",
        "records_service",
        "saga",
    }
)

BOOKING_MODULES = [
    "booking_service",
    "booking_service.config",
    "booking_service.models",
    "booking_service.contracts",
    "booking_service.intervals",
    "booking_service.errors",
    "booking_service.consumed",
    "booking_service.ports",
    "booking_service.adapters",
    "booking_service.store",
    "booking_service.engine",
    "booking_service.api",
    "booking_service.reader",
    "booking_service.transport",
    "booking_service.deployment",
]

PUBLISHED_CLIENT_OPERATIONS = {
    "create_resource",
    "read_resource",
    "create_availability",
    "list_availability",
    "create_reservation",
    "read_reservation",
    "cancel_reservation",
}


class StubAuthorizationPort:
    """A decision port with a fixed outcome: a value to return or an exception.

    The booking engine must behave identically for any port answering its
    own vocabulary — this stub is how the tests prove the enforcement logic
    depends on nothing of the real provider.
    """

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def decide(
        self,
        subject_credential,
        *,
        operation,
        resource_type,
        resource_id,
        resource_tenant_id,
        claimed_tenant_id=None,
        request_id=None,
        correlation_id=None,
    ):
        self.calls.append(
            {
                "operation": operation,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_tenant_id": resource_tenant_id,
                "claimed_tenant_id": claimed_tenant_id,
            }
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def booking_deployment(
    port: Any,
    *,
    store: BookingStore | None = None,
    seed_demo: bool = True,
) -> BookingDeployment:
    """Standalone Booking deployment over a given decision port."""
    return build_booking(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        authorization=port,
        store=store,
        seed_demo=seed_demo and store is None,
        with_http=True,
    )


def _names_of(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return [node.name]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.arg):
        return [node.arg]
    return []


# ------------------------------------------------------- import isolation
def test_importing_booking_pulls_in_no_other_service_package():
    probe = (
        "import sys; "
        f"modules = {BOOKING_MODULES!r}; "
        "[__import__(name) for name in modules]; "
        "roots = sorted({name.split('.')[0] for name in sys.modules}); "
        "print('\\n'.join(roots))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path("src").resolve())
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    roots = set(completed.stdout.split())
    assert FORBIDDEN_PACKAGES.isdisjoint(roots), roots & FORBIDDEN_PACKAGES
    # The one allowed companion: the IS-005 library surface.
    assert "idempotency" in roots
    assert "booking_service" in roots


def test_no_module_imports_a_foreign_service_package():
    for path in sorted(Path("src/booking_service").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in FORBIDDEN_PACKAGES, (
                        path.name,
                        alias.name,
                    )
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".")[0]
                assert module not in FORBIDDEN_PACKAGES, (path.name, node.module)


def test_adapters_hold_no_provider_module_reference():
    from booking_service import adapters

    namespace = dict(vars(adapters))
    for name, value in namespace.items():
        module = getattr(value, "__module__", "")
        assert module.split(".")[0] not in FORBIDDEN_PACKAGES, (name, module)


def test_commerce_and_learning_are_not_modified_by_this_slice():
    """Booking touches neither sibling SCS: their packages import nothing of it."""
    for package in ("commerce_service", "learning_service"):
        root = Path("src") / package
        if not root.exists():
            continue
        for path in sorted(root.glob("*.py")):
            assert "booking" not in path.read_text(encoding="utf-8").lower(), path


# ------------------------------------------------------------- client surface
def test_published_client_exposes_only_the_published_operations():
    methods = {
        name
        for name, member in inspect.getmembers(BookingClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == PUBLISHED_CLIENT_OPERATIONS


def test_public_surface_matches_the_declared_consumer_surface():
    contract = json.loads(
        Path("components/booking/contract/component_contract.json").read_text(
            encoding="utf-8"
        )
    )
    declared = contract["api"]["consumer_surface"]["operations"]
    methods = {
        name
        for name, member in vars(BookingClient).items()
        if callable(member) and not name.startswith("_")
    }
    assert methods == set(declared)
    assert len(methods) == 7


def test_published_client_holds_an_opaque_handle_and_nothing_else():
    deployment = booking_deployment(
        StubAuthorizationPort(
            DecisionAnswer(
                decision="ALLOW",
                reason="permitted",
                subject_id="idn_human_a",
                tenant_id="ten_a",
            )
        )
    )
    client = deployment.publish()
    assert isinstance(client, BookingClient)
    assert isinstance(client._channel, str)
    assert client._channel
    for forbidden in ("engine", "store", "deployment", "app", "audit", "config"):
        assert not hasattr(client, forbidden), forbidden
    with pytest.raises(ValueError):
        BookingClient("")


def test_reader_module_binds_no_transport_reference():
    from booking_service import reader

    for name, value in vars(reader).items():
        assert not inspect.ismodule(value), name
        assert getattr(value, "__module__", "") != "booking_service.transport", name
    assert "_CHANNELS" not in vars(reader)
    assert "call_contract" not in vars(reader)
    assert "open_channel" not in vars(reader)


# ------------------------------------------------------------- vocabulary
def test_engine_contains_no_policy_vocabulary():
    """Booking decides nothing: no grant, policy, role or permission logic."""
    stems = ("grant", "policy", "polic", "role", "permission", "privilege")
    for module in ("engine", "store", "api", "intervals"):
        tree = ast.parse(Path(f"src/booking_service/{module}.py").read_text())
        for node in ast.walk(tree):
            for name in _names_of(node):
                lowered = name.lower()
                assert not any(stem in lowered for stem in stems), (module, name)


def test_no_non_goal_concept_exists_in_booking():
    """ADR-0014 §12 non-goals and foreign domains are absent by name."""
    stems = (
        "price",
        "pricing",
        "payment",
        "pay_",
        "invoice",
        "capacity",
        "quantity",
        "recurr",
        "waitlist",
        "notif",
        "calendar",
        "icalendar",
        "external_sync",
        "checkin",
        "check_in",
        "noshow",
        "no_show",
        "product",
        "offer",
        "cart",
        "order",
        "course",
        "lesson",
        "enroll",
        "customer",
        "profile",
        "provider",
        "gateway",
        "saga",
        "search",
        "paginat",
    )
    package = Path("src/booking_service")
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for name in _names_of(node):
                lowered = name.lower()
                assert not any(stem in lowered for stem in stems), (path.name, name)


def test_store_offers_no_slot_or_occupancy_model():
    """Availability is a window, not slots; the store has no slot concept."""
    text = Path("src/booking_service/store.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        for name in _names_of(node):
            assert "slot" not in name.lower(), name


# ---------------------------------------------------------------- quality
def test_booking_tests_use_no_sleeps_or_wall_clock_waits():
    needles = ["sl" + "eep(", "time" + ".time(", "datetime" + ".now(", "utc" + "now("]
    for path in sorted(Path("tests").glob("test_booking_*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, (path.name, needle)
