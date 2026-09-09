"""IS-006 boundary proof: the saga owns workflow state and reaches nothing else.

Invariant 12 of Issue #18 is that a saga cannot bypass component or data-owner
boundaries or access another component's internals or data directly. These tests
prove it three ways: what the component imports at runtime, what its owned state
contains, and what its published reads can reach.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from authorization_service.store import AuthorizationStore
from identity_service.store import IdentityStore
from records_service.contracts import ResourceView
from records_service.store import RecordsStore
from saga.models import SagaState, StepState
from saga.store import SagaStore
from tenant_authority.store import TenantAuthorityStore
from tests.conftest import (
    RESERVATION_RELEASE,
    SAGA_CREDENTIAL_A,
    ReservationView,
    saga_harness,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Components whose internals this component must never import.
FOREIGN_PACKAGES = (
    "records_service",
    "authorization_service",
    "identity_service",
    "tenant_authority",
)

SAGA_MODULES = (
    "saga",
    "saga.executor",
    "saga.store",
    "saga.models",
    "saga.ports",
    "saga.consumed",
    "saga.errors",
)

STATE_DUNDERS = {"__closure__", "__func__", "__self__", "__defaults__", "__dict__"}


# ------------------------------------------------------- no foreign internals
def test_the_component_imports_no_business_component_at_runtime():
    """A fresh interpreter importing every saga module pulls in no foreign component."""
    program = (
        "import sys;"
        f"import {','.join(SAGA_MODULES)};"
        "foreign=sorted({m.split('.')[0] for m in sys.modules"
        f" if m.split('.')[0] in {FOREIGN_PACKAGES!r}}});"
        "print(foreign);"
        "print(sorted({m.split('.')[0] for m in sys.modules"
        " if m.split('.')[0]=='idempotency'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(REPO_ROOT / "src")},
        timeout=120,
        check=True,
    )
    foreign, idempotency = result.stdout.strip().splitlines()
    assert foreign == "[]", result.stdout
    # The IS-005 command-safety boundary IS a dependency — that one is required.
    assert idempotency == "['idempotency']"


def test_no_module_of_the_component_holds_a_foreign_component_object():
    import saga.consumed
    import saga.errors
    import saga.executor
    import saga.models
    import saga.ports
    import saga.store

    for module in (
        saga,
        saga.consumed,
        saga.errors,
        saga.executor,
        saga.models,
        saga.ports,
        saga.store,
    ):
        for name, value in vars(module).items():
            origin = getattr(value, "__module__", "") or ""
            assert not origin.startswith(FOREIGN_PACKAGES), (
                module.__name__,
                name,
                origin,
            )


def test_the_only_cross_component_surface_used_is_the_published_is_005_guard():
    from idempotency.guard import IdempotencyGuard

    harness = saga_harness()
    assert isinstance(harness.guard, IdempotencyGuard)
    # The saga hands every effect to that guard; it owns no key table itself.
    assert set(vars(SagaStore())) <= {"max_sagas", "sagas", "audit", "_lock"}


# ------------------------------------------------- owned state: workflow only
def test_the_owned_state_of_a_completed_workflow_holds_no_business_data():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.records_step("B", "rec_a1"),
    )
    harness.start(definition, saga_id="sag_own")
    harness.executor.run("sag_own", subject_credential=SAGA_CREDENTIAL_A)
    assert harness.reservations.applied_reserves == 1

    store = harness.executor.store
    record = store.get("sag_own")
    # The saga's own state: context, workflow states, failures, audit journal.
    # The step declaration is the caller's object and is deliberately excluded —
    # it is what a workflow is, not data the saga owns about a business object.
    owned = [
        record.context,
        record.state,
        *record.steps.values(),
        record.failure,
        *record.compensation_failures,
        *store.audit,
        store.snapshot("sag_own"),
    ]
    reachable = _reachable_objects(owned, prune=(record.definition,))

    # Positive control: the walk really reaches the workflow state, so the
    # assertions below are about something and not about an empty traversal.
    strings = [obj for obj in reachable if isinstance(obj, str)]
    assert "ten_a" in strings and "sag_own" in strings
    assert any(isinstance(obj, SagaState) for obj in reachable)

    assert not any(isinstance(obj, ReservationView) for obj in reachable)
    assert not any(isinstance(obj, ResourceView) for obj in reachable)
    # No business state string of either data owner was copied into the saga.
    leaked = [
        obj
        for obj in reachable
        if isinstance(obj, str)
        and obj
        in {"reserved", "available", "active", "draft", "archived", "tenant-a-secret"}
    ]
    assert not leaked, leaked


def test_published_reads_expose_workflow_state_and_no_store_handle():
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_pub")
    harness.executor.run("sag_pub", subject_credential=SAGA_CREDENTIAL_A)

    snapshot = harness.executor.snapshot("sag_pub")
    trail = harness.executor.audit_trail("sag_pub")
    reachable = _reachable_objects([snapshot, *trail])

    forbidden = (RecordsStore, AuthorizationStore, IdentityStore, TenantAuthorityStore)
    assert not any(isinstance(obj, forbidden) for obj in reachable)
    # A snapshot is a value: immutable, and granting nothing.
    with pytest.raises(Exception):
        snapshot.state = SagaState.FAILED  # type: ignore[misc]
    assert snapshot.state is SagaState.COMPLETED
    assert snapshot.step("A").state is StepState.COMPLETED


def test_the_executor_reaches_no_foreign_store_engine_or_application():
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_reach")
    harness.executor.run("sag_reach", subject_credential=SAGA_CREDENTIAL_A)

    # The step declaration is the caller's and closes over the demo data owner;
    # it is not part of what this component owns or publishes.
    record = harness.executor.store.get("sag_reach")
    reachable = _reachable_objects(
        [harness.executor], prune=(record.definition,), depth=8
    )
    forbidden = (RecordsStore, AuthorizationStore, IdentityStore, TenantAuthorityStore)
    # Positive control: the traversal reached this component's own state and the
    # IS-005 guard it delivers through — the walk is not vacuously empty.
    assert any(isinstance(obj, SagaStore) for obj in reachable)
    from idempotency.guard import IdempotencyGuard

    assert any(isinstance(obj, IdempotencyGuard) for obj in reachable)
    assert not any(isinstance(obj, forbidden) for obj in reachable)
    from fastapi import FastAPI

    assert not any(isinstance(obj, FastAPI) for obj in reachable)
    # The published clients the composition root wired in hold channel handles
    # only, which is why nothing above is reachable through them.
    assert not any(
        type(obj).__name__ in {"RecordsEngine", "AuthorizationEngine", "IdentityEngine"}
        for obj in reachable
    )


# ------------------------------- compensation runs through the same boundary
def test_a_compensation_is_authorized_at_the_data_owner_boundary():
    """Invariant 7: compensation uses the existing authorization/data-owner boundaries."""
    harness = saga_harness()
    definition = harness.definition(
        "compensation-authorization",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2", fail_before=_declined()),
    )
    harness.start(definition, saga_id="sag_comp")
    # The composition root withdraws the release permission before the undo runs:
    # the compensation must be denied by the real decision chain, not waved through.
    harness.instance.authorization.store.revoke(
        "ten_a", "idn_human_a", RESERVATION_RELEASE
    )
    final = harness.executor.run("sag_comp", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.FAILED
    assert final.step("A").state is StepState.COMPENSATION_FAILED
    assert final.step("A").failure is not None
    assert final.step("A").failure.reason == "permission_not_granted"
    assert final.step("A").failure.security_sensitive is True
    # The denied undo applied no business effect.
    assert harness.reservations.applied_releases == 0
    assert harness.reservations.states["res_a1"] == "reserved"
    assert [item.reason for item in final.compensation_failures] == [
        "permission_not_granted"
    ]


def test_a_compensation_reaches_the_data_owner_through_its_contract():
    """A successful undo is a real business operation, decided like any other access."""
    harness = saga_harness()
    definition = harness.definition(
        "compensation-authorization",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2", fail_before=_declined()),
    )
    harness.start(definition, saga_id="sag_comp2")
    final = harness.executor.run("sag_comp2", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.COMPENSATED
    assert harness.reservations.applied_releases == 1
    # ...and the decision it went through is in the authority's own journal.
    decisions = [
        event
        for event in harness.instance.authorization.store.audit
        if event.operation == RESERVATION_RELEASE
    ]
    assert decisions, "the compensation never asked the authorization boundary"
    assert decisions[0].decision.value == "ALLOW"


def _declined():
    from saga.errors import StepFailure

    return StepFailure("payment_declined")


def _reachable_objects(
    roots: list[object], *, prune=(), depth: int = 6
) -> list[object]:
    """The objects reachable from ``roots``, collected as values."""
    collected: list[object] = []
    seen: set[int] = set()
    frontier: list[tuple[int, object]] = [(0, root) for root in roots]
    while frontier:
        level, value = frontier.pop(0)
        if level > depth or id(value) in seen:
            continue
        seen.add(id(value))
        collected.append(value)
        if isinstance(value, (str, bytes, int, float, complex, type(None))):
            continue
        if any(value is skipped for skipped in prune):
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
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            if isinstance(value, Mapping):
                members.extend(list(value.keys()) + list(value.values()))
            else:
                members.extend(value)
        for member in members:
            if isinstance(member, type):
                continue
            frontier.append((level + 1, member))
    return collected
