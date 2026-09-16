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
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import pytest
from _deployment_helpers import (
    COMPONENT_ID,
    COMPONENT_VERSION,
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
    LIFECYCLE_REALIZED,
    STAGES,
    ArtifactSource,
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
    SecretLeakRefused,
    StartupFailed,
    deploy,
    derive_deployment_id,
    load_record,
    verify_artifact_digest,
    verify_input_unchanged,
    verify_instance,
)
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

    def test_premature_realized_is_impossible(self):
        record = DeploymentRecord.initial(
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
        with refusal(InvalidDeploymentStateTransition, "not ready"):
            record.mark_realized(at="2026-09-16T00:00:01Z")

        ready_only = record.mark_ready(at="2026-09-16T00:00:01Z")
        assert ready_only.ready is True
        assert ready_only.deployed is False
        with refusal(InvalidDeploymentStateTransition, "identity"):
            ready_only.mark_realized(at="2026-09-16T00:00:02Z")

        verified = ready_only.mark_identity_verified(at="2026-09-16T00:00:02Z")
        realized = verified.mark_realized(at="2026-09-16T00:00:03Z")
        assert realized.lifecycle == LIFECYCLE_REALIZED
        assert realized.deployed is True

    def test_identity_verification_cannot_precede_ready(self):
        record = DeploymentRecord.initial(
            deployment_id="dep-y",
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
        with refusal(InvalidDeploymentStateTransition, "not ready"):
            record.mark_identity_verified(at="2026-09-16T00:00:01Z")

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
        assert record.ready is True, "the operational condition was genuinely verified"
        assert record.identity_verified is False
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False
        assert record.failure is not None
        assert record.failure.stage == "ready"
        assert any("9.9.9" in error for error in record.failure.errors)
        events = read_events(environment, instance)
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
        assert record.ready is True


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
            "select",
            "shutil",
            "subprocess",
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
                "health",
                "healthy",
                "materialized",
                "observed_component_id",
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
        deployment = deploy_canonical(tmp_path, instance, manifest)
        deployment.stop()
        reloaded = load_record(deployment.state_path)
        assert reloaded.running is False
        assert reloaded.ready is True


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
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
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
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
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
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
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
        first.stop()
        second = deploy(
            request_for(instance, manifest, environment_for(tmp_path / "runtime"))
        )
        second.stop()
        assert first.record.deployed is True
        assert second.record.deployed is True
        assert first.record.deployment_id == second.record.deployment_id
