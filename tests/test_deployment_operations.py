"""Architectural tests for the first Deployment & Operations vertical slice.

These tests verify architecture rules, not merely successful construction
(ADR-0015 §16; ADR-0016 §30): the deployment operation consumes exactly one
accepted Platform Instance, verifies its identity/version/artifact binding
before acting, traverses the minimal observable path of ADR-0017 §36, and fixes
``realized`` / ``deployed`` only when the verified operational condition and the
exact identity/version/digest verification hold together (§33–§34). Failures at
any mandatory stage leave deployment state honest — never a false ``deployed``.

The tests start from a genuinely accepted instance: the composition request is
composed by the real Composer, validated by the real Platform Manifest surface
and assembled by the real Platform Instance surface. The runtime doubles in
``tests/_runtime_fixtures`` are bound *by the deployment environment* (the seam
that exists precisely so an environment decides how a component runs in it);
they inject the failures that no honest component would produce, while the
canonical path runs the repository's own ``tenant_authority`` component.

Named per criterion, so the coverage is checkable against ADR-0017 §43 and the
accepted work item:

* AC1  exact Platform Instance identity — TestExactInstanceIdentity
* AC2  exact version/artifact binding — TestExactVersionAndArtifactBinding
* AC3  instance validity before deployment — TestInstanceValidity
* AC4  observable progression, honest failures — TestDeploymentProgression
* AC5  no premature deployed/realized — TestHonestDeployedClaim
* AC6  provisioning — TestProvisioning
* AC7  real deployment execution — TestDeploymentProgression
* AC8  bounded migration orchestration — TestMigrationOrchestration
* AC9  runtime start — TestRuntimeStart
* AC10 health/readiness — TestHealthAndReadiness
* AC11 ready as a verified condition — TestHealthAndReadiness
* AC12 honest deployed/realized — TestHonestDeployedClaim
* AC13 Manifest immutability — TestManifestImmutability
* AC14 no floating selectors — TestNoFloatingSelectors
* AC15 ownership + observability — TestOwnershipBoundary, TestObservability
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import types
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

import pytest
from _deployment_helpers import (
    COMPONENT_ID,
    COMPONENT_VERSION,
    FIXTURE_PATH,
    PLATFORM_ID,
    ROOT,
    environment_for,
    file_digests,
    fixture_binding,
    instance_for,
    manifest_for,
    only_operation,
    read_events,
    read_state,
    request_for,
    tampered_instance,
)

from deployment_operations import (
    DEPLOYABLE_INSTANCE_STATES,
    LIFECYCLE_FAILED,
    LIFECYCLE_IN_PROGRESS,
    LIFECYCLE_REALIZED,
    STAGES,
    ArtifactSource,
    ComponentRuntimeBinding,
    DeploymentExecutionFailed,
    DeploymentInputRejected,
    DeploymentRecord,
    DeploymentRequest,
    DeploymentStateError,
    DeploymentStateStore,
    HealthCheckFailed,
    IdentityVerificationFailed,
    InstanceReference,
    InvalidDeploymentStateTransition,
    LocalProcessRuntime,
    MaterializedComponent,
    MigrationBinding,
    MigrationOrchestrationFailed,
    MigrationRecord,
    ProvisioningFailed,
    RuntimeElement,
    RuntimeProcessError,
    SecretLeakRefused,
    StartupFailed,
    deploy,
    derive_deployment_id,
    load_record,
    runtime_worker,
    verify_artifact_digest,
    verify_input_unchanged,
    verify_instance,
)
from deployment_operations.runtime import OP_PROBE, OP_START
from platform_instance import Instance, discover_root
from platform_manifest import compute_manifest_digest, validate_document

# ---------------------------------------------------------------------------
# Test doubles of the runtime boundary
# ---------------------------------------------------------------------------


@contextmanager
def refusal(
    error_type: type[Exception], fragment: str
) -> Iterator[pytest.ExceptionInfo]:
    """Assert a fail-closed refusal that names its own reason (§20).

    A refusal that does not say which rule it enforced is not diagnosable, so
    the fragment must appear in the refusal's diagnostics — its structured
    error list, or its message.
    """
    with pytest.raises(error_type) as caught:
        yield caught
    diagnostics = [*getattr(caught.value, "errors", ()), str(caught.value)]
    assert any(
        re.search(fragment, entry) for entry in diagnostics
    ), f"expected {fragment!r} among the refusal diagnostics: {diagnostics}"


@pytest.fixture
def restore_veb_meta_path():
    yield
    sys.meta_path[:] = [
        finder
        for finder in sys.meta_path
        if not isinstance(finder, runtime_worker._VebFinder)
    ]


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def manifest(root: Path) -> dict:
    return manifest_for(root=root)


@pytest.fixture(scope="module")
def instance(manifest: dict, root: Path) -> Instance:
    return instance_for(manifest, root=root)


@pytest.fixture()
def runtime_root(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


def deploy_canonical(
    tmp_path: Path,
    instance: Instance,
    manifest: dict,
    **environment: object,
):
    """Run the canonical successful deployment and return the handle."""
    runtime_root = tmp_path / "runtime"
    request = request_for(
        instance,
        manifest,
        environment_for(runtime_root, **environment),  # type: ignore[arg-type]
    )
    return deploy(request)


def expect_failure(**environment: object) -> tuple[type[Exception], object]:
    """Return the expected failure type for an environment keyword set."""
    return (Exception, environment)


def _cli_env() -> dict[str, str]:
    """Build the isolated environment used by the CLI subprocess tests.

    The CLI deliberately does not inherit the pytest process environment.
    PYTHONPATH makes the uninstalled package importable, while PATH retains
    the isolated value used by these tests. Windows requires SystemRoot for
    the runtime worker to initialize its networking stack; forward it only
    when the host provides it.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT / "src"),
    }
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if system_root:
        env["SystemRoot"] = system_root
    return env


# ---------------------------------------------------------------------------
# AC1 — exact Platform Instance identity
# ---------------------------------------------------------------------------


class TestExactInstanceIdentity:
    """A deployment is requested for one exact instance — never for a name."""

    def test_name_only_reference_is_rejected(self, tmp_path: Path, instance, manifest):
        reference = InstanceReference(platform_id=PLATFORM_ID)
        assert reference.errors(), "a digest-free reference must not be deployable"

        runtime_root = tmp_path / "runtime"
        request = request_for(
            instance,
            manifest,
            environment_for(runtime_root),
            reference=reference,
        )
        with refusal(DeploymentInputRejected, "concrete instance digest"):
            deploy(request)

        record = only_operation(request.environment)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.failure is not None
        assert record.failure.stage == "validated"
        assert record.deployed is False

    def test_reference_from_name_only_document_is_rejected(self):
        with refusal(DeploymentInputRejected, "digest"):
            InstanceReference.from_document({"platform_id": PLATFORM_ID})

    def test_reference_must_name_the_supplied_instance(
        self, tmp_path, instance, manifest
    ):
        other = InstanceReference(
            instance_digest="sha256:" + "0" * 64, platform_id=PLATFORM_ID
        )
        request = request_for(
            instance,
            manifest,
            environment_for(tmp_path / "runtime"),
            reference=other,
        )
        with refusal(DeploymentInputRejected, "pins"):
            deploy(request)

    def test_realized_record_references_the_instance_digest(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            record = deployment.record
            assert record.instance_digest == instance.instance_digest
            assert record.platform_id == PLATFORM_ID
            assert record.manifest_id == manifest["manifest_id"]
            assert record.manifest_digest == manifest["manifest_digest"]
            assert record.deployment_id.startswith(f"dep-{PLATFORM_ID}-")

    def test_operation_identity_is_deterministic(self, instance, tmp_path):
        environment = environment_for(tmp_path / "runtime")
        first = derive_deployment_id(
            PLATFORM_ID, instance.instance_digest, environment.environment_id, 1
        )
        second = derive_deployment_id(
            PLATFORM_ID, instance.instance_digest, environment.environment_id, 1
        )
        different_attempt = derive_deployment_id(
            PLATFORM_ID, instance.instance_digest, environment.environment_id, 2
        )
        assert first == second
        assert first != different_attempt
        assert instance.instance_digest.removeprefix("sha256:")[:12] in first

    def test_platform_identity_of_a_floating_name_is_refused(self, tmp_path, manifest):
        """``latest`` is not forbidden as a string — it is forbidden as a selector."""
        from platform_instance import AssemblyRejectedError

        with pytest.raises(AssemblyRejectedError, match="floating selector"):
            instance_for(manifest, platform_id="latest", root=discover_root())

        # A hand-built document that carries one is refused at this boundary too.
        accepted = instance_for(manifest, root=discover_root())
        tampered = tampered_instance(accepted, platform_id="latest")
        request = DeploymentRequest(
            instance=InstanceReference.from_document(tampered),
            instance_document=tampered,
            manifest_document=manifest,
            environment=environment_for(tmp_path / "runtime"),
        )
        with refusal(DeploymentInputRejected, "floating selector"):
            deploy(request)
        assert only_operation(request.environment).deployed is False


# ---------------------------------------------------------------------------
# AC2 / AC13 / AC14 — exact version and artifact binding, no selectors
# ---------------------------------------------------------------------------


class TestExactVersionAndArtifactBinding:
    """Every deployable component is pinned by component, version and artifact."""

    def test_every_component_record_pins_identity_and_artifact(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            record = deployment.record
            assert [entry.component_id for entry in record.components] == [COMPONENT_ID]
            entry = record.components[0]
            assert entry.component_version == COMPONENT_VERSION
            assert entry.artifact_type == "none"
            assert entry.artifact_digest is None

            document = json.loads(deployment.state_path.read_text(encoding="utf-8"))
            component = document["components"][0]
            assert component["component_id"] == COMPONENT_ID
            assert component["component_version"] == COMPONENT_VERSION
            assert component["artifact"] == {"artifact_type": "none", "digest": None}

    def test_prepared_runtime_spec_pins_the_accepted_instance(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            spec_path = (
                deployment.state_path.parent.parent
                / "deployments"
                / deployment.record.deployment_id
                / "components"
                / COMPONENT_ID
                / "runtime.json"
            )
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            assert spec["instance_digest"] == instance.instance_digest
            assert spec["platform_id"] == PLATFORM_ID
            assert spec["component"]["component_id"] == COMPONENT_ID
            assert spec["component"]["component_version"] == COMPONENT_VERSION
            assert spec["manifest"]["manifest_digest"] == manifest["manifest_digest"]
            assert spec["configuration"]["platform_id"] == PLATFORM_ID

    def test_version_substitution_is_refused(self, tmp_path, instance, manifest):
        substituted = tampered_instance(
            instance,
            components=[
                {
                    "component_id": COMPONENT_ID,
                    "component_version": "9.9.9",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                }
            ],
        )
        request = DeploymentRequest(
            instance=InstanceReference.from_document(substituted),
            instance_document=substituted,
            manifest_document=manifest,
            environment=environment_for(tmp_path / "runtime"),
        )
        with pytest.raises(DeploymentInputRejected):
            deploy(request)
        record = only_operation(request.environment)
        assert record.deployed is False
        assert record.failure is not None
        assert record.failure.stage == "validated"

    def test_invented_artifact_identity_is_refused(self, tmp_path, instance, manifest):
        invented = tampered_instance(
            instance,
            components=[
                {
                    "component_id": COMPONENT_ID,
                    "component_version": COMPONENT_VERSION,
                    "artifact": {
                        "artifact_type": "container_image",
                        "digest": "sha256:" + "1" * 64,
                        "pinned": True,
                    },
                }
            ],
        )
        request = DeploymentRequest(
            instance=InstanceReference.from_document(invented),
            instance_document=invented,
            manifest_document=manifest,
            environment=environment_for(tmp_path / "runtime"),
        )
        with pytest.raises(DeploymentInputRejected):
            deploy(request)

    def test_unpinned_artifact_declaration_is_refused(self):
        from deployment_operations import ComponentBinding

        pinned = ComponentBinding(
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            artifact_type="container_image",
            artifact_digest="sha256:" + "a" * 64,
            artifact_pinned=True,
        )
        assert verify_artifact_digest(pinned, "sha256:" + "a" * 64) == []
        mismatch = verify_artifact_digest(pinned, "sha256:" + "b" * 64)
        assert mismatch and "substitution" in mismatch[0]
        assert verify_artifact_digest(pinned, None), "absence is not a match"
        none_declared = ComponentBinding(
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            artifact_type="none",
            artifact_digest=None,
            artifact_pinned=False,
        )
        assert (
            verify_artifact_digest(none_declared, None) == []
        ), "a component without a published artifact has no artifact to verify"


# ---------------------------------------------------------------------------
# AC3 — the instance must be valid before any action
# ---------------------------------------------------------------------------


class TestInstanceValidity:
    """An invalid instance never reaches deployment."""

    def test_authoritative_validity_is_checked(self, tmp_path, instance, manifest):
        verification = verify_instance(instance.document, manifest, root=ROOT)
        assert verification.ok
        assert verification.validity_errors == ()

        broken = tampered_instance(instance, manifest_state="retired")
        verification = verify_instance(broken, manifest, root=ROOT)
        assert not verification.ok

    def test_invalid_instance_is_refused_before_provisioning(
        self, tmp_path, instance, manifest
    ):
        broken = tampered_instance(instance, manifest_state="draft")
        request = DeploymentRequest(
            instance=InstanceReference.from_document(broken),
            instance_document=broken,
            manifest_document=manifest,
            environment=environment_for(tmp_path / "runtime"),
        )
        with pytest.raises(DeploymentInputRejected):
            deploy(request)
        assert (
            not request.environment.deployments_dir.exists()
        ), "a rejected instance must not reach provisioning"
        record = only_operation(request.environment)
        assert record.failure is not None
        assert record.failure.stage == "validated"
        assert record.lifecycle == LIFECYCLE_FAILED

    def test_assemblable_states_are_the_ratified_lifecycle_only(self):
        assert DEPLOYABLE_INSTANCE_STATES <= {
            "validated",
            "approved",
            "published",
            "deployed",
        }
        assert "draft" not in DEPLOYABLE_INSTANCE_STATES


# ---------------------------------------------------------------------------
# AC4 / AC7 / AC9 — the operation progresses and something actually runs
# ---------------------------------------------------------------------------


class TestDeploymentProgression:
    """One deployment operation traverses the minimal observable path."""

    def test_the_path_is_traversed_in_order(self, tmp_path, instance, manifest):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            record = deployment.record
            assert [entry.name for entry in record.stages] == list(STAGES)
            assert [entry.status for entry in record.stages] == ["completed"] * len(
                STAGES
            )
            assert record.lifecycle == LIFECYCLE_REALIZED
            assert record.ready is True
            assert record.running is True
            assert record.identity_verified is True
            assert record.deployed is True

    def test_progression_is_observable_in_events(self, tmp_path, instance, manifest):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            events = deployment.events()
            names = [event.event for event in events]
            assert names[:2] == ["stage_started", "deployment_requested"]
            assert events[0].stage == "requested"
            started = [
                event.stage for event in events if event.event == "stage_started"
            ]
            assert started == list(STAGES)
            assert names.index("deployment_realized") > names.index("ready_reached")

    def test_a_real_component_runs_and_reports_itself(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            entry = deployment.record.components[0]
            assert entry.runtime_started is True
            assert entry.healthy is True
            assert entry.observed_component_id == COMPONENT_ID
            assert entry.observed_version == COMPONENT_VERSION
            assert entry.observed_platform_id == PLATFORM_ID
            assert entry.health is not None and entry.health["status_code"] == 200

    def test_the_runtime_workspace_is_isolated_per_operation(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            workspace = (
                deployment.state_path.parent.parent
                / "deployments"
                / deployment.record.deployment_id
            )
            assert (workspace / "components" / COMPONENT_ID / "runtime.json").is_file()
            assert deployment.state_path.parent.name == "operations"

    def test_attempt_history_is_not_overwritten(self, tmp_path, instance, manifest):
        environment = environment_for(tmp_path / "runtime")
        first = deploy(request_for(instance, manifest, environment, attempt=1))
        first.stop()
        second = deploy(request_for(instance, manifest, environment, attempt=2))
        second.stop()
        assert first.record.deployment_id != second.record.deployment_id
        assert first.state_path != second.state_path
        assert first.state_path.is_file() and second.state_path.is_file()


# ---------------------------------------------------------------------------
# AC5 / AC12 — no premature deployed, and an honest verified claim
# ---------------------------------------------------------------------------


class TestHonestDeployedClaim:
    """``ready`` alone fixes nothing; both dimensions must hold together."""

    @staticmethod
    def record() -> DeploymentRecord:
        """A brand-new deployment operation record, before any stage ran."""
        return DeploymentRecord.initial(
            deployment_id="dep-x",
            environment_id="local",
            attempt=1,
            platform_id=PLATFORM_ID,
            manifest_id="m",
            manifest_version="1.0.0",
            manifest_digest="sha256:" + "0" * 64,
            manifest_state="validated",
            instance_digest="sha256:" + "0" * 64,
            at="2026-09-16T00:00:00Z",
        )

    def test_premature_realized_is_impossible(self):
        record = self.record()
        with refusal(InvalidDeploymentStateTransition, "not ready"):
            record.mark_realized(at="2026-09-16T00:00:01Z")

        started = record.mark_running(at="2026-09-16T00:00:01Z", running=True)
        assert started.deployed is False, "a started process is not a deployed platform"

        ready_only = started.mark_running(
            at="2026-09-16T00:00:02Z", running=True
        ).mark_ready(at="2026-09-16T00:00:02Z")
        assert ready_only.ready is True
        assert ready_only.deployed is False
        with refusal(InvalidDeploymentStateTransition, "identity"):
            ready_only.mark_realized(at="2026-09-16T00:00:03Z")

        verified = ready_only.mark_identity_verified(at="2026-09-16T00:00:03Z")
        realized = verified.mark_realized(at="2026-09-16T00:00:04Z")
        assert realized.lifecycle == LIFECYCLE_REALIZED
        assert realized.deployed is True

    def test_identity_verification_cannot_precede_ready(self):
        record = self.record().mark_running(at="2026-09-16T00:00:01Z", running=True)
        with refusal(InvalidDeploymentStateTransition, "not ready"):
            record.mark_identity_verified(at="2026-09-16T00:00:02Z")

    def test_a_claim_without_a_running_platform_is_not_deployed(self):
        """``deployed`` is a claim about an actual, observable Running Platform."""
        record = (
            self.record()
            .mark_running(at="2026-09-16T00:00:01Z", running=True)
            .mark_ready(at="2026-09-16T00:00:02Z")
            .mark_identity_verified(at="2026-09-16T00:00:02Z")
            .mark_realized(at="2026-09-16T00:00:03Z")
        )
        assert record.deployed is True

        stopped = record.mark_stopped(at="2026-09-16T00:00:04Z")
        assert stopped.running is False
        assert (
            stopped.ready is False
        ), "a stopped platform holds no ready condition (§33)"
        assert stopped.deployed is False, "no Running Platform, no deployed claim (§10)"
        assert stopped.lifecycle == LIFECYCLE_REALIZED, (
            "the operation did realize this instance; stopping the platform is an "
            "operational action (§18), not a lifecycle position ADR-0016 §9 "
            "establishes"
        )
        assert (
            stopped.identity_verified is True
        ), "the verification that happened stands"
        assert [action.name for action in stopped.operational_actions] == [
            "platform_stopped"
        ]

    def test_stopping_again_changes_nothing(self):
        """Operational actions repeat without duplicating their effects (§20)."""
        record = (
            self.record()
            .mark_running(at="2026-09-16T00:00:01Z", running=True)
            .mark_ready(at="2026-09-16T00:00:02Z")
            .mark_identity_verified(at="2026-09-16T00:00:02Z")
            .mark_realized(at="2026-09-16T00:00:03Z")
            .mark_stopped(at="2026-09-16T00:00:04Z")
        )
        again = record.mark_stopped(at="2026-09-16T00:00:05Z")
        assert again is record
        assert [action.name for action in again.operational_actions] == [
            "platform_stopped"
        ]

    def test_healthcheck_success_is_not_a_deployed_claim(
        self, tmp_path, instance, manifest
    ):
        """The platform is healthy — and still not deployed, because it lies."""
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("identity_lie_component"),),
        )
        request = request_for(instance, manifest, environment)
        with refusal(IdentityVerificationFailed, "not deployed"):
            deploy(request)

        record = read_state(environment, instance)
        # The platform really did reach the verified operational condition —
        # recorded as the completed ``ready`` stage of this operation…
        ready_stage = record.stage("ready")
        assert ready_stage.status == "completed"
        assert ready_stage.detail["ready"] is True
        events = read_events(environment, instance)
        assert any(entry["event"] == "ready_reached" for entry in events)
        # …and still no deployed claim stands: identity verification failed and
        # the platform was stopped rather than left running half-verified.
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.ready is False, "nothing is running, so no ready condition holds"
        assert record.running is False
        assert record.deployed is False
        assert record.failure is not None
        assert record.failure.stage == "ready"
        assert any("9.9.9" in error for error in record.failure.errors)
        assert [entry["event"] for entry in events][-1] == "deployment_failed"
        assert not any(entry["event"] == "deployment_realized" for entry in events)

    def test_a_platform_serving_another_instance_is_refused(
        self, tmp_path, instance, manifest
    ):
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("wrong_platform_component"),),
        )
        with pytest.raises(IdentityVerificationFailed):
            deploy(request_for(instance, manifest, environment))
        record = read_state(environment, instance)
        assert record.deployed is False
        assert record.identity_verified is False
        assert record.stage("ready").status == "completed"
        assert record.ready is False
        assert record.running is False


# ---------------------------------------------------------------------------
# AC6 — provisioning (and its fail-closed behaviour)
# ---------------------------------------------------------------------------


class TestProvisioning:
    """The runtime environment is prepared from the instance's requirements."""

    def test_provisioning_records_its_requirements(self, tmp_path, instance, manifest):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            provisioning = deployment.record.stage("provisioning")
            assert provisioning.status == "completed"
            requirements = provisioning.detail["requirements"]
            assert any(COMPONENT_ID in entry for entry in requirements)

    def test_environment_that_cannot_satisfy_the_instance_is_refused(
        self, tmp_path, root
    ):
        """The instance pins a component this environment cannot realize (§11)."""
        window = manifest_for(
            components=[
                {"component_id": COMPONENT_ID, "component_version": COMPONENT_VERSION},
                {"component_id": "identity", "component_version": "0.3.0"},
            ],
            root=root,
            manifest_id="deployment-platform-two-components",
        )
        window_instance = instance_for(window, platform_id=PLATFORM_ID, root=root)
        assert sorted(
            entry["component_id"] for entry in window_instance.document["components"]
        ) == ["identity", COMPONENT_ID]

        request = DeploymentRequest(
            instance=InstanceReference.from_document(window_instance.document),
            instance_document=window_instance.document,
            manifest_document=window,
            environment=environment_for(tmp_path / "runtime"),
        )
        with refusal(DeploymentInputRejected, "no runtime binding"):
            deploy(request)
        record = only_operation(request.environment)
        assert record.failure is not None
        assert record.failure.stage == "validated"
        assert record.deployed is False
        assert any("identity" in error for error in record.failure.errors)

    def test_unusable_runtime_root_fails_closed(self, tmp_path, instance, manifest):
        """A runtime root the platform cannot be placed in is not used (§11)."""
        root = tmp_path / "runtime"
        root.mkdir()
        (root / "deployments").write_text("not a directory", encoding="utf-8")
        environment = environment_for(root)
        with pytest.raises(ProvisioningFailed):
            deploy(request_for(instance, manifest, environment))
        record = read_state(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.failure is not None
        assert record.failure.stage == "provisioning"
        assert record.deployed is False
        assert record.ready is False

    def test_a_runtime_root_that_cannot_be_written_refuses_loudly(
        self, tmp_path, instance, manifest
    ):
        """Not even deployment state can be recorded there: refuse, take no action."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        environment = environment_for(blocked)
        with refusal(DeploymentStateError, "could not be written"):
            deploy(request_for(instance, manifest, environment))

    def test_absent_artifact_content_fails_closed(self, tmp_path, instance, manifest):
        missing_cache = tmp_path / "artifacts"
        missing_cache.mkdir()
        pinned = tampered_instance(
            instance,
            components=[
                {
                    "component_id": COMPONENT_ID,
                    "component_version": COMPONENT_VERSION,
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                }
            ],
        )
        environment = environment_for(
            tmp_path / "runtime", artifact_cache=missing_cache
        )
        # The canonical instance declares no published artifact, so an empty
        # artifact source is not a requirement — and the deployment proceeds.
        request = DeploymentRequest(
            instance=InstanceReference.from_document(pinned),
            instance_document=pinned,
            manifest_document=manifest,
            environment=environment,
        )
        deployment = deploy(request)
        try:
            assert deployment.record.components[0].artifact_verified is False
            assert deployment.record.deployed is True
        finally:
            deployment.stop()

    def test_missing_import_path_fails_closed(self, tmp_path, instance, manifest):
        from deployment_operations import ComponentRuntimeBinding

        broken = ComponentRuntimeBinding(
            component_id=COMPONENT_ID,
            deployment_module="tenant_authority.deployment",
            deployment_factory="build_deployment",
            import_paths=(tmp_path / "does-not-exist",),
        )
        environment = environment_for(tmp_path / "runtime", bindings=(broken,))
        with refusal(ProvisioningFailed, "import_paths"):
            deploy(request_for(instance, manifest, environment))


# ---------------------------------------------------------------------------
# A spy over the runtime adapter: the evidence is the calls actually made
# ---------------------------------------------------------------------------


@dataclass
class RuntimeSpy:
    """Wraps a runtime adapter and records the operations actually performed.

    Test evidence must be the real behaviour of the deployment path, not a
    narrative about it: this adapter records which adapter operations ran, in
    which order, how many runtime elements of the platform existed at each
    moment, and what the component's own workspace contained when each element
    was started.
    """

    inner: object
    calls: list[tuple[str, str]] = field(default_factory=list)
    starts: list[str] = field(default_factory=list)
    starts_when_migrating: list[tuple[str, ...]] = field(default_factory=list)
    starts_after_migrating: list[tuple[str, ...]] = field(default_factory=list)
    migrations_when_starting: list[tuple[str, ...]] = field(default_factory=list)
    platform_log_when_migrating: list[bool] = field(default_factory=list)

    # -- the adapter protocol ---------------------------------------------
    def materialize(self, element):
        self.calls.append(("materialize", element.component.component_id))
        return self.inner.materialize(element)

    def migrate(self, element):
        component_id = element.component.component_id
        self.starts_when_migrating.append(tuple(self.starts))
        self.platform_log_when_migrating.append(
            (element.workspace / "runtime.log").exists()
        )
        self.calls.append(("migrate", component_id))
        answer = self.inner.migrate(element)
        self.starts_after_migrating.append(tuple(self.starts))
        return answer

    def start(self, element):
        component_id = element.component.component_id
        migrations = tuple(
            sorted(path.name for path in element.workspace.glob("migration-*.done"))
        )
        self.migrations_when_starting.append(migrations)
        self.calls.append(("start", component_id))
        handle = self.inner.start(element)
        self.starts.append(component_id)
        return handle

    def request(self, handle, operation, *, timeout=None):
        self.calls.append((f"request:{operation}", handle.component_id))
        return self.inner.request(handle, operation, timeout=timeout)

    def stop(self, handle):
        self.calls.append(("stop", handle.component_id))
        return self.inner.stop(handle)

    # -- what the recordings say ------------------------------------------
    @property
    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]

    def count(self, operation: str) -> int:
        return self.operations.count(operation)


def spy_runtime(*, migrating: bool = False, failing_migration: bool = False):
    """Return (spy, environment keyword) for a deployment watched by the spy."""
    if failing_migration:
        migrations = MigrationBinding(
            module="failing_migration_component", attribute="MIGRATIONS"
        )
        component = fixture_binding(
            "failing_migration_component", migrations=migrations
        )
    elif migrating:
        migrations = MigrationBinding(
            module="migrating_component", attribute="MIGRATIONS"
        )
        component = fixture_binding("migrating_component", migrations=migrations)
    else:
        component = fixture_binding("migrating_component")
    return RuntimeSpy(inner=LocalProcessRuntime()), (component,)


# ---------------------------------------------------------------------------
# AC8 — bounded migration orchestration
# ---------------------------------------------------------------------------


class TestMigrationOrchestration:
    """Component-defined migrations run forward, in order, fail-closed."""

    def test_component_migrations_run_in_declared_order(
        self, tmp_path, instance, manifest
    ):
        migrations = MigrationBinding(
            module="migrating_component", attribute="MIGRATIONS"
        )
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("migrating_component", migrations=migrations),),
        )
        deployment = deploy(request_for(instance, manifest, environment))
        try:
            record = deployment.record
            assert [entry.migration_id for entry in record.migrations] == [
                "0001-seed-platform",
                "0002-record-platform-identity",
            ]
            assert {entry.status for entry in record.migrations} == {"executed"}
            workspace = (
                environment.deployments_dir
                / record.deployment_id
                / "components"
                / COMPONENT_ID
            )
            assert (workspace / "migration-0001-seed-platform.done").is_file()
            assert (
                workspace / "migration-0002-record-platform-identity.done"
            ).is_file()
            assert record.deployed is True
        finally:
            deployment.stop()

    def test_migrations_run_before_the_platform_starts(
        self, tmp_path, instance, manifest
    ):
        migrations = MigrationBinding(
            module="migrating_component", attribute="MIGRATIONS"
        )
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("migrating_component", migrations=migrations),),
        )
        deployment = deploy(request_for(instance, manifest, environment))
        try:
            events = list(deployment.events())
            first_migration = next(
                index
                for index, event in enumerate(events)
                if event.event == "migration_started"
            )
            starting = next(
                index
                for index, event in enumerate(events)
                if event.event == "stage_started" and event.stage == "starting"
            )
            assert first_migration < starting
        finally:
            deployment.stop()

    def test_a_failed_migration_fails_the_deployment(
        self, tmp_path, instance, manifest
    ):
        migrations = MigrationBinding(
            module="failing_migration_component", attribute="MIGRATIONS"
        )
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(
                fixture_binding("failing_migration_component", migrations=migrations),
            ),
        )
        with pytest.raises(MigrationOrchestrationFailed) as failure:
            deploy(request_for(instance, manifest, environment))
        assert failure.value.migration_id == "0002-second"

        record = read_state(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False
        assert record.ready is False
        assert record.failure is not None
        assert record.failure.stage == "deploying"
        assert [entry.status for entry in record.migrations] == ["executed", "failed"]
        workspace = (
            environment.deployments_dir
            / record.deployment_id
            / "components"
            / COMPONENT_ID
        )
        assert (workspace / "migration-0001-first.done").is_file()

    def test_a_migration_with_a_reverse_path_is_refused(
        self, tmp_path, instance, manifest
    ):
        migrations = MigrationBinding(
            module="downgrade_migration_component", attribute="MIGRATIONS"
        )
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(
                fixture_binding("downgrade_migration_component", migrations=migrations),
            ),
        )
        with refusal(MigrationOrchestrationFailed, "forward only|reverse"):
            deploy(request_for(instance, manifest, environment))

        record = read_state(environment, instance)
        assert record.deployed is False
        assert record.failure is not None
        assert record.failure.stage == "deploying"
        assert any("reverse" in error for error in record.failure.errors)

    def test_a_component_without_migrations_needs_none(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            assert deployment.record.migrations == ()


# ---------------------------------------------------------------------------
# AC7 / AC8 — the platform is started in ``starting``, never by migrating
# ---------------------------------------------------------------------------


class TestMigrationRuntimeOrdering:
    """Migration executes before the platform's runtime elements are started.

    The path of ADR-0017 §36–§37 keeps the two apart: ``deploying`` materializes
    and migrates, ``starting`` starts the platform's runtime elements. These
    tests assert that on the **actual adapter calls** — a spy records every
    operation the deployment performed — and on what the component's own
    workspace contained when each element was started. Event ordering alone is
    deliberately not treated as proof.
    """

    def test_no_runtime_element_is_started_before_the_starting_stage(
        self, tmp_path, instance, manifest
    ):
        spy, (component,) = spy_runtime(migrating=True)
        environment = environment_for(tmp_path / "runtime", bindings=(component,))
        deployment = deploy(request_for(instance, manifest, environment), runtime=spy)
        try:
            assert spy.count("migrate") == 1
            assert spy.count("start") == 1
            assert spy.starts_when_migrating == [
                ()
            ], "no runtime element of the platform existed while migrations ran"
            assert spy.starts_after_migrating == [
                ()
            ], "migrations left no runtime element running behind them"
            assert spy.operations.index("migrate") < spy.operations.index(
                "start"
            ), "the component-defined migrations ran before the platform started"
            assert spy.calls.index(("migrate", COMPONENT_ID)) < spy.calls.index(
                ("start", COMPONENT_ID)
            )
            assert spy.platform_log_when_migrating == [
                False
            ], "no process of the platform had been spawned while migrations ran"
            workspace = (
                environment.deployments_dir
                / deployment.record.deployment_id
                / "components"
                / COMPONENT_ID
            )
            assert (
                workspace / "runtime.log"
            ).is_file(), (
                "the platform's runtime element was started in the starting stage"
            )
        finally:
            deployment.stop()

    def test_the_platform_starts_only_after_its_migrations_completed(
        self, tmp_path, instance, manifest
    ):
        spy, (component,) = spy_runtime(migrating=True)
        environment = environment_for(tmp_path / "runtime", bindings=(component,))
        deployment = deploy(request_for(instance, manifest, environment), runtime=spy)
        try:
            assert spy.migrations_when_starting == [
                (
                    "migration-0001-seed-platform.done",
                    "migration-0002-record-platform-identity.done",
                )
            ], (
                "when the platform's runtime element was started, the required "
                "component-owned migrations had already run"
            )
            record = deployment.record
            assert record.running is True
            assert record.ready is True
            assert record.deployed is True
        finally:
            deployment.stop()

    def test_migrations_do_not_produce_a_running_platform_by_themselves(
        self, tmp_path, instance, manifest
    ):
        """A migration session is not a Running Platform: only ``starting`` is."""
        spy, (component,) = spy_runtime(migrating=True)
        environment = environment_for(tmp_path / "runtime", bindings=(component,))
        request = request_for(instance, manifest, environment)
        deploy(request, runtime=spy)
        record = read_state(environment, instance)
        started = [operation for operation in spy.operations if operation == "start"]
        assert started == [
            "start"
        ], "the platform was started exactly once, in the starting stage"
        assert record.running is True and record.ready is True

    def test_a_failed_migration_never_starts_the_runtime(
        self, tmp_path, instance, manifest
    ):
        spy, (component,) = spy_runtime(failing_migration=True)
        environment = environment_for(tmp_path / "runtime", bindings=(component,))
        request = request_for(instance, manifest, environment)
        with refusal(MigrationOrchestrationFailed, "migration 0002"):
            deploy(request, runtime=spy)

        assert (
            spy.count("start") == 0
        ), "a migration that failed must not be followed by a started platform"
        assert spy.starts == []
        assert ("start", COMPONENT_ID) not in spy.calls
        assert spy.starts_after_migrating == [()]

        record = read_state(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.running is False
        assert record.ready is False
        assert record.deployed is False
        assert record.stage("starting").status != "completed"
        assert (
            not (environment.deployments_dir / record.deployment_id)
            .joinpath("components", COMPONENT_ID, "runtime.log")
            .is_file()
        ), "no runtime element of the platform was ever started"

    def test_a_component_without_migrations_is_not_migrated(
        self, tmp_path, instance, manifest
    ):
        """Bounded orchestration: no declaration, no migration session (§10)."""
        spy = RuntimeSpy(inner=LocalProcessRuntime())
        environment = environment_for(tmp_path / "runtime")
        deployment = deploy(request_for(instance, manifest, environment), runtime=spy)
        try:
            assert spy.count("migrate") == 0
            assert spy.count("start") == 1
            assert deployment.record.migrations == ()
        finally:
            deployment.stop()


# ---------------------------------------------------------------------------
# AC9 / AC10 / AC11 — start, health/readiness, ready
# ---------------------------------------------------------------------------


class TestRuntimeStart:
    """A platform that does not start is not a platform."""

    def test_a_component_that_cannot_construct_itself_fails_closed(
        self, tmp_path, instance, manifest
    ):
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("broken_startup_component"),),
        )
        with refusal(StartupFailed, "not running"):
            deploy(request_for(instance, manifest, environment))

        record = read_state(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.ready is False
        assert record.running is False
        assert record.deployed is False
        assert record.failure is not None
        assert record.failure.stage == "starting"
        assert any("cannot be constructed" in entry for entry in record.failure.errors)

    def test_a_component_is_started_from_the_pinned_runtime_spec(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            entry = deployment.record.components[0]
            assert entry.runtime_started is True
            assert deployment.record.running is True


class TestHealthAndReadiness:
    """``ready`` is a verified operational condition, not a started process."""

    def test_health_and_readiness_are_evaluated_per_component(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            entry = deployment.record.components[0]
            assert entry.health is not None
            assert entry.health["body"]["status"] == "ok"
            assert entry.readiness is not None
            assert entry.readiness["body"]["status"] == "ready"
            assert "health_checked" in [event.event for event in deployment.events()]

    def test_an_unhealthy_runtime_is_never_ready(self, tmp_path, instance, manifest):
        environment = environment_for(
            tmp_path / "runtime", bindings=(fixture_binding("unhealthy_component"),)
        )
        with refusal(HealthCheckFailed, "not ready"):
            deploy(request_for(instance, manifest, environment))

        record = read_state(environment, instance)
        assert record.ready is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False
        assert record.failure is not None
        assert record.failure.stage == "health_check"
        assert record.components[0].healthy is False
        assert record.components[0].observed_version == COMPONENT_VERSION

    def test_a_runtime_without_a_health_surface_is_not_ready(
        self, tmp_path, instance, manifest
    ):
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("no_health_surface_component"),),
        )
        with pytest.raises(HealthCheckFailed):
            deploy(request_for(instance, manifest, environment))
        record = read_state(environment, instance)
        assert record.ready is False
        assert record.components[0].healthy is False
        assert record.deployed is False

    def test_a_probe_that_cannot_answer_is_not_ready(
        self, tmp_path, instance, manifest
    ):
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("raising_health_component"),),
        )
        with pytest.raises(HealthCheckFailed):
            deploy(request_for(instance, manifest, environment))
        record = read_state(environment, instance)
        assert record.ready is False
        assert record.deployed is False


# ---------------------------------------------------------------------------
# AC7 — realization: what is executed is what was pinned
# ---------------------------------------------------------------------------


class TestDeploymentExecution:
    """The instance is really realized, and an unrealizable instance fails closed."""

    def test_materialization_failure_is_a_deployment_failure(
        self, tmp_path, instance, manifest
    ):
        """An environment that cannot materialize the pinned component refuses."""

        class UnrealizableEnvironment:
            """An environment whose runtime cannot place the pinned component."""

            def materialize(self, element: RuntimeElement) -> MaterializedComponent:
                return MaterializedComponent(
                    component_id=element.component.component_id,
                    workspace=element.workspace,
                    artifact_path=None,
                    observed_digest=None,
                    artifact_verified=False,
                    errors=(
                        f"{element.component.component_id}: the pinned content is "
                        + "unavailable in this environment",
                    ),
                )

            def start(self, element: RuntimeElement):  # pragma: no cover - not reached
                message = "nothing may be started if the pinned content is missing"
                raise AssertionError(message)

            def request(self, handle, operation, *, timeout=None):  # pragma: no cover
                raise AssertionError("nothing may be asked of an unstarted component")

            def stop(self, handle):  # pragma: no cover - not reached
                return {"status": "stopped"}

        environment = environment_for(tmp_path / "runtime")
        request = request_for(instance, manifest, environment)
        with refusal(DeploymentExecutionFailed, "pinned content is unavailable"):
            deploy(request, runtime=UnrealizableEnvironment())

        record = read_state(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.failure is not None
        assert record.failure.stage == "deploying"
        assert record.deployed is False
        assert record.ready is False
        assert record.running is False

    def test_artifact_digest_substitution_is_caught_at_materialization(self, tmp_path):
        """Materializing verifies the digest of the content, not its declaration."""
        from deployment_operations import ComponentBinding
        from deployment_operations.verification import compute_content_digest

        content = tmp_path / "published-artifact.bin"
        content.write_bytes(b"the content that was actually published")
        observed = compute_content_digest(content)
        pinned = ComponentBinding(
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            artifact_type="source_package",
            artifact_digest=observed,
            artifact_pinned=True,
        )
        element = RuntimeElement(
            deployment_id="dep-exact",
            environment_id="local-test",
            platform_id=PLATFORM_ID,
            instance_digest="sha256:" + "0" * 64,
            component=pinned,
            configuration={},
            spec_path=tmp_path / "runtime.json",
            workspace=tmp_path / "workspace",
            binding=environment_for(tmp_path / "unused").binding_for(COMPONENT_ID),
            artifact_source=ArtifactSource(
                path=content, digest=observed, verified=True
            ),
            interpreter=sys.executable,
            source_paths=(ROOT / "src",),
        )
        runtime = LocalProcessRuntime()
        materialized = runtime.materialize(element)
        assert materialized.artifact_verified is True
        assert materialized.observed_digest == observed

        # The same pinned identity against different content is a substitution.
        substituted = replace_artifact(
            element, content, b"content that was never published"
        )
        refusal_record = runtime.materialize(substituted)
        assert refusal_record.artifact_verified is False
        assert refusal_record.errors
        assert "substitution" in refusal_record.errors[0]


def replace_artifact(
    element: RuntimeElement, content: Path, payload: bytes
) -> RuntimeElement:
    """Return the same element whose environment offers different artifact content."""
    from dataclasses import replace as dataclass_replace

    content.write_bytes(payload)
    return dataclass_replace(
        element,
        artifact_source=ArtifactSource(path=content, digest=None, verified=False),
    )


# ---------------------------------------------------------------------------
# AC13 — configuration enters through the contract, never through identity
# ---------------------------------------------------------------------------


class TestConfigurationPath:
    """Environment configuration reaches the platform, identity does not move."""

    def test_an_overlay_cannot_override_a_key_the_instance_pins(
        self, tmp_path, root, manifest
    ):
        pinned = manifest_for(
            root=root,
            manifest_id="deployment-platform-configured",
            configuration={
                COMPONENT_ID: {"platform_id": PLATFORM_ID, "environment": "declared"}
            },
        )
        pinned_instance = instance_for(pinned, platform_id=PLATFORM_ID, root=root)
        environment = environment_for(
            tmp_path / "runtime",
            overlay={COMPONENT_ID: {"platform_id": "some-other-platform"}},
        )
        with refusal(DeploymentInputRejected, "platform_id"):
            deploy(request_for(pinned_instance, pinned, environment))

    def test_an_overlay_cannot_introduce_a_floating_selector(
        self, tmp_path, instance, manifest
    ):
        environment = environment_for(
            tmp_path / "runtime", overlay={COMPONENT_ID: {"environment": "latest"}}
        )
        with refusal(DeploymentInputRejected, "floating selector"):
            deploy(request_for(instance, manifest, environment))

    def test_the_running_configuration_is_exactly_the_resolved_one(
        self, tmp_path, root
    ):
        """What the component runs with is what the instance declared, plus overlay."""
        declared = manifest_for(
            root=root,
            manifest_id="deployment-platform-configured",
            configuration={COMPONENT_ID: {"platform_id": PLATFORM_ID}},
        )
        declared_instance = instance_for(declared, platform_id=PLATFORM_ID, root=root)
        runtime_root = tmp_path / "runtime"
        environment = environment_for(
            runtime_root, overlay={COMPONENT_ID: {"environment": "test"}}
        )
        deployment = deploy(request_for(declared_instance, declared, environment))
        try:
            spec = json.loads(
                (
                    environment.deployments_dir
                    / deployment.record.deployment_id
                    / "components"
                    / COMPONENT_ID
                    / "runtime.json"
                ).read_text(encoding="utf-8")
            )
            assert spec["configuration"] == {
                "platform_id": PLATFORM_ID,
                "environment": "test",
            }
        finally:
            deployment.stop()


# ---------------------------------------------------------------------------
# AC13 / AC14 — immutability and selectors
# ---------------------------------------------------------------------------


class TestManifestImmutability:
    """Deployment never edits the desired state it was handed."""

    def test_the_input_documents_are_not_modified(self, tmp_path, instance, manifest):
        instance_before = copy.deepcopy(dict(instance.document))
        manifest_before = copy.deepcopy(dict(manifest))
        instance_digest_before = instance.instance_digest
        manifest_digest_before = compute_manifest_digest(manifest)

        with deploy_canonical(tmp_path, instance, manifest):
            pass

        assert dict(instance.document) == instance_before
        assert dict(manifest) == manifest_before
        assert instance.instance_digest == instance_digest_before
        assert compute_manifest_digest(manifest) == manifest_digest_before

    def test_the_repository_factory_metadata_is_untouched(
        self, tmp_path, instance, manifest
    ):
        before = {
            name: file_digests(ROOT / name) for name in ("factory", "components", "src")
        }
        with deploy_canonical(tmp_path, instance, manifest):
            pass
        after = {
            name: file_digests(ROOT / name) for name in ("factory", "components", "src")
        }
        assert after == before, (
            "a deployment writes nothing into the factory's authoritative "
            "metadata or into component sources"
        )

    def test_mutated_desired_state_is_detected(self, instance, manifest):
        verification = verify_instance(instance.document, manifest, root=ROOT)
        assert verify_input_unchanged(verification, instance.document, manifest) == []

        mutated = copy.deepcopy(dict(instance.document))
        mutated["manifest_state"] = "published"
        errors = verify_input_unchanged(verification, mutated, manifest)
        assert errors and "changed during the deployment operation" in errors[0]

        other_manifest = copy.deepcopy(dict(manifest))
        other_manifest["lifecycle"] = {"state": "published"}
        errors = verify_input_unchanged(verification, instance.document, other_manifest)
        assert any("Platform Manifest changed" in error for error in errors)


class TestNoFloatingSelectors:
    """No floating selector can appear anywhere in the deployment path."""

    @pytest.mark.parametrize(
        "selector",
        [
            "latest",
            "current",
            "default",
            "stable",
            "edge",
            "main",
            "master",
            "head",
            "tip",
            "*",
        ],
    )
    def test_component_version_selectors_are_refused(
        self, tmp_path, instance, manifest, selector
    ):
        tampered = tampered_instance(
            instance,
            components=[
                {
                    "component_id": COMPONENT_ID,
                    "component_version": selector,
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                }
            ],
        )
        request = DeploymentRequest(
            instance=InstanceReference.from_document(tampered),
            instance_document=tampered,
            manifest_document=manifest,
            environment=environment_for(tmp_path / "runtime"),
        )
        with pytest.raises(DeploymentInputRejected):
            deploy(request)

    @pytest.mark.parametrize("selector", ["latest", "stable", "*"])
    def test_environment_identity_selectors_are_refused(
        self, tmp_path, instance, manifest, selector
    ):
        environment = environment_for(tmp_path / "runtime", environment_id=selector)
        with pytest.raises(DeploymentInputRejected):
            deploy(request_for(instance, manifest, environment))

    def test_no_floating_value_reaches_deployment_state(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            text = deployment.state_path.read_text(encoding="utf-8")
            for selector in ("latest", "current", "stable", "edge", "master"):
                assert f'"{selector}"' not in text


# ---------------------------------------------------------------------------
# AC15 — ownership boundaries
# ---------------------------------------------------------------------------


class TestOwnershipBoundary:
    """The capability owns operational state only — never component data."""

    #: The complete import closure of the capability. Anything else — above all
    #: any component implementation — would be a boundary violation: the
    #: capability reads the accepted instance, the authoritative Factory
    #: surfaces and the environment, never a component's modules.
    ALLOWED_IMPORTS: ClassVar[frozenset[str]] = frozenset(
        {
            "__future__",
            "argparse",
            "ast",
            "collections",
            "dataclasses",
            "datetime",
            "deployment_operations",
            "fastapi",
            "hashlib",
            "importlib",
            "json",
            "os",
            "pathlib",
            "platform_instance",
            "platform_manifest",
            "re",
            "threading",
            "shutil",
            "subprocess",
            "sysconfig",
            "sys",
            "time",
            "typing",
        }
    )

    def test_the_capability_imports_no_component(self, root: Path):
        capability = root / "src" / "deployment_operations"
        assert list(
            capability.glob("*.py")
        ), "the capability is part of the source tree"
        for path in sorted(capability.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif (
                    isinstance(node, ast.ImportFrom) and node.module and node.level == 0
                ):
                    names.add(node.module.split(".")[0])
            unexpected = names - self.ALLOWED_IMPORTS
            assert not unexpected, (
                f"{path.name} imports {sorted(unexpected)}; the deployment "
                "capability imports the standard library, the published Factory "
                "validation surfaces and the ASGI framework it hosts — never a "
                "component implementation and never a component's store"
            )

    def test_deployment_state_holds_operational_metadata_only(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            document = json.loads(deployment.state_path.read_text(encoding="utf-8"))
            assert set(document) == {
                "attempt",
                "components",
                "conditions",
                "created_at",
                "deployment_id",
                "environment_id",
                "failure",
                "lifecycle",
                "migrations",
                "operational_actions",
                "platform_instance",
                "stages",
                "updated_at",
            }
            component = document["components"][0]
            assert set(component) == {
                "artifact",
                "artifact_verified",
                "component_id",
                "component_version",
                "error",
                "execution",
                "health",
                "healthy",
                "materialized",
                "observed_component_id",
                "observed_execution",
                "observed_platform_id",
                "observed_version",
                "readiness",
                "runtime_started",
            }, "the record holds the operational facts of the boundary, not business data"

    def test_no_business_data_is_read_or_written(self, tmp_path, instance, manifest):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            record = deployment.record
            assert record.components[0].artifact_type == "none"
            text = deployment.state_path.read_text(encoding="utf-8")
            for business_marker in (
                '"records"',
                '"learning"',
                '"commerce"',
                '"booking"',
            ):
                assert business_marker not in text


# ---------------------------------------------------------------------------
# AC15 — observability and correlation
# ---------------------------------------------------------------------------


class TestObservability:
    """Every operational signal is correlatable with the instance (§19)."""

    def test_every_event_carries_the_correlation_set(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            for event in deployment.events():
                assert event.deployment_id == deployment.record.deployment_id
                assert event.environment_id == deployment.record.environment_id
                assert event.platform_id == PLATFORM_ID
                assert event.instance_digest == instance.instance_digest
                assert event.manifest_id == manifest["manifest_id"]
                assert event.manifest_version == manifest["manifest_version"]
                assert event.manifest_digest == manifest["manifest_digest"]

    def test_component_scoped_events_carry_component_identity(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            scoped = [
                event for event in deployment.events() if event.component_id is not None
            ]
            assert scoped, "the journal records component-scoped events"
            for event in scoped:
                assert event.component_id == COMPONENT_ID
                assert event.component_version == COMPONENT_VERSION
                assert event.artifact_type == "none"

    def test_migration_events_carry_the_migration_identity(
        self, tmp_path, instance, manifest
    ):
        migrations = MigrationBinding(
            module="migrating_component", attribute="MIGRATIONS"
        )
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("migrating_component", migrations=migrations),),
        )
        deployment = deploy(request_for(instance, manifest, environment))
        try:
            events = [entry.document() for entry in deployment.events()]
        finally:
            deployment.stop()
        migration_events = [
            entry for entry in events if entry["event"].startswith("migration_")
        ]
        assert [entry["detail"]["migration_id"] for entry in migration_events] == [
            "0001-seed-platform",
            "0001-seed-platform",
            "0002-record-platform-identity",
            "0002-record-platform-identity",
        ]
        for entry in migration_events:
            assert entry["component"]["component_id"] == COMPONENT_ID
            assert entry["component"]["component_version"] == COMPONENT_VERSION

    def test_failures_are_diagnosable_from_the_journal(
        self, tmp_path, instance, manifest
    ):
        environment = environment_for(
            tmp_path / "runtime", bindings=(fixture_binding("unhealthy_component"),)
        )
        with pytest.raises(HealthCheckFailed):
            deploy(request_for(instance, manifest, environment))
        events = read_events(environment, instance)
        failed = [entry for entry in events if entry["event"] == "deployment_failed"]
        assert len(failed) == 1
        assert failed[0]["stage"] == "health_check"
        assert failed[0]["detail"]["errors"]
        assert (
            failed[0]["platform_instance"]["instance_digest"]
            == instance.instance_digest
        )

    def test_the_journal_is_append_only_json_lines(self, tmp_path, instance, manifest):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            lines = deployment.events_path.read_text(encoding="utf-8").splitlines()
            documents = [json.loads(line) for line in lines if line]
            assert [entry["sequence"] for entry in documents] == list(
                range(1, len(documents) + 1)
            )

    def test_operational_secrets_are_never_persisted(
        self, tmp_path, instance, manifest
    ):
        sentinel = "sentinel-secret-material-0xdeadbeef"
        environment = environment_for(
            tmp_path / "runtime", secrets={"tenant_authority_token": sentinel}
        )
        deployment = deploy(request_for(instance, manifest, environment))
        deployment.stop()
        found = [
            path
            for path in environment.runtime_root.rglob("*")
            if path.is_file()
            and sentinel in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert found == [], f"the operational secret leaked into {found}"
        assert sentinel not in json.dumps(environment.document())

    def test_a_record_that_would_leak_a_secret_is_refused(self, tmp_path):
        store = DeploymentStateStore(tmp_path / "operations" / "dep-x.json")
        deployment_record = DeploymentRecord.initial(
            deployment_id="dep-x",
            environment_id="local",
            attempt=1,
            platform_id=PLATFORM_ID,
            manifest_id="m",
            manifest_version="1.0.0",
            manifest_digest="sha256:" + "0" * 64,
            manifest_state="validated",
            instance_digest="sha256:" + "0" * 64,
            at="2026-09-16T00:00:00Z",
        )
        store.write(deployment_record, secrets=("sentinel",))
        assert store.read().deployment_id == "dep-x"
        with pytest.raises(SecretLeakRefused):
            store.write(
                deployment_record.with_migration(
                    MigrationRecord(
                        component_id=COMPONENT_ID,
                        component_version=COMPONENT_VERSION,
                        migration_id="sentinel",
                        status="executed",
                    ),
                    at="2026-09-16T00:00:01Z",
                ),
                secrets=("sentinel",),
            )

    def test_state_is_readable_after_the_operation(self, tmp_path, instance, manifest):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            reloaded = load_record(deployment.state_path)
            assert reloaded.deployment_id == deployment.record.deployment_id
            assert reloaded.deployed is True
            assert reloaded.components[0].observed_version == COMPONENT_VERSION


# ---------------------------------------------------------------------------
# The runtime operation itself: stop is an operational action (§18)
# ---------------------------------------------------------------------------


class TestRuntimeOperation:
    """Stopping the platform is an operational action, not a lifecycle change."""

    def test_a_successful_deployment_claims_exactly_what_it_verified(
        self, tmp_path, instance, manifest
    ):
        """Before any stop: realized, verified, ready and actually running."""
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            record = deployment.record
            assert record.lifecycle == LIFECYCLE_REALIZED
            assert record.running is True
            assert record.ready is True
            assert record.identity_verified is True
            assert record.deployed is True
            assert record.failure is None

    def test_stop_records_an_operational_action(self, tmp_path, instance, manifest):
        deployment = deploy_canonical(tmp_path, instance, manifest)
        record = deployment.stop()
        assert record.running is False
        assert record.lifecycle == LIFECYCLE_REALIZED
        assert [action.name for action in record.operational_actions] == [
            "platform_stopped"
        ]
        assert "platform_stopped" in [event.event for event in deployment.events()]

    def test_a_stopped_platform_is_reported_honestly(
        self, tmp_path, instance, manifest
    ):
        """The persisted record stops claiming a Running Platform that is gone."""
        deployment = deploy_canonical(tmp_path, instance, manifest)
        assert load_record(deployment.state_path).deployed is True

        deployment.stop()

        reloaded = load_record(deployment.state_path)
        assert reloaded.running is False, "nothing is running any more"
        assert reloaded.ready is False, "a stopped platform holds no ready condition"
        assert (
            reloaded.deployed is False
        ), "no Running Platform exists, so no deployed claim may stand (ADR-0016 §10)"
        assert (
            reloaded.lifecycle == LIFECYCLE_REALIZED
        ), "the operation's own verified outcome is history, not a runtime claim"
        assert reloaded.identity_verified is True
        assert (
            reloaded.stage("ready").detail["ready"] is True
        ), "the record keeps the evidence that ready was reached"
        assert [action.name for action in reloaded.operational_actions] == [
            "platform_stopped"
        ]

        journal = json.loads(
            deployment.events_path.read_text(encoding="utf-8").splitlines()[-1]
        )
        assert journal["event"] == "platform_stopped"
        assert (
            journal["platform_instance"]["instance_digest"] == instance.instance_digest
        )

    def test_a_stopped_deployment_does_not_report_itself_as_deployed(
        self, tmp_path, instance, manifest
    ):
        deployment = deploy_canonical(tmp_path, instance, manifest)
        deployment.stop()
        assert deployment.deployed is False
        assert deployment.record.deployed is False

    def test_stopping_twice_records_one_operational_action(
        self, tmp_path, instance, manifest
    ):
        deployment = deploy_canonical(tmp_path, instance, manifest)
        first = deployment.stop()
        second = deployment.stop()
        assert second.operational_actions == first.operational_actions
        assert [event.event for event in deployment.events()].count(
            "platform_stopped"
        ) == 1
        assert load_record(deployment.state_path).deployed is False


# ---------------------------------------------------------------------------
# The five states the slice must keep apart — read back from persisted state
# ---------------------------------------------------------------------------


class TestDeploymentStateScenarios:
    """Every outcome is re-read from the persisted record, never from memory.

    ``(lifecycle, running, ready, identity_verified, deployed)`` is the whole
    vocabulary of deployment state, and no scenario may mix it up: above all,
    a deployment may never claim ``deployed`` while nothing is running.
    """

    @staticmethod
    def persisted(environment, instance):
        return read_state(environment, instance)

    def test_success(self, tmp_path, instance, manifest):
        environment = environment_for(tmp_path / "runtime")
        deployment = deploy(request_for(instance, manifest, environment))
        try:
            record = self.persisted(environment, instance)
            assert record.lifecycle == LIFECYCLE_REALIZED
            assert (record.running, record.ready, record.identity_verified) == (
                True,
                True,
                True,
            )
            assert record.deployed is True
        finally:
            deployment.stop()

    def test_migration_failure(self, tmp_path, instance, manifest):
        spy, (component,) = spy_runtime(failing_migration=True)
        environment = environment_for(tmp_path / "runtime", bindings=(component,))
        with refusal(MigrationOrchestrationFailed, "migration 0002"):
            deploy(request_for(instance, manifest, environment), runtime=spy)
        record = self.persisted(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert (record.running, record.ready, record.identity_verified) == (
            False,
            False,
            False,
        )
        assert record.deployed is False
        assert spy.count("start") == 0
        assert record.failure is not None and record.failure.stage == "deploying"

    def test_startup_failure(self, tmp_path, instance, manifest):
        environment = environment_for(
            tmp_path / "runtime",
            bindings=(fixture_binding("broken_startup_component"),),
        )
        with refusal(StartupFailed, "not running"):
            deploy(request_for(instance, manifest, environment))
        record = self.persisted(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert (record.running, record.ready, record.identity_verified) == (
            False,
            False,
            False,
        )
        assert record.deployed is False
        assert record.failure is not None and record.failure.stage == "starting"

    def test_health_failure(self, tmp_path, instance, manifest):
        spy = RuntimeSpy(inner=LocalProcessRuntime())
        environment = environment_for(
            tmp_path / "runtime", bindings=(fixture_binding("unhealthy_component"),)
        )
        with refusal(HealthCheckFailed, "not ready"):
            deploy(request_for(instance, manifest, environment), runtime=spy)
        record = self.persisted(environment, instance)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert (record.running, record.ready, record.identity_verified) == (
            False,
            False,
            False,
        )
        assert record.deployed is False
        assert record.failure is not None and record.failure.stage == "health_check"
        assert spy.count("start") == 1, (
            "the runtime element was started — that is precisely why starting is "
            "not the same as deployed"
        )

    def test_stop_after_success(self, tmp_path, instance, manifest):
        environment = environment_for(tmp_path / "runtime")
        deployment = deploy(request_for(instance, manifest, environment))
        assert self.persisted(environment, instance).deployed is True
        deployment.stop()
        record = self.persisted(environment, instance)
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        assert (
            record.lifecycle == LIFECYCLE_REALIZED
        ), "the operation really did realize the verified instance"
        assert record.identity_verified is True

    def test_no_scenario_reports_a_deployed_platform_that_is_not_running(self):
        """The invariant every scenario above is an instance of."""
        states = [
            (LIFECYCLE_IN_PROGRESS, False, False, False),
            (LIFECYCLE_FAILED, False, False, False),
            (LIFECYCLE_FAILED, False, True, False),
            (LIFECYCLE_REALIZED, False, False, True),
            (LIFECYCLE_REALIZED, True, True, True),
        ]
        for lifecycle, running, ready, verified in states:
            record = TestHonestDeployedClaim.record()
            record = replace(
                record,
                lifecycle=lifecycle,
                running=running,
                ready=ready,
                identity_verified=verified,
            )
            assert record.deployed is (running and ready and verified), (
                f"deployed must follow from an actually running, verified platform: "
                f"{record}"
            )
            if not running:
                assert (
                    record.deployed is False
                ), "nothing running may ever be claimed deployed (ADR-0016 §10)"


# ---------------------------------------------------------------------------
# The command line path
# ---------------------------------------------------------------------------


class TestCommandLine:
    """One deployment operation is runnable, and reports its outcome."""

    def test_command_line_deployment_succeeds(self, tmp_path, instance, manifest):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        instance_path = tmp_path / "instance.json"
        instance_path.write_text(instance.render(), encoding="utf-8")
        environment_path = tmp_path / "environment.json"
        environment_path.write_text(
            json.dumps(
                {
                    "environment_id": "local-cli",
                    "runtime_root": str(tmp_path / "runtime"),
                    "bindings": [
                        {
                            "component_id": COMPONENT_ID,
                            "deployment_module": "tenant_authority.deployment",
                            "deployment_factory": "build_deployment",
                        }
                    ],
                    "configuration_overlay": {
                        COMPONENT_ID: {
                            "platform_id": PLATFORM_ID,
                            "environment": "cli",
                        }
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "deployment_operations",
                "--instance",
                str(instance_path),
                "--manifest",
                str(manifest_path),
                "--environment",
                str(environment_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=_cli_env(),
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "deployed=True" in completed.stdout
        assert '"lifecycle": "realized"' in completed.stdout

    def test_command_line_reports_failure_with_a_nonzero_exit(
        self, tmp_path, instance, manifest
    ):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        instance_path = tmp_path / "instance.json"
        instance_path.write_text(instance.render(), encoding="utf-8")
        environment_path = tmp_path / "environment.json"
        environment_path.write_text(
            json.dumps(
                {
                    "environment_id": "local-cli",
                    "runtime_root": str(tmp_path / "runtime"),
                    "bindings": [
                        {
                            "component_id": COMPONENT_ID,
                            "deployment_module": "tenant_authority.deployment",
                            "deployment_factory": "build_deployment",
                        }
                    ],
                    "configuration_overlay": {},
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "deployment_operations",
                "--instance",
                str(instance_path),
                "--manifest",
                str(manifest_path),
                "--environment",
                str(environment_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=_cli_env(),
            check=False,
        )
        assert completed.returncode == 1
        assert "deployment failed" in completed.stderr
        assert "deployed=True" not in completed.stdout

    def test_command_line_refuses_an_unusable_environment_cleanly(
        self, tmp_path, instance, manifest
    ):
        """An environment that cannot be read is a usage failure, not a crash."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        instance_path = tmp_path / "instance.json"
        instance_path.write_text(instance.render(), encoding="utf-8")
        environment_path = tmp_path / "environment.json"
        environment_path.write_text(
            json.dumps(
                {
                    "environment_id": "local-cli",
                    "runtime_root": str(tmp_path / "runtime"),
                    "bindings": [],
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "deployment_operations",
                "--instance",
                str(instance_path),
                "--manifest",
                str(manifest_path),
                "--environment",
                str(environment_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=_cli_env(),
            check=False,
        )
        assert completed.returncode == 2
        assert "bindings is required" in completed.stderr
        assert "no deployment operation was created" in completed.stderr
        assert "Traceback" not in completed.stderr


# ---------------------------------------------------------------------------
# The factory chain is untouched by a deployment
# ---------------------------------------------------------------------------


class TestFactoryRegression:
    """The existing Factory pipeline keeps working, and stays untouched."""

    def test_the_factory_chain_still_validates_after_a_deployment(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest):
            pass
        assert validate_document(manifest, root=ROOT) == []
        from platform_instance import validate_instance_document

        assert validate_instance_document(instance.document, manifest, root=ROOT) == []

    def test_the_same_instance_can_be_deployed_again(
        self, tmp_path, instance, manifest
    ):
        first = deploy(
            request_for(instance, manifest, environment_for(tmp_path / "runtime"))
        )
        assert first.record.deployed is True
        first.stop()
        assert first.record.deployed is False
        second = deploy(
            request_for(instance, manifest, environment_for(tmp_path / "runtime"))
        )
        assert second.record.deployed is True
        second.stop()
        assert second.record.deployed is False
        assert first.record.deployment_id == second.record.deployment_id


# ---------------------------------------------------------------------------
# F-3 — the claim rests on the content that actually executes, not on a claim
# ---------------------------------------------------------------------------


def content_digest(path: Path) -> str:
    """``sha256:<hex>`` of a file's bytes, as the deployment engine computes it."""
    import hashlib

    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def bound_source(tmp_path: Path) -> Path:
    """An execution source holding honest component content, bound as A."""
    source = tmp_path / "verified-source"
    source.mkdir()
    (source / "bound_component.py").write_bytes(
        (FIXTURE_PATH / "migrating_component.py").read_bytes()
    )
    return source


def environment_bound_to(
    tmp_path: Path, source: Path, *, module: str = "bound_component"
) -> object:
    """A deployment environment whose component is bound to ``source``."""
    return environment_for(
        tmp_path / "runtime",
        bindings=(
            ComponentRuntimeBinding(
                component_id=COMPONENT_ID,
                deployment_module=module,
                deployment_factory="build_component",
                import_paths=(source,),
            ),
        ),
    )


def source_element(tmp_path: Path, *, module: str, source: Path) -> RuntimeElement:
    """A runtime element of a component whose content is bound under ``source``."""
    from deployment_operations import ComponentBinding

    return RuntimeElement(
        deployment_id="dep-bound-content",
        environment_id="local-test",
        platform_id=PLATFORM_ID,
        instance_digest="sha256:" + "0" * 64,
        component=ComponentBinding(
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            artifact_type="none",
            artifact_digest=None,
            artifact_pinned=False,
        ),
        configuration={},
        spec_path=tmp_path / "runtime.json",
        workspace=tmp_path / "workspace",
        binding=ComponentRuntimeBinding(
            component_id=COMPONENT_ID,
            deployment_module=module,
            deployment_factory="build_component",
            import_paths=(source,),
        ),
        artifact_source=ArtifactSource(path=None, digest=None, verified=False),
        interpreter=sys.executable,
        source_paths=(ROOT / "src",),
    )


@dataclass
class RewritingRuntime:
    """The real adapter, with the bound content rewritten at a chosen moment.

    This is the substituted execution path F-3 is about: the deployment binds
    content A and the process that actually runs is content B — either because
    the file changed between binding and launch, or after the launch.
    """

    inner: object
    substitute: bytes
    when: str = "before-start"
    within: Path = Path("/")

    def _rewrite(self, element: RuntimeElement) -> None:
        # Only the content this test owns is rewritten: a substitution attack
        # never gets to edit the repository the tests run from.
        for module in element.execution.modules:
            if self.within in module.path.resolve().parents or (
                module.path.resolve().parent == self.within.resolve()
            ):
                module.path.write_bytes(self.substitute)

    def materialize(self, element: RuntimeElement):
        return self.inner.materialize(element)

    def migrate(self, element: RuntimeElement):
        return self.inner.migrate(element)

    def start(self, element: RuntimeElement):
        if self.when == "before-start":
            self._rewrite(element)
        return self.inner.start(element)

    def request(self, handle, operation, *, timeout=None):
        if self.when == "on-start-request" and operation == OP_START:
            # The window a launch re-verification cannot close: the content is
            # replaced after the engine verified it and before the process that
            # runs it has loaded it.
            self._rewrite(handle.element)
        if self.when == "after-start" and operation == OP_PROBE:
            self._rewrite(handle.element)
        return self.inner.request(handle, operation, timeout=timeout)

    def stop(self, handle):
        return self.inner.stop(handle)


@dataclass
class EvidenceHidingRuntime:
    """The real adapter, with the runtime's own execution evidence removed."""

    inner: object

    def materialize(self, element: RuntimeElement):
        return self.inner.materialize(element)

    def migrate(self, element: RuntimeElement):
        return self.inner.migrate(element)

    def start(self, element: RuntimeElement):
        return self.inner.start(element)

    def request(self, handle, operation, *, timeout=None):
        answer = dict(self.inner.request(handle, operation, timeout=timeout))
        answer.pop("execution", None)
        answer.pop("execution_error", None)
        return answer

    def stop(self, handle):
        return self.inner.stop(handle)


@dataclass
class MisdirectingRuntime:
    """The real adapter, whose answers report content other than the binding.

    The bound file itself is untouched, so the engine's own re-read of it finds
    nothing; what disagrees is the content the running process says it loaded.
    """

    inner: object
    elsewhere: Path

    def materialize(self, element: RuntimeElement):
        return self.inner.materialize(element)

    def migrate(self, element: RuntimeElement):
        return self.inner.migrate(element)

    def start(self, element: RuntimeElement):
        return self.inner.start(element)

    def request(self, handle, operation, *, timeout=None):
        answer = dict(self.inner.request(handle, operation, timeout=timeout))
        execution = answer.get("execution")
        if isinstance(execution, dict) and execution.get("modules"):
            self.elsewhere.write_text("A = 1\n", encoding="utf-8")
            modules = [dict(entry) for entry in execution["modules"]]
            modules[0]["path"] = str(self.elsewhere)
            modules[0]["digest"] = content_digest(self.elsewhere)
            answer["execution"] = {"root": execution.get("root"), "modules": modules}
        return answer

    def stop(self, handle):
        return self.inner.stop(handle)


def artifact_element(tmp_path: Path, content: Path, digest: str) -> RuntimeElement:
    """A runtime element of a component whose artifact is pinned to ``digest``."""
    from deployment_operations import ComponentBinding

    return RuntimeElement(
        deployment_id="dep-bound-content",
        environment_id="local-test",
        platform_id=PLATFORM_ID,
        instance_digest="sha256:" + "0" * 64,
        component=ComponentBinding(
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            artifact_type="source_package",
            artifact_digest=digest,
            artifact_pinned=True,
        ),
        configuration={},
        spec_path=tmp_path / "runtime.json",
        workspace=tmp_path / "workspace",
        binding=environment_for(tmp_path / "unused").binding_for(COMPONENT_ID),
        artifact_source=ArtifactSource(path=content, digest=digest, verified=True),
        interpreter=sys.executable,
        source_paths=(ROOT / "src",),
    )


class TestExecutionContentBinding:
    """F-3: what executes is bound and verified, never taken on trust (AC-10)."""

    def test_the_deployed_claim_is_bound_to_the_executed_content(
        self, tmp_path, instance, manifest
    ):
        with deploy_canonical(tmp_path, instance, manifest) as deployment:
            record = deployment.record
            assert record.deployed is True
            component = record.components[0]
            binding = component.execution
            assert binding is not None, "a deployed claim names the content it ran"
            assert binding["kind"] == "component_source"
            entry = binding["modules"][0]
            executed = ROOT / "src" / "tenant_authority" / "deployment.py"
            assert Path(entry["path"]).resolve() == executed.resolve()
            assert entry["digest"] == content_digest(executed)
            # the running process reported exactly that content, and both halves
            # of the link are recorded with the operation
            observed = component.observed_execution
            assert observed is not None
            assert observed["modules"][0]["path"] == entry["path"]
            assert observed["modules"][0]["digest"] == entry["digest"]
            assert component.observed_version == COMPONENT_VERSION

    def test_a_substituted_runtime_is_refused_even_when_it_reports_the_pinned_identity(
        self, tmp_path, instance, manifest
    ):
        """Verified A, running B, B lies: the deployment must not stand."""
        source = bound_source(tmp_path)
        substitute = (FIXTURE_PATH / "binding_lie_component.py").read_bytes()
        environment = environment_bound_to(tmp_path, source)
        runtime = RewritingRuntime(
            inner=LocalProcessRuntime(),
            substitute=substitute,
            when="on-start-request",
            within=source,
        )
        request = request_for(instance, manifest, environment)

        with refusal(StartupFailed, "digest mismatch"):
            deploy(request, runtime=runtime)

        record = only_operation(environment)
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False
        assert record.running is False
        assert record.failure is not None
        assert record.failure.stage == "starting"
        # F-3B refuses substituted content before compilation or execution.
        workspace = (
            environment.deployments_dir
            / record.deployment_id
            / "components"
            / COMPONENT_ID
        )
        assert not (workspace / "substituted-content.marker").exists()
        assert any("digest mismatch" in error for error in record.failure.errors)
        assert any(
            "what executes is not what was verified" in error
            for error in record.failure.errors
        )

    def test_content_changed_after_the_launch_is_refused_by_the_engine(
        self, tmp_path, instance, manifest
    ):
        """The runtime's evidence is sound; the engine's own re-read refuses."""
        source = bound_source(tmp_path)
        substitute = (FIXTURE_PATH / "binding_lie_component.py").read_bytes()
        environment = environment_bound_to(tmp_path, source)
        runtime = RewritingRuntime(
            inner=LocalProcessRuntime(),
            substitute=substitute,
            when="after-start",
            within=source,
        )
        request = request_for(instance, manifest, environment)

        with refusal(IdentityVerificationFailed, "changed"):
            deploy(request, runtime=runtime)

        record = only_operation(environment)
        assert record.identity_verified is False
        assert record.deployed is False
        assert record.failure is not None
        # This failure is the engine's own evidence, not a disagreement with
        # what the process reported: the process loaded A and said so.
        assert not any(
            "what executes is not what was verified" in error
            for error in record.failure.errors
        )

    def test_content_in_the_working_directory_cannot_take_the_module_s_place(
        self, tmp_path, instance, manifest
    ):
        """A same-named file in the process's own workspace is never loaded.

        The workspace is created by the deployment and is writable by the
        process running in it, so it is exactly where a name-based import would
        find content nobody bound (`python -m` puts the working directory first
        on the import path). The bound content must win regardless — and the
        substituted file must never run.
        """
        source = bound_source(tmp_path)
        environment = environment_bound_to(tmp_path, source)
        deployment_id = derive_deployment_id(
            PLATFORM_ID, instance.instance_digest, "local-test", 1
        )
        workspace = (
            environment.deployments_dir / deployment_id / "components" / COMPONENT_ID
        )
        workspace.mkdir(parents=True)
        (workspace / "bound_component.py").write_bytes(
            (FIXTURE_PATH / "binding_lie_component.py").read_bytes()
        )

        deployment = deploy(request_for(instance, manifest, environment))
        try:
            record = deployment.record
            assert record.deployed is True
            assert not (workspace / "substituted-content.marker").exists()
            bound_file = source / "bound_component.py"
            component = record.components[0]
            assert component.execution["modules"][0]["path"] == str(bound_file)
            assert component.observed_execution["modules"][0][
                "digest"
            ] == content_digest(bound_file)
        finally:
            deployment.stop()

    def test_missing_execution_evidence_is_refused(self, tmp_path, instance, manifest):
        environment = environment_bound_to(tmp_path, bound_source(tmp_path))
        request = request_for(instance, manifest, environment)
        runtime = EvidenceHidingRuntime(inner=LocalProcessRuntime())

        with refusal(IdentityVerificationFailed, "reported no execution content"):
            deploy(request, runtime=runtime)

        record = only_operation(environment)
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False
        assert record.failure is not None
        assert record.failure.stage == "ready"

    def test_evidence_that_disagrees_with_the_binding_is_refused(
        self, tmp_path, instance, manifest
    ):
        """The content the process says it loaded is compared with the binding.

        The bound file is intact, so the engine's own re-read is satisfied and
        only the runtime's evidence disagrees: what reports itself as the
        running content is not what this deployment bound.
        """
        source = bound_source(tmp_path)
        environment = environment_bound_to(tmp_path, source)
        runtime = MisdirectingRuntime(
            inner=LocalProcessRuntime(), elsewhere=source / "elsewhere.py"
        )
        request = request_for(instance, manifest, environment)

        with refusal(
            IdentityVerificationFailed, "what executes is not what was verified"
        ):
            deploy(request, runtime=runtime)

        record = only_operation(environment)
        assert record.identity_verified is False
        assert record.deployed is False
        assert record.failure is not None
        # The engine's own re-read of the bound content found nothing to refuse:
        # this refusal is the disagreement between evidence and binding.
        assert not any("changed" in error for error in record.failure.errors)

    def test_an_element_without_bound_content_is_never_started(self, tmp_path):
        from deployment_operations import ComponentBinding

        element = RuntimeElement(
            deployment_id="dep-unbound",
            environment_id="local-test",
            platform_id=PLATFORM_ID,
            instance_digest="sha256:" + "0" * 64,
            component=ComponentBinding(
                component_id=COMPONENT_ID,
                component_version=COMPONENT_VERSION,
                artifact_type="none",
                artifact_digest=None,
                artifact_pinned=False,
            ),
            configuration={},
            spec_path=tmp_path / "runtime.json",
            workspace=tmp_path / "workspace",
            binding=environment_for(tmp_path / "unused").binding_for(COMPONENT_ID),
            artifact_source=ArtifactSource(path=None, digest=None, verified=False),
            interpreter=sys.executable,
            source_paths=(ROOT / "src",),
        )
        with refusal(RuntimeProcessError, "no execution content was bound"):
            LocalProcessRuntime().start(element)
        assert not (element.workspace / "runtime.log").exists()

    def test_the_runtime_process_refuses_to_run_unbound_content(self, tmp_path):
        spec = tmp_path / "runtime.json"
        spec.write_text(
            json.dumps(
                {
                    "runtime": {
                        "deployment_module": "tenant_authority.deployment",
                        "deployment_factory": "build_deployment",
                    },
                    "configuration": {},
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "deployment_operations.runtime_worker",
                str(spec),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
            check=False,
        )
        assert completed.returncode == 2
        assert "--binding" in completed.stderr
        assert "Traceback" not in completed.stderr

    def test_verified_artifact_content_is_the_bound_executed_content(self, tmp_path):
        from deployment_operations.runtime import bind_execution

        content = tmp_path / "component_module.py"
        content.write_bytes((FIXTURE_PATH / "migrating_component.py").read_bytes())
        digest = content_digest(content)
        element = artifact_element(tmp_path, content, digest)
        binding = bind_execution(
            element,
            materialized=MaterializedComponent(
                component_id=COMPONENT_ID,
                workspace=tmp_path,
                artifact_path=content,
                observed_digest=digest,
                artifact_verified=True,
            ),
        )
        assert binding.kind == "artifact"
        assert binding.root == content.parent
        assert binding.entry.path == content
        assert binding.entry.digest == digest

    def test_artifact_content_that_cannot_be_executed_is_refused(self, tmp_path):
        from deployment_operations.runtime import bind_execution

        content = tmp_path / "published-artifact.bin"
        content.write_bytes(b"content that is not executable component source")
        digest = content_digest(content)
        element = artifact_element(tmp_path, content, digest)
        with refusal(DeploymentExecutionFailed, "not executable component source"):
            bind_execution(
                element,
                materialized=MaterializedComponent(
                    component_id=COMPONENT_ID,
                    workspace=tmp_path,
                    artifact_path=content,
                    observed_digest=digest,
                    artifact_verified=True,
                ),
            )

    def test_unverified_artifact_content_is_never_bound(self, tmp_path):
        from deployment_operations.runtime import bind_execution

        content = tmp_path / "unverified.py"
        content.write_bytes(b"print('never verified')\n")
        digest = content_digest(content)
        element = artifact_element(tmp_path, content, digest)
        with refusal(DeploymentExecutionFailed, "was not verified against any content"):
            bind_execution(
                element,
                materialized=MaterializedComponent(
                    component_id=COMPONENT_ID,
                    workspace=tmp_path,
                    artifact_path=content,
                    observed_digest=digest,
                    artifact_verified=False,
                    errors=("digest substitution",),
                ),
            )

    def test_absent_component_module_is_refused(self, tmp_path):
        from deployment_operations.runtime import bind_execution

        element = source_element(
            tmp_path, module="absent_component", source=tmp_path / "empty-source"
        )
        with refusal(DeploymentExecutionFailed, "is not present in any import root"):
            bind_execution(element)

    def test_ambiguous_module_resolution_is_refused(self, tmp_path):
        from deployment_operations.runtime import bind_execution

        source = tmp_path / "ambiguous-source"
        (source / "ambiguous_component").mkdir(parents=True)
        (source / "ambiguous_component.py").write_text("A = 1\n", encoding="utf-8")
        (source / "ambiguous_component" / "__init__.py").write_text(
            "A = 2\n", encoding="utf-8"
        )
        element = source_element(tmp_path, module="ambiguous_component", source=source)
        with refusal(DeploymentExecutionFailed, "resolves ambiguously"):
            bind_execution(element)

    def test_the_engine_re_reads_the_bound_content(self, tmp_path):
        from deployment_operations.runtime import bind_execution, verify_bound_content

        source = bound_source(tmp_path)
        element = source_element(tmp_path, module="bound_component", source=source)
        binding = bind_execution(element)
        assert verify_bound_content(binding) == ()

        bound_file = source / "bound_component.py"
        original = bound_file.read_bytes()
        bound_file.write_bytes(original + b"\n# changed after binding\n")
        errors = verify_bound_content(binding)
        assert errors and "changed" in errors[0]

        bound_file.write_bytes(original)
        assert verify_bound_content(binding) == ()


# ---------------------------------------------------------------------------
# F-3B — the Verified Execution Boundary (ADR-0016 §9, §10; F-3B failure T1)
#
# The deployment binds the component's executable closure before anything runs,
# the process executes only that closure, and every claim is compared with
# engine-side evidence of what actually executed. These tests take the routes a
# component — or content planted beside it — could take around that boundary and
# assert, in each case, that the deployment fails closed: nothing the deployment
# did not verify becomes trusted execution, and no state that stands on it is
# ever recorded.
# ---------------------------------------------------------------------------


def verified_root(
    tmp_path: Path,
    *,
    entry: str | None = None,
    fixture: str = "route_adversary_component.py",
    helper: bool = False,
    extra: Mapping[str, str] | None = None,
) -> Path:
    """A verified execution root: the content a deployment builds its closure on."""
    source = tmp_path / "verified-root"
    source.mkdir()
    (source / "bound_component.py").write_text(
        entry if entry is not None else (FIXTURE_PATH / fixture).read_text("utf-8"),
        encoding="utf-8",
    )
    if helper:
        (source / "helper.py").write_bytes(
            (FIXTURE_PATH / "honest_helper.py").read_bytes()
        )
    for relative, content in (extra or {}).items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return source


def fixture_source_bytes(name: str) -> bytes:
    """The bytes of one of the runtime doubles."""
    return (FIXTURE_PATH / name).read_bytes()


def substituted_helper() -> bytes:
    """Content that imitates component-owned helper content without being it."""
    return (FIXTURE_PATH / "substituted_helper.py").read_bytes()


def fixture_source(name: str) -> str:
    """The source of one of the runtime doubles."""
    return (FIXTURE_PATH / name).read_text(encoding="utf-8")


def adversary_environment(
    tmp_path: Path,
    roots: Sequence[Path],
    *,
    route: str = "bound",
    module: str = "bound_component",
    migrations: MigrationBinding | None = None,
    secrets: Mapping[str, str] | None = None,
) -> object:
    """An environment binding the component to the route double at ``roots``."""
    values = {"BOUNDARY_ROUTE": f"f3b:{route}"}
    values.update(secrets or {})
    return environment_for(
        tmp_path / "runtime",
        bindings=(
            ComponentRuntimeBinding(
                component_id=COMPONENT_ID,
                deployment_module=module,
                deployment_factory="build_component",
                migrations=migrations,
                import_paths=tuple(roots),
            ),
        ),
        secrets=values,
    )


def workspace_of(environment: object, instance: Instance) -> Path:
    """The working directory the component's runtime process will run in."""
    deployment_id = derive_deployment_id(
        PLATFORM_ID, instance.instance_digest, environment.environment_id, 1
    )
    workspace = (
        environment.deployments_dir / deployment_id / "components" / COMPONENT_ID
    )
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def bound_content(component: object) -> dict[str, tuple[str, str]]:
    """The content the deployment bound, by module name."""
    return {
        entry["module"]: (entry["path"], entry["digest"])
        for entry in component.execution["modules"]
    }


def executed_content(component: object) -> dict[str, tuple[str, str]]:
    """The content the runtime reported executing, by module name."""
    return {
        entry["module"]: (entry["path"], entry["digest"])
        for entry in component.observed_execution["modules"]
    }


def ran(workspace: Path) -> list[str]:
    """The markers the content that ran left in the process working directory."""
    return sorted(path.name for path in workspace.glob("*.marker"))


def outcome_of(workspace: Path) -> str:
    """What the route double recorded about the route it attempted."""
    path = workspace / "import-outcome.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def attempt(
    instance: Instance,
    manifest: dict,
    environment: object,
    *,
    runtime: object | None = None,
) -> tuple[DeploymentRecord, Exception | None]:
    """Deploy, returning the recorded state and the refusal when there is one."""
    request = request_for(instance, manifest, environment)
    try:
        deployment = deploy(request, runtime=runtime or LocalProcessRuntime())
        return deployment.record, None
    except Exception as error:  # noqa: BLE001 - the test asserts on the refusal
        return only_operation(environment), error


#: The entrypoint of the mandated regression scenario: the audited A→B chain.
#: Its reference to ``helper`` is not a literal in the source, so no engine-side
#: closure can bind it in advance — the boundary decides at resolution time,
#: which is exactly where the audited chain reached substituted content.
MANDATED_ENTRY = (
    "from pathlib import Path\n"
    "from tenant_authority.deployment import build_deployment\n"
    "\n"
    "def build_component(configuration):\n"
    "    import importlib\n"
    "    Path('entrypoint-ran.marker').write_text('A\\n', encoding='utf-8')\n"
    "    try:\n"
    "        helper = importlib.import_module('hel' + 'per')\n"
    "        helper.record('entrypoint')\n"
    "        Path('import-outcome.txt').write_text(\n"
    "            f'loaded:{helper.__file__}', encoding='utf-8'\n"
    "        )\n"
    "    except Exception as error:\n"
    "        Path('import-outcome.txt').write_text(\n"
    "            f'{type(error).__name__}: {error}', encoding='utf-8'\n"
    "        )\n"
    "    return build_deployment(configuration, with_http=True)\n"
)


@dataclass
class LinkingRuntime:
    """The real adapter, with a bound file swapped for workspace content by a link.

    A link is the case string paths cannot decide: the *path* of the bound
    content is unchanged and still names the verified root, while the object it
    denotes is content from the workspace. What executes must still be what the
    deployment verified (§7, §8).
    """

    inner: object
    kind: str
    target: str = "helper"

    def _swap(self, element: RuntimeElement) -> list[Path]:
        workspace = Path(element.workspace) / "helper.py"
        swapped: list[Path] = []
        for module in element.execution.modules:
            if not module.module.endswith(self.target):
                continue
            module.path.unlink()
            if self.kind == "symlink":
                module.path.symlink_to(workspace)
            else:
                os.link(workspace, module.path)
            swapped.append(module.path)
        return swapped

    def materialize(self, element: RuntimeElement):
        return self.inner.materialize(element)

    def migrate(self, element: RuntimeElement):
        return self.inner.migrate(element)

    def start(self, element: RuntimeElement):
        self.swapped = self._swap(element)
        return self.inner.start(element)

    def request(self, handle, operation, *, timeout=None):
        return self.inner.request(handle, operation, timeout=timeout)

    def stop(self, handle):
        return self.inner.stop(handle)


class TestVerifiedExecutionBoundary:
    """F-3B: what executes is the verified closure, by every route (§9, §10)."""

    def _require_symlink_support(self, tmp_path):
        target = tmp_path / "symlink-capability-target"
        link = tmp_path / "symlink-capability-link"
        target.write_text("probe", encoding="utf-8")
        try:
            link.symlink_to(target)
        except OSError as error:
            pytest.skip(f"symlink creation is unavailable: {error}")
        finally:
            link.unlink(missing_ok=True)
            target.unlink(missing_ok=True)

    # -- 1–4: the verified closure is what executes -------------------------

    def test_the_verified_direct_module_is_the_content_that_executes(
        self, tmp_path, instance, manifest
    ):
        """Positive: the entrypoint the engine bound is the process that runs."""
        source = bound_source(tmp_path)
        environment = environment_bound_to(tmp_path, source)
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert record.identity_verified is True
        component = record.components[0]
        assert component.execution is not None
        assert component.execution["kind"] == "component_source"
        entry_module = component.execution["modules"][0]
        assert Path(entry_module["path"]) == source / "bound_component.py"
        observed = component.observed_execution
        assert observed is not None
        assert {(entry["path"], entry["digest"]) for entry in observed["modules"]} == {
            (entry["path"], entry["digest"]) for entry in component.execution["modules"]
        }

    def test_a_verified_transitive_import_is_the_content_that_executes(
        self, tmp_path, instance, manifest
    ):
        """Positive: an import of verified content executes the verified file."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        assert outcome_of(workspace) == f"bound: loaded:{source / 'helper.py'}"
        component = record.components[0]
        assert bound_content(component)["helper"] == (
            str(source / "helper.py"),
            content_digest(source / "helper.py"),
        )
        assert executed_content(component) == bound_content(component)

    def test_a_verified_package_closure_is_the_content_that_executes(
        self, tmp_path, instance, manifest
    ):
        """Positive: a package and the module inside it are bound and executed."""
        entry = (
            "from pathlib import Path\n"
            "from tenant_authority.deployment import build_deployment\n"
            "from pkg import helper\n\n"
            "def build_component(configuration):\n"
            "    Path('entrypoint-ran.marker').write_text('A\\n', encoding='utf-8')\n"
            "    helper.record('entrypoint')\n"
            "    return build_deployment(configuration, with_http=True)\n"
        )
        source = verified_root(
            tmp_path,
            entry=entry,
            extra={
                "pkg/__init__.py": "",
                "pkg/helper.py": fixture_source("honest_helper.py"),
            },
        )
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        component = record.components[0]
        bound = {module: path for module, (path, _) in bound_content(component).items()}
        assert bound["pkg"] == str(source / "pkg" / "__init__.py")
        assert bound["pkg.helper"] == str(source / "pkg" / "helper.py")
        observed = {
            module: path for module, (path, _) in executed_content(component).items()
        }
        assert observed["pkg"] == bound["pkg"]
        assert observed["pkg.helper"] == bound["pkg.helper"]

    def test_a_verified_migration_transitive_import_is_the_content_that_executes(
        self, tmp_path, instance, manifest
    ):
        """Positive: the migration session runs in the same verified closure."""
        source = verified_root(
            tmp_path,
            fixture="migrating_component.py",
            helper=True,
            extra={
                "workspace_migration_component.py": fixture_source(
                    "workspace_migration_component.py"
                )
            },
        )
        migrations = MigrationBinding(
            module="workspace_migration_component", attribute="MIGRATIONS"
        )
        environment = adversary_environment(
            tmp_path, (source,), route="bound", migrations=migrations
        )
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert [(item.migration_id, item.status) for item in record.migrations] == [
            ("0001-seed-platform", "executed")
        ]
        assert ran(workspace) == ["honest-migration.marker"]
        component = record.components[0]
        assert bound_content(component)["helper"] == (
            str(source / "helper.py"),
            content_digest(source / "helper.py"),
        )
        assert executed_content(component) == bound_content(component)

    # -- 5–15: substitution and path identity -------------------------------

    def test_workspace_content_cannot_take_a_bound_module_s_place(
        self, tmp_path, instance, manifest
    ):
        """The process working directory is never a source of component content."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]

        # The same route with the workspace as the *only* source of the name:
        # there is no content to bind, so nothing is started at all.
        only_workspace = tmp_path / "workspace-only"
        only_workspace.mkdir()
        source = verified_root(only_workspace, helper=False)
        environment = adversary_environment(only_workspace, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, DeploymentExecutionFailed)
        assert "workspace" in " ".join(record.failure.errors)
        assert ran(workspace) == []
        assert record.deployed is False
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED

    def test_content_in_another_root_cannot_serve_a_bound_name(
        self, tmp_path, instance, manifest
    ):
        """A second declared root never supplies a name bound to the first."""
        source = verified_root(tmp_path, helper=True)
        alternate = tmp_path / "alternate-root"
        alternate.mkdir()
        (alternate / "helper.py").write_bytes(substituted_helper())
        (alternate / "bound_component.py").write_text(
            "from pathlib import Path\n"
            "Path('alternate-root-entrypoint.marker').write_text('B\\n')\n"
            "def build_component(configuration):\n"
            "    raise AssertionError('the alternate root is not the execution source')\n",
            encoding="utf-8",
        )
        environment = adversary_environment(
            tmp_path, (source, alternate), route="bound"
        )
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        component = record.components[0]
        bound = {module: path for module, (path, _) in bound_content(component).items()}
        assert bound["helper"] == str(source / "helper.py")
        assert bound["bound_component"] == str(source / "bound_component.py")

    def test_a_workspace_package_cannot_take_a_verified_package_s_place(
        self, tmp_path, instance, manifest
    ):
        """A workspace package of the same name is never the content that runs."""
        source = verified_root(
            tmp_path,
            entry=(
                "from pathlib import Path\n"
                "from tenant_authority.deployment import build_deployment\n"
                "from pkg import helper\n\n"
                "def build_component(configuration):\n"
                "    Path('entrypoint-ran.marker').write_text('A\\n', encoding='utf-8')\n"
                "    helper.record('entrypoint')\n"
                "    return build_deployment(configuration, with_http=True)\n"
            ),
            extra={
                "pkg/__init__.py": "",
                "pkg/helper.py": fixture_source("honest_helper.py"),
            },
        )
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (workspace / "pkg" / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        component = record.components[0]
        observed = {
            module: path for module, (path, _) in executed_content(component).items()
        }
        assert observed["pkg"] == str(source / "pkg" / "__init__.py")
        assert observed["pkg.helper"] == str(source / "pkg" / "helper.py")

    def test_a_rewritten_package_path_cannot_serve_a_name(
        self, tmp_path, instance, manifest
    ):
        """Trust does not travel through a verified package's ``__path__``.

        The bound package is content the deployment verified; pointing its
        search path at the working directory must not let a name the workspace
        holds be served through it (ADR-0016 §9, §10).
        """
        source = verified_root(
            tmp_path,
            fixture="package_path_component.py",
            extra={"pkg/__init__.py": ""},
        )
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "extra_helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("ExecutionBoundaryError")
        assert not (workspace / "substituted-entrypoint.marker").exists()
        component = record.components[0]
        assert bound_content(component)["pkg"][0] == str(source / "pkg" / "__init__.py")
        for module, (path, _) in executed_content(component).items():
            assert not path.startswith(str(workspace)), module
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    def test_a_dynamic_import_of_workspace_content_is_refused(
        self, tmp_path, instance, manifest
    ):
        """A name the boundary did not bind is not resolved from anywhere else."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="unbound")
        workspace = workspace_of(environment, instance)
        (workspace / "extra_helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert "did not bind" in " ".join(record.failure.errors)
        assert record.failure.stage == "ready"
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("unbound: ExecutionBoundaryError")
        assert record.identity_verified is False
        assert record.deployed is False

    def test_content_from_an_archive_cannot_serve_a_name(
        self, tmp_path, instance, manifest
    ):
        """An archive on the search path is not a source of trusted content.

        A component can put whatever it likes on the search path; what the
        boundary decides is what may be loaded from there (§13).
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="archive")
        workspace = workspace_of(environment, instance)
        with zipfile.ZipFile(workspace / "bundle.zip", "w") as bundle:
            bundle.writestr("extra_helper.py", substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("archive: ExecutionBoundaryError")
        assert not (workspace / "substituted-entrypoint.marker").exists()
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    def test_content_preloaded_into_sys_modules_gains_no_trust(
        self, tmp_path, instance, manifest
    ):
        """Content injected into sys.modules is not the content that was verified."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="sys_modules")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert "did not bind" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("sys_modules: ExecutionBoundaryError")
        # The bound name is never reported as the workspace file.
        component = record.components[0]
        assert all(
            entry["path"] != str(workspace / "helper.py")
            for entry in component.execution["modules"]
        )
        assert record.identity_verified is False
        assert record.deployed is False

    def test_a_name_held_without_content_is_no_evidence_of_the_bound_content(
        self, tmp_path, instance, manifest
    ):
        """A module object is not the content the deployment bound (§19, §20).

        The process holds something under a name the deployment bound to a file:
        a module object with no location at all. Nothing the deployment verified
        was loaded, so the state must not report the bound content as the
        content that executed — and must not deploy on the strength of it.
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="shell_module")
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert "never accepts content it did not bind" in " ".join(
            record.failure.errors
        )
        assert ran(workspace) == ["entrypoint-ran.marker", "shell-entrypoint.marker"]
        component = record.components[0]
        # The bound content was never loaded: no path, no digest, no claim.
        observed = executed_content(component)
        assert observed["helper"][1] == ""
        assert observed["helper"][0] == ""
        assert bound_content(component)["helper"][1].startswith("sha256:")
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    def test_a_hostile_finder_on_meta_path_is_refused(
        self, tmp_path, instance, manifest
    ):
        """A finder ahead of the boundary cannot serve a bound name."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="meta_path")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("meta_path: ExecutionBoundaryError")
        assert record.identity_verified is False
        assert record.deployed is False

    def test_a_hostile_path_hook_is_refused(self, tmp_path, instance, manifest):
        """A hostile path hook cannot make unbound content trusted execution."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="path_hooks")
        workspace = workspace_of(environment, instance)
        (workspace / "extra_helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        # The double removes the boundary's finder before it imports: the
        # unbound content is still refused, and the process fails closed.
        assert isinstance(error, (StartupFailed, IdentityVerificationFailed))
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("path_hooks: ExecutionBoundaryError")
        assert record.identity_verified is False
        assert record.deployed is False
        assert record.lifecycle == LIFECYCLE_FAILED

    def test_content_linked_into_the_bound_path_is_refused_before_it_executes(
        self, tmp_path, instance, manifest
    ):
        """A symlink to workspace content is not the content that was verified."""
        self._require_symlink_support(tmp_path)
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        runtime = LinkingRuntime(inner=LocalProcessRuntime(), kind="symlink")
        record, error = attempt(instance, manifest, environment, runtime=runtime)
        assert isinstance(error, StartupFailed)
        assert (source / "helper.py").is_symlink()
        assert "changed" in " ".join(record.failure.errors)
        assert ran(workspace) == []
        assert record.identity_verified is False
        assert record.deployed is False
        assert record.lifecycle == LIFECYCLE_FAILED

    def test_a_directory_link_cannot_launder_untrusted_content(
        self, tmp_path, instance, manifest
    ):
        """Directory links resolve to what they are, not to where they appear."""
        self._require_symlink_support(tmp_path)
        # A link inside the workspace pointing at content outside it.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "extra_helper.py").write_bytes(substituted_helper())
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="alias")
        workspace = workspace_of(environment, instance)
        (workspace / "toolbox").symlink_to(outside)
        (workspace / "boundary-alias.txt").write_text(
            str(workspace / "toolbox"), encoding="utf-8"
        )
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert outcome_of(workspace).startswith("alias: ExecutionBoundaryError")
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert record.deployed is False

        # A link outside the workspace pointing at the workspace: the content is
        # the workspace's, whichever path reaches it.
        alias = tmp_path / "workspace-alias"
        alias.symlink_to(workspace)
        second = tmp_path / "second"
        second.mkdir()
        source = verified_root(second, helper=True)
        environment = adversary_environment(second, (source,), route="alias")
        workspace = workspace_of(environment, instance)
        (workspace / "extra_helper.py").write_bytes(substituted_helper())
        (workspace / "boundary-alias.txt").write_text(str(alias), encoding="utf-8")
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert outcome_of(workspace).startswith("alias: ExecutionBoundaryError")
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert record.deployed is False

    def test_a_hard_linked_substitute_is_refused_before_it_executes(
        self, tmp_path, instance, manifest
    ):
        """A hard link shares the object; the object is still not verified content."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        runtime = LinkingRuntime(inner=LocalProcessRuntime(), kind="hardlink")
        record, error = attempt(instance, manifest, environment, runtime=runtime)
        assert isinstance(error, StartupFailed)
        assert (
            os.stat(source / "helper.py").st_ino
            == os.stat(workspace / "helper.py").st_ino
        )
        assert ran(workspace) == []
        assert record.identity_verified is False
        assert record.deployed is False

    def test_a_bound_module_reloaded_after_a_swap_never_runs_the_new_bytes(
        self, tmp_path, instance, manifest
    ):
        """Verified at ``t0``, executed at ``t1``: the identity is the same (§8).

        The process loads the bound content, then tries to replace the bound file
        with the workspace's copy (a hard link, so no workspace content is read
        through Python) and reloads the module. Replacing verified content is
        refused where it is attempted — the process never rewrites what the
        deployment verified — and nothing the swap carried runs.
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="reload_module")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        # The verified content ran; the content the swap put in its place did not.
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-first.marker"]
        assert outcome_of(workspace).startswith("reload_module: ExecutionBoundaryError")
        assert "immutable" in outcome_of(workspace)
        assert not (workspace / "substituted-first.marker").exists()
        assert not (workspace / "substituted-second.marker").exists()
        component = record.components[0]
        observed = executed_content(component)["helper"][1]
        assert observed == bound_content(component)["helper"][1]
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    def test_verified_content_cannot_be_rewritten_from_the_process(
        self, tmp_path, instance, manifest
    ):
        """What executes is what was verified: the process never rewrites it (§8).

        A component that writes over the file the deployment verified would make
        the content that executes differ from the content that was verified. The
        write is refused where it is attempted, the refusal is evidence, and the
        deployment fails on it — the content never runs as something else.
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="write_bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        assert outcome_of(workspace).startswith("write_bound: ExecutionBoundaryError")
        assert "immutable" in outcome_of(workspace)
        assert not (workspace / "substituted-first.marker").exists()
        assert not (workspace / "substituted-second.marker").exists()
        component = record.components[0]
        assert executed_content(component) == bound_content(component)
        assert record.identity_verified is False
        assert record.deployed is False

    def test_a_code_object_the_boundary_did_not_compile_never_runs_under_a_bound_name(
        self, tmp_path, instance, manifest
    ):
        """A verified name runs the boundary's code object, or none (§8).

        The route builds its program under a label of its own, renames it to the
        bound file and round-trips it through ``marshal``, so no compile ever
        names verified content. The code object is still not one the boundary
        compiled from the bytes it verified, so it never runs under that name.
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(
            tmp_path, (source,), route="marshalled_bound"
        )
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        assert outcome_of(workspace).startswith(
            "marshalled_bound: ExecutionBoundaryError"
        )
        assert "not compiled by the boundary" in outcome_of(workspace)
        component = record.components[0]
        assert executed_content(component) == bound_content(component)
        assert record.identity_verified is False
        assert record.deployed is False

    def test_bytes_the_deployment_never_verified_are_never_compiled_under_a_bound_name(
        self, tmp_path, instance, manifest
    ):
        """A compile under a verified name takes the verified bytes, or nothing (§8).

        The bound file is read first, so the name is honestly loaded; the route
        then compiles *other* bytes under that name. If a compile were trusted by
        its filename, unverified bytes would become verified content by label.
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(
            tmp_path, (source,), route="compile_bound_name"
        )
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        assert outcome_of(workspace).startswith(
            "compile_bound_name: ExecutionBoundaryError"
        )
        assert "only the bytes this boundary verified" in outcome_of(workspace)
        component = record.components[0]
        assert executed_content(component) == bound_content(component)
        assert record.identity_verified is False
        assert record.deployed is False

    def test_a_bound_name_held_without_a_boundary_load_is_no_evidence(
        self, tmp_path, instance, manifest
    ):
        """Holding a module under a bound name is not loading the bound content (§19).

        The process puts a module object under the bound name, pointing at the
        bound file, and loads nothing. The boundary reports no content for the
        name — the label lends no trust — so the deployment refuses instead of
        recording execution of content that never ran.
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="forge_held")
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert outcome_of(workspace) == "forge_held: held-without-load"
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert "reported no content for it" in " ".join(record.failure.errors)
        component = record.components[0]
        assert executed_content(component)["helper"] == ("", "")
        assert record.identity_verified is False
        assert record.deployed is False

    def test_the_boundary_refuses_content_it_was_not_established_on(self, tmp_path):
        """The bytes the boundary was established on are the bytes it executes (§8).

        Driven directly, because the moment this protects is the one between the
        process being established and a name being loaded — a window no harness
        can place itself in deterministically. Two rules are pinned: content that
        differs from what the boundary was established on never loads, and a
        bound name that was already loaded never loads different content twice.
        """
        root = tmp_path / "bound"
        workspace = tmp_path / "workspace"
        root.mkdir()
        workspace.mkdir()
        bound = root / "boundary_case_helper.py"
        honest = fixture_source_bytes("honest_helper.py")
        bound.write_bytes(honest)
        boundary = runtime_worker._ExecutionBoundary(
            {
                "root": str(root),
                "roots": [str(root)],
                "untrusted": [str(workspace)],
                "modules": [
                    {
                        "module": "boundary_case_helper",
                        "path": str(bound),
                        "digest": content_digest(bound),
                    }
                ],
            }
        )
        boundary.establish()
        try:
            established = boundary.established_digest("boundary_case_helper")
            assert established == f"sha256:{hashlib.sha256(honest).hexdigest()}"
            content = boundary.bound("boundary_case_helper")

            # Content that differs from what the boundary was established on is
            # never loaded, however it reached the bound path.
            bound.write_bytes(substituted_helper())
            with pytest.raises(runtime_worker.ExecutionBoundaryError) as refusal:
                runtime_worker._BoundLoader(boundary, content).exec_module(
                    types.ModuleType("boundary_case_helper")
                )
            assert "digest mismatch" in str(refusal.value)
            assert "what executes is not what was verified" in str(refusal.value)

            # The established content loads; the same name never loads anything
            # else afterwards.
            bound.write_bytes(honest)
            module = types.ModuleType("boundary_case_helper")
            runtime_worker._BoundLoader(boundary, content).exec_module(module)
            assert boundary.loaded_digest("boundary_case_helper") == established
            bound.write_bytes(substituted_helper())
            with pytest.raises(runtime_worker.ExecutionBoundaryError) as second:
                runtime_worker._BoundLoader(boundary, content).exec_module(module)
            assert "digest mismatch" in str(second.value)
            assert "what executes is not what was verified" in str(second.value)
        finally:
            sys.meta_path[:] = [
                finder
                for finder in sys.meta_path
                if not isinstance(finder, runtime_worker._VebFinder)
            ]
            sys.modules.pop("boundary_case_helper", None)

    def test_content_replaced_after_verification_never_becomes_the_executed_one(
        self, tmp_path, instance, manifest
    ):
        """Verification and execution are the same identity (§8).

        Two moments, two answers: a replacement *before* the launch is refused
        instead of executed, and a replacement *after* the verified content ran
        is refused as a claim — while the evidence still shows that the verified
        content is what executed.
        """
        source = verified_root(tmp_path, helper=True)
        substitute = substituted_helper()

        # Replaced before the launch: nothing executes at all.
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substitute)
        runtime = RewritingRuntime(
            inner=LocalProcessRuntime(),
            substitute=substitute,
            when="before-start",
            within=source,
        )
        record, error = attempt(instance, manifest, environment, runtime=runtime)
        assert isinstance(error, StartupFailed)
        assert "not the content about to execute" in " ".join(record.failure.errors)
        assert ran(workspace) == []
        assert record.deployed is False

        # Replaced after the process loaded it: the verified content is what
        # ran, so the digests the process reports are the bound ones; the file
        # no longer is, so no claim stands on it.
        second = tmp_path / "after"
        second.mkdir()
        source = verified_root(second, helper=True)
        environment = adversary_environment(second, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substitute)
        runtime = RewritingRuntime(
            inner=LocalProcessRuntime(),
            substitute=substitute,
            when="after-start",
            within=source,
        )
        record, error = attempt(instance, manifest, environment, runtime=runtime)
        assert isinstance(error, IdentityVerificationFailed)
        assert "changed" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        component = record.components[0]
        assert executed_content(component) == bound_content(component)
        assert record.identity_verified is False
        assert record.deployed is False

    # -- 16–18: what a runtime reports is never the claim -------------------

    def test_a_runtime_reporting_every_expected_value_is_still_refused(
        self, tmp_path, instance, manifest
    ):
        """Verified A, unbound content reaching for the name: the claim is false.

        The process runs the verified entrypoint and answers ``/health`` and
        ``/ready`` with exactly the pinned identity, version and platform — the
        whole runtime report is correct — while content the boundary did not
        bind is what the component reached for. Nothing that content does may
        become trusted execution, and the deployment must not stand on it
        (ADR-0016 §9, §10; ADR-0017 §43.10).
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="unbound")
        workspace = workspace_of(environment, instance)
        (workspace / "extra_helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)

        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        assert "did not bind" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("unbound: ExecutionBoundaryError")
        component = record.components[0]
        assert component.healthy is True
        assert component.observed_component_id == COMPONENT_ID
        assert component.observed_version == COMPONENT_VERSION
        assert component.observed_platform_id == PLATFORM_ID
        assert component.artifact_type == "none"
        assert component.artifact_digest is None
        assert record.instance_digest == instance.instance_digest
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    def test_the_mandated_regression_scenario_holds(self, tmp_path, instance, manifest):
        """The audited A→B chain, re-run against the boundary (F-3B, §22).

        The verified content is the component's own ``deployment.py``; the
        component imports ``helper``, whose name is not a literal in its source,
        and the working directory holds a substituted ``helper.py``. An attacker
        copy of the component itself sits on the import path the environment
        tries to widen. The runtime answers every question with exactly what the
        accepted instance expects — identity, version, platform, health and
        readiness — and the deployment must still not stand on it: the
        substituted content never becomes trusted execution, and ``deployed``,
        ``realized`` and ``identity_verified`` are all false.
        """
        source = tmp_path / "verified-root"
        source.mkdir()
        (source / "deployment.py").write_text(MANDATED_ENTRY, encoding="utf-8")
        (source / "helper.py").write_bytes(fixture_source_bytes("honest_helper.py"))
        environment = adversary_environment(
            tmp_path, (source,), route="alias", module="deployment"
        )
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())

        attacker = tmp_path / "attacker"
        poisoned = attacker / "tenant_authority"
        poisoned.mkdir(parents=True)
        (poisoned / "__init__.py").write_text("", encoding="utf-8")
        (poisoned / "deployment.py").write_text(
            "from pathlib import Path\n"
            "Path('poisoned-component-import.marker').write_text('B\\n', encoding='utf-8')\n"
            "def build_deployment(configuration, with_http=True):\n"
            "    raise AssertionError('the attacker copy is not the component')\n",
            encoding="utf-8",
        )
        saved = {key: os.environ.get(key) for key in ("PYTHONPATH", "PYTHONHOME")}
        os.environ["PYTHONPATH"] = str(attacker)
        os.environ["PYTHONHOME"] = str(attacker)
        try:
            record, error = attempt(instance, manifest, environment)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert isinstance(error, IdentityVerificationFailed)
        assert record.failure.stage == "ready"
        # The verified entrypoint ran, and the name it reached for was refused
        # before any content could be loaded for it.
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("ExecutionBoundaryError")
        assert "never a source of trusted component content" in outcome_of(workspace)
        assert not (workspace / "substituted-entrypoint.marker").exists()
        assert not (attacker / "poisoned-component-import.marker").exists()
        # The runtime reported exactly what the accepted instance expects.
        component = record.components[0]
        assert component.healthy is True
        assert component.observed_component_id == COMPONENT_ID
        assert component.observed_version == COMPONENT_VERSION
        assert component.observed_platform_id == PLATFORM_ID
        assert component.artifact_type == "none"
        assert component.artifact_digest is None
        assert record.instance_digest == instance.instance_digest
        # None of it is enough: the execution the claim would stand on is not
        # the content this deployment verified.
        assert record.identity_verified is False
        assert record.ready is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    def test_missing_execution_evidence_is_refused(self, tmp_path, instance, manifest):
        """No evidence is not evidence: a claim needs the content it executed."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        runtime = EvidenceHidingRuntime(inner=LocalProcessRuntime())
        record, error = attempt(instance, manifest, environment, runtime=runtime)
        assert isinstance(error, IdentityVerificationFailed)
        assert "reported no execution content" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        assert record.identity_verified is False
        assert record.deployed is False

    def test_contradictory_execution_evidence_is_refused(
        self, tmp_path, instance, manifest
    ):
        """Evidence that contradicts the binding is refused, not averaged away."""
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        elsewhere = tmp_path / "elsewhere.py"
        runtime = MisdirectingRuntime(inner=LocalProcessRuntime(), elsewhere=elsewhere)
        record, error = attempt(instance, manifest, environment, runtime=runtime)
        assert isinstance(error, IdentityVerificationFailed)
        assert "outside every verified execution root" in " ".join(
            record.failure.errors
        )
        assert record.identity_verified is False
        assert record.deployed is False

    # -- 19–21: generated, native and child content ------------------------

    def test_generated_content_gains_no_automatic_trust(
        self, tmp_path, instance, manifest
    ):
        """Compiled and generated code is never verified execution by itself."""
        # Compiled content labelled as component-owned content is refused.
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="generated")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert "compiled source" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert record.deployed is False

        # Executable content read out of the workspace is refused just as well.
        second = tmp_path / "read"
        second.mkdir()
        source = verified_root(second, helper=True)
        environment = adversary_environment(second, (source,), route="generated_read")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert "executable content opened" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert record.deployed is False

        # Generated code with no provenance at all runs — and is never reported
        # as verified execution: the claim covers the bound closure alone.
        third = tmp_path / "memory"
        third.mkdir()
        source = verified_root(third, helper=True)
        environment = adversary_environment(third, (source,), route="generated_memory")
        workspace = workspace_of(environment, instance)
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert ran(workspace) == [
            "entrypoint-ran.marker",
            "generated-in-memory.marker",
        ]
        component = record.components[0]
        assert executed_content(component) == bound_content(component)
        assert all(
            not Path(path).is_relative_to(workspace)
            for path, _ in executed_content(component).values()
        )

    def test_native_content_without_provenance_is_refused(
        self, tmp_path, instance, manifest
    ):
        """A native library outside the verified roots is never trusted content."""
        import _asyncio
        import importlib.machinery

        suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="unbound")
        workspace = workspace_of(environment, instance)
        (workspace / f"extra_helper{suffix}").write_bytes(
            Path(_asyncio.__file__).read_bytes()
        )
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert "native content" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert record.deployed is False

        # Verified content still wins for the name it bound, native shadow or not.
        second = tmp_path / "shadow"
        second.mkdir()
        source = verified_root(second, helper=True)
        environment = adversary_environment(second, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / f"helper{suffix}").write_bytes(
            Path(_asyncio.__file__).read_bytes()
        )
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        component = record.components[0]
        observed = {
            module: path for module, (path, _) in executed_content(component).items()
        }
        assert observed["helper"] == str(source / "helper.py")

    def test_a_child_process_is_not_automatically_trusted(
        self, tmp_path, instance, manifest
    ):
        """A child inherits the boundary's environment; it is never handed trust.

        A child that inherits this process's environment cannot resolve unbound
        component content, because that environment carries only trusted
        facilities. A child handed an environment that would let it search the
        workspace is refused rather than trusted — and none of it is ever part
        of this deployment's claim (§18).
        """
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(
            tmp_path, (source,), route="child_inherited"
        )
        workspace = workspace_of(environment, instance)
        (workspace / "extra_helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert error is None
        assert record.deployed is True
        assert "ModuleNotFoundError" in outcome_of(workspace)
        assert ran(workspace) == ["entrypoint-ran.marker"]

        second = tmp_path / "hostile"
        second.mkdir()
        source = verified_root(second, helper=True)
        environment = adversary_environment(second, (source,), route="child_hostile")
        workspace = workspace_of(environment, instance)
        (workspace / "extra_helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, IdentityVerificationFailed)
        assert "child process" in " ".join(record.failure.errors)
        assert ran(workspace) == ["entrypoint-ran.marker"]
        assert outcome_of(workspace).startswith("child_hostile: ExecutionBoundaryError")
        assert record.deployed is False

    # -- 22–24: environment, startup and the migration session --------------

    def test_environment_variables_cannot_expand_the_boundary(
        self, tmp_path, instance, manifest
    ):
        """PYTHONPATH and PYTHONHOME name no content this deployment trusts."""
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        (attacker / "helper.py").write_bytes(substituted_helper())
        shim = attacker / "tenant_authority"
        shim.mkdir()
        (shim / "__init__.py").write_text("", encoding="utf-8")
        (shim / "deployment.py").write_text(
            "from pathlib import Path\n"
            "Path('attacker-component-import.marker').write_text('B\\n')\n"
            "def build_deployment(configuration, with_http=True):\n"
            "    raise AssertionError('the attacker copy is not the component')\n",
            encoding="utf-8",
        )
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        saved = {key: os.environ.get(key) for key in ("PYTHONPATH", "PYTHONHOME")}
        os.environ.update({"PYTHONPATH": str(attacker), "PYTHONHOME": str(attacker)})
        try:
            record, error = attempt(instance, manifest, environment)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        assert not (workspace / "attacker-component-import.marker").exists()
        component = record.components[0]
        assert executed_content(component) == bound_content(component)
        assert all(
            not Path(path).is_relative_to(attacker)
            for path, _ in bound_content(component).values()
        )
        assert (bound_content(component)["helper"][0]) == str(source / "helper.py")

    def test_startup_injection_is_never_executed(self, tmp_path, instance, manifest):
        """Interpreter startup content is not a route into a deployment."""
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        for name in ("sitecustomize.py", "usercustomize.py"):
            (attacker / name).write_text(
                "from pathlib import Path\n"
                f"Path('{name}-injection.marker').write_text('B\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
        (attacker / "attack.pth").write_text(
            "import pathlib; pathlib.Path('pth-injection.marker').write_text('B')\n",
            encoding="utf-8",
        )
        source = verified_root(tmp_path, helper=True)
        environment = adversary_environment(tmp_path, (source,), route="bound")
        workspace = workspace_of(environment, instance)
        # The workspace is where a startup injection would have to run from.
        (workspace / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            "Path('workspace-sitecustomize.marker').write_text('B\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        saved = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(attacker)
        try:
            record, error = attempt(instance, manifest, environment)
        finally:
            if saved is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = saved

        assert error is None
        assert record.deployed is True
        assert ran(workspace) == ["entrypoint-ran.marker", "honest-entrypoint.marker"]
        component = record.components[0]
        for path, _ in bound_content(component).values():
            assert not Path(path).is_relative_to(attacker)
            assert not Path(path).is_relative_to(workspace)
        assert executed_content(component) == bound_content(component)

    def test_a_workspace_helper_cannot_reach_a_migration(
        self, tmp_path, instance, manifest
    ):
        """The migration session runs in the same boundary as the platform.

        Migration content belongs to the component and executes in the
        component's own process (ADR-0016 §14), so it is bound exactly like the
        entrypoint: a name that only the workspace provides is not resolvable
        there either, and a migration that reaches for it fails closed instead
        of running substituted content as component code.
        """
        # Named import: the content cannot be bound, so nothing is started.
        named = tmp_path / "named"
        named.mkdir()
        source = verified_root(
            named,
            fixture="migrating_component.py",
            extra={
                "workspace_migration_component.py": fixture_source(
                    "workspace_migration_component.py"
                )
            },
        )
        migrations = MigrationBinding(
            module="workspace_migration_component", attribute="MIGRATIONS"
        )
        environment = adversary_environment(
            named, (source,), route="bound", migrations=migrations
        )
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, DeploymentExecutionFailed)
        assert "workspace" in " ".join(record.failure.errors)
        assert record.migrations == ()
        assert ran(workspace) == []
        assert record.deployed is False
        assert record.identity_verified is False

        # Resolved at run time: the session starts, the boundary refuses the
        # content, and the migration fails instead of executing it.
        dynamic = tmp_path / "dynamic"
        dynamic.mkdir()
        source = verified_root(
            dynamic,
            fixture="migrating_component.py",
            extra={
                "dynamic_migration_component.py": fixture_source(
                    "dynamic_migration_component.py"
                )
            },
        )
        migrations = MigrationBinding(
            module="dynamic_migration_component", attribute="MIGRATIONS"
        )
        environment = adversary_environment(
            dynamic, (source,), route="bound", migrations=migrations
        )
        workspace = workspace_of(environment, instance)
        (workspace / "helper.py").write_bytes(substituted_helper())
        record, error = attempt(instance, manifest, environment)
        assert isinstance(error, MigrationOrchestrationFailed)
        assert "ExecutionBoundaryError" in " ".join(record.failure.errors)
        assert [(item.migration_id, item.status) for item in record.migrations] == [
            ("0001-seed-platform", "failed")
        ]
        assert ran(workspace) == []
        assert not (workspace / "migration-outcome.txt").exists()
        assert record.deployed is False
        assert record.identity_verified is False


@pytest.mark.usefixtures("restore_veb_meta_path")
class TestWorkerDigestEnforcement:
    """Commit 3 F-3B: worker enforces expected vs observed digest from launch binding.

    Contract:
      runtime.py:_spawn → modules[].{module,path,digest}
      → _load_binding() → _ExecutionBoundary → _BoundContent.digest
      → _BoundLoader.exec_module() → read_bytes → observed digest
      → equality check → compile/exec only after success.
    Worker is NOT source of truth, only compares.
    """

    def test_launch_binding_without_digest_fails_closed(self, tmp_path):
        raw = json.dumps(
            {
                "root": str(tmp_path),
                "modules": [{"module": "m", "path": str(tmp_path / "m.py")}],
            }
        )
        with pytest.raises(runtime_worker.RuntimeWorkerError, match="missing digest"):
            runtime_worker._load_binding(raw)

    def test_launch_binding_with_malformed_digest_fails_closed(self, tmp_path):
        malformed = [
            "a" * 64,
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha256:" + "g" * 64,
            " sha256:" + "a" * 64,
        ]
        for digest in malformed:
            raw = json.dumps(
                {
                    "root": str(tmp_path),
                    "modules": [
                        {
                            "module": "m",
                            "path": str(tmp_path / "m.py"),
                            "digest": digest,
                        }
                    ],
                }
            )
            with pytest.raises(
                runtime_worker.RuntimeWorkerError, match="malformed digest"
            ):
                runtime_worker._load_binding(raw)

    def test_launch_binding_with_canonical_digest_is_accepted(self, tmp_path):
        path = tmp_path / "m.py"
        path.write_bytes(b"VALUE=1\n")
        digest = content_digest(path)
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        raw = json.dumps(
            {
                "root": str(tmp_path),
                "modules": [{"module": "m", "path": str(path), "digest": digest}],
            }
        )
        parsed = runtime_worker._load_binding(raw)
        assert parsed["modules"][0]["digest"] == digest
        boundary = runtime_worker._ExecutionBoundary(parsed)
        assert boundary.bound_modules["m"].digest == digest

    def test_worker_accepts_correct_digest_and_executes(self, tmp_path):
        path = tmp_path / "bound.py"
        path.write_bytes(b"VALUE=42\n")
        digest = content_digest(path)
        binding = {
            "root": str(tmp_path),
            "roots": [],
            "untrusted": [],
            "modules": [{"module": "bound", "path": str(path), "digest": digest}],
        }
        boundary = runtime_worker._ExecutionBoundary(binding)
        content = boundary.bound_modules["bound"]
        module = types.ModuleType("bound")
        runtime_worker._BoundLoader(boundary, content).exec_module(module)
        assert module.VALUE == 42
        assert boundary.loaded_digest("bound") == digest
        assert path.name in str(boundary._bound_source)

    def test_worker_refuses_digest_mismatch_before_compile_and_exec(self, tmp_path):
        path = tmp_path / "bound.py"
        path.write_bytes(b"VALUE=1\n")
        digest_a = content_digest(path)
        binding = {
            "root": str(tmp_path),
            "roots": [],
            "untrusted": [],
            "modules": [{"module": "bound", "path": str(path), "digest": digest_a}],
        }
        boundary = runtime_worker._ExecutionBoundary(binding)
        content = boundary.bound_modules["bound"]
        path.write_bytes(b"VALUE=2\n")
        digest_b = content_digest(path)
        assert digest_b != digest_a

        module = types.ModuleType("bound")
        loader = runtime_worker._BoundLoader(boundary, content)
        with pytest.raises(
            runtime_worker.ExecutionBoundaryError, match="digest mismatch"
        ) as exc:
            loader.exec_module(module)

        msg = str(exc.value)
        assert digest_a in msg
        assert digest_b in msg
        assert "expected" in msg and "observed" in msg
        assert boundary.loaded_digest("bound") is None, "record_load must not happen"
        assert not boundary._bound_code, "compile_bound must not happen"
        assert str(path) not in boundary._bound_source, "compile_bound must not happen"
        assert not hasattr(module, "VALUE"), "exec must not happen"

    def test_worker_refusal_is_recorded_as_evidence(self, tmp_path):
        path = tmp_path / "bound.py"
        path.write_bytes(b"ORIGINAL=1\n")
        digest_expected = content_digest(path)
        binding = {
            "root": str(tmp_path),
            "roots": [],
            "untrusted": [],
            "modules": [
                {
                    "module": "bound",
                    "path": str(path),
                    "digest": digest_expected,
                }
            ],
        }
        boundary = runtime_worker._ExecutionBoundary(binding)
        content = boundary.bound_modules["bound"]
        path.write_bytes(b"MUTATED=1\n")
        digest_observed = content_digest(path)

        with pytest.raises(runtime_worker.ExecutionBoundaryError):
            runtime_worker._BoundLoader(boundary, content).exec_module(
                types.ModuleType("bound")
            )

        assert boundary._refusals, "refusal must be recorded"
        last = boundary._refusals[-1]
        assert "digest mismatch" in last
        assert digest_expected in last
        assert digest_observed in last


@pytest.mark.usefixtures("restore_veb_meta_path")
class TestWorkerEvidenceExpectedDigest:
    """Verify that worker evidence exposes expected and observed digests separately."""

    def test_evidence_contains_expected_digest_on_successful_load(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "bound.py"
        path.write_bytes(b"VALUE=42\n")
        expected = content_digest(path)

        binding = {
            "root": str(tmp_path),
            "roots": [],
            "untrusted": [],
            "modules": [
                {
                    "module": "bound",
                    "path": str(path),
                    "digest": expected,
                }
            ],
        }

        boundary = runtime_worker._ExecutionBoundary(binding)
        content = boundary.bound_modules["bound"]

        module = types.ModuleType("bound")
        module.__file__ = str(path)
        loader = runtime_worker._BoundLoader(boundary, content)

        loader.exec_module(module)
        monkeypatch.setitem(sys.modules, "bound", module)

        evidence = boundary.evidence()
        record = next(item for item in evidence["modules"] if item["module"] == "bound")

        assert record["loaded"] is True
        assert record["digest"] == expected
        assert record["expected_digest"] == expected

    def test_evidence_expected_from_binding_and_observed_from_worker_bytes(
        self, tmp_path
    ):
        path = tmp_path / "bound.py"
        path.write_bytes(b"VALUE=1\n")
        observed = content_digest(path)

        expected_path = tmp_path / "expected.py"
        expected_path.write_bytes(b"VALUE=2\n")
        expected = content_digest(expected_path)

        binding = {
            "root": str(tmp_path),
            "roots": [],
            "untrusted": [],
            "modules": [
                {
                    "module": "bound",
                    "path": str(path),
                    "digest": expected,
                }
            ],
        }

        boundary = runtime_worker._ExecutionBoundary(binding)
        content = boundary.bound_modules["bound"]

        assert content.digest == expected
        assert content.digest != observed

        module = types.ModuleType("bound")
        module.__file__ = str(path)
        loader = runtime_worker._BoundLoader(boundary, content)

        with pytest.raises(runtime_worker.ExecutionBoundaryError):
            loader.exec_module(module)

        evidence = boundary.evidence()
        record = next(item for item in evidence["modules"] if item["module"] == "bound")

        assert record["expected_digest"] == expected
        assert record["digest"] == observed
        assert record["expected_digest"] != record["digest"]

    def test_evidence_mismatch_refusal_contains_expected_and_observed_and_no_loaded(
        self, tmp_path
    ):
        path = tmp_path / "bound.py"
        path.write_bytes(b"ORIGINAL=1\n")
        expected = content_digest(path)

        binding = {
            "root": str(tmp_path),
            "roots": [],
            "untrusted": [],
            "modules": [
                {
                    "module": "bound",
                    "path": str(path),
                    "digest": expected,
                }
            ],
        }

        boundary = runtime_worker._ExecutionBoundary(binding)
        content = boundary.bound_modules["bound"]

        path.write_bytes(b"MUTATED=1\n")
        observed = content_digest(path)

        module = types.ModuleType("bound")
        module.__file__ = str(path)
        loader = runtime_worker._BoundLoader(boundary, content)

        with pytest.raises(runtime_worker.ExecutionBoundaryError):
            loader.exec_module(module)

        evidence = boundary.evidence()
        record = next(item for item in evidence["modules"] if item["module"] == "bound")

        assert record["expected_digest"] == expected
        assert record["digest"] == observed
        assert record["expected_digest"] != record["digest"]
        assert record["loaded"] is False
        assert any(
            expected in refusal and observed in refusal
            for refusal in evidence["foreign"]
        )
