"""Architectural tests for the D&O ongoing runtime management / restart slice.

These tests verify architecture rules, not merely successful construction
(ADR-0015 §16; ADR-0016 §30). A restart is the explicit re-execution of a
runtime this deployment operation already realized: the platform is stopped,
started again from the very same bound content, and only a fresh
health/readiness verification and a fresh identity confirmation may establish
``running`` and ``ready`` again. It is *not* a new Platform Instance, not a new
deployment operation, not a new version and not a floating selector: the
identity, version, artifact digest, execution/content binding and deployment
target the authoritative record pins are preserved, or the attempt is refused
(ADR-0016 §5, §7–§10, §18; ADR-0017 §11, §33–§34, §39, §44).

The classes below hold the slice to the acceptance criteria of Issue #121 and
to nothing more:

* AC 1/2/18 — identity preservation: the restarted runtime is the same Platform
  Instance, the same component identity, version, artifact digest,
  execution/content binding and deployment target, and the attempt records the
  evidence it preserved instead of asserting it;
* AC 3/10 — the execution boundary is re-verified before the fresh start, and
  content, digest, version or instance substituted at any moment (before the
  attempt, during the stop, or immediately before execution) is refused rather
  than started;
* AC 4/5/6/12 — honest state: after the stop the platform is neither ``ready``
  nor ``running``; a started process claims nothing; ``ready`` comes only from
  fresh verification; a stop, start, health/readiness or identity failure is
  never turned into a success and never fabricates ``deployed``;
* AC 7/8/13 — stop/start policy: a plain stop starts nothing again, a failure
  triggers no automatic restart, one attempt at a time is admitted, a failed
  attempt leaves no uncontrolled runtime element behind and a controlled retry
  is a separately identified attempt;
* AC 9/11/14 — desired state, reconciliation and observability: a restart
  changes no desired state, repairs and hides no drift, writes no reconciliation
  observation of its own, and publishes distinct, ordered, correlated signals
  for every phase, carrying no secret material and no business data.

The synthetic fixtures below pin the *contract* at its edges (policy, admission,
phase failure, concurrency, substitution) with a deterministic runtime adapter
double and no process at all; the final class runs the same operation against a
genuinely deployed platform of this repository, realized through the real
Factory surfaces and the real runtime.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import re
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _deployment_helpers import (
    COMPONENT_ID,
    COMPONENT_VERSION,
    FIXTURE_PATH,
    PLATFORM_ID,
    ROOT,
    environment_for,
    instance_for,
    manifest_for,
    producer_correlation,
    request_for,
)

from deployment_operations import (
    LIFECYCLE_FAILED,
    LIFECYCLE_IN_PROGRESS,
    LIFECYCLE_REALIZED,
    LIFECYCLE_ROLLED_BACK,
    LIFECYCLE_SUPERSEDED,
    RECONCILIATION_DRIFT,
    RESTART_COMPLETED,
    RESTART_FAILED,
    RESTART_OUTCOMES,
    RESTART_PHASES,
    STRICT_STOP_START_POLICY,
    ActualComponentIdentity,
    ActualIdentityUnavailable,
    ActualPlatformSnapshot,
    ArtifactSource,
    ComponentBinding,
    ComponentRecord,
    ComponentRuntimeBinding,
    Deployment,
    DeploymentInputRejected,
    DeploymentRecord,
    DeploymentStateError,
    DeploymentStateStore,
    EventJournal,
    EvidenceProvenance,
    FailureRecord,
    IdentityField,
    InstanceVerification,
    InvalidDeploymentStateTransition,
    LocalProcessRuntime,
    OwnerSuppliedPlatformIdentityProvider,
    PlatformIdentityBinding,
    ReconciliationDriftDetected,
    ReconciliationRequest,
    RestartFailed,
    RestartInProgress,
    RestartPhaseRecord,
    RestartRecord,
    RestartRequest,
    RuntimeElement,
    RuntimeHandle,
    RuntimeProcessError,
    SecretLeakRefused,
    StopStartPolicy,
    deploy,
    derive_reconciliation_id,
    derive_restart_id,
    reconcile,
    restart,
    validate_stop_start_policy,
    verify_artifact_digest,
    verify_instance,
)
from deployment_operations.events import (
    EVENT_IDENTITY_VERIFIED,
    EVENT_PLATFORM_STOPPED,
    EVENT_RESTART_COMPLETED,
    EVENT_RESTART_EXECUTION_VERIFIED,
    EVENT_RESTART_FAILED,
    EVENT_RESTART_HEALTH_CHECKED,
    EVENT_RESTART_REQUESTED,
    EVENT_RESTART_STARTED,
    EVENT_RESTART_STOPPED,
    EVENT_RUNTIME_STARTED,
)
from deployment_operations.runtime import (
    OP_PROBE,
    OP_START,
    BoundModule,
    ExecutionBinding,
    verify_bound_content,
)
from deployment_operations.state import (
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_PENDING,
)
from deployment_operations.verification import compute_content_digest

#: The whole cycle of one restart attempt, in the order it must run (AC 11).
CYCLE_EVENTS: tuple[str, ...] = (
    EVENT_RESTART_REQUESTED,
    EVENT_PLATFORM_STOPPED,
    EVENT_RESTART_STOPPED,
    EVENT_RESTART_EXECUTION_VERIFIED,
    EVENT_RUNTIME_STARTED,
    EVENT_RESTART_STARTED,
    EVENT_RESTART_HEALTH_CHECKED,
    EVENT_IDENTITY_VERIFIED,
    EVENT_RESTART_COMPLETED,
)

#: The slice under test. ``import deployment_operations.restart`` would bind the
#: exported *function* of the same name, so the module is resolved explicitly —
#: the same way the rollback and upgrade slices are tested.
restart_module = importlib.import_module("deployment_operations.restart")

AT = "2026-09-30T00:00:00Z"

#: Honest component content bound as the execution closure of a synthetic
#: element — a copy of a fixture this repository owns, so that a test which
#: substitutes content never edits repository content.
BOUND_CONTENT = (FIXTURE_PATH / "migrating_component.py").read_bytes()

#: Content that imitates the pinned component without being it: the substitute
#: a §10 attack uses. It reports the pinned component identity, version and
#: platform and answers ``/health`` and ``/ready`` positively, so nothing a
#: runtime says about itself can expose it — only the content the engine bound
#: can. It leaves a marker where it runs, so a test can prove that it never did.
SUBSTITUTED_CONTENT = (FIXTURE_PATH / "binding_lie_component.py").read_bytes()

#: The marker the substituted content leaves behind if it ever really runs.
SUBSTITUTION_MARKER = "substituted-content.marker"


@contextmanager
def refusal(
    error_type: type[Exception], fragment: str
) -> Iterator[pytest.ExceptionInfo]:
    """Assert a fail-closed refusal that names its own reason (ADR-0016 §20)."""
    with pytest.raises(error_type) as caught:
        yield caught
    diagnostics = [*getattr(caught.value, "errors", ()), str(caught.value)]
    assert any(
        re.search(fragment, entry) for entry in diagnostics
    ), f"expected {fragment!r} among the refusal diagnostics: {diagnostics}"


# ---------------------------------------------------------------------------
# Synthetic fixtures: one realized deployment operation with a runtime double
# ---------------------------------------------------------------------------


class _Clock:
    """A deterministic, strictly ordered clock of second-precision instants."""

    def __init__(self) -> None:
        self.readings: list[str] = []

    def __call__(self) -> str:
        index = len(self.readings)
        instant = (
            f"2026-09-30T{index // 3600:02d}:"
            f"{(index // 60) % 60:02d}:{index % 60:02d}Z"
        )
        self.readings.append(instant)
        return instant


@dataclass
class _FakeProcess:
    """A process double: the runtime double below never touches a real one."""

    alive: bool = True
    pid: int = 4242
    returncode: int | None = None

    def poll(self) -> int | None:
        return None if self.alive else (self.returncode if self.returncode else 0)

    def wait(self, timeout: float | None = None) -> int:
        self.alive = False
        return self.returncode if self.returncode is not None else 0


@dataclass
class FakeRuntime:
    """A runtime adapter double: it records what was asked and answers honestly.

    It stands for the environment of a deployment operation whose runtime
    elements are already up. Everything the restart slice may ask of an adapter
    is answered from the element it was given — including the identity and the
    bound content a probe must report — and every failure the issue requires is
    expressible as a stated property of the double, never as a hidden default:

    * ``fail_stop`` / ``fail_start`` / ``fail_start_answer`` — a phase that
      raises or answers with an error;
    * ``unhealthy`` / ``probe_deadline`` — health/readiness that is not ready,
      or that never answers within its deadline;
    * ``before_stop`` / ``before_start`` / ``before_probe`` — a hook run at that
      moment, which is how content is substituted mid-attempt (§10) and how a
      test observes the state an attempt holds *between* two of its phases;
    * ``probe_body_extra`` — values a probe reports in addition to the honest
      ones, which is how secret material reaches a claim (§12);
    * ``pause`` — how long ``stop`` blocks, for the concurrency test.

    ``materialize`` and ``migrate`` refuse to be called at all: a restart
    neither provisions a slot nor executes a migration again (ADR-0017 §11).
    """

    fail_stop: frozenset[str] = frozenset()
    fail_start: frozenset[str] = frozenset()
    fail_start_answer: frozenset[str] = frozenset()
    unhealthy: frozenset[str] = frozenset()
    probe_deadline: frozenset[str] = frozenset()
    before_stop: Callable[[str], None] | None = None
    before_start: Callable[[RuntimeElement], None] | None = None
    before_probe: Callable[[str], None] | None = None
    #: Extra values a probe reports in its health body — how a test puts secret
    #: material where the operation would otherwise persist it (§12).
    probe_body_extra: Mapping[str, Any] = field(default_factory=dict)
    pause: threading.Event | None = None
    release: threading.Event | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    starts: list[str] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    live: dict[str, RuntimeHandle] = field(default_factory=dict)

    def materialize(self, element: RuntimeElement) -> Any:
        raise AssertionError("a restart materializes no runtime slot again")

    def migrate(self, element: RuntimeElement) -> Any:
        raise AssertionError("a restart executes no component migration again")

    def start(self, element: RuntimeElement) -> RuntimeHandle:
        component_id = element.component.component_id
        self.calls.append(("start", component_id))
        if self.before_start is not None:
            self.before_start(element)
        if component_id in self.fail_start:
            raise RuntimeProcessError(
                f"{component_id}: the runtime process could not be started again"
            )
        handle = RuntimeHandle(element=element, process=_FakeProcess(), started=True)
        self.live[component_id] = handle
        self.starts.append(component_id)
        return handle

    def request(
        self, handle: RuntimeHandle, operation: str, *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        component_id = handle.component_id
        self.calls.append((f"request:{operation}", component_id))
        if operation == OP_START:
            if component_id in self.fail_start_answer:
                return {
                    "status": "error",
                    "error": "the component runtime refused to construct itself",
                }
            return {"status": "ok"}
        if operation == OP_PROBE:
            if self.before_probe is not None:
                self.before_probe(component_id)
            if component_id in self.probe_deadline:
                raise RuntimeProcessError(
                    f"{component_id}: the runtime process did not answer the "
                    f"'probe' request within {timeout} s"
                )
            return self._probe(handle, healthy=component_id not in self.unhealthy)
        raise AssertionError(f"unexpected runtime operation {operation!r}")

    def stop(self, handle: RuntimeHandle) -> Mapping[str, Any]:
        component_id = handle.component_id
        self.calls.append(("stop", component_id))
        if self.pause is not None and self.release is not None:
            self.pause.set()
            assert self.release.wait(timeout=10.0), "the attempt never resumed"
        if self.before_stop is not None:
            self.before_stop(component_id)
        if component_id in self.fail_stop:
            raise RuntimeProcessError(
                f"{component_id}: the runtime element refused to stop"
            )
        was_alive = handle.process.poll() is None
        self.live.pop(component_id, None)
        self.stops.append(component_id)
        handle.process.alive = False
        return {
            "status": "stopped",
            "was_running": was_alive,
        }

    def _probe(self, handle: RuntimeHandle, *, healthy: bool) -> Mapping[str, Any]:
        """What an honest runtime element reports about itself and its content."""
        element = handle.element
        execution = element.execution
        assert execution is not None
        return {
            "status": "ok",
            "health": {
                "path": "/health",
                "status_code": 200,
                "body": {
                    "status": "ok" if healthy else "degraded",
                    "component_id": element.component.component_id,
                    "version": element.component.component_version,
                    "platform_id": element.platform_id,
                    **dict(self.probe_body_extra),
                },
            },
            "ready": {
                "path": "/ready",
                "status_code": 200,
                "body": {"status": "ready" if healthy else "unavailable"},
            },
            "execution": {
                "kind": execution.kind,
                "root": str(execution.root),
                "roots": [str(root) for root in execution.roots],
                "modules": [module.document() for module in execution.modules],
            },
        }


class _OwnerSource:
    """Test double of the Running Platform owner's observation surface.

    It answers for whatever evaluation the boundary bound it to and records the
    bindings it received, so the tests can prove that a restart asked the owner
    under a binding of *its own* attempt (AC 2, AC 14).
    """

    def __init__(self, build: Callable[[PlatformIdentityBinding], Any]) -> None:
        self.build = build
        self.bindings: list[PlatformIdentityBinding] = []

    def observe(self, binding: PlatformIdentityBinding) -> Any:
        self.bindings.append(binding)
        return self.build(binding)


def _snapshot(
    instance: Any, manifest: Mapping[str, Any], binding: PlatformIdentityBinding
) -> ActualPlatformSnapshot:
    """The platform that is actually running: the composition that was realized."""
    components = []
    for component in manifest.get("components", []):
        artifact = component.get("artifact")
        components.append(
            ActualComponentIdentity(
                component["component_id"],
                component["component_version"],
                artifact if isinstance(artifact, Mapping) else None,
            )
        )
    configuration = instance.document.get("configuration")
    return ActualPlatformSnapshot(
        platform_id=instance.document["platform_id"],
        manifest={
            "manifest_id": manifest["manifest_id"],
            "manifest_version": manifest["manifest_version"],
            "manifest_digest": manifest["manifest_digest"],
        },
        manifest_state=manifest["lifecycle"]["state"],
        components=tuple(components),
        membership_established=True,
        configuration=(
            IdentityField.present(configuration)
            if configuration is not None
            else IdentityField.absent()
        ),
        golden_bundle=IdentityField.absent(),
        golden_bundle_inventory_established=True,
        extensions=IdentityField.absent(),
        branding=IdentityField.absent(),
        provenance=EvidenceProvenance.MEASURED,
        correlation=producer_correlation(
            binding, target=str(instance.document["platform_id"])
        ),
        freshness_current=True,
    )


def _owner(instance: Any, manifest: Mapping[str, Any]) -> _OwnerSource:
    """An owner surface that reports the expected platform, freshly measured."""
    return _OwnerSource(lambda binding: _snapshot(instance, manifest, binding))


def _failing_owner(error: Exception) -> _OwnerSource:
    """An owner surface whose evidence is unavailable: fail closed (§20)."""

    def build(binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        raise error

    return _OwnerSource(build)


def _provider(source: _OwnerSource) -> OwnerSuppliedPlatformIdentityProvider:
    return OwnerSuppliedPlatformIdentityProvider(source)


@dataclass
class SyntheticPlatform:
    """One realized deployment operation with a runtime that answers, not runs.

    The desired state is real: the instance, its Manifest binding and the
    verification are produced by the real Factory surfaces, so the identity a
    restart must preserve is a genuine digest and not a fabricated string. The
    runtime is a double, so a phase can be made to fail exactly where the issue
    requires it to fail, in milliseconds and without a process.
    """

    deployment: Deployment
    runtime: FakeRuntime
    owner: _OwnerSource
    clock: _Clock
    element: RuntimeElement
    source: Path
    spec: Path
    workspace: Path
    operations: Path
    record: DeploymentRecord
    instance: Any
    manifest: Mapping[str, Any]
    verification: InstanceVerification
    environment_id: str = "local-test"

    @property
    def component(self) -> ComponentBinding:
        return self.element.component

    @property
    def component_id(self) -> str:
        return self.element.component.component_id

    def request(self, **overrides: Any) -> RestartRequest:
        """A restart request naming this operation, its instance and attempt 1."""
        values: dict[str, Any] = {
            "deployment": self.deployment,
            "restart_id": derive_restart_id(self.deployment),
        }
        values.update(overrides)
        return RestartRequest(**values)

    def restart(self, **overrides: Any) -> Any:
        """Run one restart attempt of this operation through the public API."""
        return restart(
            self.request(**overrides),
            runtime=self.runtime,
            identity_provider=_provider(self.owner),
            clock=self.clock,
        )

    def persisted(self) -> DeploymentRecord:
        """The authoritative record as deployment state holds it now."""
        return DeploymentStateStore(self.deployment.state_path).read()

    def events(self) -> list[dict[str, Any]]:
        """Every published signal of this operation, oldest first."""
        path = Path(self.deployment.events_path)
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def event_names(self) -> list[str]:
        return [event["event"] for event in self.events()]

    def attempt(self, index: int = -1) -> RestartRecord:
        attempts = self.persisted().restarts
        assert attempts, "the attempt was never recorded in deployment state"
        return attempts[index]

    def substitute_content(self) -> None:
        """Replace the bound execution content with content of another digest."""
        for module in self.element.execution.modules:  # type: ignore[union-attr]
            module.path.write_bytes(SUBSTITUTED_CONTENT)

    def restore_content(self) -> None:
        for module in self.element.execution.modules:  # type: ignore[union-attr]
            module.path.write_bytes(BOUND_CONTENT)


def synthetic_platform(
    tmp_path: Path,
    instance: Any,
    manifest: Mapping[str, Any],
    verification: InstanceVerification,
    *,
    running: bool = True,
    secrets: Sequence[str] = (),
    runtime: FakeRuntime | None = None,
) -> SyntheticPlatform:
    """Realize one deployment operation synthetically, then hand it over.

    What is built here is what a successful deployment leaves behind: an
    authoritative record that is realized, running, ready and identity-verified,
    a persisted state file that holds exactly that record, a journal, the
    runtime elements the operation owns — including the execution binding whose
    content digests are real — and the verified instance identity of the
    Factory surfaces.
    """
    environment_id = "local-test"
    binding = verification.components[0]
    deployment_id = f"dep-{verification.platform_id}-{environment_id}-{verification.instance_digest[:12]}"

    source = tmp_path / "verified-source"
    source.mkdir(parents=True, exist_ok=True)
    module_path = source / "bound_component.py"
    module_path.write_bytes(BOUND_CONTENT)
    workspace = (
        tmp_path
        / "runtime"
        / "deployments"
        / deployment_id
        / "components"
        / binding.component_id
    )
    workspace.mkdir(parents=True, exist_ok=True)
    spec = workspace / "runtime.json"

    execution = ExecutionBinding(
        component_id=binding.component_id,
        kind="component_source",
        root=source,
        roots=(source,),
        untrusted=(workspace,),
        modules=(
            BoundModule(
                module="bound_component",
                path=module_path,
                digest=compute_content_digest(module_path),
            ),
        ),
    )
    runtime_binding = ComponentRuntimeBinding(
        component_id=binding.component_id,
        deployment_module="bound_component",
        deployment_factory="build_component",
        import_paths=(source,),
    )
    element = RuntimeElement(
        deployment_id=deployment_id,
        environment_id=environment_id,
        platform_id=str(verification.platform_id),
        instance_digest=str(verification.instance_digest),
        component=binding,
        configuration={},
        spec_path=spec,
        workspace=workspace,
        binding=runtime_binding,
        artifact_source=ArtifactSource(path=None, digest=None, verified=False),
        interpreter=sys.executable,
        source_paths=(source,),
        execution=execution,
        timeout_seconds=5.0,
    )
    spec.write_text(
        json.dumps(
            {
                "$comment": "Runtime spec of one component of one operation (§8).",
                "deployment_id": deployment_id,
                "environment_id": environment_id,
                "platform_id": verification.platform_id,
                "instance_digest": verification.instance_digest,
                "manifest": {
                    "manifest_id": verification.manifest_id,
                    "manifest_version": verification.manifest_version,
                    "manifest_digest": verification.manifest_digest,
                },
                "component": binding.document(),
                "configuration": {},
                "artifact_source": element.artifact_source.document(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    record = DeploymentRecord.initial(
        deployment_id=deployment_id,
        environment_id=environment_id,
        attempt=1,
        platform_id=str(verification.platform_id),
        manifest_id=str(verification.manifest_id),
        manifest_version=str(verification.manifest_version),
        manifest_digest=str(verification.manifest_digest),
        manifest_state=str(verification.manifest_state),
        instance_digest=str(verification.instance_digest),
        at=AT,
    ).with_components(
        (
            ComponentRecord(
                component_id=binding.component_id,
                component_version=binding.component_version,
                artifact_type=binding.artifact_type,
                artifact_digest=binding.artifact_digest,
                materialized=True,
                artifact_verified=binding.has_published_artifact,
                runtime_started=running,
                execution=execution.document(),
                observed_execution=execution.document(),
                observed_component_id=binding.component_id,
                observed_version=binding.component_version,
                observed_platform_id=str(verification.platform_id),
                healthy=running,
            ),
        ),
        at=AT,
    )
    record = record.mark_ready(at=AT)
    record = record.mark_identity_verified(at=AT)
    record = record.mark_running(at=AT, running=True)
    record = record.mark_realized(at=AT)
    if not running:
        record = record.mark_stopped(at=AT)

    operations = tmp_path / "runtime" / "operations"
    operations.mkdir(parents=True, exist_ok=True)
    state_path = DeploymentStateStore.path_for(operations, deployment_id)
    store = DeploymentStateStore(state_path)
    store.write(record, secrets=tuple(secrets))
    journal = EventJournal(EventJournal.path_for(operations, deployment_id))
    adapter = runtime if runtime is not None else FakeRuntime()
    clock = _Clock()
    deployment = Deployment(
        record=record,
        verification=verification,
        state_path=state_path,
        events_path=journal.path,
        _runtime=adapter,
        _handles=(
            RuntimeHandle(
                element=element, process=_FakeProcess(alive=running), started=running
            ),
        ),
        _journal=journal,
        _store=store,
        _secrets=tuple(secrets),
        _clock=clock,
    )
    return SyntheticPlatform(
        deployment=deployment,
        runtime=adapter,
        owner=_owner(instance, manifest),
        clock=clock,
        element=element,
        source=source,
        spec=spec,
        workspace=workspace,
        operations=operations,
        record=record,
        instance=instance,
        manifest=manifest,
        verification=verification,
        environment_id=environment_id,
    )


def _pinned_identity(platform: SyntheticPlatform) -> dict[str, Any]:
    """Every field of desired state a restart must leave exactly as it is."""
    record = platform.persisted()
    return {
        "platform_id": record.platform_id,
        "instance_digest": record.instance_digest,
        "manifest_id": record.manifest_id,
        "manifest_version": record.manifest_version,
        "manifest_digest": record.manifest_digest,
        "manifest_state": record.manifest_state,
        "environment_id": record.environment_id,
        "deployment_id": record.deployment_id,
        "attempt": record.attempt,
        "components": [
            {
                "component_id": entry.component_id,
                "component_version": entry.component_version,
                "artifact_type": entry.artifact_type,
                "artifact_digest": entry.artifact_digest,
                "execution": entry.execution,
            }
            for entry in record.components
        ],
    }


def _authority_summary(record: DeploymentRecord) -> dict[str, Any]:
    """The authoritative state fields a restart must not redefine (§9, §18)."""
    return {
        "lifecycle": record.lifecycle,
        "failure": record.failure,
        "stages": record.stages,
        "migrations": record.migrations,
        "reconciliations": record.reconciliations,
        "created_at": record.created_at,
    }


def _digests(root: Path) -> dict[str, str]:
    """Content digests of a runtime tree, for «nothing was touched» checks."""
    digests: dict[str, str] = {}
    if not root.is_dir():
        return digests
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


@pytest.fixture(scope="module")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="module")
def manifest(root: Path) -> dict[str, Any]:
    return manifest_for(root=root)


@pytest.fixture(scope="module")
def instance(manifest: dict[str, Any], root: Path) -> Any:
    return instance_for(manifest, root=root)


@pytest.fixture(scope="module")
def verification(
    instance: Any, manifest: Mapping[str, Any], root: Path
) -> InstanceVerification:
    """The verified identity of the real instance the synthetic record pins."""
    result = verify_instance(
        instance.document, manifest, root=root, environment_id="local-test"
    )
    assert result.ok, result.all_errors()
    return result


@pytest.fixture
def platform(
    tmp_path: Path,
    instance: Any,
    manifest: Mapping[str, Any],
    verification: InstanceVerification,
) -> SyntheticPlatform:
    """One realized deployment operation whose runtime is up and honest."""
    return synthetic_platform(tmp_path, instance, manifest, verification)


@pytest.fixture
def stopped_platform(
    tmp_path: Path,
    instance: Any,
    manifest: Mapping[str, Any],
    verification: InstanceVerification,
) -> SyntheticPlatform:
    """The same operation after a plain stop: down, and not ready."""
    return synthetic_platform(tmp_path, instance, manifest, verification, running=False)


def _tamper_record(platform: SyntheticPlatform, **changes: Any) -> DeploymentRecord:
    """Replace the authoritative record, in memory and in state, with a lie.

    Both sides are replaced so that the attempt is admitted past the
    «persisted state is the record» check and is then judged on the record
    itself: this is what a tampered or substituted deployment state looks like
    from inside the capability (ADR-0016 §9).
    """
    record = replace(platform.deployment.record, **changes)
    platform.deployment._store.write(record)
    platform.deployment.record = record
    return record


def _tamper_components(
    platform: SyntheticPlatform, components: Sequence[ComponentRecord]
) -> DeploymentRecord:
    return _tamper_record(
        platform, components=tuple(components), updated_at=platform.record.updated_at
    )


def _component(platform: SyntheticPlatform, **changes: Any) -> ComponentRecord:
    return replace(platform.deployment.record.components[0], **changes)


def _called_names(path: Path) -> set[str]:
    """Every name this module calls, for «there is no such path» proofs."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    return called


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


RESTART_PATH = Path(restart_module.__file__)

#: Every other module of the capability: none of them may reach for a restart.
OTHER_MODULES: tuple[str, ...] = (
    "__main__.py",
    "deployment.py",
    "environment.py",
    "errors.py",
    "events.py",
    "health.py",
    "platform_identity.py",
    "platform_identity_source.py",
    "provisioning.py",
    "reconciliation.py",
    "rollback.py",
    "runtime.py",
    "runtime_worker.py",
    "state.py",
    "upgrade.py",
    "verification.py",
)


# ---------------------------------------------------------------------------
# AC 7 / AC 8 — a plain stop stops; a restart is a separate operation
# ---------------------------------------------------------------------------


class TestStopStartPolicy:
    """The stop/start rules are stated, enforced and never implicit."""

    def test_the_policy_states_the_rules_the_architecture_already_fixes(self) -> None:
        assert STRICT_STOP_START_POLICY == StopStartPolicy()
        assert validate_stop_start_policy(STRICT_STOP_START_POLICY) == []
        assert STRICT_STOP_START_POLICY.document() == {
            "restart_after_stop": False,
            "restart_on_failure": False,
            "allow_identity_change": False,
            "readiness_requires_verification": True,
            "max_in_flight_attempts": 1,
        }

    @pytest.mark.parametrize(
        ("weakened", "fragment"),
        [
            ({"restart_after_stop": True}, r"a plain stop may not start a runtime"),
            ({"restart_on_failure": True}, r"no implicit recovery"),
            ({"allow_identity_change": True}, r"may not select another"),
            (
                {"readiness_requires_verification": False},
                r"only by a fresh health/readiness verification",
            ),
            ({"max_in_flight_attempts": 2}, r"exactly one restart attempt"),
            ({"max_in_flight_attempts": 0}, r"exactly one restart attempt"),
        ],
    )
    def test_every_weakened_rule_is_a_refusal_not_a_knob(
        self, platform: SyntheticPlatform, weakened: dict[str, Any], fragment: str
    ) -> None:
        policy = replace(STRICT_STOP_START_POLICY, **weakened)
        assert validate_stop_start_policy(
            policy
        ), "a weakened policy must name the rule it weakens"
        with refusal(DeploymentInputRejected, fragment):
            platform.restart(policy=policy)
        assert platform.runtime.calls == []
        assert platform.event_names() == []
        assert platform.persisted() == platform.record

    def test_a_policy_that_is_not_a_policy_of_this_capability_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        assert validate_stop_start_policy(SimpleNamespace()) == [
            "$.policy: a stop/start policy of this capability is required"
        ]
        with refusal(DeploymentInputRejected, r"\$\.policy"):
            platform.restart(policy=SimpleNamespace())  # type: ignore[arg-type]

    def test_a_plain_stop_starts_nothing_again(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 7 — stopping is stopping, and it records exactly that."""
        record = platform.deployment.stop()

        assert [name for name, _ in platform.runtime.calls] == ["stop"]
        assert platform.runtime.starts == []
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        assert record.restarts == ()
        assert record.restarted is False
        assert [action.name for action in record.operational_actions] == [
            "platform_stopped"
        ]
        assert platform.event_names() == [EVENT_PLATFORM_STOPPED]
        assert platform.persisted() == record

    def test_a_repeated_stop_records_nothing_twice(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 7 — a second stop changes no state and invents no signal."""
        first = platform.deployment.stop()
        calls = list(platform.runtime.calls)
        events = platform.event_names()

        second = platform.deployment.stop()

        assert second == first
        assert [action.name for action in second.operational_actions] == [
            "platform_stopped"
        ]
        assert platform.event_names() == events
        assert platform.persisted() == first
        assert (
            len(platform.runtime.calls) == len(calls) + 1
        ), "the adapter is asked again, but no state or signal is invented twice"

    def test_nothing_in_the_capability_restarts_on_its_own_initiative(self) -> None:
        """AC 7 — there is no path from a stop or a failure to a start again."""
        for name in OTHER_MODULES:
            path = RESTART_PATH.parent / name
            assert "restart" not in _called_names(
                path
            ), f"{name} performs a restart of its own initiative"
            assert not any(
                module.endswith("restart") for module in _imported_modules(path)
            ), f"{name} reaches for the restart slice"

    def test_the_restart_slice_remediates_upgrades_and_rolls_back_nothing(self) -> None:
        """Non-goals — a restart is not a repair, an upgrade or a rollback."""
        called = _called_names(RESTART_PATH)
        for verb in ("deploy", "reconcile", "rollback", "upgrade", "supersede"):
            assert verb not in called, f"the restart slice calls {verb}()"
        imported = _imported_modules(RESTART_PATH)
        assert not any(
            module.endswith(("reconciliation", "rollback", "upgrade"))
            for module in imported
        ), f"the restart slice reaches for another operational slice: {imported}"

    def test_a_restart_is_a_separate_operation_of_the_same_deployment(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 8 — one operation, one state file, one more attempt in history."""
        before = platform.persisted()
        state_files = sorted(path.name for path in platform.operations.glob("*.json"))

        result = platform.restart()

        record = platform.persisted()
        assert result.completed is True
        assert result.outcome == RESTART_COMPLETED
        assert record.deployment_id == before.deployment_id
        assert record.attempt == before.attempt == 1
        assert record.environment_id == before.environment_id
        assert record.lifecycle == LIFECYCLE_REALIZED
        assert state_files and len(state_files) == 1
        assert (
            sorted(path.name for path in platform.operations.glob("*.json"))
            == state_files
        )
        assert len(record.restarts) == 1
        assert record.restarted is True
        assert record.last_restart is not None
        assert record.last_restart.sequence == 1
        assert _authority_summary(record) == _authority_summary(before)

    def test_a_restart_provisions_migrates_and_rewrites_nothing(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 3 — re-execution is not re-provisioning (ADR-0017 §11)."""
        workspace = _digests(platform.workspace)
        source = _digests(platform.source)

        result = platform.restart()

        assert result.completed is True
        assert [name for name, _ in platform.runtime.calls] == [
            "stop",
            "start",
            "request:start",
            "request:probe",
        ]
        assert _digests(platform.workspace) == workspace
        assert _digests(platform.source) == source
        assert platform.deployment.record.migrations == platform.record.migrations


# ---------------------------------------------------------------------------
# AC 3 / AC 8 — admission: an inexact subject is refused before any action
# ---------------------------------------------------------------------------


class TestRestartAdmission:
    """What may be restarted is one exact, realized, verified operation."""

    @pytest.mark.parametrize(
        "restart_id",
        [
            "",
            "deployment-platform",
            "latest",
            "restart:another-deployment",
            "restart:dep-x@platform:deployment-platform@instance:sha256:"
            + "0" * 64
            + "@local-test#attempt:1",
        ],
    )
    def test_an_id_that_does_not_name_this_attempt_is_refused(
        self, platform: SyntheticPlatform, restart_id: str
    ) -> None:
        with refusal(DeploymentInputRejected, r"\$\.restart_id"):
            platform.restart(restart_id=restart_id)
        assert platform.runtime.calls == []
        assert platform.event_names() == []
        assert platform.persisted() == platform.record

    def test_the_derived_id_names_the_operation_the_instance_and_the_attempt(
        self, platform: SyntheticPlatform
    ) -> None:
        record = platform.record
        assert derive_restart_id(platform.deployment) == (
            f"restart:{record.deployment_id}"
            f"@platform:{record.platform_id}"
            f"@instance:{record.instance_digest}"
            f"@{record.environment_id}"
            "#attempt:1"
        )

    def test_a_reason_that_states_nothing_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        with refusal(DeploymentInputRejected, r"\$\.reason"):
            platform.restart(reason="   ")
        assert platform.runtime.calls == []

    @pytest.mark.parametrize(
        ("tamper", "fragment"),
        [
            ({"lifecycle": LIFECYCLE_IN_PROGRESS}, r"not a realized deployment"),
            ({"lifecycle": LIFECYCLE_FAILED}, r"not a realized deployment"),
            ({"lifecycle": LIFECYCLE_ROLLED_BACK}, r"rolled back"),
            ({"lifecycle": LIFECYCLE_SUPERSEDED}, r"superseded"),
            ({"identity_verified": False}, r"no verified identity/version/digest"),
            (
                {
                    "failure": FailureRecord(
                        stage="ready", reason="it failed", occurred_at=AT
                    )
                },
                r"states a failure, not a realized platform",
            ),
            ({"instance_digest": "latest"}, r"does not pin a concrete instance digest"),
            (
                {"instance_digest": "deployment-platform"},
                r"does not pin a concrete instance digest",
            ),
            (
                {"platform_id": ""},
                r"does not carry a concrete Platform Instance identity",
            ),
            ({"platform_id": "latest"}, r"floating selector"),
            (
                {"manifest_id": "not an id"},
                r"does not carry a concrete Manifest identity",
            ),
            (
                {"manifest_version": "1.0"},
                r"does not carry a concrete Manifest version",
            ),
            (
                {"manifest_digest": "sha256:short"},
                r"does not pin a concrete Manifest digest",
            ),
            (
                {"manifest_state": "unknown"},
                r"does not carry a Platform Manifest lifecycle state",
            ),
            ({"components": ()}, r"carries no component identity"),
        ],
    )
    def test_a_record_that_is_not_one_exact_realized_operation_is_refused(
        self,
        platform: SyntheticPlatform,
        tamper: dict[str, Any],
        fragment: str,
    ) -> None:
        _tamper_record(platform, **tamper)
        with refusal(DeploymentInputRejected, fragment):
            platform.restart()
        assert platform.runtime.calls == [], "nothing was touched before the refusal"
        assert platform.event_names() == []

    @pytest.mark.parametrize(
        ("tamper", "fragment"),
        [
            ({"component_version": "1.0"}, r"a concrete component version is required"),
            ({"component_id": "not an id"}, r"a concrete component identity"),
            (
                {"artifact_type": "source_package", "artifact_digest": None},
                r"a sealed artifact digest is required",
            ),
            (
                {"artifact_type": "invented", "artifact_digest": None},
                r"the artifact identity is incomplete",
            ),
            (
                {"artifact_type": "none", "artifact_digest": "sha256:" + "a" * 64},
                r"artifact_type none carries the digest",
            ),
        ],
    )
    def test_a_component_that_is_not_exactly_pinned_is_refused(
        self,
        platform: SyntheticPlatform,
        tamper: dict[str, Any],
        fragment: str,
    ) -> None:
        _tamper_components(platform, (_component(platform, **tamper),))
        with refusal(DeploymentInputRejected, fragment):
            platform.restart()
        assert platform.runtime.calls == []

    def test_a_record_that_lists_one_component_twice_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        entry = _component(platform)
        _tamper_components(platform, (entry, entry))
        with refusal(DeploymentInputRejected, r"lists this component twice"):
            platform.restart()

    def test_an_operation_that_holds_no_runtime_element_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 3 — a restart re-executes elements, it never rebuilds them."""
        platform.deployment._handles = ()
        with refusal(DeploymentInputRejected, r"holds no runtime elements"):
            platform.restart()
        assert platform.runtime.calls == []

    def test_an_operation_without_a_runtime_adapter_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.deployment._runtime = None
        with refusal(DeploymentInputRejected, r"holds no runtime adapter"):
            platform.restart()

    def test_an_operation_without_a_verified_instance_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.deployment.verification = SimpleNamespace(components=())
        with refusal(DeploymentInputRejected, r"no verified instance identity"):
            platform.restart()

    def test_a_runtime_element_of_another_instance_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        substituted = replace(platform.element, instance_digest="sha256:" + "b" * 64)
        platform.deployment._handles = (
            RuntimeHandle(element=substituted, process=_FakeProcess()),
        )
        with refusal(DeploymentInputRejected, r"the runtime element was built for"):
            platform.restart()
        assert platform.runtime.calls == []

    def test_a_runtime_element_of_another_deployment_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        substituted = replace(platform.element, deployment_id="dep-another-operation")
        platform.deployment._handles = (
            RuntimeHandle(element=substituted, process=_FakeProcess()),
        )
        with refusal(DeploymentInputRejected, r"belongs to deployment"):
            platform.restart()

    def test_a_runtime_element_the_record_does_not_pin_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        foreign = replace(
            platform.element,
            component=replace(platform.element.component, component_id="other"),
        )
        platform.deployment._handles = (
            *platform.deployment._handles,
            RuntimeHandle(element=foreign, process=_FakeProcess()),
        )
        with refusal(DeploymentInputRejected, r"the record does not pin"):
            platform.restart()

    def test_a_state_file_that_is_not_the_record_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 3 — the attempt acts on the authoritative record or not at all."""
        platform.deployment._store.write(replace(platform.record, ready=False))
        with refusal(InvalidDeploymentStateTransition, r"changed or was tampered with"):
            platform.restart()
        assert platform.runtime.calls == []
        assert platform.event_names() == []

    def test_a_state_file_that_cannot_be_read_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        Path(platform.deployment.state_path).unlink()
        with refusal(
            InvalidDeploymentStateTransition,
            r"persisted deployment state is unavailable",
        ):
            platform.restart()
        assert platform.runtime.calls == []

    def test_a_refused_attempt_leaves_the_running_platform_alone(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 3 / AC 10 — a refusal is not a teardown: nothing is stopped."""
        state_bytes = Path(platform.deployment.state_path).read_bytes()
        workspace = _digests(platform.workspace)
        handle = platform.deployment._handles[0]
        platform.substitute_content()

        with refusal(DeploymentInputRejected, r"the content a claim would rest on"):
            platform.restart()

        assert platform.runtime.calls == []
        assert platform.event_names() == []
        assert Path(platform.deployment.state_path).read_bytes() == state_bytes
        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (True, True, True)
        assert record.restarts == ()
        assert handle.process.poll() is None, "the running platform was not touched"
        assert _digests(platform.workspace) == workspace
        platform.restore_content()
        assert verify_bound_content(platform.element.execution) == ()


# ---------------------------------------------------------------------------
# AC 1 / AC 2 / AC 3 / AC 10 / AC 18 — identity is preserved, or refused
# ---------------------------------------------------------------------------


class TestIdentityPreservation:
    """A restart re-executes the same instance, or it does not run at all."""

    def test_a_successful_restart_preserves_identity_version_and_digest(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 1 / AC 18 — and the attempt records the evidence, not a claim."""
        pinned = _pinned_identity(platform)
        bound = {
            module.module: module.digest
            for module in platform.element.execution.modules  # type: ignore[union-attr]
        }

        result = platform.restart()

        assert result.completed is True
        assert _pinned_identity(platform) == pinned, "desired state moved"
        attempt = result.attempt
        assert attempt.identity["platform_id"] == PLATFORM_ID
        assert attempt.identity["instance_digest"] == pinned["instance_digest"]
        assert attempt.identity["manifest_id"] == pinned["manifest_id"]
        assert attempt.identity["manifest_version"] == pinned["manifest_version"]
        assert attempt.identity["manifest_digest"] == pinned["manifest_digest"]
        assert attempt.identity["manifest_state"] == pinned["manifest_state"]
        assert len(attempt.identity["components"]) == 1
        component = attempt.identity["components"][0]
        assert component["component_id"] == COMPONENT_ID
        assert component["component_version"] == COMPONENT_VERSION
        assert component["artifact_type"] == "none"
        assert component["artifact_digest"] is None
        assert component["bound_modules"] == bound
        entry = platform.persisted().components[0]
        assert entry.observed_component_id == COMPONENT_ID
        assert entry.observed_version == COMPONENT_VERSION
        assert entry.observed_platform_id == PLATFORM_ID
        assert entry.healthy is True
        assert entry.execution == pinned["components"][0]["execution"]
        assert entry.artifact_digest is None
        assert entry.artifact_type == "none"

    def test_the_restarted_runtime_is_the_same_runtime_element(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 1 — the element that runs again is the element that was bound."""
        element = platform.element

        result = platform.restart()

        assert result.completed is True
        handle = platform.deployment._handles[0]
        assert handle.element is element
        assert handle.element.spec_path == platform.spec
        assert handle.element.workspace == platform.workspace
        assert handle.element.binding == element.binding
        assert handle.element.execution == element.execution
        assert handle.element.component == element.component

    def test_a_restart_asks_the_owner_under_a_binding_of_its_own_attempt(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 2 / AC 14 — fresh owner-side evidence, correlated to this attempt."""
        result = platform.restart()

        tokens = [binding.token for binding in platform.owner.bindings]
        assert tokens == [f"{platform.record.deployment_id}#restart:1"]
        detail = result.attempt.phase("identity_verification").detail
        assert detail["binding_token"] == tokens[0]
        assert detail["instance_digest"] == platform.record.instance_digest
        assert detail["identity_verified"] is True

    def test_the_execution_boundary_is_reverified_before_the_fresh_start(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 3 — verification happens between the stop and the start."""
        result = platform.restart()

        assert result.phases == RESTART_PHASES
        order = result.attempt.phase_order()
        assert order.index("stop") < order.index("execution_verification")
        assert order.index("execution_verification") < order.index("start")
        phase = result.attempt.phase("execution_verification")
        assert phase.status == STAGE_COMPLETED
        assert phase.detail["verified"] == list(restart_module.EXECUTION_CHECKS)
        assert phase.detail["identity"] == result.attempt.identity
        calls = [name for name, _ in platform.runtime.calls]
        assert calls.index("stop") < calls.index("start")

    def test_content_changed_between_the_initial_run_and_the_restart_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 1 — content that changed is content no claim rests on."""
        platform.substitute_content()

        with refusal(DeploymentInputRejected, r"the content a claim would rest on"):
            platform.restart()

        assert platform.runtime.calls == []
        assert platform.event_names() == []
        assert platform.persisted() == platform.record
        assert platform.persisted().restarts == ()
        assert platform.deployment._handles[0].process.poll() is None

    def test_bound_content_that_is_gone_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        for module in platform.element.execution.modules:  # type: ignore[union-attr]
            module.path.unlink()

        with refusal(
            DeploymentInputRejected, r"is gone; the executed content cannot be verified"
        ):
            platform.restart()

        assert platform.runtime.calls == []

    def test_a_changed_artifact_digest_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 2 — the sealed artifact identity is not a restart input."""
        _tamper_components(
            platform,
            (
                _component(
                    platform,
                    artifact_type="source_package",
                    artifact_digest="sha256:" + "c" * 64,
                ),
            ),
        )

        with refusal(DeploymentInputRejected, r"the record pins artifact digest"):
            platform.restart()

        assert platform.runtime.calls == []
        assert platform.event_names() == []

    def test_verified_artifact_content_is_reverified_against_the_pinned_digest(
        self, tmp_path: Path
    ) -> None:
        """Adversarial 2 — the same guarantee, applied to the bytes themselves."""
        artifact = tmp_path / "component-1.0.0.tar.gz"
        artifact.write_bytes(b"the pinned artifact content\n")
        pinned = compute_content_digest(artifact)
        binding = ComponentBinding(
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            artifact_type="source_package",
            artifact_digest=pinned,
            artifact_pinned=True,
            artifact_canonical_form="source_package/v1",
        )
        execution = ExecutionBinding(
            component_id=COMPONENT_ID,
            kind="artifact",
            root=artifact.parent,
            roots=(artifact.parent,),
            untrusted=(tmp_path / "workspace",),
            modules=(BoundModule(module="component", path=artifact, digest=pinned),),
        )

        assert restart_module._artifact_content_errors(binding, execution) == []
        assert verify_artifact_digest(binding, pinned) == []

        artifact.write_bytes(b"another artifact content\n")
        changed = restart_module._artifact_content_errors(binding, execution)
        assert len(changed) == 1
        assert "does not match the pinned artifact identity" in changed[0]

        artifact.unlink()
        gone = restart_module._artifact_content_errors(binding, execution)
        assert len(gone) == 1
        assert "is gone" in gone[0]

        # A component with no published artifact carries no artifact claim.
        assert (
            restart_module._artifact_content_errors(
                replace(binding, artifact_type="none", artifact_digest=None),
                replace(execution, kind="component_source"),
            )
            == []
        )

    def test_a_changed_component_version_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 3 — a restart selects no version."""
        _tamper_components(platform, (_component(platform, component_version="9.9.9"),))

        with refusal(DeploymentInputRejected, r"a restart selects no version"):
            platform.restart()

        assert platform.runtime.calls == []

    def test_an_element_bound_to_another_version_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        substituted = replace(
            platform.element,
            component=replace(platform.element.component, component_version="9.9.9"),
        )
        platform.deployment._handles = (
            RuntimeHandle(element=substituted, process=_FakeProcess()),
        )

        with refusal(DeploymentInputRejected, r"the runtime element binds version"):
            platform.restart()

        assert platform.runtime.calls == []

    def test_a_substituted_platform_instance_is_refused(
        self, platform: SyntheticPlatform, root: Path
    ) -> None:
        """Adversarial 4 — another instance is another deployment, not a restart."""
        other_manifest = manifest_for(root=root, manifest_id="another-platform")
        other = verify_instance(
            instance_for(other_manifest, root=root).document,
            other_manifest,
            root=root,
            environment_id=platform.environment_id,
        )
        assert other.ok, other.all_errors()
        assert other.manifest_digest != platform.verification.manifest_digest
        platform.deployment.verification = other

        with refusal(
            DeploymentInputRejected,
            r"a restart re-executes one exact Platform Instance and refuses a substituted one",
        ):
            platform.restart()

        assert platform.runtime.calls == []
        assert platform.event_names() == []
        assert platform.persisted() == platform.record

    def test_a_runtime_spec_that_names_another_instance_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 4 — the spec a process starts from is pinned, never edited."""
        document = json.loads(platform.spec.read_text(encoding="utf-8"))
        document["instance_digest"] = "sha256:" + "d" * 64
        document["manifest"]["manifest_version"] = "9.9.9"
        document["component"]["component_version"] = "9.9.9"
        platform.spec.write_text(json.dumps(document, indent=2), encoding="utf-8")

        with refusal(DeploymentInputRejected, r"the prepared runtime spec names"):
            platform.restart()

        assert platform.runtime.calls == []

    def test_an_unreadable_runtime_spec_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.spec.write_text("{ this is not json", encoding="utf-8")

        with refusal(DeploymentInputRejected, r"is unreadable"):
            platform.restart()

        assert platform.runtime.calls == []

    def test_an_execution_boundary_that_no_longer_confirms_content_is_refused(
        self, platform: SyntheticPlatform, tmp_path: Path
    ) -> None:
        """Adversarial 5 — the element now binds content the record never bound."""
        substituted = tmp_path / "substituted-source"
        substituted.mkdir()
        module_path = substituted / "bound_component.py"
        module_path.write_bytes(SUBSTITUTED_CONTENT)
        execution = ExecutionBinding(
            component_id=platform.component_id,
            kind="component_source",
            root=substituted,
            roots=(substituted,),
            untrusted=(platform.workspace,),
            modules=(
                BoundModule(
                    module="bound_component",
                    path=module_path,
                    digest=compute_content_digest(module_path),
                ),
            ),
        )
        assert (
            verify_bound_content(execution) == ()
        ), "the substituted content is self-consistent"
        platform.deployment._handles = (
            RuntimeHandle(
                element=replace(platform.element, execution=execution),
                process=_FakeProcess(),
            ),
        )

        with refusal(
            DeploymentInputRejected,
            r"the execution boundary no longer confirms the runtime content",
        ):
            platform.restart()

        assert platform.runtime.calls == []

    def test_a_record_without_an_execution_binding_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        _tamper_components(platform, (_component(platform, execution=None),))

        with refusal(DeploymentInputRejected, r"the record holds no execution binding"):
            platform.restart()

        assert platform.runtime.calls == []

    def test_an_element_without_bound_content_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.deployment._handles = (
            RuntimeHandle(
                element=replace(platform.element, execution=None),
                process=_FakeProcess(),
            ),
        )

        with refusal(
            DeploymentInputRejected, r"no execution content was bound for this element"
        ):
            platform.restart()

        assert platform.runtime.calls == []

    def test_content_substituted_during_the_stop_is_refused_before_any_start(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 17 — the window between the stop and the start is verified."""
        platform.runtime.before_stop = lambda _: platform.substitute_content()

        with refusal(
            RestartFailed, r"the content this deployment verified is not the content"
        ) as caught:
            platform.restart()

        assert caught.value.phase == "execution_verification"
        assert platform.runtime.starts == []
        assert platform.runtime.live == {}
        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        attempt = record.restarts[-1]
        assert attempt.outcome == RESTART_FAILED
        assert attempt.failure_phase == "execution_verification"
        assert attempt.phase_order() == ("requested", "stop", "execution_verification")
        assert any(
            "the content a claim would rest on" in entry for entry in attempt.errors
        )
        assert platform.event_names() == [
            EVENT_RESTART_REQUESTED,
            EVENT_PLATFORM_STOPPED,
            EVENT_RESTART_STOPPED,
            EVENT_RESTART_FAILED,
        ]
        assert EVENT_RESTART_EXECUTION_VERIFIED not in platform.event_names()
        assert EVENT_RUNTIME_STARTED not in platform.event_names()

    def test_an_identity_that_diverged_after_the_start_is_refused(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 2 — the owner's fresh evidence decides, not the record's old claim."""

        def diverged(binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
            snapshot = _snapshot(platform.instance, platform.manifest, binding)
            return replace(
                snapshot,
                components=tuple(
                    replace(entry, component_version="9.9.9")
                    for entry in snapshot.components
                ),
            )

        platform.owner.build = diverged

        with refusal(
            RestartFailed,
            r"running platform identity does not match the requested instance",
        ) as caught:
            platform.restart()

        assert caught.value.phase == "identity_verification"
        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        assert record.restarts[-1].failure_phase == "identity_verification"
        assert EVENT_IDENTITY_VERIFIED not in platform.event_names()
        assert platform.runtime.live == {}
        assert record.identity_verified is True, (
            "identity_verified keeps stating what the deployment operation "
            "verified; no running claim rests on it while nothing runs"
        )

    def test_identity_evidence_that_is_unavailable_fails_closed(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.owner = _failing_owner(
            ActualIdentityUnavailable(["the owner published no state"])
        )

        with refusal(
            RestartFailed, r"running platform identity evidence is unavailable"
        ):
            platform.restart()

        record = platform.persisted()
        assert record.restarts[-1].failure_phase == "identity_verification"
        assert (record.running, record.ready, record.deployed) == (False, False, False)


# ---------------------------------------------------------------------------
# AC 4 / AC 5 / AC 6 / AC 12 — the state says what happened, and only that
# ---------------------------------------------------------------------------


class TestHonestState:
    """Stopped is stopped, started is not ready, and failed is never success."""

    def test_after_the_stop_the_platform_is_neither_ready_nor_running(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 4 — observed from inside the attempt, between two of its phases."""
        seen: dict[str, DeploymentRecord] = {}
        platform.runtime.before_start = lambda element: seen.setdefault(
            "record", platform.persisted()
        )

        result = platform.restart()

        assert result.completed is True
        mid = seen["record"]
        assert (mid.running, mid.ready, mid.deployed) == (False, False, False)
        assert [action.name for action in mid.operational_actions][-1] == (
            "platform_stopped"
        )
        assert mid.components[0].runtime_started is False

    def test_a_started_process_claims_no_readiness_of_its_own(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 5 — ``ready`` comes only from the fresh verification that follows."""
        seen: dict[str, DeploymentRecord] = {}
        platform.runtime.before_probe = lambda _: seen.setdefault(
            "record", platform.persisted()
        )

        result = platform.restart()

        assert result.completed is True
        mid = seen["record"]
        assert mid.running is True, "the fresh start was recorded before the probe"
        assert mid.ready is False, "a started process is not a ready platform"
        assert mid.deployed is False
        assert mid.components[0].runtime_started is True
        final = platform.persisted()
        assert (final.running, final.ready, final.deployed) == (True, True, True)
        assert final.identity_verified is True

    def test_a_stop_that_failed_is_not_reported_as_a_restart(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 6 — an element that refused to stop leaves no false stop claim."""
        platform.runtime.fail_stop = frozenset({platform.component_id})

        with refusal(RestartFailed, r"refused to stop") as caught:
            platform.restart()

        assert caught.value.phase == "stop"
        assert platform.runtime.starts == []
        record = platform.persisted()
        assert record.running is True, "the element that refused is still up"
        assert record.ready is False, "readiness it cannot verify is withdrawn"
        assert record.deployed is False
        assert [action.name for action in record.operational_actions] == [
            "readiness_withdrawn",
            "platform_restart_failed",
        ]
        assert record.components[0].runtime_started is True
        attempt = record.restarts[-1]
        assert attempt.outcome == RESTART_FAILED
        assert attempt.failure_phase == "stop"
        assert attempt.phase_order() == ("requested", "stop")
        assert attempt.errors and platform.component_id in attempt.errors[0]
        assert platform.event_names() == [
            EVENT_RESTART_REQUESTED,
            EVENT_RESTART_FAILED,
        ]
        assert EVENT_RESTART_STOPPED not in platform.event_names()

    def test_a_start_that_failed_after_a_successful_stop_is_recorded_honestly(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 6 — the stop happened, the start did not, both are stated."""
        platform.runtime.fail_start = frozenset({platform.component_id})

        with refusal(RestartFailed, r"could not be started") as caught:
            platform.restart()

        assert caught.value.phase == "start"
        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        assert [action.name for action in record.operational_actions] == [
            "platform_stopped",
            "platform_restart_failed",
        ]
        assert record.components[0].runtime_started is False
        attempt = record.restarts[-1]
        assert attempt.failure_phase == "start"
        assert attempt.phase_order() == (
            "requested",
            "stop",
            "execution_verification",
            "start",
        )
        assert platform.event_names() == [
            EVENT_RESTART_REQUESTED,
            EVENT_PLATFORM_STOPPED,
            EVENT_RESTART_STOPPED,
            EVENT_RESTART_EXECUTION_VERIFIED,
            EVENT_RESTART_FAILED,
        ]
        assert platform.runtime.live == {}

    def test_a_start_the_component_refused_is_not_a_started_platform(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.runtime.fail_start_answer = frozenset({platform.component_id})

        with refusal(RestartFailed, r"refused to construct itself"):
            platform.restart()

        record = platform.persisted()
        assert record.running is False
        assert record.components[0].runtime_started is False
        assert platform.runtime.live == {}
        assert record.restarts[-1].failure_phase == "start"

    def test_a_health_failure_after_a_successful_start_is_not_ready(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 7 — started and answering, but not healthy."""
        platform.runtime.unhealthy = frozenset({platform.component_id})

        with refusal(RestartFailed, r"did not establish ready") as caught:
            platform.restart()

        assert caught.value.phase == "health_check"
        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        entry = record.components[0]
        assert entry.healthy is False
        assert entry.health is not None
        assert entry.health["body"]["status"] == "degraded"
        attempt = record.restarts[-1]
        assert attempt.failure_phase == "health_check"
        assert any("is not healthy" in error for error in attempt.errors)
        assert platform.runtime.live == {}, "the started element was stopped again"
        assert [name for name, _ in platform.runtime.calls][-1] == "stop"
        events = platform.events()
        assert [event["event"] for event in events][-2:] == [
            EVENT_RESTART_HEALTH_CHECKED,
            EVENT_RESTART_FAILED,
        ]
        assert events[-2]["detail"]["ready"] is False
        assert events[-1]["detail"]["failure_phase"] == "health_check"

    def test_a_health_deadline_is_a_failure_not_a_ready_platform(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 8 — no answer within the deadline is not readiness."""
        platform.runtime.probe_deadline = frozenset({platform.component_id})

        with refusal(RestartFailed, r"did not answer the 'probe' request within"):
            platform.restart()

        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        assert record.restarts[-1].failure_phase == "health_check"
        assert platform.runtime.live == {}

    @pytest.mark.parametrize(
        ("weaken", "phase"),
        [
            ("fail_stop", "stop"),
            ("fail_start", "start"),
            ("fail_start_answer", "start"),
            ("unhealthy", "health_check"),
            ("probe_deadline", "health_check"),
        ],
    )
    def test_no_phase_failure_becomes_a_deployed_claim(
        self, platform: SyntheticPlatform, weaken: str, phase: str
    ) -> None:
        """AC 12 — ``deployed`` keeps meaning realized, running, ready, verified."""
        setattr(platform.runtime, weaken, frozenset({platform.component_id}))
        before = _authority_summary(platform.persisted())

        with pytest.raises(RestartFailed) as caught:
            platform.restart()

        assert caught.value.phase == phase
        record = platform.persisted()
        assert record.deployed is False
        assert record.restarted is False
        assert (
            record.lifecycle == LIFECYCLE_REALIZED
        ), "a restart is an operational action, never a lifecycle transition"
        assert record.failure is None, (
            "a restart failure is runtime-management history, not the deployment "
            "operation's own failure record (ADR-0016 §20)"
        )
        assert _authority_summary(record) == before
        assert record.restarts[-1].outcome == RESTART_FAILED
        assert platform.persisted() == platform.deployment.record

    def test_a_successful_restart_is_deployed_because_of_its_conditions(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 12 — and a success states why: every condition, freshly held."""
        result = platform.restart()

        record = platform.persisted()
        assert result.record == record
        assert record.lifecycle == LIFECYCLE_REALIZED
        assert record.running is True
        assert record.ready is True
        assert record.identity_verified is True
        assert record.deployed is True
        assert record.restarted is True
        assert record.deployed == (
            record.lifecycle == LIFECYCLE_REALIZED
            and record.running
            and record.ready
            and record.identity_verified
        )

    def test_a_repeated_stop_before_a_restart_creates_no_false_state(
        self, stopped_platform: SyntheticPlatform
    ) -> None:
        """Adversarial 9 — an already stopped platform is restarted, not re-stopped."""
        platform = stopped_platform
        assert platform.persisted().running is False
        assert [action.name for action in platform.record.operational_actions] == [
            "platform_stopped"
        ]

        result = platform.restart()

        assert result.completed is True
        attempt = platform.attempt()
        assert attempt.phase("stop").detail["already_stopped"] is True
        assert EVENT_PLATFORM_STOPPED not in platform.event_names()
        assert [action.name for action in platform.persisted().operational_actions] == [
            "platform_stopped",
            "platform_restarted",
        ]
        assert platform.runtime.stops == [platform.component_id]
        assert platform.runtime.starts == [platform.component_id]

    def test_a_restart_after_two_plain_stops_is_one_restart(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 9 — stopping twice, then restarting once, is one attempt."""
        platform.deployment.stop()
        platform.deployment.stop()
        assert [action.name for action in platform.persisted().operational_actions] == [
            "platform_stopped"
        ]

        result = platform.restart()

        assert result.completed is True
        assert len(platform.persisted().restarts) == 1
        assert platform.runtime.starts == [platform.component_id]

    def test_a_vanished_runtime_process_is_replaced_and_never_reused(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 13 — the element that disappeared is not what runs now."""
        handle = platform.deployment._handles[0]
        handle.process.alive = False
        handle.process.returncode = 137

        result = platform.restart()

        assert result.completed is True
        fresh = platform.deployment._handles[0]
        assert fresh is not handle
        assert handle.process.poll() is not None
        assert fresh.process.poll() is None
        assert platform.runtime.starts == [platform.component_id]
        stopped = platform.attempt().phase("stop")
        assert stopped.detail["answers"][platform.component_id]["was_running"] is False
        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (True, True, True)


# ---------------------------------------------------------------------------
# AC 13 — a controlled retry, and never two runtimes at once
# ---------------------------------------------------------------------------


class TestRetryAndConcurrency:
    """One attempt at a time; a retry is a new, separately identified attempt."""

    def test_a_failed_attempt_leaves_no_uncontrolled_runtime_element(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.runtime.unhealthy = frozenset({platform.component_id})

        with pytest.raises(RestartFailed):
            platform.restart()

        assert platform.runtime.live == {}
        assert all(
            handle.process.poll() is not None for handle in platform.deployment._handles
        ), "the operation keeps managing elements that exist — and none is alive"
        calls = [name for name, _ in platform.runtime.calls]
        assert calls.count("start") == 1
        assert calls.count("stop") == 2, "the attempt's stop, then its own cleanup"
        assert calls[-1] == "stop"

    def test_a_controlled_retry_is_a_separately_identified_attempt(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 14 — a retry adds an attempt, it never rewrites one."""
        platform.runtime.unhealthy = frozenset({platform.component_id})
        with refusal(RestartFailed, r"did not establish ready"):
            platform.restart()
        first = platform.attempt()
        assert (first.outcome, first.sequence) == (RESTART_FAILED, 1)
        assert first.restart_id.endswith("#attempt:1")
        assert (
            first.phase("identity_verification").status == STAGE_PENDING
        ), "the failed attempt never reached the identity confirmation"
        assert platform.owner.bindings == []

        platform.runtime.unhealthy = frozenset()
        second_id = derive_restart_id(platform.deployment)
        assert second_id.endswith("#attempt:2")

        result = platform.restart()

        assert result.completed is True
        record = platform.persisted()
        assert len(record.restarts) == 2
        assert [entry.sequence for entry in record.restarts] == [1, 2]
        assert [entry.outcome for entry in record.restarts] == [
            RESTART_FAILED,
            RESTART_COMPLETED,
        ]
        assert record.restarts[0] == first, "the failed attempt was not rewritten"
        assert record.restarts[0].failure_phase == "health_check"
        assert record.restarts[1].restart_id == second_id
        assert (record.running, record.ready, record.deployed) == (True, True, True)
        assert record.restarted is True
        assert platform.owner.bindings[-1].token == (
            f"{record.deployment_id}#restart:2"
        ), "the retry was verified under a binding of its own attempt"
        assert derive_restart_id(platform.deployment).endswith("#attempt:3")

    def test_a_finished_attempt_id_names_a_finished_attempt(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 8 — the id of attempt 1 cannot be replayed as attempt 2."""
        platform.runtime.fail_start = frozenset({platform.component_id})
        with pytest.raises(RestartFailed):
            platform.restart()
        stale = platform.persisted().restarts[0].restart_id

        platform.runtime.fail_start = frozenset()
        with refusal(DeploymentInputRejected, r"\$\.restart_id"):
            platform.restart(restart_id=stale)

        assert len(platform.persisted().restarts) == 1
        assert platform.runtime.starts == []

    def test_two_concurrent_attempts_do_not_spawn_two_runtimes(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 16 — one attempt runs, the other is refused outright."""
        pause, release = threading.Event(), threading.Event()
        platform.runtime.pause = pause
        platform.runtime.release = release
        outcomes: dict[str, Any] = {}

        def attempt(name: str) -> None:
            try:
                outcomes[name] = platform.restart()
            except Exception as error:  # noqa: BLE001 - asserted below
                outcomes[name] = error

        admitted = threading.Thread(target=attempt, args=("admitted",), daemon=True)
        admitted.start()
        assert pause.wait(timeout=10.0), "the admitted attempt never reached its stop"

        refused = threading.Thread(target=attempt, args=("refused",), daemon=True)
        refused.start()
        refused.join(timeout=10.0)
        release.set()
        admitted.join(timeout=10.0)
        assert not admitted.is_alive() and not refused.is_alive()

        assert isinstance(outcomes["refused"], RestartInProgress)
        assert any("already in flight" in entry for entry in outcomes["refused"].errors)
        assert isinstance(outcomes["admitted"], restart_module.Restart)
        assert outcomes["admitted"].completed is True
        record = platform.persisted()
        assert len(record.restarts) == 1
        assert platform.runtime.starts == [platform.component_id]
        assert len(platform.deployment._handles) == 1
        assert [name for name, _ in platform.runtime.calls] == [
            "stop",
            "start",
            "request:start",
            "request:probe",
        ], "the refused attempt performed no runtime action at all"
        assert platform.event_names().count(EVENT_RESTART_REQUESTED) == 1
        assert (record.running, record.ready, record.deployed) == (True, True, True)

    def test_an_attempt_while_one_is_in_flight_is_refused_before_any_action(
        self, platform: SyntheticPlatform
    ) -> None:
        lock = restart_module._attempt_lock(platform.deployment.state_path)
        assert lock.acquire(blocking=False)
        try:
            with refusal(RestartInProgress, r"already in flight"):
                platform.restart()
            assert platform.runtime.calls == []
            assert platform.event_names() == []
            assert platform.persisted() == platform.record
        finally:
            lock.release()

        assert platform.restart().completed is True

    def test_the_attempt_lock_is_the_one_of_the_authoritative_record(
        self,
        tmp_path: Path,
        instance: Any,
        manifest: Mapping[str, Any],
        verification: InstanceVerification,
    ) -> None:
        """Serialization is per deployment operation, not per capability."""
        one = synthetic_platform(tmp_path / "one", instance, manifest, verification)
        two = synthetic_platform(tmp_path / "two", instance, manifest, verification)

        assert restart_module._attempt_lock(one.deployment.state_path) is (
            restart_module._attempt_lock(Path(one.deployment.state_path))
        )
        assert restart_module._attempt_lock(one.deployment.state_path) is not (
            restart_module._attempt_lock(two.deployment.state_path)
        )
        assert one.restart().completed is True
        assert two.restart().completed is True


# ---------------------------------------------------------------------------
# AC 11 / AC 14 — ordered, correlated, honest and secret-free observability
# ---------------------------------------------------------------------------


class TestObservability:
    """Every phase is a distinct signal, correlated to the platform it re-ran."""

    def test_the_signals_of_a_successful_restart_are_ordered(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 15 — the exact order of the operational events."""
        result = platform.restart()

        assert result.completed is True
        assert platform.event_names() == list(CYCLE_EVENTS)
        events = platform.events()
        assert [event["detail"]["phase"] for event in events] == [
            "requested",
            "stop",
            "stop",
            "execution_verification",
            "start",
            "start",
            "health_check",
            "identity_verification",
            "completed",
        ]
        sequences = [event["sequence"] for event in events]
        assert sequences == list(range(1, len(events) + 1))
        stamps = [event["occurred_at"] for event in events]
        assert stamps == sorted(stamps)

    def test_every_signal_is_correlated_to_the_platform_it_re_executed(
        self, platform: SyntheticPlatform
    ) -> None:
        result = platform.restart()
        record = platform.record
        attempt = result.attempt

        for event in platform.events():
            assert event["stage"] == "restart"
            assert event["deployment_id"] == record.deployment_id
            assert event["environment_id"] == record.environment_id
            assert event["platform_instance"]["platform_id"] == PLATFORM_ID
            assert event["platform_instance"]["instance_digest"] == (
                record.instance_digest
            )
            assert event["manifest"]["manifest_id"] == record.manifest_id
            assert event["manifest"]["manifest_version"] == record.manifest_version
            assert event["manifest"]["manifest_digest"] == record.manifest_digest
            assert event["detail"]["restart_id"] == attempt.restart_id
            assert event["detail"]["restart_sequence"] == attempt.sequence

    def test_the_runtime_signals_of_the_attempt_name_the_component(
        self, platform: SyntheticPlatform
    ) -> None:
        platform.restart()
        events = platform.events()
        started = [event for event in events if event["event"] == EVENT_RUNTIME_STARTED]
        assert len(started) == 1
        assert started[0]["component"]["component_id"] == COMPONENT_ID
        assert started[0]["component"]["component_version"] == COMPONENT_VERSION
        assert started[0]["component"]["artifact_type"] == "none"
        assert started[0]["component"]["artifact_digest"] is None
        assert started[0]["detail"]["restart"] is True
        stopped = [
            event for event in events if event["event"] == EVENT_PLATFORM_STOPPED
        ]
        assert len(stopped) == 1
        assert stopped[0]["detail"]["restart"] is True
        assert stopped[0]["detail"]["stopped"] == [COMPONENT_ID]

    def test_the_phases_of_the_attempt_are_recorded_in_order(
        self, platform: SyntheticPlatform
    ) -> None:
        result = platform.restart()
        attempt = result.attempt

        assert [entry.name for entry in attempt.phases] == list(RESTART_PHASES)
        assert attempt.phase_order() == RESTART_PHASES
        assert all(entry.status == STAGE_COMPLETED for entry in attempt.phases)
        stamps = [entry.occurred_at for entry in attempt.phases]
        assert all(stamps) and stamps == sorted(stamps)
        assert attempt.requested_at <= attempt.completed_at
        assert attempt.failure_phase is None
        assert attempt.failure_reason is None
        assert attempt.errors == ()
        assert attempt.reason == "explicit_runtime_management_request"
        assert attempt.outcome == RESTART_COMPLETED

    @pytest.mark.parametrize(
        ("weaken", "phase", "reached"),
        [
            ("fail_stop", "stop", ("requested", "stop")),
            (
                "fail_start",
                "start",
                ("requested", "stop", "execution_verification", "start"),
            ),
            (
                "unhealthy",
                "health_check",
                (
                    "requested",
                    "stop",
                    "execution_verification",
                    "start",
                    "health_check",
                ),
            ),
            (
                "probe_deadline",
                "health_check",
                (
                    "requested",
                    "stop",
                    "execution_verification",
                    "start",
                    "health_check",
                ),
            ),
        ],
    )
    def test_a_failed_attempt_publishes_its_phase_and_its_cause(
        self,
        platform: SyntheticPlatform,
        weaken: str,
        phase: str,
        reached: tuple[str, ...],
    ) -> None:
        """AC 6 / AC 14 — a failure is diagnosable from the signal alone."""
        setattr(platform.runtime, weaken, frozenset({platform.component_id}))

        with pytest.raises(RestartFailed) as caught:
            platform.restart()

        failure = caught.value
        assert failure.phase == phase
        events = platform.events()
        assert events[-1]["event"] == EVENT_RESTART_FAILED
        assert events[-1]["detail"]["phase"] == phase
        assert events[-1]["detail"]["failure_phase"] == phase
        assert events[-1]["detail"]["reason"] == failure.reason
        assert events[-1]["detail"]["errors"] == list(failure.errors)
        assert events[-1]["detail"]["deployed"] is False
        assert events[-1]["detail"]["ready"] is False
        attempt = platform.attempt()
        assert attempt.outcome == RESTART_FAILED
        assert attempt.failure_phase == phase
        assert attempt.failure_reason == failure.reason
        assert attempt.errors == tuple(failure.errors)
        assert attempt.phase_order() == reached
        assert attempt.phase(phase).status == STAGE_FAILED
        assert all(
            entry.status == STAGE_PENDING
            for entry in attempt.phases[attempt.phase_order().__len__() :]
        )

    def test_state_is_written_before_the_signal_that_claims_it(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 14 / §19 — the journal never claims what the state does not hold."""
        seen: list[DeploymentRecord] = []

        def watch(_: object) -> None:
            seen.append(platform.persisted())

        platform.runtime.before_start = watch
        platform.runtime.before_probe = watch
        result = platform.restart()

        assert result.completed is True
        at_start, at_probe = seen
        names = platform.event_names()
        assert (at_start.running, at_start.ready) == (
            False,
            False,
        ), "the stop was written before the platform was started again"
        assert at_start.restarts == ()
        assert names.index(EVENT_RESTART_STOPPED) < names.index(EVENT_RESTART_STARTED)
        assert (at_probe.running, at_probe.ready) == (
            True,
            False,
        ), "the start was written before readiness was claimed"
        assert at_probe.restarts == (), "the attempt is recorded when it is over"
        assert names.index(EVENT_RESTART_HEALTH_CHECKED) < names.index(
            EVENT_RESTART_COMPLETED
        )
        assert platform.persisted().restarts[-1].outcome == RESTART_COMPLETED

    def test_a_runtime_answer_carrying_a_secret_is_refused_not_stored(
        self,
        tmp_path: Path,
        instance: Any,
        manifest: Mapping[str, Any],
        verification: InstanceVerification,
    ) -> None:
        """AC 14 / §12 — the secret is refused, and the state stays honest."""
        secret = "hunter2-runtime-secret"
        platform = synthetic_platform(
            tmp_path, instance, manifest, verification, secrets=(secret,)
        )
        platform.runtime.probe_body_extra = {"token": secret}

        with refusal(RestartFailed, SecretLeakRefused.__name__) as caught:
            platform.restart()

        diagnostics = [*caught.value.errors, str(caught.value)]
        assert any(
            "operational secret" in entry for entry in diagnostics
        ), "the refusal states what it refused: secret material in a claim"
        assert all(secret not in entry for entry in diagnostics)
        state_text = Path(platform.deployment.state_path).read_text(encoding="utf-8")
        journal_text = Path(platform.deployment.events_path).read_text(encoding="utf-8")
        assert secret not in state_text
        assert secret not in journal_text
        record = platform.persisted()
        assert (record.running, record.ready, record.deployed) == (False, False, False)
        assert record.restarts[-1].outcome == RESTART_FAILED
        assert record.restarts[-1].failure_phase == "health_check"
        assert platform.runtime.live == {}, "nothing was left running"
        assert platform.event_names()[-1] == EVENT_RESTART_FAILED

    def test_the_record_holds_operational_metadata_only(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 14 / §6 — restart history is identity, order and outcome."""
        platform.restart()
        document = json.loads(
            Path(platform.deployment.state_path).read_text(encoding="utf-8")
        )
        block = document["restarts"]
        assert block["attempt_count"] == 1
        assert block["outcome"] == RESTART_COMPLETED
        assert block["restarted"] is True
        assert len(block["attempts"]) == 1
        attempt = block["attempts"][0]
        assert set(attempt) == {
            "restart_id",
            "sequence",
            "outcome",
            "requested_at",
            "completed_at",
            "reason",
            "identity",
            "phases",
            "failure_phase",
            "failure_reason",
            "errors",
        }
        assert [phase["name"] for phase in attempt["phases"]] == list(RESTART_PHASES)
        assert set(attempt["identity"]) == {
            "platform_id",
            "instance_digest",
            "manifest_id",
            "manifest_version",
            "manifest_digest",
            "manifest_state",
            "components",
        }
        assert set(attempt["identity"]["components"][0]) == {
            "component_id",
            "component_version",
            "artifact_type",
            "artifact_digest",
            "execution_kind",
            "bound_modules",
        }
        assert platform.persisted() == platform.deployment.record


# ---------------------------------------------------------------------------
# AC 9 / AC 12 — desired state is untouched; drift is neither repaired nor hidden
# ---------------------------------------------------------------------------


def _diverged_owner(platform: SyntheticPlatform) -> _OwnerSource:
    """An owner surface that reports another component version: proven drift."""

    def build(binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        snapshot = _snapshot(platform.instance, platform.manifest, binding)
        return replace(
            snapshot,
            components=tuple(
                replace(entry, component_version="9.9.9")
                for entry in snapshot.components
            ),
        )

    return _OwnerSource(build)


def _drift(platform: SyntheticPlatform) -> DeploymentRecord:
    """Bring the record into a proven drift — through reconciliation itself."""
    with pytest.raises(ReconciliationDriftDetected):
        reconcile(
            ReconciliationRequest(
                deployment=platform.deployment,
                reconciliation_id=derive_reconciliation_id(platform.deployment),
            ),
            identity_provider=_provider(_diverged_owner(platform)),
        )
    record = platform.persisted()
    assert record.drifted is True
    return record


class TestDesiredStateAndDrift:
    """A restart re-executes the runtime; it changes and repairs nothing else."""

    def test_a_restart_changes_no_desired_state(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 11 — the instance, the Manifest and the record stand still."""
        instance_before = copy.deepcopy(platform.instance.document)
        manifest_before = copy.deepcopy(dict(platform.manifest))
        verification_before = platform.verification
        pinned = _pinned_identity(platform)
        spec_bytes = platform.spec.read_bytes()
        workspace = _digests(platform.workspace)
        source = _digests(platform.source)
        operations = _digests(platform.operations)

        result = platform.restart()

        assert result.completed is True
        assert platform.instance.document == instance_before
        assert dict(platform.manifest) == manifest_before
        assert platform.deployment.verification == verification_before
        assert _pinned_identity(platform) == pinned
        assert platform.spec.read_bytes() == spec_bytes
        assert _digests(platform.workspace) == workspace
        assert _digests(platform.source) == source
        written = {
            name
            for name, digest in _digests(platform.operations).items()
            if operations.get(name) != digest
        }
        assert written == {
            Path(platform.deployment.state_path).name,
            Path(platform.deployment.events_path).name,
        }, "a restart writes its own operation's state and journal, nothing else"

    def test_a_restart_creates_no_second_deployment_operation(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 8 — one operation, one attempt, one history of stages."""
        before = platform.persisted()

        result = platform.restart()

        assert result.completed is True
        record = platform.persisted()
        assert record.deployment_id == before.deployment_id
        assert record.attempt == before.attempt == 1
        assert record.environment_id == before.environment_id
        assert record.created_at == before.created_at
        assert record.stages == before.stages
        assert record.migrations == before.migrations
        assert record.lifecycle == LIFECYCLE_REALIZED
        assert record.failure is None
        assert [entry.component_id for entry in record.components] == [
            entry.component_id for entry in before.components
        ]
        assert len(list(platform.operations.glob("*.json"))) == 1

    def test_a_restart_repairs_no_drift_and_hides_none(
        self, platform: SyntheticPlatform
    ) -> None:
        """Adversarial 10 — drift survives a restart exactly as it was proven."""
        drifted = _drift(platform)
        history = drifted.reconciliations
        assert history and history[-1].outcome == RECONCILIATION_DRIFT

        result = platform.restart()

        assert result.completed is True
        record = platform.persisted()
        assert record.drifted is True, "a restart neither repairs nor hides drift"
        assert record.in_correspondence is False
        assert record.reconciliations == history
        assert record.restarts[-1].outcome == RESTART_COMPLETED
        assert record.lifecycle == LIFECYCLE_REALIZED
        assert record.instance_digest == drifted.instance_digest
        assert [entry.component_version for entry in record.components] == [
            entry.component_version for entry in drifted.components
        ], "the restart re-executed the pinned version, not the observed one"
        assert not [
            event
            for event in platform.events()
            if event["event"].startswith("reconciliation")
            and event["detail"].get("restart_sequence")
        ], "the restart published no reconciliation observation of its own"

    def test_a_restart_writes_no_reconciliation_observation(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 12 — correspondence is reconciliation's question, not this one's."""
        result = platform.restart()

        assert result.completed is True
        record = platform.persisted()
        assert record.reconciliations == ()
        assert record.drifted is False
        assert (
            record.in_correspondence is None
        ), "correspondence is a reconciliation fact; a restart states none"
        assert platform.event_names() == list(CYCLE_EVENTS)
        assert "reconcile" not in _called_names(RESTART_PATH)

    def test_a_drifted_platform_is_restarted_as_the_instance_it_pins(
        self, platform: SyntheticPlatform
    ) -> None:
        """AC 1 / AC 12 — drift does not authorize a different desired state."""
        _drift(platform)
        pinned = _pinned_identity(platform)

        result = platform.restart()

        assert result.completed is True
        assert _pinned_identity(platform) == pinned
        attempt = platform.attempt()
        assert attempt.identity["instance_digest"] == pinned["instance_digest"]
        assert attempt.identity["components"][0]["component_version"] == (
            COMPONENT_VERSION
        )
        assert platform.owner.bindings[-1].token.endswith("#restart:1")


# ---------------------------------------------------------------------------
# The state vocabulary of the slice
# ---------------------------------------------------------------------------


class TestRestartStateVocabulary:
    """Phases, outcomes and history are a fixed, append-only vocabulary."""

    def test_the_vocabulary_is_fixed(self) -> None:
        assert RESTART_PHASES == (
            "requested",
            "stop",
            "execution_verification",
            "start",
            "health_check",
            "identity_verification",
            "completed",
        )
        assert RESTART_OUTCOMES == (RESTART_COMPLETED, RESTART_FAILED)
        assert (RESTART_COMPLETED, RESTART_FAILED) == ("restarted", "failed")
        assert RestartPhaseRecord(name="stop").status == STAGE_PENDING
        assert STAGE_PENDING not in ("in_progress", STAGE_COMPLETED, STAGE_FAILED)

    def test_an_unknown_outcome_or_phase_is_refused_by_the_state(
        self, platform: SyntheticPlatform
    ) -> None:
        attempt = RestartRecord(
            restart_id="restart:x", sequence=1, outcome="half-done", requested_at=AT
        )
        with pytest.raises(DeploymentStateError, match="is not one of"):
            platform.record.with_restart(attempt, at=AT)

        invented = replace(
            attempt,
            outcome=RESTART_COMPLETED,
            phases=(RestartPhaseRecord(name="teleported"),),
        )
        with pytest.raises(DeploymentStateError, match="are not phases"):
            platform.record.with_restart(invented, at=AT)

    def test_the_history_is_append_only_and_numbered_by_the_record(
        self, platform: SyntheticPlatform
    ) -> None:
        first = RestartRecord(
            restart_id="restart:first",
            sequence=99,
            outcome=RESTART_FAILED,
            requested_at=AT,
            completed_at=AT,
            failure_phase="stop",
        )
        record = platform.record.with_restart(first, at=AT)
        assert [entry.sequence for entry in record.restarts] == [
            1
        ], "the record numbers the attempt; a caller cannot invent a sequence"
        assert record.last_restart is record.restarts[-1]
        assert record.restarted is False

        second = replace(first, restart_id="restart:second", outcome=RESTART_COMPLETED)
        record = record.with_restart(second, at=AT)
        assert [entry.sequence for entry in record.restarts] == [1, 2]
        assert [entry.outcome for entry in record.restarts] == [
            RESTART_FAILED,
            RESTART_COMPLETED,
        ]
        assert record.restarted is True
        assert record.restarts[0].sequence == 1, "an earlier attempt is never rewritten"
        assert record.lifecycle == platform.record.lifecycle
        assert record.reconciliations == platform.record.reconciliations

    def test_a_completed_attempt_is_not_a_failed_one(
        self, platform: SyntheticPlatform
    ) -> None:
        attempt = RestartRecord(
            restart_id="restart:x",
            sequence=1,
            outcome=RESTART_COMPLETED,
            requested_at=AT,
        )
        assert attempt.completed is True and attempt.failed is False
        assert replace(attempt, outcome=RESTART_FAILED).failed is True
        assert replace(attempt, outcome=RESTART_FAILED).completed is False
        assert attempt.phase("stop") is None
        assert attempt.phase_order() == ()


# ---------------------------------------------------------------------------
# The same operation against a platform this repository really ran
# ---------------------------------------------------------------------------


class _RunningPlatformOwner:
    """Owner-side surface of a Running Platform realized from ``instance``.

    It reports the platform that is actually running — the same composition the
    deployment realized — and can be told to report a divergence, so the slice
    is exercised against a real deployment rather than a hand-written record.
    """

    def __init__(
        self,
        instance: Any,
        manifest: Mapping[str, Any],
        *,
        component_version: str | None = None,
    ) -> None:
        self.instance = instance
        self.manifest = manifest
        self.component_version = component_version

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        components = []
        for component in self.manifest.get("components", []):
            artifact = component.get("artifact")
            components.append(
                ActualComponentIdentity(
                    component["component_id"],
                    self.component_version or component["component_version"],
                    artifact if isinstance(artifact, Mapping) else None,
                )
            )
        configuration = self.instance.document.get("configuration")
        return ActualPlatformSnapshot(
            platform_id=self.instance.document["platform_id"],
            manifest={
                "manifest_id": self.manifest["manifest_id"],
                "manifest_version": self.manifest["manifest_version"],
                "manifest_digest": self.manifest["manifest_digest"],
            },
            manifest_state=self.manifest["lifecycle"]["state"],
            components=tuple(components),
            membership_established=True,
            configuration=(
                IdentityField.present(configuration)
                if configuration is not None
                else IdentityField.absent()
            ),
            golden_bundle=IdentityField.absent(),
            golden_bundle_inventory_established=True,
            extensions=IdentityField.absent(),
            branding=IdentityField.absent(),
            provenance=EvidenceProvenance.MEASURED,
            correlation=producer_correlation(
                binding, target=str(self.instance.document["platform_id"])
            ),
            freshness_current=True,
        )


#: The entrypoint name a bound environment gives to the copied fixture content.
BOUND_MODULE = "bound_component"


def bound_environment_for(
    tmp_path: Path,
    fixture: str,
    *,
    probe_timeout_seconds: float | None = None,
) -> tuple[Path, Any]:
    """An environment whose component is bound to a copy of one fixture module.

    The copy — ``bound_component.py`` under a test-owned source root — is what
    an adversarial test may substitute: repository content is never edited. The
    binding names it as the component's own deployment entrypoint, exactly as a
    real environment would, and the engine binds the rest of the component's
    package from the repository's own verified source root.
    """
    source = tmp_path / "verified-source"
    source.mkdir(parents=True, exist_ok=True)
    (source / f"{BOUND_MODULE}.py").write_bytes(
        (FIXTURE_PATH / f"{fixture}.py").read_bytes()
    )
    overrides: dict[str, Any] = {
        "bindings": (
            ComponentRuntimeBinding(
                component_id=COMPONENT_ID,
                deployment_module=BOUND_MODULE,
                deployment_factory="build_component",
                import_paths=(source,),
            ),
        )
    }
    if probe_timeout_seconds is not None:
        overrides["probe_timeout_seconds"] = probe_timeout_seconds
    return source, replace(environment_for(tmp_path / "runtime"), **overrides)


@dataclass
class SubstitutingRuntime:
    """The real adapter, with the bound content replaced at a chosen moment.

    This is the substituted execution path §10 is about: the deployment bound
    content A and the process that would run is content B — replaced after the
    engine verified it and immediately before the launch. Only content this test
    owns is rewritten.
    """

    inner: Any
    substitute: bytes
    when: str = "before-start"
    within: Path = Path("/")
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _rewrite(self, element: RuntimeElement, at: str) -> None:
        self.calls.append((at, element.component.component_id))
        if at != self.when:
            return
        execution = element.execution
        assert execution is not None
        for module in execution.modules:
            path = module.path.resolve()
            if self.within.resolve() in path.parents:
                path.write_bytes(self.substitute)

    def materialize(self, element: RuntimeElement) -> Any:
        return self.inner.materialize(element)

    def migrate(self, element: RuntimeElement) -> Any:
        return self.inner.migrate(element)

    def start(self, element: RuntimeElement) -> RuntimeHandle:
        self._rewrite(element, "before-start")
        return self.inner.start(element)

    def request(
        self, handle: RuntimeHandle, operation: str, *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        self._rewrite(handle.element, f"on-{operation}")
        return self.inner.request(handle, operation, timeout=timeout)

    def stop(self, handle: RuntimeHandle) -> Mapping[str, Any]:
        self._rewrite(handle.element, "on-stop")
        return self.inner.stop(handle)


def _deploy(
    tmp_path: Path,
    instance: Any,
    manifest: Mapping[str, Any],
    *,
    environment: Any = None,
    runtime: Any = None,
    owner: _RunningPlatformOwner | None = None,
) -> Deployment:
    """Realize the real instance through the real Factory surfaces and runtime."""
    bound_environment = (
        environment
        if environment is not None
        else environment_for(tmp_path / "runtime")
    )
    source = owner if owner is not None else _RunningPlatformOwner(instance, manifest)
    return deploy(
        request_for(instance, manifest, bound_environment),
        runtime=runtime,
        identity_provider=OwnerSuppliedPlatformIdentityProvider(source),
    )


def _restart(
    deployment: Deployment,
    instance: Any,
    manifest: Mapping[str, Any],
    *,
    runtime: Any = None,
    owner: _RunningPlatformOwner | None = None,
) -> Any:
    """One explicit restart of a real deployment, through the public API."""
    source = owner if owner is not None else _RunningPlatformOwner(instance, manifest)
    return restart(
        RestartRequest(deployment=deployment, restart_id=derive_restart_id(deployment)),
        runtime=runtime,
        identity_provider=OwnerSuppliedPlatformIdentityProvider(source),
    )


def _identity_of(record: DeploymentRecord) -> tuple[Any, ...]:
    """The pinned identity, version, artifact and target of a record."""
    return (
        record.platform_id,
        record.instance_digest,
        record.manifest_id,
        record.manifest_version,
        record.manifest_digest,
        record.manifest_state,
        record.environment_id,
        record.deployment_id,
        tuple(
            (
                entry.component_id,
                entry.component_version,
                entry.artifact_type,
                entry.artifact_digest,
            )
            for entry in record.components
        ),
    )


class TestRestartAgainstARealDeployment:
    """A real platform, really stopped, really started, really verified."""

    def test_a_real_runtime_restarts_and_preserves_its_identity(
        self, tmp_path: Path, instance: Any, manifest: Mapping[str, Any]
    ) -> None:
        """AC 1 / AC 18 — the same instance runs again, in a fresh process."""
        deployment = _deploy(tmp_path, instance, manifest)
        try:
            assert deployment.deployed is True
            before = deployment.record
            identity = _identity_of(before)
            handle = deployment._handles[0]
            vanished = handle.process.pid
            spec_bytes = handle.element.spec_path.read_bytes()
            bound = _digests(handle.element.execution.root)
            published = len(deployment.events())

            result = _restart(deployment, instance, manifest)

            assert result.completed is True
            assert result.outcome == RESTART_COMPLETED
            record = deployment.record
            fresh = deployment._handles[0]
            assert fresh is not handle
            assert fresh.process.pid != vanished
            assert handle.process.poll() is not None, "the stopped process is gone"
            assert fresh.process.poll() is None, "a real runtime process runs again"
            assert _identity_of(record) == identity, "identity, version, digest, target"
            assert record.deployed is True
            assert record.restarted is True
            assert record.lifecycle == LIFECYCLE_REALIZED
            entry = record.components[0]
            assert entry.observed_component_id == COMPONENT_ID
            assert entry.observed_version == COMPONENT_VERSION
            assert entry.observed_platform_id == PLATFORM_ID
            assert entry.healthy is True
            assert entry.runtime_started is True
            assert entry.execution == before.components[0].execution
            attempt = result.attempt
            assert attempt.sequence == 1
            assert attempt.phase_order() == RESTART_PHASES
            assert attempt.identity["instance_digest"] == record.instance_digest
            assert attempt.identity["components"][0]["component_version"] == (
                COMPONENT_VERSION
            )
            assert attempt.identity["components"][0]["bound_modules"] == {
                module.module: module.digest
                for module in fresh.element.execution.modules
            }
            assert fresh.element.spec_path.read_bytes() == spec_bytes
            assert _digests(fresh.element.execution.root) == bound
            names = [event.event for event in deployment.events()][published:]
            assert names == list(CYCLE_EVENTS)
            assert [action.name for action in record.operational_actions] == [
                "platform_stopped",
                "platform_restarted",
            ]
            assert DeploymentStateStore(deployment.state_path).read() == record
        finally:
            deployment.stop()

    def test_a_real_restart_of_a_stopped_platform_starts_it_again(
        self, tmp_path: Path, instance: Any, manifest: Mapping[str, Any]
    ) -> None:
        """AC 7 / AC 8 — a plain stop left it down; a restart brings it back."""
        deployment = _deploy(tmp_path, instance, manifest)
        try:
            stopped = deployment.stop()
            assert (stopped.running, stopped.ready, stopped.deployed) == (
                False,
                False,
                False,
            )
            published = len(deployment.events())

            result = _restart(deployment, instance, manifest)

            assert result.completed is True
            record = deployment.record
            assert (record.running, record.ready, record.deployed) == (
                True,
                True,
                True,
            )
            assert deployment._handles[0].process.poll() is None
            assert result.attempt.phase("stop").detail["already_stopped"] is True
            names = [event.event for event in deployment.events()][published:]
            assert names == [
                name for name in CYCLE_EVENTS if name != EVENT_PLATFORM_STOPPED
            ]
            assert [action.name for action in record.operational_actions] == [
                "platform_stopped",
                "platform_restarted",
            ]
        finally:
            deployment.stop()

    def test_a_real_restart_after_the_process_vanished_runs_a_fresh_one(
        self, tmp_path: Path, instance: Any, manifest: Mapping[str, Any]
    ) -> None:
        """Adversarial 13 — the element that disappeared is replaced, not reused."""
        deployment = _deploy(tmp_path, instance, manifest)
        try:
            handle = deployment._handles[0]
            vanished = handle.process.pid
            handle.process.kill()
            handle.process.wait(timeout=30)
            assert handle.process.poll() is not None
            assert (
                deployment.record.running is True
            ), "the record still claims the platform the deployment realized"

            result = _restart(deployment, instance, manifest)

            assert result.completed is True
            fresh = deployment._handles[0]
            assert fresh is not handle
            assert fresh.process.pid != vanished
            assert fresh.process.poll() is None
            record = deployment.record
            assert (record.running, record.ready, record.deployed) == (
                True,
                True,
                True,
            )
            assert record.components[0].observed_version == COMPONENT_VERSION
            assert result.attempt.phase("stop").detail["answers"][COMPONENT_ID] == {
                "status": "stopped"
            }
        finally:
            deployment.stop()

    def test_a_real_health_deadline_fails_the_restart(
        self, tmp_path: Path, instance: Any, manifest: Mapping[str, Any]
    ) -> None:
        """Adversarial 8 — a real process that does not answer in time."""
        source, environment = bound_environment_for(
            tmp_path, "hanging_probe_component", probe_timeout_seconds=5.0
        )
        deployment = _deploy(tmp_path, instance, manifest, environment=environment)
        try:
            assert deployment.deployed is True
            handle = deployment._handles[0]
            bound = _digests(source)
            marker = handle.element.workspace / "restart-hang.marker"
            marker.write_text("8\n", encoding="utf-8")

            with refusal(
                RestartFailed, r"did not answer the 'probe' request within"
            ) as caught:
                _restart(deployment, instance, manifest)

            assert caught.value.phase == "health_check"
            record = deployment.record
            assert (record.running, record.ready, record.deployed) == (
                False,
                False,
                False,
            )
            attempt = record.restarts[-1]
            assert attempt.outcome == RESTART_FAILED
            assert attempt.failure_phase == "health_check"
            assert attempt.phase_order() == (
                "requested",
                "stop",
                "execution_verification",
                "start",
                "health_check",
            )
            assert handle.process.poll() is not None, "the pre-restart process is gone"
            assert all(
                element.process.poll() is not None for element in deployment._handles
            ), "the attempt left no uncontrolled runtime element behind"
            assert (
                _digests(source) == bound
            ), "the attempt re-executed the bound content and changed none"
        finally:
            deployment.stop()

    def test_content_substituted_immediately_before_execution_is_refused(
        self, tmp_path: Path, instance: Any, manifest: Mapping[str, Any]
    ) -> None:
        """Adversarial 17 — the launch verification refuses the substitution."""
        source, environment = bound_environment_for(tmp_path, "migrating_component")
        deployment = _deploy(tmp_path, instance, manifest, environment=environment)
        try:
            assert deployment.deployed is True
            handle = deployment._handles[0]
            workspace = handle.element.workspace
            pinned = _digests(source)
            # The substitution happens on the restart's launch and not before:
            # the platform that runs now is the platform this deployment bound.
            runtime = SubstitutingRuntime(
                inner=LocalProcessRuntime(),
                substitute=SUBSTITUTED_CONTENT,
                when="before-start",
                within=source,
            )

            with refusal(
                RestartFailed,
                r"the content this deployment verified is not the content",
            ) as caught:
                _restart(deployment, instance, manifest, runtime=runtime)

            assert caught.value.phase == "start"
            assert any(
                "the content a claim would rest on" in entry
                for entry in caught.value.errors
            ), "the digest divergence itself is part of the diagnosis"
            record = deployment.record
            assert (record.running, record.ready, record.deployed) == (
                False,
                False,
                False,
            )
            assert record.restarts[-1].failure_phase == "start"
            assert handle.process.poll() is not None
            assert all(
                element.process.poll() is not None for element in deployment._handles
            )
            assert not (
                workspace / SUBSTITUTION_MARKER
            ).exists(), (
                "the substituted content never ran: no process was started from it"
            )
            assert ("before-start", COMPONENT_ID) in runtime.calls
            assert _digests(source) != pinned, "the test really did substitute content"
        finally:
            deployment.stop()

    def test_content_changed_between_the_run_and_the_restart_is_refused(
        self, tmp_path: Path, instance: Any, manifest: Mapping[str, Any]
    ) -> None:
        """Adversarial 1 — a refusal before teardown: the platform keeps running."""
        source, environment = bound_environment_for(tmp_path, "migrating_component")
        deployment = _deploy(tmp_path, instance, manifest, environment=environment)
        try:
            assert deployment.deployed is True
            before = deployment.record
            handle = deployment._handles[0]
            published = len(deployment.events())
            (source / f"{BOUND_MODULE}.py").write_bytes(SUBSTITUTED_CONTENT)

            with refusal(DeploymentInputRejected, r"the content a claim would rest on"):
                _restart(deployment, instance, manifest)

            assert deployment.record == before, "a refusal changed no state"
            assert deployment.deployed is True
            assert handle.process.poll() is None, "the platform was left running"
            assert DeploymentStateStore(deployment.state_path).read() == before
            assert len(deployment.events()) == published, "and published nothing"
            assert not (
                handle.element.workspace / SUBSTITUTION_MARKER
            ).exists(), "the substituted content never ran"
        finally:
            deployment.stop()

    def test_a_real_restart_of_a_drifted_platform_keeps_the_drift(
        self, tmp_path: Path, instance: Any, manifest: Mapping[str, Any]
    ) -> None:
        """Adversarial 10 — drift is an observation a restart neither fixes nor hides."""
        deployment = _deploy(tmp_path, instance, manifest)
        try:
            with pytest.raises(ReconciliationDriftDetected):
                reconcile(
                    ReconciliationRequest(
                        deployment=deployment,
                        reconciliation_id=derive_reconciliation_id(deployment),
                    ),
                    identity_provider=OwnerSuppliedPlatformIdentityProvider(
                        _RunningPlatformOwner(
                            instance, manifest, component_version="9.9.9"
                        )
                    ),
                )
            drifted = deployment.record
            assert drifted.drifted is True
            history = drifted.reconciliations
            assert history[-1].outcome == RECONCILIATION_DRIFT

            result = _restart(deployment, instance, manifest)

            assert result.completed is True
            record = deployment.record
            assert record.drifted is True
            assert record.in_correspondence is False
            assert record.reconciliations == history
            assert record.restarts[-1].outcome == RESTART_COMPLETED
            assert _identity_of(record) == _identity_of(drifted)
            assert [entry.component_version for entry in record.components] == [
                COMPONENT_VERSION
            ]
            assert record.deployed is True
        finally:
            deployment.stop()
