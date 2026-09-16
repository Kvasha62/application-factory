"""Runtime management — starting the platform and asking it how it is (§18).

Runtime management in the first slice is deliberately minimal (ADR-0017 §11):
start the runtime elements of the instance, execute their component-owned
migrations in order, and evaluate the health/readiness their published
surfaces report. There is no reconciliation loop, no scheduling, no restart
policy, no autoscaling and no fleet management here — those are later stages
(ADR-0017 §39).

The capability talks to its environment through one narrow seam,
:class:`RuntimeAdapter`. The only implementation shipped with this slice is
:class:`LocalProcessRuntime`: one OS process per component of the instance,
speaking the JSON protocol of :mod:`deployment_operations.runtime_worker` over
stdin/stdout. That choice is an **implementation detail, not an architectural
decision**: it selects no deployment engine, no orchestrator and no cloud
provider (ADR-0016 §23; ADR-0017 §40), it changes no contract, ownership or
lifecycle rule, and a different adapter — another process model, another
runtime, another environment — replaces it without the Platform Instance, the
Manifest or this boundary changing (ADR-0016 §11).

What the adapter must never do is implicit in the protocol: it starts what the
instance pinned, it materializes what the instance pinned, and it reports what
the platform actually answered. It never substitutes a version, never invents
an artifact identity, and never alters the desired state (ADR-0016 §7, §8).
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deployment_operations.environment import (
    ComponentRuntimeBinding,
    DeploymentEnvironment,
)
from deployment_operations.errors import DeploymentOperationsError
from deployment_operations.provisioning import (
    ArtifactSource,
    ProvisionedEnvironment,
)
from deployment_operations.verification import (
    ComponentBinding,
    InstanceVerification,
    compute_content_digest,
    verify_artifact_digest,
)

#: Operations of the component runtime protocol.
OP_START = "start"
OP_MIGRATE = "migrate"
OP_PROBE = "probe"
OP_STOP = "stop"


@dataclass(frozen=True)
class RuntimeElement:
    """Everything needed to start one component of one deployment operation."""

    deployment_id: str
    environment_id: str
    platform_id: str
    instance_digest: str
    component: ComponentBinding
    configuration: Mapping[str, Any]
    spec_path: Path
    workspace: Path
    binding: ComponentRuntimeBinding
    artifact_source: ArtifactSource
    interpreter: str
    source_paths: tuple[Path, ...]
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class MaterializedComponent:
    """What was materialized into the runtime slot for one pinned component."""

    component_id: str
    workspace: Path
    artifact_path: Path | None
    observed_digest: str | None
    artifact_verified: bool
    errors: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "workspace": str(self.workspace),
            "artifact_path": str(self.artifact_path) if self.artifact_path else None,
            "observed_digest": self.observed_digest,
            "artifact_verified": self.artifact_verified,
            "errors": list(self.errors),
        }


@dataclass
class RuntimeHandle:
    """A started runtime element of the platform."""

    element: RuntimeElement
    process: subprocess.Popen[str]
    log_stream: Any = None
    started: bool = False

    @property
    def component_id(self) -> str:
        return self.element.component.component_id


class RuntimeAdapter(Protocol):
    """The seam between the deployment operation and its environment."""

    def materialize(self, element: RuntimeElement) -> MaterializedComponent: ...

    def start(self, element: RuntimeElement) -> RuntimeHandle: ...

    def request(
        self,
        handle: RuntimeHandle,
        operation: str,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, Any]: ...

    def stop(self, handle: RuntimeHandle) -> Mapping[str, Any]: ...


def build_elements(
    environment: DeploymentEnvironment,
    provisioned: ProvisionedEnvironment,
    verification: InstanceVerification,
    *,
    source_paths: Sequence[Path],
) -> dict[str, RuntimeElement]:
    """Build the runtime elements of an instance from what was provisioned."""
    elements: dict[str, RuntimeElement] = {}
    for binding in verification.components:
        runtime_binding = environment.binding_for(binding.component_id)
        if runtime_binding is None:  # pragma: no cover - validated before provisioning
            raise RuntimeProcessError(
                f"{binding.component_id}: the environment bound no runtime entrypoint"
            )
        workspace = provisioned.component_workspace(binding.component_id)
        elements[binding.component_id] = RuntimeElement(
            deployment_id=provisioned.deployment_id,
            environment_id=environment.environment_id,
            platform_id=str(verification.platform_id),
            instance_digest=str(verification.instance_digest),
            component=binding,
            configuration=provisioned.configuration[binding.component_id],
            spec_path=provisioned.spec_path(binding.component_id),
            workspace=workspace,
            binding=runtime_binding,
            artifact_source=provisioned.artifact_sources[binding.component_id],
            interpreter=environment.interpreter(),
            source_paths=tuple(source_paths),
            secrets=environment.secrets,
            timeout_seconds=environment.probe_timeout_seconds,
        )
    return elements


class LocalProcessRuntime:
    """One component per process, speaking the component runtime protocol."""

    def __init__(self, *, source_paths: Sequence[Path] = ()) -> None:
        self._source_paths = tuple(source_paths)

    # -- materialization ---------------------------------------------------
    def materialize(self, element: RuntimeElement) -> MaterializedComponent:
        """Materialize the pinned component into its runtime slot.

        A published artifact is copied into the slot and the digest of the copy
        is computed and compared with the pinned artifact identity: what is
        materialized is exactly what the instance pinned, and a mismatch stops
        the operation (ADR-0016 §7). A component whose declaration is
        ``artifact_type: none`` has no artifact content: the slot is prepared
        and no artifact identity is invented for it.
        """
        binding = element.component
        workspace = element.workspace
        workspace.mkdir(parents=True, exist_ok=True)

        if not binding.has_published_artifact:
            return MaterializedComponent(
                component_id=binding.component_id,
                workspace=workspace,
                artifact_path=None,
                observed_digest=None,
                artifact_verified=False,
            )

        source = element.artifact_source
        if source.path is None:
            return MaterializedComponent(
                component_id=binding.component_id,
                workspace=workspace,
                artifact_path=None,
                observed_digest=None,
                artifact_verified=False,
                errors=source.errors
                or (
                    (
                        f"{binding.component_id}: the pinned artifact "
                        f"{binding.artifact_digest!r} has no content to materialize"
                    ),
                ),
            )

        target = (
            workspace
            / f"artifact-{str(binding.artifact_digest).removeprefix('sha256:')}"
        )
        shutil.copyfile(source.path, target)
        observed = compute_content_digest(target)
        errors = verify_artifact_digest(binding, observed)
        return MaterializedComponent(
            component_id=binding.component_id,
            workspace=workspace,
            artifact_path=target,
            observed_digest=observed,
            artifact_verified=not errors,
            errors=tuple(errors),
        )

    # -- runtime -----------------------------------------------------------
    def start(self, element: RuntimeElement) -> RuntimeHandle:
        """Start the component's runtime process from its pinned spec."""
        paths: list[str] = [str(path) for path in element.source_paths]
        paths.extend(str(path) for path in element.binding.import_paths)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(paths),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # Secrets are operational inputs injected at the boundary: they reach
        # the component process and are never written anywhere (ADR-0016 §12).
        environment.update({key: value for key, value in element.secrets.items()})

        element.workspace.mkdir(parents=True, exist_ok=True)
        log_path = element.workspace / "runtime.log"
        log_stream = log_path.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [
                    element.interpreter,
                    "-m",
                    "deployment_operations.runtime_worker",
                    str(element.spec_path),
                ],
                cwd=str(element.workspace),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_stream,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            log_stream.close()
            message = (
                f"{element.component.component_id}: the runtime process could not "
                f"be started ({error.__class__.__name__}: {error})"
            )
            raise RuntimeProcessError(message) from error
        return RuntimeHandle(element=element, process=process, log_stream=log_stream)

    def request(
        self,
        handle: RuntimeHandle,
        operation: str,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Send one operation and read its answer, within the deadline."""
        deadline = timeout if timeout is not None else handle.element.timeout_seconds
        process = handle.process
        if process.stdin is None or process.stdout is None:
            message = (
                f"{handle.component_id}: the runtime process has no protocol channel"
            )
            raise RuntimeProcessError(message)
        if process.poll() is not None:
            message = (
                f"{handle.component_id}: the runtime process is not running "
                f"(exit code {process.returncode})"
            )
            raise RuntimeProcessError(message)

        try:
            process.stdin.write(json.dumps({"op": operation}) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            message = f"{handle.component_id}: the runtime process is unreachable"
            raise RuntimeProcessError(message) from error

        line = _read_line(process, deadline)
        if line is None:
            message = (
                f"{handle.component_id}: the runtime process did not answer the "
                f"{operation!r} request within {deadline} s"
            )
            raise RuntimeProcessError(message)
        try:
            answer = json.loads(line)
        except json.JSONDecodeError as error:
            message = (
                f"{handle.component_id}: the runtime process answered invalid JSON"
            )
            raise RuntimeProcessError(message) from error
        if not isinstance(answer, Mapping):
            message = (
                f"{handle.component_id}: the runtime process answered a non-object"
            )
            raise RuntimeProcessError(message)
        return answer

    def stop(self, handle: RuntimeHandle) -> Mapping[str, Any]:
        """Stop the runtime process of one component and close its channels."""
        answer: Mapping[str, Any] = {"status": "stopped"}
        process = handle.process
        if process.poll() is None:
            try:
                answer = self.request(handle, OP_STOP, timeout=10.0)
            except DeploymentOperationsError:
                answer = {"status": "stopped", "note": "no cooperative answer"}
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        if handle.log_stream is not None:
            handle.log_stream.close()
        handle.started = False
        return answer


class RuntimeProcessError(DeploymentOperationsError):
    """A runtime process could not be started, reached or read."""


def _read_line(process: subprocess.Popen[str], timeout: float) -> str | None:
    """Read one line from the process within the deadline, or return None."""
    if process.stdout is None:  # pragma: no cover - checked by the caller
        raise RuntimeProcessError("the runtime process has no stdout channel")
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            return None
        line = process.stdout.readline()
        if not line:
            return None
        if line.strip():
            return line


__all__ = [
    "OP_MIGRATE",
    "OP_PROBE",
    "OP_START",
    "OP_STOP",
    "LocalProcessRuntime",
    "MaterializedComponent",
    "RuntimeAdapter",
    "RuntimeElement",
    "RuntimeHandle",
    "RuntimeProcessError",
    "build_elements",
]
