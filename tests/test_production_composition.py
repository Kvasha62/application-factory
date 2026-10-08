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

import ast
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _deployment_helpers import (
    environment_for,
    instance_for,
    manifest_for,
    only_operation,
    request_for,
)

from deployment_operations import (
    LIFECYCLE_FAILED,
    RESTART_COMPLETED,
    STRICT_STOP_START_POLICY,
    Deployment,
    DeploymentEnvironment,
    DeploymentRequest,
    IdentityVerificationFailed,
    LocalProcessRuntime,
    ReconciliationRequest,
    RestartRequest,
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
    compose_production_root,
)
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

    def _arguments(self, cell: LayerRCell, paths) -> list[str]:
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
        for name, value in sorted(cell.options.items()):
            arguments += ["--layer-r-option", f"{name}={value}"]
        return arguments

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
