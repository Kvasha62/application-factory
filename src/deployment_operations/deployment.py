"""The deployment operation — one accepted Platform Instance, realized.

This is the first vertical slice of Deployment & Operations (ADR-0017 §42): one
deployment operation for one accepted Platform Instance, traversing the
capability end to end at the minimum needed to prove the boundary of ADR-0016
in action:

    requested → validated → provisioning → deploying → migrations
        → starting → health_check → ready
        → identity/version/digest verification
        → realized / deployed

The path is the minimal **observable** sequence fixed by ADR-0017 §36; it is not
a new global lifecycle, and the internal form above is this slice's engineering
form of that observable behavior, not additional architecture (§36).

What the operation refuses to do is as much of the slice as what it does:

* it never acts on a name-only reference — a deployment is a concrete Platform
  Instance identified by identity, version and digest (ADR-0016 §8);
* it never proceeds on an instance that fails its own validity checks (§20);
* it never substitutes a version, an artifact or a digest, and never edits the
  Manifest or the instance (§7);
* it never fixes ``realized``/``deployed`` on the strength of a started process
  or a passing health check alone: both the verified operational condition and
  the exact identity/version/digest verification must hold at the same time
  (ADR-0017 §34);
* it never leaves deployment state dishonest: a failure at any stage is
  recorded at the stage where it happened, and no claim survives it (§9, §20);
* it owns no business data: migrations are component-owned code executed from
  the component's own deployment, stores stay with their components, and no
  component database is ever opened here (§6, §14; LAW-03, LAW-04).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn, Self

from deployment_operations.environment import (
    DeploymentEnvironment,
    validate_environment,
)
from deployment_operations.errors import (
    DeploymentExecutionFailed,
    DeploymentInputRejected,
    DeploymentOperationsError,
    DeploymentStateError,
    HealthCheckFailed,
    IdentityVerificationFailed,
    InvalidDeploymentStateTransition,
    MigrationOrchestrationFailed,
    ProvisioningFailed,
    StartupFailed,
)
from deployment_operations.events import (
    EVENT_COMPONENT_MATERIALIZED,
    EVENT_DEPLOYMENT_FAILED,
    EVENT_DEPLOYMENT_REALIZED,
    EVENT_DEPLOYMENT_REQUESTED,
    EVENT_HEALTH_CHECKED,
    EVENT_IDENTITY_VERIFIED,
    EVENT_MIGRATION_COMPLETED,
    EVENT_MIGRATION_FAILED,
    EVENT_MIGRATION_STARTED,
    EVENT_PLATFORM_ATTACHED,
    EVENT_PLATFORM_STOPPED,
    EVENT_READY_REACHED,
    EVENT_RUNTIME_STARTED,
    EVENT_STAGE_COMPLETED,
    EVENT_STAGE_STARTED,
    DeploymentEvent,
    EventJournal,
)
from deployment_operations.health import (
    ComponentObservation,
    evaluate_health,
    verify_identity,
)
from deployment_operations.platform_identity import (
    ActualIdentityUnavailable,
    IdentityCorrespondenceResult,
    PlatformIdentityBinding,
    PlatformIdentityProvider,
    establish_identity_correspondence,
)
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from deployment_operations.provisioning import provision
from deployment_operations.runtime import (
    OP_PROBE,
    OP_START,
    ExecutionBinding,
    LocalProcessRuntime,
    MaterializedComponent,
    RuntimeAdapter,
    RuntimeElement,
    RuntimeHandle,
    RuntimeProcessError,
    bind_execution,
    build_elements,
    verify_bound_content,
)
from deployment_operations.state import (
    LIFECYCLE_REALIZED,
    STAGE_COMPLETED,
    STAGE_IN_PROGRESS,
    ComponentRecord,
    DeploymentRecord,
    DeploymentStateStore,
    MigrationRecord,
    derive_deployment_id,
    utc_now,
)
from deployment_operations.verification import (
    ComponentBinding,
    InstanceVerification,
    verify_input_unchanged,
    verify_instance,
)
from running_platform.owner_state import (
    FileRunningPlatformOwnerStateReader,
    OwnerStateSnapshotSource,
)

Clock = Callable[[], str]
_MISSING_DIGEST = "the deployment request does not pin a concrete instance digest"


def _pin(component: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Flatten a component document into the operational correlation set (§19).

    Component documents carry the pinned artifact identity nested under
    ``artifact``; the journal records the pinned identity flat, so every
    component-scoped signal can be correlated with component, version and
    artifact digest without reading a second document.
    """
    if not isinstance(component, Mapping):
        return {}
    artifact = component.get("artifact")
    pinned: Mapping[str, Any] = artifact if isinstance(artifact, Mapping) else {}
    return {
        "component_id": component.get("component_id"),
        "component_version": component.get("component_version"),
        "artifact_type": pinned.get("artifact_type"),
        "artifact_digest": pinned.get("digest"),
    }


def _requested_manifest(document: object) -> Mapping[str, Any]:
    """The manifest identity the operation was requested for (never a verdict).

    What is recorded here is what the request named; the authoritative
    verification replaces it with the verified identity once the input has been
    verified (§15).
    """
    if not isinstance(document, Mapping):
        return {}
    lifecycle = document.get("lifecycle")
    state: Mapping[str, Any] = lifecycle if isinstance(lifecycle, Mapping) else {}
    fields = {
        "manifest_id": document.get("manifest_id"),
        "manifest_version": document.get("manifest_version"),
        "manifest_digest": document.get("manifest_digest"),
        "manifest_state": state.get("state"),
    }
    return {
        name: value if isinstance(value, str) else None
        for name, value in fields.items()
    }


def default_source_paths() -> tuple[Path, ...]:
    """Import paths of the runtime processes: this repository's ``src``."""
    return (Path(__file__).resolve().parent.parent,)


def _verify_running_platform_identity(
    provider: PlatformIdentityProvider,
    expected_instance: Mapping[str, Any],
    *,
    binding_token: object,
) -> None:
    """Cross the owner-side identity adapter boundary exactly once.

    The provider receives only an opaque evaluation binding. The expected
    Platform Instance remains on the D&O side and is used only after actual
    evidence has been independently obtained. RuntimeAdapter is deliberately
    absent from this function: runtime control and Running Platform identity
    evidence are separate boundaries.
    """
    binding = PlatformIdentityBinding(binding_token)
    try:
        evidence = provider.observe_identity(binding)
        result = establish_identity_correspondence(expected_instance, evidence)
    except ActualIdentityUnavailable as error:
        raise IdentityVerificationFailed(
            [f"running platform identity evidence is unavailable: {error}"]
        ) from error
    except Exception as error:
        raise IdentityVerificationFailed(
            [f"running platform identity provider failed closed: {error}"]
        ) from error

    if result is IdentityCorrespondenceResult.MATCH:
        return
    if result is IdentityCorrespondenceResult.MISMATCH:
        raise IdentityVerificationFailed(
            ["running platform identity does not match the requested instance"]
        )
    raise IdentityVerificationFailed(
        ["running platform identity evidence is unavailable"]
    )


@dataclass(frozen=True)
class InstanceReference:
    """The exact Platform Instance a deployment is requested for (§8).

    ``instance_digest`` is mandatory. A reference that carries only a
    human-readable platform name is not a deployment input: there is no
    «deploy the platform called X» — only «deploy this exact instance».
    """

    instance_digest: str = ""
    platform_id: str | None = None

    @classmethod
    def from_document(cls, document: object) -> InstanceReference:
        """Build a reference from an instance document's own identity.

        Raises :class:`DeploymentInputRejected` when the document carries no
        instance digest: an unnamed instance cannot be deployed.
        """
        if not isinstance(document, Mapping):
            raise DeploymentInputRejected(
                ["$: a Platform Instance document is required"]
            )
        digest = document.get("instance_digest")
        if not isinstance(digest, str) or not digest.strip():
            message = (
                "$.instance_digest: a concrete instance digest is required; "
                "a name-only reference cannot be deployed"
            )
            raise DeploymentInputRejected([message])
        platform_id = document.get("platform_id")
        return cls(
            instance_digest=digest,
            platform_id=platform_id if isinstance(platform_id, str) else None,
        )

    def errors(self) -> list[str]:
        errors: list[str] = []
        if (
            not isinstance(self.instance_digest, str)
            or not self.instance_digest.strip()
        ):
            errors.append(_MISSING_DIGEST)
        if self.platform_id is not None and not self.platform_id.strip():
            errors.append(
                "$.platform_id: a declared platform identity must be non-empty"
            )
        return errors


@dataclass(frozen=True)
class DeploymentRequest:
    """Everything one deployment operation is requested with."""

    instance: InstanceReference
    instance_document: Mapping[str, Any]
    manifest_document: Mapping[str, Any]
    environment: DeploymentEnvironment
    attempt: int = 1
    factory_root: Path | None = None


@dataclass
class Deployment:
    """One completed deployment operation and its running platform.

    The operation is fail-closed: a failure raises instead of returning, and
    the failed record stays readable in deployment state. A successful
    operation returns this handle — the realized platform and the record that
    states exactly what is running, pinned to which instance and digest.
    """

    record: DeploymentRecord
    verification: InstanceVerification
    state_path: Path
    events_path: Path
    _runtime: RuntimeAdapter | None = field(repr=False, default=None)
    _handles: tuple[RuntimeHandle, ...] = field(repr=False, default=())
    _journal: EventJournal | None = field(repr=False, default=None)
    _store: DeploymentStateStore | None = field(repr=False, default=None)
    _secrets: tuple[str, ...] = field(repr=False, default=())
    _clock: Clock = field(repr=False, default=utc_now)

    @property
    def deployed(self) -> bool:
        """True only for an honest, verified ``deployed`` claim (§10)."""
        return self.record.deployed

    def events(self) -> tuple[DeploymentEvent, ...]:
        """The operational signals of this operation, in order (§19)."""
        return self._journal.events() if self._journal is not None else ()

    def stop(self) -> DeploymentRecord:
        """Stop the platform's runtime elements (ADR-0016 §18).

        An operational action, not a lifecycle change: the record keeps saying
        what this operation did (``realized``, with the identity/version/digest
        verification it performed) and stops claiming a Running Platform where
        there is none — nothing is running, the verified operational condition
        no longer holds, and so no ``deployed`` claim stands (§10, §33).

        A plain stop starts nothing again, and no failure triggers an automatic
        restart: re-executing a runtime is the separate, explicit operation of
        :mod:`deployment_operations.restart`, under the stop/start policy stated
        there, and drift handling is the separate observational operation of
        :mod:`deployment_operations.reconciliation` (ADR-0016 §18, §20;
        ADR-0017 §39). Stopping twice is idempotent and records nothing twice.

        Fail-closed when this operation is bound to no runtime element: an
        operation that holds no handle reaches no runtime, so it cannot stop
        one, and deployment state is **not** moved to ``stopped`` on the
        strength of an action that never happened (§9, §18, §20). A deployment
        operation whose process was replaced re-binds its runtime element
        through :func:`attach` first; the runtime's own owner stops the runtime.
        """
        at = self._clock()
        if self.record.running and not self._handles:
            raise DeploymentStateError(
                f"{self.record.deployment_id}: this deployment operation holds "
                "no runtime element, so it is bound to no Running Platform it "
                "could stop; deployment state is not moved to stopped by an "
                "operation that reaches no runtime (ADR-0016 §9, §18, §20). "
                "Re-bind the operation with deployment_operations.attach, or "
                "stop the runtime through its own owner."
            )
        for handle in self._handles:
            if self._runtime is not None:
                self._runtime.stop(handle)
        record = self.record.mark_stopped(at=at)
        if record is self.record:
            # Already stopped: stopping again changes nothing and records
            # nothing twice (§20).
            return record
        self.record = record
        if self._store is not None:
            self._store.write(record, secrets=self._secrets)
        if self._journal is not None:
            self._journal.append(
                self._event(EVENT_PLATFORM_STOPPED, at=at, stage="ready"),
                secrets=self._secrets,
            )
        return record

    def _event(
        self,
        name: str,
        *,
        at: str,
        stage: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> DeploymentEvent:
        return DeploymentEvent(
            sequence=0,
            event=name,
            occurred_at=at,
            deployment_id=self.record.deployment_id,
            environment_id=self.record.environment_id,
            platform_id=self.record.platform_id,
            instance_digest=self.record.instance_digest,
            manifest_id=self.record.manifest_id,
            manifest_version=self.record.manifest_version,
            manifest_digest=self.record.manifest_digest,
            stage=stage,
            detail=dict(detail) if detail else {},
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class _Recorder:
    """Writes deployment state and operational events as the path advances."""

    def __init__(
        self,
        *,
        store: DeploymentStateStore,
        journal: EventJournal,
        record: DeploymentRecord,
        secrets: Sequence[str],
        clock: Clock,
    ) -> None:
        self.store = store
        self.journal = journal
        self.record = record
        self.secrets = tuple(secrets)
        self.clock = clock

    # -- persistence -------------------------------------------------------
    def _write(self) -> None:
        self.store.write(self.record, secrets=self.secrets)

    def append(self, event: DeploymentEvent) -> None:
        sequenced = replace(event, sequence=self.journal.next_sequence())
        self.journal.append(sequenced, secrets=self.secrets)

    def event(
        self,
        name: str,
        *,
        stage: str | None = None,
        component: Mapping[str, Any] | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> DeploymentEvent:
        entry = _pin(component)
        return DeploymentEvent(
            sequence=0,
            event=name,
            occurred_at=self.clock(),
            deployment_id=self.record.deployment_id,
            environment_id=self.record.environment_id,
            platform_id=self.record.platform_id,
            instance_digest=self.record.instance_digest,
            manifest_id=self.record.manifest_id,
            manifest_version=self.record.manifest_version,
            manifest_digest=self.record.manifest_digest,
            stage=stage,
            component_id=entry.get("component_id"),
            component_version=entry.get("component_version"),
            artifact_type=entry.get("artifact_type"),
            artifact_digest=entry.get("artifact_digest"),
            detail=dict(detail) if detail else {},
        )

    # -- transitions -------------------------------------------------------
    def update(self, record: DeploymentRecord) -> None:
        self.record = record
        self._write()

    def stage(
        self,
        name: str,
        status: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.update(
            self.record.with_stage(name, status, at=self.clock(), detail=detail)
        )
        event = (
            EVENT_STAGE_STARTED
            if status == STAGE_IN_PROGRESS
            else EVENT_STAGE_COMPLETED
        )
        self.append(self.event(event, stage=name, detail=detail))

    def identity(self, verification: InstanceVerification) -> None:
        """Attach the verified instance identity to the record (§8)."""
        record = replace(
            self.record,
            platform_id=verification.platform_id or self.record.platform_id,
            manifest_id=verification.manifest_id,
            manifest_version=verification.manifest_version,
            manifest_digest=verification.manifest_digest,
            manifest_state=verification.manifest_state,
            instance_digest=verification.instance_digest,
            updated_at=self.clock(),
        )
        self.update(record)

    def bind_components(self, bindings: Sequence[Any]) -> None:
        records = tuple(
            ComponentRecord(
                component_id=binding.component_id,
                component_version=binding.component_version,
                artifact_type=binding.artifact_type,
                artifact_digest=binding.artifact_digest,
            )
            for binding in bindings
        )
        self.update(self.record.with_components(records, at=self.clock()))

    def components(self, changes: Mapping[str, Mapping[str, Any]]) -> None:
        at = self.clock()
        record = self.record
        for component_id, fields in changes.items():
            record = record.with_component(component_id, at=at, **dict(fields))
        self.update(record)

    def migration(self, migration: MigrationRecord) -> None:
        self.update(self.record.with_migration(migration, at=self.clock()))

    def ready(self) -> None:
        self.update(self.record.mark_ready(at=self.clock()))
        self.append(
            self.event(EVENT_READY_REACHED, stage="ready", detail={"ready": True})
        )

    def identity_verified(self) -> None:
        self.update(self.record.mark_identity_verified(at=self.clock()))
        self.append(
            self.event(
                EVENT_IDENTITY_VERIFIED,
                stage="ready",
                detail={
                    "instance_digest": self.record.instance_digest,
                    "components": _component_ids(self.record),
                    "executed_content": _executed_content(self.record),
                },
            )
        )

    def realized(self) -> None:
        self.update(self.record.mark_realized(at=self.clock()))
        self.append(
            self.event(
                EVENT_DEPLOYMENT_REALIZED,
                stage="ready",
                detail={"deployed": True, "ready": True, "identity_verified": True},
            )
        )

    def running(self, running: bool) -> None:
        self.update(self.record.mark_running(at=self.clock(), running=running))

    def fail(
        self,
        stage: str,
        reason: str,
        errors: Sequence[str],
        error: DeploymentOperationsError,
    ) -> NoReturn:
        """Record the failure honestly at its stage, then raise it (§20)."""
        self.update(
            self.record.mark_failed(
                stage=stage, reason=reason, errors=list(errors), at=self.clock()
            )
        )
        self.append(
            self.event(
                EVENT_DEPLOYMENT_FAILED,
                stage=stage,
                detail={"reason": reason, "errors": list(errors)},
            )
        )
        raise error


def _component_ids(record: DeploymentRecord) -> list[str]:
    return [component.component_id for component in record.components]


def _executed_content(record: DeploymentRecord) -> dict[str, dict[str, str]]:
    """The content this deployment bound and the content that reported running.

    Recorded beside the instance digest so the operation's claim can always be
    read back against concrete content, not only against an identity string.
    """
    content: dict[str, dict[str, str]] = {}
    for component in record.components:
        binding = component.execution
        if not isinstance(binding, Mapping):
            continue
        modules = binding.get("modules")
        entry = (
            modules[0]
            if isinstance(modules, Sequence)
            and modules
            and isinstance(modules[0], Mapping)
            else {}
        )
        observed = component.observed_execution
        observed_modules = (
            observed.get("modules") if isinstance(observed, Mapping) else None
        )
        observed_entry = (
            observed_modules[0]
            if isinstance(observed_modules, Sequence)
            and observed_modules
            and isinstance(observed_modules[0], Mapping)
            else {}
        )
        content[component.component_id] = {
            "bound_digest": str(entry.get("digest")),
            "observed_digest": str(observed_entry.get("digest")),
        }
    return content


def _precheck(request: DeploymentRequest) -> list[str]:
    """Refuse an unusable request before anything is touched (fail-closed)."""
    errors = list(request.instance.errors())
    if not isinstance(request.instance_document, Mapping):
        errors.append("$: a Platform Instance document is required")
    if not isinstance(request.manifest_document, Mapping):
        errors.append("$.manifest: the bound Platform Manifest document is required")
    if not isinstance(request.attempt, int) or request.attempt < 1:
        errors.append("$.attempt: attempts are counted from 1")

    reference = request.instance
    if isinstance(request.instance_document, Mapping):
        declared = request.instance_document.get("instance_digest")
        if (
            isinstance(declared, str)
            and reference.instance_digest
            and (declared != reference.instance_digest)
        ):
            errors.append(
                "$.instance_digest: the request pins "
                f"{reference.instance_digest!r}, but the supplied instance "
                f"document declares {declared!r}; a deployment realizes exactly "
                "the instance it was requested for"
            )
        platform_id = request.instance_document.get("platform_id")
        if (
            reference.platform_id
            and isinstance(platform_id, str)
            and (platform_id != reference.platform_id)
        ):
            errors.append(
                "$.platform_id: the request names "
                f"{reference.platform_id!r}, but the supplied instance document "
                f"is {platform_id!r}"
            )
    return errors


def deploy(
    request: DeploymentRequest,
    *,
    runtime: RuntimeAdapter | None = None,
    identity_provider: PlatformIdentityProvider | None = None,
    source_paths: Sequence[Path] | None = None,
    clock: Any = None,
) -> Deployment:
    """Realize one accepted Platform Instance in one environment.

    Raises the fail-closed error of the stage that failed; the failed
    deployment state and the event journal remain readable at the returned
    paths (``DeploymentStateStore.path_for`` / ``EventJournal.path_for`` with
    the operation's :func:`derive_deployment_id`).
    """
    now = clock or utc_now
    environment = request.environment
    reference = request.instance

    deployment_id = derive_deployment_id(
        reference.platform_id,
        reference.instance_digest,
        environment.environment_id,
        request.attempt,
    )
    store = DeploymentStateStore(
        DeploymentStateStore.path_for(environment.operations_dir, deployment_id)
    )
    journal = EventJournal(
        EventJournal.path_for(environment.operations_dir, deployment_id)
    )
    record = DeploymentRecord.initial(
        deployment_id=deployment_id,
        environment_id=environment.environment_id,
        attempt=request.attempt,
        platform_id=reference.platform_id,
        instance_digest=reference.instance_digest,
        at=now(),
        **_requested_manifest(request.manifest_document),
    )
    recorder = _Recorder(
        store=store,
        journal=journal,
        record=record,
        secrets=tuple(environment.secrets.values()),
        clock=now,
    )
    recorder.stage("requested", STAGE_IN_PROGRESS, detail={"attempt": request.attempt})
    recorder.append(
        recorder.event(
            EVENT_DEPLOYMENT_REQUESTED,
            stage="requested",
            detail={
                "attempt": request.attempt,
                "environment_id": environment.environment_id,
            },
        )
    )
    recorder.stage("requested", STAGE_COMPLETED, detail={"attempt": request.attempt})

    # -- validated ---------------------------------------------------------
    recorder.stage("validated", STAGE_IN_PROGRESS)
    precheck = _precheck(request)
    if precheck:
        recorder.fail(
            "validated",
            "the deployment input was rejected",
            precheck,
            DeploymentInputRejected(precheck),
        )

    verification = verify_instance(
        request.instance_document,
        request.manifest_document,
        root=request.factory_root,
        environment_id=environment.environment_id,
    )
    recorder.identity(verification)
    recorder.bind_components(verification.components)
    if not verification.ok:
        errors = list(verification.all_errors())
        recorder.fail(
            "validated",
            "the deployment input failed verification",
            errors,
            DeploymentInputRejected(errors),
        )

    environment_errors = validate_environment(environment, verification)
    if environment_errors:
        recorder.fail(
            "validated",
            "the environment cannot satisfy this instance",
            environment_errors,
            DeploymentInputRejected(environment_errors),
        )
    recorder.stage(
        "validated",
        STAGE_COMPLETED,
        detail={
            "platform_id": verification.platform_id,
            "instance_digest": verification.instance_digest,
            "manifest_digest": verification.manifest_digest,
            "components": [
                f"{binding.component_id}@{binding.component_version}"
                for binding in verification.components
            ],
            "verified": ["identity", "digest", "artifact_binding", "validity"],
        },
    )

    # -- provisioning ------------------------------------------------------
    recorder.stage("provisioning", STAGE_IN_PROGRESS)
    try:
        provisioned = provision(environment, verification, deployment_id)
    except ProvisioningFailed as error:
        recorder.fail("provisioning", str(error), error.errors, error)
    recorder.stage("provisioning", STAGE_COMPLETED, detail=provisioned.document())

    # -- deploying ---------------------------------------------------------
    recorder.stage("deploying", STAGE_IN_PROGRESS)
    paths = tuple(source_paths) if source_paths is not None else default_source_paths()
    if identity_provider is None:
        identity_provider = OwnerSuppliedPlatformIdentityProvider(
            OwnerStateSnapshotSource(
                FileRunningPlatformOwnerStateReader(
                    environment.runtime_root / "running_platform_identity.json"
                )
            )
        )
    adapter: RuntimeAdapter = runtime or LocalProcessRuntime(source_paths=paths)
    elements = build_elements(
        environment, provisioned, verification, source_paths=paths
    )
    handles: dict[str, RuntimeHandle] = {}

    try:
        materialized = _materialize(recorder, adapter, verification, elements)
        immutability = verify_input_unchanged(
            verification, request.instance_document, request.manifest_document
        )
        if immutability:
            recorder.fail(
                "deploying",
                "the deployment input changed during the operation",
                immutability,
                DeploymentInputRejected(immutability, stage="deploying"),
            )
        executions = _bind_executions(recorder, verification, elements, materialized)
        bound_elements = {
            component_id: replace(element, execution=executions[component_id])
            for component_id, element in elements.items()
        }
        _run_migrations(recorder, adapter, verification, bound_elements)
        recorder.stage(
            "deploying",
            STAGE_COMPLETED,
            detail={
                "materialized": sorted(elements),
                "execution": {
                    component_id: executions[component_id].entry.digest
                    for component_id in sorted(executions)
                },
                "migrations": [
                    f"{entry.component_id}:{entry.migration_id}"
                    for entry in recorder.record.migrations
                ],
            },
        )

        # -- starting ------------------------------------------------------
        # The platform's runtime elements are started here and only here: the
        # earlier stages materialize and migrate, they do not run the platform.
        recorder.stage("starting", STAGE_IN_PROGRESS)
        _start_elements(recorder, adapter, bound_elements, handles)
        recorder.running(True)
        recorder.stage(
            "starting",
            STAGE_COMPLETED,
            detail={"started": sorted(handles)},
        )

        # -- health_check --------------------------------------------------
        recorder.stage("health_check", STAGE_IN_PROGRESS)
        observations, probe_errors = _probe(adapter, verification, handles)
        evaluation = evaluate_health(
            verification.components, observations, probe_errors
        )
        _record_observations(recorder, observations)
        recorder.append(
            recorder.event(
                EVENT_HEALTH_CHECKED, stage="health_check", detail=evaluation.document()
            )
        )
        if not evaluation.ready:
            recorder.fail(
                "health_check",
                "health/readiness verification did not establish ready",
                evaluation.errors,
                HealthCheckFailed(evaluation.errors),
            )
        recorder.stage(
            "health_check",
            STAGE_COMPLETED,
            detail={
                "healthy": [entry.component_id for entry in evaluation.observations]
            },
        )

        # -- ready ---------------------------------------------------------
        recorder.stage("ready", STAGE_IN_PROGRESS)
        recorder.ready()
        recorder.stage(
            "ready",
            STAGE_COMPLETED,
            detail={
                "ready": True,
                "deployed": False,
                "note": (
                    "ready is a verified operational condition; it fixes no "
                    "deployed claim by itself (ADR-0017 §34)"
                ),
            },
        )

        # -- identity/version/digest verification of the actual platform ----
        # The running platform is verified against what this deployment bound
        # before it started anything: the content each process was launched from
        # (re-read by the engine, so a change during the operation is refused)
        # and the content each process reported loading. Component identity,
        # version and platform are compared as before — and none of it stands
        # on the runtime's word alone (§9, §10).
        identity_errors = verify_identity(
            verification.components,
            observations,
            platform_id=verification.platform_id,
            instance_digest=verification.instance_digest,
            executions={
                component_id: execution.document()
                for component_id, execution in executions.items()
            },
        )
        identity_errors.extend(
            error
            for component_id in sorted(executions)
            for error in verify_bound_content(executions[component_id])
        )
        identity_errors.extend(
            verify_input_unchanged(
                verification, request.instance_document, request.manifest_document
            )
        )
        if identity_errors:
            recorder.fail(
                "ready",
                "identity/version/digest verification failed",
                identity_errors,
                IdentityVerificationFailed(identity_errors),
            )
        if identity_provider is not None:
            try:
                _verify_running_platform_identity(
                    identity_provider,
                    request.instance_document,
                    binding_token=deployment_id,
                )
            except IdentityVerificationFailed as error:
                recorder.fail(
                    "ready",
                    "identity/version/digest verification failed",
                    error.errors,
                    error,
                )
        recorder.identity_verified()

        # -- realized / deployed -------------------------------------------
        recorder.realized()
    except DeploymentOperationsError:
        # Fail-closed: nothing half-verified keeps running. The record keeps the
        # history (the stages that completed, the verification that failed) and
        # stops claiming a platform that is no longer running (§20).
        for handle in handles.values():
            adapter.stop(handle)
        if recorder.record.running:
            recorder.update(recorder.record.mark_stopped(at=recorder.clock()))
        raise

    return Deployment(
        record=recorder.record,
        verification=verification,
        state_path=store.path,
        events_path=journal.path,
        _runtime=adapter,
        _handles=tuple(handles.values()),
        _journal=journal,
        _store=store,
        _secrets=tuple(environment.secrets.values()),
        _clock=now,
    )


def _materialize(
    recorder: _Recorder,
    adapter: RuntimeAdapter,
    verification: InstanceVerification,
    elements: Mapping[str, RuntimeElement],
) -> dict[str, MaterializedComponent]:
    """Materialize every pinned component into its runtime slot (§10, §7)."""
    materialized: dict[str, MaterializedComponent] = {}
    for binding in verification.components:
        element = elements[binding.component_id]
        result = adapter.materialize(element)
        materialized[binding.component_id] = result
        recorder.components(
            {
                binding.component_id: {
                    "materialized": True,
                    "artifact_verified": result.artifact_verified,
                }
            }
        )
        recorder.append(
            recorder.event(
                EVENT_COMPONENT_MATERIALIZED,
                stage="deploying",
                component=binding.document(),
                detail=result.document(),
            )
        )
        if result.errors:
            recorder.fail(
                "deploying",
                f"component {binding.component_id!r} could not be materialized",
                result.errors,
                DeploymentExecutionFailed(result.errors),
            )
    return materialized


def _bind_executions(
    recorder: _Recorder,
    verification: InstanceVerification,
    elements: Mapping[str, RuntimeElement],
    materialized: Mapping[str, MaterializedComponent],
) -> dict[str, ExecutionBinding]:
    """Bind each component's process to the content it will execute (§9, §10).

    The binding is established before anything is launched and recorded with the
    operation, so the deployment's claim is relative to concrete content: what
    the process is started from, with the digest the engine computed for it. A
    component whose content cannot be bound — absent, ambiguous, or an artifact
    that cannot be executed as the verified content — is refused here, before
    any process exists.
    """
    bindings: dict[str, ExecutionBinding] = {}
    for binding in verification.components:
        component_id = binding.component_id
        try:
            execution = bind_execution(
                elements[component_id], materialized=materialized.get(component_id)
            )
        except DeploymentExecutionFailed as error:
            recorder.fail(
                "deploying",
                f"the execution content of {component_id!r} could not be bound",
                error.errors,
                error,
            )
        bindings[component_id] = execution
        recorder.components({component_id: {"execution": execution.document()}})
    return bindings


def _start_element(
    recorder: _Recorder,
    adapter: RuntimeAdapter,
    element: RuntimeElement,
    handles: dict[str, RuntimeHandle],
) -> RuntimeHandle:
    """Start one runtime element and confirm the component constructed itself."""
    component_id = element.component.component_id
    handle = handles.get(component_id)
    started_now = False
    if handle is None:
        try:
            handle = adapter.start(element)
        except RuntimeProcessError as error:
            # A process that is not started is a recorded failure, not an
            # exception escaping the operation: state never claims a platform
            # whose verified content was refused at launch (§9, §20).
            errors = [*error.errors, str(error)]
            recorder.fail(
                "starting",
                f"component {component_id!r} did not start",
                errors,
                StartupFailed(errors),
            )
        handles[component_id] = handle
        started_now = True
    try:
        answer = adapter.request(handle, OP_START)
    except RuntimeProcessError as error:
        errors = [*error.errors, str(error)]
        recorder.fail(
            "starting",
            f"component {component_id!r} did not start",
            errors,
            StartupFailed(errors),
        )
    if answer.get("status") != "ok":
        detail = str(answer.get("error", "the component's runtime reported a failure"))
        errors = [f"{component_id}: {detail}"]
        recorder.fail(
            "starting",
            f"component {component_id!r} did not start",
            errors,
            StartupFailed(errors),
        )
    if started_now:
        recorder.components({component_id: {"runtime_started": True}})
        recorder.append(
            recorder.event(
                EVENT_RUNTIME_STARTED,
                stage="starting",
                component=element.component.document(),
                detail={"workspace": str(element.workspace)},
            )
        )
    return handle


def _start_elements(
    recorder: _Recorder,
    adapter: RuntimeAdapter,
    elements: Mapping[str, RuntimeElement],
    handles: dict[str, RuntimeHandle],
) -> None:
    for component_id in sorted(elements):
        _start_element(recorder, adapter, elements[component_id], handles)


def _run_migrations(
    recorder: _Recorder,
    adapter: RuntimeAdapter,
    verification: InstanceVerification,
    elements: Mapping[str, RuntimeElement],
) -> None:
    """Execute the component-defined migrations the instance requires (§14).

    Bounded by ADR-0017 §10: only the migrations the accepted instance needs as
    part of this deployment, in the order the components declare them, forward
    only. A component's migrations execute against the component's own
    deployment, from the component's own code — this capability orders and
    records execution, it owns no migration and no data.

    Migrations run in their own execution session, never by starting one of the
    platform's runtime elements: the platform is started in the ``starting``
    stage alone (§36–§37), and reaching ``ready`` requires runtime elements that
    were started there, not a migration session that has already ended.
    """
    for binding in verification.components:
        element = elements[binding.component_id]
        if element.binding.migrations is None:
            continue
        try:
            answer = adapter.migrate(element)
        except RuntimeProcessError as error:
            recorder.migration(
                MigrationRecord(
                    component_id=binding.component_id,
                    component_version=binding.component_version,
                    migration_id="orchestration",
                    status="failed",
                    error=str(error),
                )
            )
            recorder.fail(
                "deploying",
                f"migration orchestration failed for {binding.component_id!r}",
                [*error.errors, str(error)],
                MigrationOrchestrationFailed(
                    [*error.errors, str(error)], component_id=binding.component_id
                ),
            )
        required = [str(item) for item in answer.get("required", [])]
        executed = [str(item) for item in answer.get("executed", [])]
        # What the component reported as executed is recorded as executed —
        # including when a later migration of the same component then fails.
        # Deployment state says what ran against which component version (§14).
        _record_migrations(recorder, binding, executed)
        if answer.get("status") != "ok":
            migration_id = answer.get("migration_id")
            detail = str(answer.get("error", "the migration declaration was refused"))
            recorder.append(
                recorder.event(
                    EVENT_MIGRATION_FAILED,
                    stage="deploying",
                    component=binding.document(),
                    detail={
                        "migration_id": migration_id,
                        "required": required,
                        "executed": executed,
                        "error": detail,
                    },
                )
            )
            recorder.migration(
                MigrationRecord(
                    component_id=binding.component_id,
                    component_version=binding.component_version,
                    migration_id=str(migration_id or "unknown"),
                    status="failed",
                    error=detail,
                )
            )
            recorder.fail(
                "deploying",
                f"a required migration of {binding.component_id!r} failed",
                [f"{binding.component_id}: {detail}"],
                MigrationOrchestrationFailed(
                    [f"{binding.component_id}: {detail}"],
                    component_id=binding.component_id,
                    migration_id=str(migration_id) if migration_id else None,
                ),
            )


def _record_migrations(
    recorder: _Recorder,
    binding: ComponentBinding,
    executed: Sequence[str],
) -> None:
    """Record the migrations a component reported as executed, in order (§14).

    The capability orders and records execution; the migration code itself is
    the component's, and so is every artifact it reads or writes.
    """
    for migration_id in executed:
        recorder.append(
            recorder.event(
                EVENT_MIGRATION_STARTED,
                stage="deploying",
                component=binding.document(),
                detail={"migration_id": migration_id, "direction": "forward"},
            )
        )
        recorder.migration(
            MigrationRecord(
                component_id=binding.component_id,
                component_version=binding.component_version,
                migration_id=migration_id,
                status="executed",
            )
        )
        recorder.append(
            recorder.event(
                EVENT_MIGRATION_COMPLETED,
                stage="deploying",
                component=binding.document(),
                detail={"migration_id": migration_id},
            )
        )


def _probe(
    adapter: RuntimeAdapter,
    verification: InstanceVerification,
    handles: Mapping[str, RuntimeHandle],
) -> tuple[list[ComponentObservation], list[str]]:
    """Collect what every running component answers about itself (§19)."""
    observations: list[ComponentObservation] = []
    errors: list[str] = []
    for binding in verification.components:
        handle = handles.get(binding.component_id)
        if handle is None:
            observations.append(
                ComponentObservation(
                    component_id=binding.component_id,
                    error="the component was not started",
                )
            )
            continue
        try:
            answer = adapter.request(handle, OP_PROBE)
        except RuntimeProcessError as error:
            observations.append(
                ComponentObservation(
                    component_id=binding.component_id, error=str(error)
                )
            )
            continue
        if answer.get("status") != "ok":
            observations.append(
                ComponentObservation(
                    component_id=binding.component_id,
                    error=str(answer.get("error", "the runtime refused the probe")),
                )
            )
            continue
        health = answer.get("health")
        readiness = answer.get("ready")
        execution = answer.get("execution")
        observations.append(
            ComponentObservation(
                component_id=binding.component_id,
                health=health if isinstance(health, Mapping) else None,
                readiness=readiness if isinstance(readiness, Mapping) else None,
                execution=(dict(execution) if isinstance(execution, Mapping) else None),
            )
        )
    return observations, errors


def _record_observations(
    recorder: _Recorder,
    observations: Sequence[ComponentObservation],
) -> None:
    changes: dict[str, dict[str, Any]] = {}
    for observation in observations:
        component_id, version, platform_id = observation.observed_identity()
        changes[observation.component_id] = {
            "observed_component_id": component_id,
            "observed_version": version,
            "observed_platform_id": platform_id,
            "observed_execution": (
                dict(observation.execution) if observation.execution else None
            ),
            "health": dict(observation.health) if observation.health else None,
            "readiness": dict(observation.readiness) if observation.readiness else None,
            "healthy": observation.healthy,
            "error": observation.error,
        }
    recorder.components(changes)


def _attach_subject_errors(
    record: DeploymentRecord,
    verification: InstanceVerification,
    environment: DeploymentEnvironment,
    deployment_id: str,
) -> list[str]:
    """Everything the authoritative record must state before it can be bound.

    Deterministic and fail-closed (§8, §20). :func:`attach` binds an operation
    that already realized exactly this instance on a platform that is running
    now; every other situation — another instance, an altered record, a
    platform that is not running, a claim that was never verified — is refused
    **before** any runtime element is touched, so a refused attach changes
    nothing and reaches nothing.
    """
    errors: list[str] = []

    if record.deployment_id != deployment_id:
        errors.append(
            f"the authoritative record names {record.deployment_id!r}, not the "
            f"operation this request identifies ({deployment_id!r})"
        )
    if (
        derive_deployment_id(
            record.platform_id,
            record.instance_digest,
            record.environment_id,
            record.attempt,
        )
        != record.deployment_id
    ):
        errors.append(
            "the authoritative record does not name itself: its identity "
            "fields no longer derive its own deployment identity, so the "
            "record was altered after it was written"
        )
    if record.environment_id != environment.environment_id:
        errors.append(
            f"the record belongs to environment {record.environment_id!r}, not "
            f"to {environment.environment_id!r}"
        )
    if record.instance_digest != verification.instance_digest:
        errors.append(
            "$.instance_digest: the record pins "
            f"{record.instance_digest!r}, but the supplied instance verifies "
            f"as {verification.instance_digest!r}; attach binds exactly the "
            "instance this operation realized"
        )
    if record.platform_id != verification.platform_id:
        errors.append(
            f"$.platform_id: the record pins {record.platform_id!r}, but the "
            f"supplied instance declares {verification.platform_id!r}"
        )
    if record.manifest_digest != verification.manifest_digest:
        errors.append(
            "$.manifest_digest: the record pins "
            f"{record.manifest_digest!r}, but the supplied instance binds "
            f"{verification.manifest_digest!r}"
        )
    if record.failure is not None:
        errors.append(
            f"the record carries a failure at stage {record.failure.stage!r}; "
            "a failed operation has no Running Platform to bind to"
        )
    if record.lifecycle != LIFECYCLE_REALIZED:
        errors.append(
            f"the record's lifecycle is {record.lifecycle!r}; attach binds an "
            "operation that already realized its instance and realizes "
            "nothing itself"
        )
    if not record.running:
        errors.append(
            "the record claims no running platform; attach binds an "
            "already-standing Running Platform and starts nothing — an "
            "explicit restart is deployment_operations.restart"
        )
    if not record.ready:
        errors.append(
            "the record holds no verified operational condition; there is no "
            "Running Platform whose identity attach could re-verify"
        )
    if not record.identity_verified:
        errors.append(
            "the record was never identity-verified; attach re-verifies an "
            "existing verification and invents none"
        )

    pinned = {entry.component_id: entry for entry in record.components}
    bound = {binding.component_id: binding for binding in verification.components}
    for component_id in sorted(set(pinned) - set(bound)):
        errors.append(
            f"{component_id}: the record pins a component this instance does "
            "not contain"
        )
    for component_id in sorted(set(bound) - set(pinned)):
        errors.append(
            f"{component_id}: the instance requires a component the record "
            "does not pin"
        )
    for component_id in sorted(set(pinned) & set(bound)):
        entry = pinned[component_id]
        binding = bound[component_id]
        if entry.component_version != binding.component_version:
            errors.append(
                f"{component_id}: the record pins version "
                f"{entry.component_version!r}, but the instance pins "
                f"{binding.component_version!r}"
            )
        if entry.artifact_digest != binding.artifact_digest:
            errors.append(
                f"{component_id}: the record pins artifact digest "
                f"{entry.artifact_digest!r}, but the instance pins "
                f"{binding.artifact_digest!r}"
            )
    return errors


def attach(
    request: DeploymentRequest,
    *,
    runtime: RuntimeAdapter,
    identity_provider: PlatformIdentityProvider,
    source_paths: Sequence[Path] | None = None,
    clock: Any = None,
) -> Deployment:
    """Re-bind one already-realized deployment operation to its Running Platform.

    The operational action ADR-0016 §18 covers for a deployment operation whose
    own process was replaced: the Running Platform outlived it, the authoritative
    deployment record is still on disk, and what is missing is only this
    operation's **reference** to the runtime elements it realized. ``attach``
    restores exactly that reference and nothing else.

    It is deliberately not a deployment. It takes the same request as
    :func:`deploy` so the exact instance is **re-verified** rather than trusted,
    fresh-reads the authoritative record, and refuses unless that record states
    an honest realized, running, identity-verified platform for exactly this
    instance. It then asks the injected :class:`RuntimeAdapter` to bind the
    runtime elements its environment already supervises, and re-verifies the
    actual platform identity through the same S4 seam ``deploy`` uses.

    What ``attach`` never does (ADR-0016 §7, §9, §10, §18, §20):

    * it creates, starts, stops, restarts and migrates **nothing** — the
      runtime's lifecycle belongs to its own owner, and this operation only
      holds a reference to it;
    * it edits no desired state and invents no lifecycle position: the record
      keeps the ``realized`` claim it already earned, and gains one operational
      action recording the re-binding;
    * it issues no ``deployed``/``ready`` claim of its own — those stand or fall
      on the record's own verification and on the fresh identity verification
      performed here;
    * it persists no runtime handle: a handle is a reference to a runtime
      element of the Running Platform, not deployment state.

    Fail-closed, before any runtime element is touched: a missing or altered
    authoritative record, an instance that does not verify or does not match
    the record, a record that is not realized/running/identity-verified, an
    adapter with no ``attach`` operation, an adapter that binds no element for
    a pinned component, and any actual identity evidence that is unavailable,
    stale or mismatched.

    Raises:
        DeploymentInputRejected: the request or the injected adapter is unusable.
        DeploymentStateError: there is no authoritative record to bind to.
        InvalidDeploymentStateTransition: the record cannot be bound to this
            request, or the adapter bound another element than the one pinned.
        ProvisioningFailed: the environment cannot prepare this instance.
        IdentityVerificationFailed: the actual platform identity is not
            established for the requested instance.
        RuntimeProcessError: the adapter refuses to bind a standing runtime.
    """
    now: Clock = clock or utc_now
    errors = _precheck(request)
    if errors:
        raise DeploymentInputRejected(errors)

    environment = request.environment
    reference = request.instance
    deployment_id = derive_deployment_id(
        reference.platform_id,
        reference.instance_digest,
        environment.environment_id,
        request.attempt,
    )
    store = DeploymentStateStore(
        DeploymentStateStore.path_for(environment.operations_dir, deployment_id)
    )
    journal = EventJournal(
        EventJournal.path_for(environment.operations_dir, deployment_id)
    )

    # -- the authoritative record, fresh-read: never the request's word ------
    try:
        record = store.read()
    except DeploymentStateError as error:
        raise DeploymentStateError(
            f"{deployment_id}: there is no authoritative deployment record to "
            f"bind to ({error}). Attach re-binds an operation that already "
            "realized its instance; it realizes nothing itself (§10)."
        ) from error

    # -- the exact instance, verified again: never the record's word alone ---
    verification = verify_instance(
        request.instance_document,
        request.manifest_document,
        root=request.factory_root,
        environment_id=environment.environment_id,
    )
    if not verification.ok:
        raise DeploymentInputRejected(list(verification.all_errors()))
    immutable = verify_input_unchanged(
        verification, request.instance_document, request.manifest_document
    )
    if immutable:
        raise DeploymentInputRejected(immutable)

    subject = _attach_subject_errors(record, verification, environment, deployment_id)
    if subject:
        raise InvalidDeploymentStateTransition(
            f"{deployment_id}: the authoritative record cannot be bound to this "
            "request; no runtime element was touched",
            errors=subject,
        )

    # -- the runtime seam: bind, never create --------------------------------
    if not callable(getattr(runtime, "attach", None)):
        raise DeploymentInputRejected(
            [
                (
                    "the injected runtime adapter provides no attach operation; "
                    "binding an already-standing Running Platform requires an "
                    "adapter whose environment supervises that runtime "
                    "(ADR-0016 §18)"
                )
            ]
        )
    provisioned = provision(environment, verification, deployment_id)
    paths = tuple(source_paths) if source_paths is not None else default_source_paths()
    elements = build_elements(
        environment, provisioned, verification, source_paths=paths
    )
    handles: dict[str, RuntimeHandle] = {}
    for component_id in sorted(elements):
        handle = runtime.attach(elements[component_id])
        if handle.component_id != component_id:
            raise InvalidDeploymentStateTransition(
                f"{deployment_id}: the runtime adapter bound "
                f"{handle.component_id!r} where {component_id!r} is pinned; "
                "attach binds exactly the elements this operation realized",
            )
        element = handle.element
        if element.deployment_id != deployment_id or element.instance_digest != str(
            verification.instance_digest
        ):
            raise InvalidDeploymentStateTransition(
                f"{deployment_id}: the runtime adapter bound an element of "
                f"deployment {element.deployment_id!r} pinning instance "
                f"{element.instance_digest!r}; attach binds the runtime of this "
                "operation and of no other (§8, §20)",
            )
        handles[component_id] = handle

    # -- the actual platform identity, verified afresh through the S4 seam ---
    _verify_running_platform_identity(
        identity_provider,
        request.instance_document,
        binding_token=deployment_id,
    )

    # -- record the re-binding as the operational action it is (§18) ---------
    at = now()
    secrets = tuple(environment.secrets.values())
    detail = {"components": sorted(handles)}
    bound = record.with_operational_action("platform_attached", at=at, detail=detail)
    deployment = Deployment(
        record=bound,
        verification=verification,
        state_path=store.path,
        events_path=journal.path,
        _runtime=runtime,
        _handles=tuple(handles[component_id] for component_id in sorted(handles)),
        _journal=journal,
        _store=store,
        _secrets=secrets,
        _clock=now,
    )
    store.write(bound, secrets=secrets)
    journal.append(
        deployment._event(EVENT_PLATFORM_ATTACHED, at=at, stage="ready", detail=detail),
        secrets=secrets,
    )
    return deployment


__all__ = [
    "Deployment",
    "DeploymentRequest",
    "InstanceReference",
    "attach",
    "default_source_paths",
    "deploy",
]
