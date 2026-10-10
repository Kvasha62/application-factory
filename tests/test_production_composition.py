"""The production composition root — D5 implementation tests.

The production path is: an explicit, operator-declared Layer R entry point
supplies the ``RuntimeAdapter`` and the owner-side identity evidence source, and
the composition root injects both into the shipped ``deploy`` / ``attach`` /
``reconcile`` / ``restart``. These tests exercise that path end to end against
an owner-side cell double whose code is **copied outside this repository** before
it is declared, so the ownership rules the root enforces are actually exercised:

* the runtime seam and the identity facts come from owner-side code — never from
  ``LocalProcessRuntime`` and never from the D&O-owned
  ``<runtime_root>/running_platform_identity.json`` surface;
* actual identity is the cell's own fact, obtained independently under the
  evaluation binding and compared with the expected instance — a cell that
  reports another platform, another component version, another membership, a
  different manifest, stale facts, a foreign binding, no surface at all, an
  unexpected error or something that is not a snapshot is refused, and a refused
  attempt detaches from Layer R without retiring what the cell supervises;
* ``restart`` re-executes through the production seams and ``reconcile``
  observes freshly through the owner-side provider and mutates no runtime;
* the entry point is explicit: without ``--layer-r`` there is nothing to fall
  back to, and ``--check`` reports the composition without touching anything.

What these tests are **not**: production evidence. The cell here is a double, as
every other D&O test double in this repository is, its process mechanics delegate
to the shipped member-runtime protocol implementation, and its authoritative
state is written by the test. A production claim still requires a real Running
Platform run, the acceptance seam's own evidence, and the independent D1–D8
conformance audit.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _deployment_helpers import (
    environment_for,
    instance_for,
    manifest_for,
    only_journal,
    only_operation,
    request_for,
)

from deployment_operations import (
    LIFECYCLE_FAILED,
    LIFECYCLE_IN_PROGRESS,
    RESTART_COMPLETED,
    RESTART_FAILED,
    STRICT_STOP_START_POLICY,
    Deployment,
    DeploymentEnvironment,
    DeploymentExecutionFailed,
    DeploymentOperationsError,
    DeploymentRequest,
    DeploymentStateStore,
    IdentityVerificationFailed,
    LocalProcessRuntime,
    ReconciliationRequest,
    RestartFailed,
    RestartRequest,
    RuntimeProcessError,
    StartupFailed,
    attach,
    deploy,
    derive_reconciliation_id,
    derive_restart_id,
    load_environment,
    reconcile,
    restart,
)
from deployment_operations.deployment import EVALUATION_HANDLE_PREFIX
from deployment_operations.production import (
    ProductionCompositionError,
    ProductionCompositionRoot,
    ProductionOperationRefused,
    compose_production_root,
    deployment_identity,
)
from deployment_operations.production.locking import deployment_mutation
from platform_instance import Instance, discover_root

#: The owner-side cell double. It is deliberately declared from a copy placed
#: outside the repository at run time — never from this path.
CELL_FIXTURE = Path(__file__).parent / "_layer_r_fixtures" / "cell.py"

#: Names the production wiring layer must not be able to reach for. It cannot
#: construct the local defaults because it never names them (checked below).
FORBIDDEN_NAMES = frozenset(
    {
        "LocalProcessRuntime",
        "FileRunningPlatformOwnerStateReader",
        "OwnerStateSnapshotSource",
    }
)

#: Names that would give Deployment & Operations lifecycle control over the
#: owner's runtime. The production path releases its own references with the
#: runtime contract's ``stop`` — the detach of ADR-0016 §18 — and never
#: terminates, kills, shuts down or retires what the owner supervises.
LIFECYCLE_CONTROL_NAMES = frozenset(
    {
        "terminate",
        "kill",
        "killpg",
        "SIGKILL",
        "SIGTERM",
        "shutdown",
        "retire",
    }
)

#: The shipped engine modules the production root drives with the owner's own
#: runtime seam. They are checked with the package itself: the production path
#: is the package *and* the shipped operations it invokes.
PRODUCTION_PATH_ENGINE_MODULES = (
    "deployment.py",
    "restart.py",
    "reconciliation.py",
    "state.py",
)

#: The one identity file the D&O side owns. It is a local default, not a
#: production authority, and it must not appear in this environment at all.
AMBIENT_IDENTITY_FILENAME = "running_platform_identity.json"

#: The deterministically wrong digest used to make one identity fact disagree.
WRONG_DIGEST = "sha256:" + "0" * 64


# ---------------------------------------------------------------------------
# Fixtures: one genuinely accepted Platform Instance, through the real Factory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def manifest(root: Path) -> dict[str, Any]:
    return manifest_for(root=root)


@pytest.fixture(scope="module")
def instance(manifest: dict[str, Any], root: Path) -> Instance:
    return instance_for(manifest, root=root)


def owner_state_document(
    instance: Instance,
    manifest: Mapping[str, Any],
    **overrides: Any,
) -> dict[str, Any]:
    """The cell's own authoritative state, as this owner double states it.

    It is the *cell's* document about the platform it runs — not a deployment
    record and not a copy of the request. What it states is what the canonical
    platform actually is: its identity, the manifest it realizes, its component
    membership and the identity-bearing fields it does or does not have. Tests
    edit it (or replace it) to make the owner answer differently; the expected
    instance is never handed to the owner side.
    """
    configuration = instance.document.get("configuration")
    document: dict[str, Any] = {
        "answer": "correlation",
        "platform_id": instance.document["platform_id"],
        "manifest": {
            "manifest_id": manifest["manifest_id"],
            "manifest_version": manifest["manifest_version"],
            "manifest_digest": manifest["manifest_digest"],
        },
        "manifest_state": manifest["lifecycle"]["state"],
        "components": [
            {
                "component_id": component["component_id"],
                "component_version": component["component_version"],
                "artifact_identity": (
                    component["artifact"]
                    if isinstance(component.get("artifact"), Mapping)
                    else None
                ),
            }
            for component in manifest["components"]
        ],
        "membership_established": True,
        "configuration": (
            {"state": "PRESENT", "value": configuration}
            if configuration is not None
            else {"state": "ABSENT"}
        ),
        "golden_bundle": {"state": "ABSENT"},
        "golden_bundle_inventory_established": True,
        "extensions": {"state": "ABSENT"},
        "branding": {"state": "ABSENT"},
        "provenance": "MEASURED",
        "freshness_current": True,
    }
    document.update(overrides)
    return document


def environment_document(environment: DeploymentEnvironment) -> dict[str, Any]:
    """The operator-facing environment document of one deployment environment.

    This is the loadable form (the shape of
    ``factory/environments/example_runtime_dependency_environment.json``) — the
    form the entry point and the local runner read — not
    ``render_environment``'s record, which is an output.
    """
    return {
        "environment_id": environment.environment_id,
        "runtime_root": str(environment.runtime_root),
        "probe_timeout_seconds": environment.probe_timeout_seconds,
        "bindings": [
            {
                "component_id": binding.component_id,
                "deployment_module": binding.deployment_module,
                "deployment_factory": binding.deployment_factory,
                "import_paths": [str(path) for path in binding.import_paths],
                "published_endpoint": binding.published_endpoint,
                "dependency_endpoints": {
                    declared.component_id: declared.document()
                    for declared in binding.dependency_endpoints
                },
            }
            for binding in environment.bindings
        ],
        "configuration_overlay": {
            component_id: dict(values)
            for component_id, values in environment.configuration_overlay.items()
        },
    }


@dataclass
class LayerRCell:
    """The declared Running Platform of one test, and its owner-side audit."""

    reference: str
    options: dict[str, str]
    module_path: Path
    state_path: Path
    audit_path: Path
    runtime_audit_path: Path

    # -- the cell's own authoritative state --------------------------------
    def write_state(self, document: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(dict(document), indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def edit(self, **overrides: Any) -> None:
        """State one of the cell's facts differently, as the owner would."""
        self.write_state({**self.state(), **overrides})

    # -- the cell's own audit ----------------------------------------------
    def observations(self) -> tuple[dict[str, Any], ...]:
        """Every evaluation the cell's producer answered, in order."""
        if not self.audit_path.is_file():
            return ()
        return tuple(
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def audit_text(self) -> str:
        if not self.audit_path.is_file():
            return ""
        return self.audit_path.read_text(encoding="utf-8")

    def runtime_calls(self) -> tuple[str, ...]:
        """Every call the layer's runtime seam served, in order."""
        if not self.runtime_audit_path.is_file():
            return ()
        return tuple(
            json.loads(line)["call"]
            for line in self.runtime_audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )


@pytest.fixture()
def cell(tmp_path: Path, instance: Instance, manifest: dict[str, Any]) -> LayerRCell:
    """One owner-side cell, declared from a copy that lives outside the repository."""
    owner_dir = tmp_path / "owner" / "cell"
    owner_dir.mkdir(parents=True)
    module_path = owner_dir / "cell.py"
    shutil.copyfile(CELL_FIXTURE, module_path)
    state_path = tmp_path / "owner" / "state" / "identity.json"
    audit_path = tmp_path / "owner" / "audit" / "observations.jsonl"
    runtime_audit_path = tmp_path / "owner" / "audit" / "runtime-calls.jsonl"
    declared = LayerRCell(
        reference=f"{module_path}:build_layer_r_wiring",
        options={
            "state_file": str(state_path),
            "audit_file": str(audit_path),
            "runtime_audit_file": str(runtime_audit_path),
        },
        module_path=module_path,
        state_path=state_path,
        audit_path=audit_path,
        runtime_audit_path=runtime_audit_path,
    )
    declared.write_state(owner_state_document(instance, manifest))
    return declared


@dataclass
class Production:
    """One composed production root, with the cell retired after the test."""

    root: ProductionCompositionRoot
    cell: LayerRCell
    composed: list[ProductionCompositionRoot] = field(default_factory=list, repr=False)

    def compose(self) -> ProductionCompositionRoot:
        """Compose once more — as a fresh Layer O process does, cell standing."""
        composed = compose_production_root(self.cell.reference, self.cell.options)
        self.composed.append(composed)
        return composed

    def close(self) -> None:
        """The layer's own retirement policy — never the composition root's."""
        for composed in [self.root, *self.composed]:
            composed.runtime.shutdown()


@pytest.fixture()
def production(cell: LayerRCell):
    """Compose the production root, and let the cell retire its members after."""
    composed = Production(
        root=compose_production_root(cell.reference, cell.options), cell=cell
    )
    try:
        yield composed
    finally:
        composed.close()


@dataclass
class DeployAttempt:
    """One production deployment attempt, in its own environment."""

    production: Production
    request: DeploymentRequest
    environment: DeploymentEnvironment

    def deploy(self) -> Deployment:
        return self.production.root.deploy(self.request)

    def refused(self) -> IdentityVerificationFailed:
        with pytest.raises(IdentityVerificationFailed) as refusal:
            self.deploy()
        return refusal.value

    def record(self):
        return only_operation(self.environment)


@pytest.fixture()
def attempt(cell: LayerRCell, production: Production, instance, manifest, tmp_path):
    """Run one production deployment attempt per call, each in its own environment."""
    attempts = iter(range(1, 100))

    def start() -> DeployAttempt:
        environment = environment_for(tmp_path / "dno" / f"runtime-{next(attempts)}")
        return DeployAttempt(
            production=production,
            request=request_for(instance, manifest, environment),
            environment=environment,
        )

    return start


def refuse(
    attempt: DeployAttempt, *, reason: str | None = None
) -> IdentityVerificationFailed:
    """Assert a fail-closed refusal and an honest record behind it."""
    refusal = attempt.refused()
    if reason is not None:
        stated = "\n".join(refusal.errors)
        assert reason in stated, stated
    record = attempt.record()
    assert record.deployed is False
    assert record.identity_verified is False
    assert record.ready is False
    assert record.failure is not None
    return refusal


# ---------------------------------------------------------------------------
# A — the composition: the declared Running Platform, and nothing else
# ---------------------------------------------------------------------------


class TestProductionWiring:
    def test_the_declared_layer_r_supplies_both_seams(self, cell, production):
        composed = production.root

        assert composed.layer_r.module_path == cell.module_path
        assert composed.layer_r.declaration.factory_name == "build_layer_r_wiring"
        # The runtime seam is the owner's own implementation, loaded from the
        # declared module — the D&O package defines no runtime here.
        assert type(composed.runtime).__module__.startswith("layer_r_declared_")
        assert type(composed.runtime).__name__ == "CellRuntimeAdapter"
        # The identity evidence comes from the owner's own source; the shipped
        # adapter only adapts it to the consumer contract.
        assert composed.layer_r.adapted_owner_source is True
        assert type(composed.identity_provider).__name__ == (
            "OwnerSuppliedPlatformIdentityProvider"
        )

    def test_the_runtime_seam_is_not_the_local_default(self, production):
        composed = production.root

        assert not isinstance(composed.runtime, LocalProcessRuntime)
        document = composed.document()
        assert document["local_defaults_used"] is False
        assert document["dno_owned_runtime_used"] is False
        assert document["dno_owned_identity_authority_used"] is False
        assert document["runtime_adapter"]["operations"] == [
            "materialize",
            "migrate",
            "start",
            "attach",
            "request",
            "stop",
        ]

    def test_the_wiring_layer_cannot_express_the_local_defaults(self):
        """Structural guard: the production package never names them."""
        package = Path(sys.modules["deployment_operations.production"].__file__)
        modules = [package, *sorted(package.parent.glob("*.py"))]
        found = {name: [] for name in FORBIDDEN_NAMES}
        for module in modules:
            names = _names_used(module)
            for name in FORBIDDEN_NAMES:
                if name in names:
                    found[name].append(module.name)
        assert found == {name: [] for name in FORBIDDEN_NAMES}, found

    def test_nothing_on_the_production_path_controls_the_owner_runtime(self):
        """Scenario: no terminate/kill/shutdown/retire in the production path.

        ``RuntimeAdapter.stop`` is the runtime contract's detach: it releases
        this operation's reference, so the production path may call it and must
        never reach further — no process handle, no signal, no owner retirement
        policy. Docstrings may *name* these actions (that is why this reads
        identifiers out of the syntax tree rather than the file text); no code
        on the path may use one.
        """
        package = Path(sys.modules["deployment_operations.production"].__file__)
        engine = package.parent.parent
        modules = [package, *sorted(package.parent.glob("*.py"))]
        modules += [engine / name for name in PRODUCTION_PATH_ENGINE_MODULES]
        found = {name: [] for name in LIFECYCLE_CONTROL_NAMES}
        for module in modules:
            used = LIFECYCLE_CONTROL_NAMES & _names_used(module)
            for name in used:
                found[name].append(module.parent.name + "/" + module.name)
        assert found == {name: [] for name in LIFECYCLE_CONTROL_NAMES}, found
        # and the one call the contract does allow is on the path by name, so
        # this guard is not vacuously true about a path that stops nothing
        restart = (engine / "restart.py").read_text(encoding="utf-8")
        assert "adapter.stop(" in restart or "self.adapter.stop(" in restart

    def test_a_missing_declaration_is_refused(self):
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root("")

        assert "no default" in refusal.value.errors[0]
        assert refusal.value.stage == "composition"
        assert "no operation was started" in str(refusal.value)

    def test_a_relative_declaration_is_refused(self):
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root("cell.py:build_layer_r_wiring")

        assert "relative" in refusal.value.errors[0]

    def test_a_declaration_inside_the_repository_is_refused(self):
        """The owner's code is not this repository's code (ADR-0020 §4)."""
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{CELL_FIXTURE}:build_layer_r_wiring")

        assert "inside the Deployment & Operations code" in refusal.value.errors[0]

    def test_a_dotted_module_of_this_repository_is_refused(self):
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root("deployment_operations.runtime:LocalProcessRuntime")

        assert "resolves inside the Deployment & Operations code" in (
            refusal.value.errors[0]
        )

    def test_a_dotted_module_of_the_shipped_owner_side_is_refused(self):
        """The D&O-owned file surface is shipped code, never a production cell."""
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(
                "running_platform.owner_state:FileRunningPlatformOwnerStateReader"
            )

        assert "resolves inside the Deployment & Operations code" in (
            refusal.value.errors[0]
        )

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{tmp_path / 'absent.py'}:build_layer_r_wiring")

        assert "does not exist" in refusal.value.errors[0]

    def test_a_factory_answering_the_dno_runtime_is_refused(self, tmp_path):
        """AC-2: production mode cannot be handed the D&O-owned runtime."""
        module = _write_module(
            tmp_path,
            "answers_dno_runtime.py",
            "from deployment_operations.runtime import LocalProcessRuntime\n"
            "\n"
            "class Wiring:\n"
            "    pass\n"
            "\n"
            "def build_layer_r_wiring(**options):\n"
            "    wiring = Wiring()\n"
            "    wiring.runtime_adapter = LocalProcessRuntime()\n"
            "    wiring.identity_source = None\n"
            "    return wiring\n",
        )
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{module}:build_layer_r_wiring")

        assert "is defined inside the Deployment & Operations code" in (
            refusal.value.errors[0]
        )

    def test_a_factory_answering_a_dno_owned_identity_source_is_refused(self, tmp_path):
        """AC-3: production mode cannot read a D&O-owned identity surface."""
        module = _write_module(
            tmp_path,
            "answers_dno_identity_source.py",
            "from pathlib import Path\n"
            "\n"
            "from running_platform.owner_state import (\n"
            "    FileRunningPlatformOwnerStateReader,\n"
            "    OwnerStateSnapshotSource,\n"
            ")\n"
            "\n"
            "class Runtime:\n"
            "    def materialize(self, element): ...\n"
            "    def migrate(self, element): ...\n"
            "    def start(self, element): ...\n"
            "    def attach(self, element): ...\n"
            "    def request(self, handle, operation, *, timeout=None): ...\n"
            "    def stop(self, handle): ...\n"
            "\n"
            "class Wiring:\n"
            "    pass\n"
            "\n"
            "def build_layer_r_wiring(**options):\n"
            "    wiring = Wiring()\n"
            "    wiring.runtime_adapter = Runtime()\n"
            "    wiring.identity_source = OwnerStateSnapshotSource(\n"
            "        FileRunningPlatformOwnerStateReader(\n"
            "            Path('/srv/running-platform/running_platform_identity.json')\n"
            "        )\n"
            "    )\n"
            "    return wiring\n",
        )
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{module}:build_layer_r_wiring")

        assert "is defined inside the Deployment & Operations code" in (
            refusal.value.errors[0]
        )

    def test_a_factory_answering_the_local_default_identity_chain_is_refused(
        self, tmp_path
    ):
        """AC-3: the local default chain is exactly what production must not be."""
        module = _write_module(
            tmp_path,
            "answers_local_default_identity.py",
            "from pathlib import Path\n"
            "\n"
            "from deployment_operations.platform_identity_source import (\n"
            "    OwnerSuppliedPlatformIdentityProvider,\n"
            ")\n"
            "from running_platform.owner_state import (\n"
            "    FileRunningPlatformOwnerStateReader,\n"
            "    OwnerStateSnapshotSource,\n"
            ")\n"
            "\n"
            "class Runtime:\n"
            "    def materialize(self, element): ...\n"
            "    def migrate(self, element): ...\n"
            "    def start(self, element): ...\n"
            "    def attach(self, element): ...\n"
            "    def request(self, handle, operation, *, timeout=None): ...\n"
            "    def stop(self, handle): ...\n"
            "\n"
            "class Wiring:\n"
            "    pass\n"
            "\n"
            "def build_layer_r_wiring(**options):\n"
            "    wiring = Wiring()\n"
            "    wiring.runtime_adapter = Runtime()\n"
            "    wiring.identity_provider = OwnerSuppliedPlatformIdentityProvider(\n"
            "        OwnerStateSnapshotSource(\n"
            "            FileRunningPlatformOwnerStateReader(\n"
            "                Path('/srv/running-platform/running_platform_identity.json')\n"
            "            )\n"
            "        )\n"
            "    )\n"
            "    return wiring\n",
        )
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{module}:build_layer_r_wiring")

        assert "is not owner-side code" in refusal.value.errors[0]

    def test_a_factory_without_identity_evidence_is_refused(self, tmp_path):
        module = _write_module(
            tmp_path,
            "answers_no_identity.py",
            "class Runtime:\n"
            "    def materialize(self, element): ...\n"
            "    def migrate(self, element): ...\n"
            "    def start(self, element): ...\n"
            "    def attach(self, element): ...\n"
            "    def request(self, handle, operation, *, timeout=None): ...\n"
            "    def stop(self, handle): ...\n"
            "\n"
            "class Wiring:\n"
            "    pass\n"
            "\n"
            "def build_layer_r_wiring(**options):\n"
            "    wiring = Wiring()\n"
            "    wiring.runtime_adapter = Runtime()\n"
            "    return wiring\n",
        )
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{module}:build_layer_r_wiring")

        assert "neither identity_provider" in refusal.value.errors[0]

    def test_a_failing_factory_is_refused(self, tmp_path):
        module = _write_module(
            tmp_path,
            "failing_factory.py",
            "def build_layer_r_wiring(**options):\n"
            "    raise RuntimeError('the cell refused to start')\n",
        )
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{module}:build_layer_r_wiring")

        assert "failed (RuntimeError)" in refusal.value.errors[0]

    def test_an_unconformant_runtime_seam_is_refused(self, tmp_path):
        module = _write_module(
            tmp_path,
            "missing_operations.py",
            "class Runtime:\n"
            "    def materialize(self, element): ...\n"
            "\n"
            "class Wiring:\n"
            "    pass\n"
            "\n"
            "def build_layer_r_wiring(**options):\n"
            "    wiring = Wiring()\n"
            "    wiring.runtime_adapter = Runtime()\n"
            "    return wiring\n",
        )
        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{module}:build_layer_r_wiring")

        assert "does not implement the runtime seam" in refusal.value.errors[0]


# ---------------------------------------------------------------------------
# B/C — actual identity from the owner side, compared with expected identity
# ---------------------------------------------------------------------------


class TestOwnerSideActualIdentity:
    def test_a_matching_platform_is_accepted(self, attempt, cell):
        deployment = attempt().deploy()

        assert deployment.deployed is True
        assert deployment.record.identity_verified is True
        assert deployment.record.ready is True
        assert deployment.record.lifecycle == "realized"
        # provenance, correlation and freshness were established through the
        # owner-side source, and the cell answered exactly one evaluation
        observations = cell.observations()
        assert len(observations) == 1
        assert observations[0]["answer"] == "correlation"

    def test_the_dno_owned_identity_file_is_absent_and_nothing_writes_it(self, attempt):
        """AC-3 by construction: the file authority does not even exist here."""
        deployed = attempt()
        deployed.deploy()

        ambient = deployed.environment.runtime_root / AMBIENT_IDENTITY_FILENAME
        assert not ambient.exists()

    def test_the_owner_was_asked_once_under_a_binding_of_its_own(self, attempt, cell):
        deployed = attempt()
        deployment = deployed.deploy()

        observations = cell.observations()
        assert len(observations) == 1
        answered = observations[0]
        # only the opaque handle and the binding semantics crossed the boundary
        assert answered["answered_for_handle"].startswith(EVALUATION_HANDLE_PREFIX)
        assert len(str(answered["answered_for_handle"])) == (
            len(EVALUATION_HANDLE_PREFIX) + 32
        )
        assert answered["context_fields"] == [
            "authority",
            "basis",
            "established_at",
            "scope",
            "sequence",
            "target",
        ]
        assert answered["binding_scope"] == deployed.environment.environment_id
        assert answered["binding_sequence"] == deployment.record.attempt
        # and nothing expected-derived is anywhere in the owner-side audit
        assert "instance_digest" not in cell.audit_text()
        assert "expected_instance" not in cell.audit_text()

    def test_actual_identity_is_the_cells_own_fact(self, attempt, cell):
        """The expected side never changes; the owner's answer decides."""
        assert attempt().deploy().deployed is True

        # The cell now runs another component version — its own fact, not a
        # request, and the projected actual identity provably differs.
        document = cell.state()
        document["components"][0]["component_version"] = "9.9.9"
        cell.write_state(document)

        refuse(attempt(), reason="does not match")


# ---------------------------------------------------------------------------
# D–G — every disagreement fails closed
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_platform_that_is_not_the_expected_one_is_refused(self, attempt, cell):
        # A producer that observed another platform states that platform as the
        # target of its own observation, so the correlation cannot agree.
        cell.edit(platform_id="another-platform")

        refuse(attempt(), reason="unavailable")

    def test_a_mismatched_component_version_is_refused(self, attempt, cell):
        document = cell.state()
        document["components"][0]["component_version"] = "9.9.9"
        cell.write_state(document)

        refuse(attempt(), reason="does not match")

    def test_an_extra_component_in_the_membership_is_refused(self, attempt, cell):
        document = cell.state()
        document["components"].append(
            {
                "component_id": "identity_service",
                "component_version": "0.4.0",
                # a member this cell really runs: `none` states the absence of
                # published artifact content, it is not missing evidence
                "artifact_identity": {
                    "artifact_type": "none",
                    "canonical_form": None,
                    "digest": None,
                    "pinned": False,
                },
            }
        )
        cell.write_state(document)

        refuse(attempt(), reason="does not match")

    def test_a_mismatched_manifest_identity_is_refused(self, attempt, cell):
        document = cell.state()
        document["manifest"]["manifest_digest"] = WRONG_DIGEST
        cell.write_state(document)

        refuse(attempt(), reason="does not match")

    def test_a_mismatched_artifact_identity_is_refused(self, attempt, cell):
        document = cell.state()
        document["components"][0]["artifact_identity"] = {
            "artifact_type": "container_image",
            "canonical_form": "registry.example/x@sha256:" + "1" * 64,
            "digest": "sha256:" + "1" * 64,
            "pinned": True,
        }
        cell.write_state(document)

        refuse(attempt(), reason="contradictory")

    def test_stale_evidence_is_refused(self, attempt, cell):
        cell.edit(freshness_current=False)

        refuse(attempt(), reason="stale")
        # the owner answered — the refusal is about the evidence, not an outage
        assert cell.observations()[-1]["answer"] == "correlation"

    def test_evidence_for_another_handle_is_refused(self, attempt, cell):
        cell.edit(correlation={"token": EVALUATION_HANDLE_PREFIX + "f" * 32})

        refuse(attempt(), reason="unavailable")

    def test_evidence_for_another_target_is_refused(self, attempt, cell):
        cell.edit(correlation={"target": "another-platform"})

        refuse(attempt(), reason="unavailable")

    def test_evidence_for_another_position_is_refused(self, attempt, cell):
        cell.edit(correlation={"sequence": 99})

        refuse(attempt(), reason="unavailable")

    def test_an_unavailable_owner_surface_is_refused(self, attempt, cell):
        cell.edit(answer="unavailable")

        refuse(attempt(), reason="not served")

    def test_an_unexpected_owner_error_is_refused(self, attempt, cell):
        cell.edit(answer="error")

        refuse(attempt(), reason="invalid actual evidence")

    def test_an_answer_that_is_not_a_snapshot_is_refused(self, attempt, cell):
        cell.edit(answer="malformed")

        refuse(attempt(), reason="invalid snapshot")

    def test_a_refused_attempt_detaches_without_retiring_the_layer(
        self, cell, production, attempt
    ):
        """Fail-closed cleanup is the established detach, not a kill (§7)."""
        cell.edit(platform_id="another-platform")
        deployed = attempt()

        refuse(deployed, reason="unavailable")

        # the layer the cell supervises is intact, and the record never claimed
        # a deployment
        assert "detach:tenant_authority" in cell.runtime_calls()
        assert "retire:tenant_authority" not in cell.runtime_calls()
        assert production.root.runtime.supervised() == ("tenant_authority",)
        assert deployed.record().lifecycle == LIFECYCLE_FAILED


# ---------------------------------------------------------------------------
# H/I — restart and reconcile through the production seams
# ---------------------------------------------------------------------------


class TestProductionOperations:
    def test_restart_re_executes_through_the_production_seams(self, attempt, cell):
        deployed = attempt()
        deployment = deployed.deploy()
        assert deployment.deployed is True
        calls_after_deploy = cell.runtime_calls()
        observations_after_deploy = len(cell.observations())

        restarted = deployed.production.root.restart(
            RestartRequest(
                deployment=deployment,
                restart_id=derive_restart_id(deployment),
                policy=STRICT_STOP_START_POLICY,
            )
        )

        assert restarted.outcome == RESTART_COMPLETED
        assert restarted.completed is True
        # the runtime was re-executed by the cell's own seam ...
        new_calls = cell.runtime_calls()[len(calls_after_deploy) :]
        assert "detach:tenant_authority" in new_calls
        assert any(call.startswith("start:") for call in new_calls)
        assert not any(call.startswith("materialize:") for call in new_calls)
        assert not any(call.startswith("migrate:") for call in new_calls)
        # ... and correspondence was re-established by a fresh evaluation
        assert len(cell.observations()) == observations_after_deploy + 1

    def test_reconcile_observes_freshly_and_mutates_no_runtime(self, attempt, cell):
        deployed = attempt()
        deployment = deployed.deploy()
        calls_before = cell.runtime_calls()
        observations_before = cell.observations()

        reconciliation = deployed.production.root.reconcile(
            ReconciliationRequest(
                deployment=deployment,
                reconciliation_id=derive_reconciliation_id(deployment),
            )
        )

        assert reconciliation.in_correspondence is True
        # a fresh observation, made by the owner's own source for this evaluation
        observations = cell.observations()
        assert len(observations) == len(observations_before) + 1
        fresh = observations[-1]
        assert (
            fresh["answered_for_handle"]
            != observations_before[-1]["answered_for_handle"]
        )
        assert fresh["binding_scope"] == deployment.record.environment_id
        # and no runtime action of any kind was taken
        assert cell.runtime_calls() == calls_before


# ---------------------------------------------------------------------------
# The entry point — production mode is selected, never implied
# ---------------------------------------------------------------------------


class TestProductionEntryPoint:
    def _documents(self, deployed: DeployAttempt, instance, manifest):
        """The three input documents of one operation, on disk."""
        return _operation_documents(deployed, instance, manifest)

    def _arguments(self, cell: LayerRCell, paths) -> list[str]:
        return _entry_arguments(cell, paths)

    def test_check_reports_the_composition_and_runs_nothing(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        from deployment_operations.production.__main__ import main

        deployed = attempt()
        paths = self._documents(deployed, instance, manifest)
        code = main(
            [
                "deploy",
                *self._arguments(cell, paths),
                "--check",
                "--result-out",
                str(tmp_path / "check.json"),
            ]
        )

        assert code == 0
        transcript = json.loads(capsys.readouterr().out)
        assert transcript["composition"]["local_defaults_used"] is False
        assert transcript["composition"]["module"] == str(cell.module_path)
        assert "no operation was run" in transcript["result"]
        assert not deployed.environment.operations_dir.exists()
        assert json.loads((tmp_path / "check.json").read_text(encoding="utf-8"))

    def test_deploy_through_the_entry_point(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        from deployment_operations.production.__main__ import main

        paths = self._documents(attempt(), instance, manifest)
        code = main(["deploy", *self._arguments(cell, paths)])

        assert code == 0
        transcript = json.loads(capsys.readouterr().out)
        assert transcript["result"] == "accepted"
        assert transcript["record"]["deployed"] is True
        assert transcript["record"]["identity_verified"] is True
        assert transcript["composition"]["dno_owned_runtime_used"] is False
        assert transcript["composition"]["dno_owned_identity_authority_used"] is False
        assert "wiring evidence only" in transcript["evidence_note"]

    def test_restart_through_the_entry_point_attaches_and_re_executes(
        self, attempt, cell, instance, manifest, capsys
    ):
        from deployment_operations.production.__main__ import main

        paths = self._documents(attempt(), instance, manifest)
        assert main(["deploy", *self._arguments(cell, paths)]) == 0
        capsys.readouterr()
        calls_after_deploy = cell.runtime_calls()

        code = main(["restart", *self._arguments(cell, paths)])

        assert code == 0
        transcript = json.loads(capsys.readouterr().out)
        assert transcript["attached"] is True
        assert transcript["outcome"] == RESTART_COMPLETED
        # a fresh Layer O process re-binds first (F-1) and only then re-executes
        phase = cell.runtime_calls()[len(calls_after_deploy) :]
        assert phase[0] == "attach:tenant_authority"
        assert any(call.startswith("start:") for call in phase)
        assert not any(call.startswith("materialize:") for call in phase)
        assert not any(call.startswith("migrate:") for call in phase)

    def test_the_entry_point_has_no_layer_r_default(
        self, attempt, instance, manifest, tmp_path, capsys
    ):
        from deployment_operations.production.__main__ import main

        paths = self._documents(attempt(), instance, manifest)
        with pytest.raises(SystemExit) as exit_code:
            main(
                [
                    "deploy",
                    "--instance",
                    str(paths[0]),
                    "--manifest",
                    str(paths[1]),
                    "--environment",
                    str(paths[2]),
                ]
            )

        assert exit_code.value.code == 2

    def test_a_digest_that_does_not_name_the_instance_is_refused(
        self, attempt, cell, instance, manifest, capsys
    ):
        from deployment_operations.production.__main__ import main

        paths = self._documents(attempt(), instance, manifest)
        code = main(
            [
                "deploy",
                *self._arguments(cell, paths),
                "--digest",
                WRONG_DIGEST,
            ]
        )

        assert code == 2
        captured = capsys.readouterr()
        assert "floating selector is refused" in captured.err
        assert "no deployment operation was created" in captured.err

    def test_an_input_document_that_is_not_json_is_refused(
        self, attempt, cell, instance, manifest, capsys
    ):
        from deployment_operations.production.__main__ import main

        paths = self._documents(attempt(), instance, manifest)
        instance_path, manifest_path, environment_path = paths
        manifest_path.write_text("{ not json", encoding="utf-8")

        code = main(
            [
                "deploy",
                "--instance",
                str(instance_path),
                "--manifest",
                str(manifest_path),
                "--environment",
                str(environment_path),
                "--layer-r",
                cell.reference,
            ]
        )

        assert code == 2
        captured = capsys.readouterr()
        assert "not valid JSON" in captured.err
        assert "no deployment operation was created" in captured.err

    def test_the_documented_invocation_deploys_in_a_fresh_process(
        self, attempt, cell, instance, manifest
    ):
        paths = self._documents(attempt(), instance, manifest)
        command = [
            sys.executable,
            "-m",
            "deployment_operations.production",
            "deploy",
            *self._arguments(cell, paths),
        ]
        repo_root = discover_root()
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root / "src"), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        process = subprocess.Popen(
            command,
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        process_group = os.getpgid(process.pid)
        try:
            out, err = process.communicate(timeout=600)
        finally:
            # The layer's own retirement policy, applied by the owner (here the
            # test harness) once the operation is over: no layer survives it,
            # and the composition root never stops Layer R itself.
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass

        assert process.returncode == 0, err
        transcript = json.loads(out)
        assert transcript["operation"] == "deploy"
        assert transcript["record"]["deployed"] is True
        assert transcript["composition"]["dno_owned_runtime_used"] is False
        assert transcript["composition"]["module"] == str(cell.module_path)


# ---------------------------------------------------------------------------
# The engine itself is untouched (AC-8/AC-9)
# ---------------------------------------------------------------------------


class TestEnginePreserved:
    def test_the_engine_seams_still_default_to_nothing(self):
        """The production root injects; it never edits the engine."""
        import inspect

        parameters = inspect.signature(deploy).parameters
        assert parameters["runtime"].default is None
        assert parameters["identity_provider"].default is None
        assert inspect.signature(attach).parameters["runtime"].default is (
            inspect.Parameter.empty
        )
        assert inspect.signature(attach).parameters["identity_provider"].default is (
            inspect.Parameter.empty
        )
        assert (
            inspect.signature(reconcile).parameters["identity_provider"].default is None
        )
        assert inspect.signature(restart).parameters["runtime"].default is None
        assert (
            inspect.signature(restart).parameters["identity_provider"].default is None
        )

    def test_the_local_default_runtime_and_loader_still_work(self, tmp_path):
        """Local/test composition keeps working exactly as before (AC-9)."""
        assert type(LocalProcessRuntime()).__name__ == "LocalProcessRuntime"
        environment = environment_for(tmp_path / "local-runtime")
        loaded = load_environment(environment_document(environment))
        assert loaded.environment_id == environment.environment_id
        assert loaded.runtime_root == environment.runtime_root
        assert [b.component_id for b in loaded.bindings] == [
            b.component_id for b in environment.bindings
        ]
        assert loaded.configuration_overlay == dict(environment.configuration_overlay)


def _names_used(path: Path) -> set[str]:
    """Every identifier a module uses in code (docstrings are not identifiers)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _write_module(directory: Path, name: str, source: str) -> Path:
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Corrective round: production-safety hardening
#
# Each class below pins one corrective finding. Every scenario runs through the
# production composition root and the owner-side cell double — the entry point
# and the engine are exercised exactly as an operator exercises them, and the
# owner's own audit is what proves whether Layer R was addressed or not.
#
# A note on ``retire:`` in the cell's audit: the double applies the owner's own
# one-member-per-component policy, so a restart records a ``retire:`` line for
# the member the cell replaces. That line is the cell's decision, never a
# Deployment & Operations action: D&O calls ``stop`` (the double records it as
# ``detach:``) and never destroys, retires or shuts down what the owner
# supervises — only the owner's ``shutdown`` does that, and no operation here
# ever reaches it.
# ---------------------------------------------------------------------------


def _cli(argv: list[str]) -> int:
    """Run the production entry point in this process, as an operator would."""
    from deployment_operations.production.__main__ import main

    return main(argv)


def _layer_r_options(options: Mapping[str, str]) -> list[str]:
    arguments: list[str] = []
    for name, value in sorted(options.items()):
        arguments += ["--layer-r-option", f"{name}={value}"]
    return arguments


def _operation_documents(
    deployed: DeployAttempt, instance: Instance, manifest: dict[str, Any]
) -> tuple[Path, Path, Path]:
    """The three input documents of one operation, on disk."""
    dno_dir = deployed.environment.runtime_root.parent
    dno_dir.mkdir(parents=True, exist_ok=True)
    instance_path = dno_dir / "platform-instance.json"
    manifest_path = dno_dir / "platform-manifest.json"
    environment_path = dno_dir / "environment.json"
    instance_path.write_text(
        json.dumps(instance.document, indent=2, sort_keys=True), encoding="utf-8"
    )
    manifest_path.write_text(
        json.dumps(dict(manifest), indent=2, sort_keys=True), encoding="utf-8"
    )
    environment_path.write_text(
        json.dumps(environment_document(deployed.environment), indent=2) + "\n",
        encoding="utf-8",
    )
    return instance_path, manifest_path, environment_path


def _entry_arguments(
    cell: LayerRCell,
    paths: tuple[Path, Path, Path],
    *extra_options: Mapping[str, str],
) -> list[str]:
    instance_path, manifest_path, environment_path = paths
    arguments = [
        "--instance",
        str(instance_path),
        "--manifest",
        str(manifest_path),
        "--environment",
        str(environment_path),
        "--layer-r",
        cell.reference,
    ]
    arguments += _layer_r_options(cell.options)
    for options in extra_options:
        arguments += _layer_r_options(options)
    return arguments


def _entry_point_environment(*extra_paths: Path) -> dict[str, str]:
    """The environment one production entry-point process runs in."""
    repo_root = discover_root()
    entries = [str(repo_root / "src"), *(str(path) for path in extra_paths)]
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        entries.append(existing)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    return environment


def _run_entry_point(
    arguments: Sequence[str],
    *,
    extra_paths: Sequence[Path] = (),
    timeout: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    """Run the production entry point in a fresh process, as an operator does.

    The process is started in its own session so that the harness can apply the
    owner's own retirement policy afterwards: killing the group retires the
    member the cell left standing, and Deployment & Operations is never asked to
    stop a platform it just built.
    """
    command = [sys.executable, "-m", "deployment_operations.production", *arguments]
    process = subprocess.Popen(
        command,
        cwd=str(discover_root()),
        env=_entry_point_environment(*extra_paths),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    group = os.getpgid(process.pid)
    try:
        out, err = process.communicate(timeout=timeout)
    finally:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
    return subprocess.CompletedProcess(command, process.returncode, out, err)


def _start_entry_point(arguments: Sequence[str]) -> subprocess.Popen[str]:
    """Start one production entry point in its own session, without waiting."""
    command = [sys.executable, "-m", "deployment_operations.production", *arguments]
    return subprocess.Popen(
        command,
        cwd=str(discover_root()),
        env=_entry_point_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _reap(process: subprocess.Popen[str]) -> tuple[str, str, int]:
    """Collect one entry-point process, then retire the layer it left standing."""
    group = os.getpgid(process.pid)
    try:
        out, err = process.communicate(timeout=600)
        code = process.returncode
    finally:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
    return out, err, code


@dataclass
class FailingAttempt:
    """One attempt whose owner-side seam is told to fail on named operations."""

    root: ProductionCompositionRoot
    cell: LayerRCell
    request: DeploymentRequest
    environment: DeploymentEnvironment

    def deploy(self) -> Deployment:
        return self.root.deploy(self.request)

    def restart(self, deployment: Deployment):
        return self.root.restart(
            RestartRequest(
                deployment=deployment,
                restart_id=derive_restart_id(deployment),
                policy=STRICT_STOP_START_POLICY,
            )
        )

    def record(self):
        return only_operation(self.environment)


@pytest.fixture()
def failing(cell: LayerRCell, production: Production, instance, manifest, tmp_path):
    """Compose fresh roots whose cell seam refuses the named runtime operations."""
    attempts = iter(range(1, 100))

    def build(
        fail_on: str,
        fail_kind: str = "error",
        *,
        request: DeploymentRequest | None = None,
    ) -> FailingAttempt:
        """A root whose cell seam fails; it can be pointed at a live operation."""
        environment = (
            request.environment
            if request is not None
            else environment_for(tmp_path / "dno" / f"failing-{next(attempts)}")
        )
        composed = compose_production_root(
            cell.reference,
            {**cell.options, "fail_on": fail_on, "fail_kind": fail_kind},
        )
        production.composed.append(composed)
        return FailingAttempt(
            root=composed,
            cell=cell,
            request=request or request_for(instance, manifest, environment),
            environment=environment,
        )

    return build


# ---------------------------------------------------------------------------
# P0-1 — a deployment attempt is an identity, and it is never re-run
# ---------------------------------------------------------------------------


class TestDuplicateAttemptRefusal:
    def test_the_second_identical_deploy_is_refused_untouched(self, attempt, cell):
        deployed = attempt()
        first = deployed.deploy()
        assert first.deployed is True
        deployment_id = deployment_identity(deployed.request)
        state_path = DeploymentStateStore.path_for(
            deployed.environment.operations_dir, deployment_id
        )
        state_before = state_path.read_bytes()
        journal_before = only_journal(deployed.environment)
        calls_before = cell.runtime_calls()

        with pytest.raises(ProductionOperationRefused) as refusal:
            deployed.deploy()

        stated = "\n".join(refusal.value.errors)
        assert "already exists" in stated
        assert "never renumbers" in stated
        # Layer R was never addressed a second time: no materialize, no start
        assert cell.runtime_calls() == calls_before
        # and the record and the journal the first attempt earned are untouched
        assert state_path.read_bytes() == state_before
        assert only_journal(deployed.environment) == journal_before
        record = only_operation(deployed.environment)
        assert record.deployed is True
        assert record.attempt == 1
        assert record.deployment_id == deployment_id

    def test_a_refused_rerun_leaves_the_next_attempt_to_the_operator(
        self, attempt, production, instance, manifest, tmp_path
    ):
        deployed = attempt()
        assert deployed.deploy().deployed is True
        with pytest.raises(ProductionOperationRefused):
            deployed.deploy()

        # A *new* attempt is a different operation, and only an operator names it
        environment = environment_for(tmp_path / "dno" / "runtime-next")
        next_attempt = DeployAttempt(
            production=production,
            request=request_for(instance, manifest, environment, attempt=2),
            environment=environment,
        )
        second = next_attempt.deploy()

        assert second.deployed is True
        assert second.record.attempt == 2
        assert second.record.deployment_id != deployed.record().deployment_id

    def test_the_entry_point_reports_the_duplicate_refusal_as_input(
        self, attempt, cell, instance, manifest, capsys
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        assert _cli(["deploy", *arguments]) == 0
        capsys.readouterr()
        calls_before = cell.runtime_calls()

        code = _cli(["deploy", *arguments])

        assert code == 2
        captured = capsys.readouterr()
        assert "already exists" in captured.err
        assert "nothing was addressed and nothing was written" in captured.err
        assert "Traceback (most recent call last)" not in captured.err
        assert cell.runtime_calls() == calls_before
        transcript = json.loads(captured.out)
        assert transcript["result"] == "refused"
        assert transcript["error_type"] == "ProductionOperationRefused"


# ---------------------------------------------------------------------------
# P0-2 — an owner seam that raises is a failure of the operation, not a traceback
# ---------------------------------------------------------------------------


class TestRuntimeSeamFailureNormalization:
    def test_an_unexpected_materialize_failure_is_normalized(self, failing, cell):
        deployed = failing("materialize", "error")

        with pytest.raises(DeploymentExecutionFailed) as refusal:
            deployed.deploy()

        assert "ValueError" in "\n".join(refusal.value.errors)
        record = deployed.record()
        assert record.failure is not None
        assert record.failure.stage == "deploying"
        assert record.deployed is False
        assert record.lifecycle == LIFECYCLE_FAILED
        # nothing was started, so nothing had to be released
        assert [
            call for call in cell.runtime_calls() if call.startswith("start:")
        ] == []
        assert "detach:tenant_authority" not in cell.runtime_calls()

    def test_an_unexpected_start_failure_is_normalized_for_that_stage(
        self, failing, cell
    ):
        deployed = failing("start", "error")

        with pytest.raises(StartupFailed) as refusal:
            deployed.deploy()

        assert "ValueError" in "\n".join(refusal.value.errors)
        record = deployed.record()
        assert record.failure is not None
        assert record.failure.stage == "starting"
        assert record.running is False
        assert record.ready is False
        calls = cell.runtime_calls()
        assert "failed:start:tenant_authority" in calls
        # the seam never returned a handle: D&O released nothing, and — crucially
        # — retired nothing the owner supervises
        assert [call for call in calls if call.startswith("detach:")] == []
        assert "shutdown" not in calls

    def test_an_unexpected_request_failure_releases_the_started_element(
        self, failing, cell
    ):
        deployed = failing("request", "error")

        with pytest.raises(StartupFailed) as refusal:
            deployed.deploy()

        assert "ValueError" in "\n".join(refusal.value.errors)
        # the release is the established detach: a reference, not a lifecycle end
        assert "detach:tenant_authority" in cell.runtime_calls()
        assert "shutdown" not in cell.runtime_calls()
        assert deployed.root.runtime.supervised() == ("tenant_authority",)
        record = deployed.record()
        assert record.failure is not None
        assert record.failure.stage == "starting"
        assert record.running is False

    def test_a_release_that_also_fails_is_reported_with_the_failure(self, failing):
        deployed = failing("request,stop", "error")

        with pytest.raises(StartupFailed) as refusal:
            deployed.deploy()

        stated = "\n".join(refusal.value.errors)
        assert "ValueError" in stated
        assert "could not be released" in stated
        record = deployed.record()
        assert record.failure is not None
        assert record.failure.stage == "starting"
        assert any("could not be released" in error for error in record.failure.errors)

    def test_a_base_exception_is_never_normalized_into_a_failure(self, failing, cell):
        deployed = failing("request", "interrupt")

        with pytest.raises(KeyboardInterrupt):
            deployed.deploy()

        # A process that was interrupted did not fail the operation: the record
        # keeps the honest position it had reached, invents no failure, and the
        # layer keeps exactly what it supervises.
        record = deployed.record()
        assert record.lifecycle == LIFECYCLE_IN_PROGRESS
        assert record.failure is None
        assert record.deployed is False
        assert "failed:request:tenant_authority" in cell.runtime_calls()

    def test_an_unexpected_attach_failure_is_a_controlled_dno_failure(
        self, failing, attempt
    ):
        deployed = attempt()
        assert deployed.deploy().deployed is True
        broken = failing("attach", "error", request=deployed.request)

        with pytest.raises(RuntimeProcessError) as refusal:
            broken.root.attach(broken.request)

        stated = "\n".join(refusal.value.errors)
        assert "ValueError" in stated
        assert isinstance(refusal.value.__cause__, ValueError)
        # attach refused while binding: nothing was created, started or stopped,
        # and the authoritative operation is exactly as it was
        record = only_operation(deployed.environment)
        assert record.deployed is True
        assert record.failure is None
        assert "shutdown" not in broken.cell.runtime_calls()


# ---------------------------------------------------------------------------
# P1-5 — the restart boundary: detach, never a lifecycle end
# ---------------------------------------------------------------------------


class TestRestartBoundary:
    def test_a_completed_restart_leaves_owner_supervision_intact(
        self, attempt, cell, production
    ):
        deployed = attempt()
        deployment = deployed.deploy()
        calls_before = len(cell.runtime_calls())

        restarted = deployed.production.root.restart(
            RestartRequest(
                deployment=deployment,
                restart_id=derive_restart_id(deployment),
                policy=STRICT_STOP_START_POLICY,
            )
        )

        assert restarted.outcome == RESTART_COMPLETED
        assert restarted.completed is True
        phase = cell.runtime_calls()[calls_before:]
        # the stop D&O performed is the detach of its own reference ...
        assert "detach:tenant_authority" in phase
        # ... and the owner's retirement policy was never invoked by D&O
        assert "shutdown" not in phase
        assert production.root.runtime.supervised() == ("tenant_authority",)
        record = only_operation(deployed.environment)
        assert record.deployment_id == deployment.record.deployment_id
        assert record.attempt == 1
        assert record.running is True
        assert record.ready is True
        assert record.deployed is True
        assert [entry.outcome for entry in record.restarts] == [RESTART_COMPLETED]

    def test_a_stop_phase_failure_keeps_the_conservative_running_claim(
        self, attempt, failing, cell
    ):
        deployed = attempt()
        deployment = deployed.deploy()
        broken = failing("stop", "error")

        with pytest.raises(RestartFailed) as refusal:
            broken.restart(deployment)

        assert refusal.value.phase == "stop"
        assert "ValueError" in "\n".join(refusal.value.errors)
        # the element refused to be released: the platform is not stopped, and
        # saying so is the only honest state (running stands, ready is withdrawn)
        record = only_operation(deployed.environment)
        assert record.running is True
        assert record.ready is False
        assert record.failure is None
        assert [entry.outcome for entry in record.restarts] == [RESTART_FAILED]
        assert record.restarts[-1].failure_phase == "stop"
        # and nothing about the owner's layer was destroyed
        assert "shutdown" not in cell.runtime_calls()
        assert broken.root.runtime.supervised() == ("tenant_authority",)

    def test_a_start_phase_failure_never_retires_the_owner_runtime(
        self, attempt, failing, cell
    ):
        deployed = attempt()
        deployment = deployed.deploy()
        broken = failing("request", "error")

        with pytest.raises(RestartFailed) as refusal:
            broken.restart(deployment)

        assert refusal.value.phase == "start"
        assert "ValueError" in "\n".join(refusal.value.errors)
        # the fresh start never confirmed: the attempt released what it started,
        # and the record claims no stopped platform it did not establish — a
        # detach is the contract's reference release, not evidence of a stop
        record = only_operation(deployed.environment)
        assert record.running is True
        assert record.ready is False
        assert record.deployed is False
        assert record.failure is None
        assert record.restarts[-1].failure_phase == "start"
        assert [entry.outcome for entry in record.restarts] == [RESTART_FAILED]
        # and the record and journal claim no stopped platform anywhere
        assert "platform_stopped" not in [
            action.name for action in record.operational_actions
        ]
        # the member the owner supervises is still the owner's, and the runtime
        # that refused is never torn down by the operation that failed
        assert "shutdown" not in cell.runtime_calls()
        assert broken.root.runtime.supervised() == ("tenant_authority",)


# ---------------------------------------------------------------------------
# P1-6 — an interrupted deploy: the record is the recovery state
# ---------------------------------------------------------------------------


class TestInterruptedDeployment:
    def test_an_interrupted_attempt_persists_what_it_had_reached(
        self, attempt, cell, instance, manifest
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell,
            _operation_documents(deployed, instance, manifest),
            {"fail_on": "request", "fail_kind": "interrupt"},
        )

        with pytest.raises(KeyboardInterrupt):
            _cli(["deploy", *arguments])

        # The engine persists the operation before it addresses Layer R, so the
        # interruption leaves a record of exactly what was reached — never an
        # unrecorded runtime element that a later process could only guess at.
        record = only_operation(deployed.environment)
        assert record.lifecycle == LIFECYCLE_IN_PROGRESS
        assert record.failure is None
        assert record.attempt == 1
        assert record.deployed is False
        assert record.running is False
        assert record.ready is False
        assert record.identity_verified is False
        assert [
            entry.name for entry in record.stages if entry.status == "in_progress"
        ] == ["starting"]
        events = only_journal(deployed.environment)
        assert events
        assert events[-1]["stage"] == "starting"
        # the layer really did start work: the cell supervises a member now
        assert "failed:request:tenant_authority" in cell.runtime_calls()

    def test_a_fresh_process_invents_no_recovery(
        self, attempt, cell, instance, manifest
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell,
            _operation_documents(deployed, instance, manifest),
            {"fail_on": "request", "fail_kind": "interrupt"},
        )
        with pytest.raises(KeyboardInterrupt):
            _cli(["deploy", *arguments])
        interrupted = only_operation(deployed.environment)
        state_path = DeploymentStateStore.path_for(
            deployed.environment.operations_dir, interrupted.deployment_id
        )
        state_before = state_path.read_bytes()
        calls_before = cell.runtime_calls()
        fresh = deployed.production.compose()

        # The same attempt may not be re-run: nothing is materialized, nothing
        # is started a second time, no state is overwritten.
        with pytest.raises(ProductionOperationRefused) as refusal:
            fresh.deploy(deployed.request)
        assert "already exists" in "\n".join(refusal.value.errors)

        # And attach invents nothing either: the record does not claim a
        # realized, running, identity-verified platform, so there is nothing to
        # re-bind — the operator's own decision is what comes next.
        with pytest.raises(DeploymentOperationsError) as attach_refusal:
            fresh.attach(deployed.request)
        stated = "\n".join(attach_refusal.value.errors)
        assert "lifecycle is 'in_progress'" in stated
        assert "realizes nothing itself" in stated
        assert cell.runtime_calls() == calls_before
        assert state_path.read_bytes() == state_before
        assert only_operation(deployed.environment).deployment_id == (
            interrupted.deployment_id
        )

        # The recovery that does exist is an explicit operator decision: a new
        # attempt number, which is a different operation and never a rewrite of
        # the interrupted one.
        next_environment = environment_for(
            deployed.environment.runtime_root.parent / "runtime-next"
        )
        recovered = fresh.deploy(
            request_for(instance, manifest, next_environment, attempt=2)
        )
        assert recovered.deployed is True
        assert recovered.record.attempt == 2
        assert recovered.record.deployment_id != interrupted.deployment_id


# ---------------------------------------------------------------------------
# P2-7 — the owner's module keeps its own import shape
# ---------------------------------------------------------------------------


SIBLING_FACTORY = '''"""An owner-side factory module that imports its own siblings.

A real cell is not one file: its adapter, its identity reader and its factory
live beside each other under <RP_CELL_HOME>, and the owner's factory reaches
them the way the owner's own code does — by putting its own directory on the
import path and importing the sibling module by name. Deployment & Operations
executes this declared file and calls this declared factory; the shape of the
owner's code is the owner's business.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rp_cell_seams import build_layer_r_wiring
'''

PACKAGE_INIT = '''"""An owner-side Running Platform package.

The declared entry point is the package's own factory: the seams are its own
submodules, reached by package-relative imports, and the wiring is constructed
directly by the factory.
"""

from .factory import build_wiring

__all__ = ["build_wiring"]
'''

PACKAGE_FACTORY = '''"""Directly constructed wiring over this package's own seams."""

from pathlib import Path

from .double import CellIdentitySource, CellRuntimeAdapter


class _Wiring:
    """The owner's own wiring object: the seams, and nothing else."""

    def __init__(self) -> None:
        self.runtime_adapter = None
        self.identity_provider = None
        self.identity_source = None


def build_wiring(
    state_file: str,
    audit_file: str = "",
    runtime_audit_file: str = "",
) -> _Wiring:
    """Construct this cell's wiring from its own submodules, directly."""
    if not state_file:
        message = "state_file is required: the cell owns its authoritative state"
        raise ValueError(message)
    state_path = Path(state_file)
    wiring = _Wiring()
    wiring.runtime_adapter = CellRuntimeAdapter(
        state_path.parent,
        Path(runtime_audit_file) if runtime_audit_file else None,
    )
    wiring.identity_source = CellIdentitySource(
        state_path,
        Path(audit_file) if audit_file else None,
    )
    return wiring
'''


class TestOwnerModuleImports:
    def test_sibling_package_and_direct_wiring_all_run_operations(
        self, cell, attempt, instance, manifest, tmp_path
    ):
        deployed = attempt()
        paths = _operation_documents(deployed, instance, manifest)
        owner_dir = cell.module_path.parent

        # 1. a declared file whose module imports its siblings by name
        shutil.copyfile(CELL_FIXTURE, owner_dir / "rp_cell_seams.py")
        (owner_dir / "rp_cell_factory.py").write_text(SIBLING_FACTORY, encoding="utf-8")
        sibling_reference = f"{owner_dir / 'rp_cell_factory.py'}:build_layer_r_wiring"
        sibling_arguments = _entry_arguments(cell, paths)
        layer_r = sibling_arguments.index("--layer-r")
        sibling_arguments[layer_r + 1] = sibling_reference

        deploy_run = _run_entry_point(["deploy", *sibling_arguments])
        assert deploy_run.returncode == 0, deploy_run.stderr
        transcript = json.loads(deploy_run.stdout)
        assert transcript["record"]["deployed"] is True
        assert transcript["composition"]["module"] == str(
            (owner_dir / "rp_cell_factory.py").resolve()
        )
        # it really was the owner's code that served the operation
        assert "materialize:tenant_authority" in cell.runtime_calls()

        # 2. a declared package, whose factory answers with a directly built
        # wiring over its own package-relative submodules
        package_dir = owner_dir / "rp_pkg"
        package_dir.mkdir()
        shutil.copyfile(CELL_FIXTURE, package_dir / "double.py")
        (package_dir / "__init__.py").write_text(PACKAGE_INIT, encoding="utf-8")
        (package_dir / "factory.py").write_text(PACKAGE_FACTORY, encoding="utf-8")
        calls_before = len(cell.runtime_calls())
        package_arguments = _entry_arguments(cell, paths)
        layer_r = package_arguments.index("--layer-r")
        package_arguments[layer_r + 1] = "rp_pkg:build_wiring"

        restart_run = _run_entry_point(
            ["restart", *package_arguments], extra_paths=(owner_dir,)
        )
        assert restart_run.returncode == 0, restart_run.stderr
        restarted = json.loads(restart_run.stdout)
        assert restarted["record"]["deployed"] is True
        assert restarted["composition"]["module"] == str(
            (package_dir / "__init__.py").resolve()
        )
        phase = cell.runtime_calls()[calls_before:]
        assert phase[0] == "attach:tenant_authority"
        assert any(call.startswith("start:") for call in phase)

        # 3. the same declared package serves an observation too
        calls_before = len(cell.runtime_calls())
        reconcile_run = _run_entry_point(
            ["reconcile", *package_arguments], extra_paths=(owner_dir,)
        )
        assert reconcile_run.returncode == 0, reconcile_run.stderr
        observed = json.loads(reconcile_run.stdout)
        assert observed["result"] == "observed"
        # the observation re-binds its reference and touches nothing else
        assert cell.runtime_calls()[calls_before:] == ("attach:tenant_authority",)


# ---------------------------------------------------------------------------
# P2-8 / P2-14 — a root reports only what was validated
# ---------------------------------------------------------------------------


class TestCompositionConstruction:
    def test_a_wiring_that_was_never_validated_cannot_be_reported(self, tmp_path, cell):
        from deployment_operations.production.layer_r import (
            LayerREntryPoint,
            LayerRWiring,
        )

        wiring = LayerRWiring(
            declaration=LayerREntryPoint(reference=cell.reference),
            module_path=cell.module_path,
            runtime_adapter=LocalProcessRuntime(),
            identity_provider=object(),
            adapted_owner_source=False,
        )

        with pytest.raises(ProductionCompositionError) as refusal:
            ProductionCompositionRoot(layer_r=wiring)

        assert refusal.value.stage == "composition"
        stated = "\n".join(refusal.value.errors)
        assert "runtime_adapter" in stated
        assert "is defined inside the Deployment & Operations code" in stated

    def test_a_wiring_that_is_not_a_wiring_is_refused(self):
        with pytest.raises(ProductionCompositionError) as refusal:
            ProductionCompositionRoot(layer_r=object())

        assert "must be built from a resolved Layer R wiring" in (
            refusal.value.errors[0]
        )

    def test_an_owner_object_that_raises_while_inspected_is_a_refusal(self, tmp_path):
        """A raising property on an owner object is a composition refusal."""
        module = _write_module(
            tmp_path,
            "hostile_owner_object.py",
            "class Runtime:\n"
            "    def materialize(self, element): ...\n"
            "    def migrate(self, element): ...\n"
            "    def start(self, element): ...\n"
            "    def attach(self, element): ...\n"
            "    def request(self, handle, operation, *, timeout=None): ...\n"
            "    def stop(self, handle): ...\n"
            "\n"
            "class HostileSource:\n"
            "    @property\n"
            "    def observe(self):\n"
            "        message = 'this owner object refuses to be inspected'\n"
            "        raise RuntimeError(message)\n"
            "\n"
            "class Wiring:\n"
            "    pass\n"
            "\n"
            "def build_layer_r_wiring(**options):\n"
            "    wiring = Wiring()\n"
            "    wiring.runtime_adapter = Runtime()\n"
            "    wiring.identity_source = HostileSource()\n"
            "    return wiring\n",
        )

        with pytest.raises(ProductionCompositionError) as refusal:
            compose_production_root(f"{module}:build_layer_r_wiring")

        stated = "\n".join(refusal.value.errors)
        assert "could not be inspected" in stated


# ---------------------------------------------------------------------------
# P2-9 / P2-10 / P2-11 — exit codes say what actually happened
# ---------------------------------------------------------------------------


class TestEntryPointExitCodes:
    def test_a_completed_operation_exits_zero(
        self, attempt, cell, instance, manifest, capsys
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )

        assert _cli(["deploy", *arguments]) == 0

        transcript = json.loads(capsys.readouterr().out)
        assert transcript["result"] == "accepted"
        assert transcript["record"]["deployed"] is True

    def test_an_input_that_is_not_utf8_exits_two_and_starts_nothing(
        self, attempt, cell, instance, manifest, capsys
    ):
        for document in ("instance", "manifest", "environment"):
            deployed = attempt()
            paths = list(_operation_documents(deployed, instance, manifest))
            index = ("instance", "manifest", "environment").index(document)
            paths[index].write_bytes(b'{"document": "\xff\xfe not utf-8"}')

            code = _cli(["deploy", *_entry_arguments(cell, tuple(paths))])

            assert code == 2, document
            captured = capsys.readouterr()
            assert "not valid UTF-8" in captured.err, document
            assert "no deployment operation was created" in captured.err, document
            assert "Traceback (most recent call last)" not in captured.err, document
            assert cell.runtime_calls() == (), document

    def test_a_composition_refusal_exits_two_without_a_traceback(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        deployed = attempt()
        paths = _operation_documents(deployed, instance, manifest)
        module = _write_module(
            tmp_path,
            "hostile_module.py",
            "class Wiring:\n"
            "    @property\n"
            "    def runtime_adapter(self):\n"
            "        message = 'this owner object refuses to be inspected'\n"
            "        raise RuntimeError(message)\n"
            "\n"
            "def build_layer_r_wiring(**options):\n"
            "    return Wiring()\n",
        )
        arguments = _entry_arguments(cell, paths)
        layer_r = arguments.index("--layer-r")
        arguments[layer_r + 1] = f"{module}:build_layer_r_wiring"

        code = _cli(["deploy", *arguments])

        assert code == 2
        captured = capsys.readouterr()
        assert "could not be inspected" in captured.err
        assert "no deployment operation was created" in captured.err
        assert "Traceback (most recent call last)" not in captured.err
        assert cell.runtime_calls() == ()

    def test_an_identity_refusal_exits_three_and_is_never_a_generic_failure(
        self, attempt, cell, instance, manifest, capsys
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        # the owner answers for another platform: its own fact, its own refusal
        cell.edit(platform_id="another-platform")

        code = _cli(["deploy", *arguments])

        assert code == 3
        captured = capsys.readouterr()
        transcript = json.loads(captured.out)
        assert transcript["result"] == "refused"
        assert transcript["error_type"] == "IdentityVerificationFailed"
        assert "Traceback (most recent call last)" not in captured.err
        record = only_operation(deployed.environment)
        assert record.failure is not None
        assert record.failure.stage == "ready"
        assert record.deployed is False
        # a refused attempt still releases what it started — and never retires it
        assert "detach:tenant_authority" in cell.runtime_calls()
        assert "shutdown" not in cell.runtime_calls()

    def test_identity_evidence_that_cannot_be_established_exits_three(
        self, attempt, cell, instance, manifest, capsys
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        assert _cli(["deploy", *arguments]) == 0
        capsys.readouterr()
        # the owner's surface stops answering: the evidence cannot be established
        cell.edit(answer="unavailable")

        code = _cli(["reconcile", *arguments])

        assert code == 3
        captured = capsys.readouterr()
        transcript = json.loads(captured.out)
        assert transcript["result"] == "refused"
        # the refusal is about identity evidence, so it is neither exit 4 nor a
        # generic "the operation failed"
        assert transcript["error_type"] in {
            "IdentityVerificationFailed",
            "ReconciliationEvidenceUnavailable",
        }
        assert transcript["identity_refusal"] is True
        assert "Traceback (most recent call last)" not in captured.err

    def test_an_operation_failure_exits_four_and_hides_the_traceback_until_asked(
        self, attempt, cell, instance, manifest, capsys
    ):
        deployed = attempt()
        paths = _operation_documents(deployed, instance, manifest)
        failing_options = {"fail_on": "start", "fail_kind": "error"}
        arguments = _entry_arguments(cell, paths, failing_options)

        code = _cli(["deploy", *arguments])

        assert code == 4
        captured = capsys.readouterr()
        transcript = json.loads(captured.out)
        assert transcript["result"] == "failed"
        assert transcript["error_type"] == "StartupFailed"
        assert "Traceback (most recent call last)" not in captured.err
        # the controlled diagnostic names the failure and the original exception
        assert "runtime start failed" in captured.err
        assert "ValueError: this cell's runtime seam failed in start" in captured.err
        assert "the deployment operation stopped unexpectedly" not in captured.err

        # a second invocation of the *same* attempt is refused as a duplicate —
        # the operator's traceback request is served for the attempt that runs
        assert _cli(["deploy", *arguments, "--traceback"]) == 2
        capsys.readouterr()
        assert _cli(["deploy", *arguments, "--attempt", "2", "--traceback"]) == 4
        captured = capsys.readouterr()
        assert "Traceback (most recent call last)" in captured.err

    def test_check_runs_nothing_and_never_reads_an_operation_secret(
        self, attempt, cell, instance, manifest, capsys, monkeypatch
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        monkeypatch.delenv("RUNNING_PLATFORM_TOKEN", raising=False)

        code = _cli(
            [
                "deploy",
                *arguments,
                "--check",
                "--secret",
                "platform_token=RUNNING_PLATFORM_TOKEN",
            ]
        )

        assert code == 0
        captured = capsys.readouterr()
        transcript = json.loads(captured.out)
        assert "composition checked; no operation was run" in transcript["result"]
        assert "is not set" not in captured.err
        assert cell.runtime_calls() == ()

    def test_a_secret_is_read_only_for_an_operation_that_runs(
        self, attempt, cell, instance, manifest, capsys, monkeypatch
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        monkeypatch.delenv("RUNNING_PLATFORM_TOKEN", raising=False)

        code = _cli(
            [
                "deploy",
                *arguments,
                "--secret",
                "platform_token=RUNNING_PLATFORM_TOKEN",
            ]
        )

        assert code == 2
        captured = capsys.readouterr()
        assert "RUNNING_PLATFORM_TOKEN" in captured.err
        assert "is not set" in captured.err
        assert cell.runtime_calls() == ()
        assert not DeploymentStateStore.path_for(
            deployed.environment.operations_dir,
            deployment_identity(deployed.request),
        ).is_file()


# ---------------------------------------------------------------------------
# P0-3 / P2-12 — the result transcript never lands on authoritative state
# ---------------------------------------------------------------------------


class TestResultOutSafety:
    def test_a_result_path_that_names_an_input_document_is_refused(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        deployed = attempt()
        instance_path, manifest_path, environment_path = _operation_documents(
            deployed, instance, manifest
        )
        (manifest_path.parent / "sub").mkdir()
        link = tmp_path / "manifest-link.json"
        link.symlink_to(manifest_path)

        for candidate in (
            environment_path,
            manifest_path.parent / "sub" / ".." / "platform-manifest.json",
            link,
        ):
            arguments = _entry_arguments(
                cell, (instance_path, manifest_path, environment_path)
            )
            code = _cli(["deploy", *arguments, "--result-out", str(candidate)])
            assert code == 2, candidate
            captured = capsys.readouterr()
            assert "names the input document" in captured.err, candidate
            assert cell.runtime_calls() == ()
            assert not DeploymentStateStore.path_for(
                deployed.environment.operations_dir,
                deployment_identity(deployed.request),
            ).is_file()

    def test_a_rejected_result_path_is_refused_before_the_declaration_is_resolved(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        """The collision is decided before composition: Layer R is never reached."""
        deployed = attempt()
        paths = _operation_documents(deployed, instance, manifest)
        arguments = _entry_arguments(cell, paths)
        layer_r = arguments.index("--layer-r")
        arguments[layer_r + 1] = f"{tmp_path / 'absent-cell.py'}:build_layer_r_wiring"

        code = _cli(["deploy", *arguments, "--result-out", str(paths[1])])

        assert code == 2
        captured = capsys.readouterr()
        # the collision is reported, not the missing declaration: nothing was
        # composed, imported or addressed
        assert "names the input document" in captured.err
        assert "absent-cell.py" not in captured.err
        assert cell.runtime_calls() == ()

    def test_a_result_path_that_normalizes_into_authoritative_state_is_refused(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        deployed = attempt()
        paths = _operation_documents(deployed, instance, manifest)
        runtime_root = deployed.environment.runtime_root

        candidates = (
            runtime_root / "state.json",
            runtime_root / "nested" / ".." / "state.json",
            runtime_root / "locks" / "graph.json",
        )
        for candidate in candidates:
            arguments = _entry_arguments(cell, paths)
            code = _cli(["deploy", *arguments, "--result-out", str(candidate)])
            assert code == 2, candidate
            captured = capsys.readouterr()
            assert "runtime root" in captured.err, candidate
            assert cell.runtime_calls() == ()

    def test_a_result_path_that_names_the_declared_module_is_refused(
        self, attempt, cell, instance, manifest, capsys
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )

        code = _cli(["deploy", *arguments, "--result-out", str(cell.module_path)])

        assert code == 2
        captured = capsys.readouterr()
        assert "names the declared owner-side module" in captured.err
        assert cell.runtime_calls() == ()

    def test_an_unwritable_result_path_never_makes_a_completed_operation_fail(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        blocked = tmp_path / "transcript-directory"
        blocked.mkdir()

        code = _cli(["deploy", *arguments, "--result-out", str(blocked)])

        assert code == 0
        captured = capsys.readouterr()
        transcript = json.loads(captured.out)
        assert transcript["result"] == "accepted"
        assert transcript["record"]["deployed"] is True
        assert "could not be written" in captured.err
        assert "Do not retry blindly" in captured.err
        assert only_operation(deployed.environment).deployed is True

    def test_a_permitted_result_path_receives_the_transcript(
        self, attempt, cell, instance, manifest, tmp_path, capsys
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        target = tmp_path / "transcripts" / "deploy.json"

        code = _cli(["deploy", *arguments, "--result-out", str(target)])

        assert code == 0
        capsys.readouterr()
        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["result"] == "accepted"
        assert written["record"]["deployed"] is True


# ---------------------------------------------------------------------------
# P0-4 — conflicting mutations are serialized across independent processes
# ---------------------------------------------------------------------------


class TestMutationSerialization:
    def test_another_process_holding_the_operation_refuses_the_mutation(
        self, attempt, cell, instance, manifest
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )

        # Another process — here the harness, acting as a concurrent operator —
        # holds the deployment operation's mutation lock.
        with deployment_mutation(
            deployed.environment, deployment_identity(deployed.request), timeout=0
        ):
            run = _run_entry_point(["deploy", *arguments, "--lock-timeout", "0.5"])

        assert run.returncode == 2, run.stderr
        assert "another process is mutating this deployment operation" in run.stderr
        assert "started no operation" in run.stderr
        assert "Traceback (most recent call last)" not in run.stderr
        # nothing was addressed and nothing was written
        assert cell.runtime_calls() == ()
        assert not DeploymentStateStore.path_for(
            deployed.environment.operations_dir,
            deployment_identity(deployed.request),
        ).is_file()

    def test_two_concurrent_processes_are_serialized_and_keep_both_attempts(
        self, attempt, cell, instance, manifest
    ):
        """Two live processes mutate one operation: both attempts survive.

        The two invocations are started together and each waits for the
        operation's mutation lock, so whichever runs first, the record ends up
        holding both attempts — the second process re-reads what the first one
        wrote instead of overwriting it with a stale view.
        """
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        assert _cli(["deploy", *arguments]) == 0

        first = _start_entry_point(["restart", *arguments, "--lock-timeout", "60"])
        second = _start_entry_point(["restart", *arguments, "--lock-timeout", "60"])
        results = [_reap(first), _reap(second)]

        for out, err, code in results:
            assert code == 0, err
            assert json.loads(out)["record"]["deployed"] is True
        record = only_operation(deployed.environment)
        assert [entry.sequence for entry in record.restarts] == [1, 2]
        assert [entry.outcome for entry in record.restarts] == [
            RESTART_COMPLETED,
            RESTART_COMPLETED,
        ]
        assert record.attempt == 1
        assert record.running is True
        assert record.ready is True

    def test_sequential_processes_append_history_instead_of_losing_it(
        self, attempt, cell, instance, manifest
    ):
        deployed = attempt()
        arguments = _entry_arguments(
            cell, _operation_documents(deployed, instance, manifest)
        )
        assert _cli(["deploy", *arguments]) == 0
        deployment_id = deployment_identity(deployed.request)

        first = _run_entry_point(["restart", *arguments])
        assert first.returncode == 0, first.stderr
        # a second, independent process re-reads the record that the first one
        # wrote and appends its own attempt to it
        assert _cli(["restart", *arguments]) == 0

        record = only_operation(deployed.environment)
        assert record.deployment_id == deployment_id
        assert record.attempt == 1
        assert record.instance_digest == deployed.request.instance.instance_digest
        assert [entry.outcome for entry in record.restarts] == [
            RESTART_COMPLETED,
            RESTART_COMPLETED,
        ]
        assert [entry.sequence for entry in record.restarts] == [1, 2]
        restarted_ids = {
            action.detail["restart_id"]
            for action in record.operational_actions
            if action.name == "platform_restarted"
        }
        assert len(restarted_ids) == 2
        events = only_journal(deployed.environment)
        assert len([event for event in events if event["stage"] == "restart"]) > 2


# ---------------------------------------------------------------------------
# P2-13 — the CLI boundary is total
# ---------------------------------------------------------------------------


class TestEntryPointBoundary:
    def test_an_unexpected_exception_is_a_controlled_diagnostic(self, capsys):
        from deployment_operations.production import __main__ as production_cli

        code = production_cli._report_failure(
            RuntimeError("the owner's seam exploded"),
            argparse.Namespace(traceback=False),
            {"operation": "deploy"},
            None,
        )

        assert code == production_cli.EXIT_FAILED == 4
        captured = capsys.readouterr()
        assert "the owner's seam exploded" in captured.err
        assert "RuntimeError" in captured.err
        assert "Traceback (most recent call last)" not in captured.err
        transcript = json.loads(captured.out)
        assert transcript["result"] == "failed"
        assert transcript["error_type"] == "RuntimeError"

    def test_the_full_traceback_is_printed_only_when_asked(self, capsys):
        from deployment_operations.production import __main__ as production_cli

        try:
            raise RuntimeError("the owner's seam exploded")
        except RuntimeError as error:
            code = production_cli._report_failure(
                error,
                argparse.Namespace(traceback=True),
                {"operation": "deploy"},
                None,
            )

        assert code == 4
        captured = capsys.readouterr()
        assert "Traceback (most recent call last)" in captured.err

    def test_a_wrapped_identity_refusal_is_still_an_identity_refusal(self):
        from deployment_operations.production import __main__ as production_cli

        identity = IdentityVerificationFailed(
            ["the platform does not correspond to the pinned instance"]
        )
        wrapped = RuntimeError("another layer reported it")
        wrapped.__cause__ = identity

        assert production_cli._identity_refusal(wrapped) is identity
        phase = RestartFailed(
            ["the platform does not correspond to the pinned instance"],
            phase="identity_verification",
            reason="the restarted runtime does not correspond",
        )
        found = production_cli._identity_refusal(phase)
        assert isinstance(found, IdentityVerificationFailed)
        assert production_cli._identity_refusal(RuntimeError("no")) is None
