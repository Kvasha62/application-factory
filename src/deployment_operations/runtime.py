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
stdin/stdout.

That choice is an **implementation detail, not an architectural decision**: it
selects no deployment engine, no orchestrator and no cloud provider (ADR-0016
§23; ADR-0017 §40), it changes no contract, ownership or lifecycle rule, and a
different adapter — another process model, another runtime, another environment
— replaces it without the Platform Instance, the Manifest or this boundary
changing (ADR-0016 §11).

Two of the adapter's operations are easy to confuse and are kept apart on
purpose: :meth:`RuntimeAdapter.migrate` runs the component-defined migrations the
accepted instance requires in a **transient execution session** that ends when
they have run, and :meth:`RuntimeAdapter.start` creates a **runtime element of
the Running Platform**. Migrations therefore never start the platform: the
platform's elements are created in the ``starting`` stage, and only there
(ADR-0017 §36–§37).

What the adapter must never do is implicit in the protocol: it starts what the
instance pinned, it materializes what the instance pinned, and it reports what
the platform actually answered. It never substitutes a version, never invents
an artifact identity, and never alters the desired state (ADR-0016 §7, §8).
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deployment_operations.environment import (
    ComponentRuntimeBinding,
    DeploymentEnvironment,
)
from deployment_operations.errors import (
    DeploymentExecutionFailed,
    DeploymentOperationsError,
)
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
class BoundModule:
    """One module whose content the engine bound to a launched process.

    ``digest`` is computed by the engine from the file's bytes *before* the
    process is started. It is the engine's own value, never one a runtime
    reported: what a runtime reports is compared against it (§9–§10).
    """

    module: str
    path: Path
    digest: str

    def document(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "path": str(self.path),
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ExecutionBinding:
    """The Verified Execution Boundary of one runtime element.

    This is the engine-side link between verified content and a concrete
    process. ``roots`` are the verified execution sources the engine resolved
    the component's content out of; ``untrusted`` are locations that never
    carry trust (the runtime workspace, whose contents the deployment's own
    process writes); and ``modules`` is the **closed** set of component-owned
    content the process may load — the entrypoint, the component's migration
    content, and every module reachable from them inside the verified roots,
    each with the digest the engine computed for it before anything ran.

    The VEB is established before the element is started, handed to the process
    as its launch instruction, and enforced by the process while it loads
    content: a name the boundary did not bind is refused, not searched for.
    Verification compares the whole closed set, so the claim covers what is
    executed, not only the entrypoint (ADR-0016 §9, §10).
    """

    component_id: str
    #: ``"artifact"`` when the executed content is the verified materialized
    #: artifact of the component, ``"component_source"`` otherwise.
    kind: str
    #: The primary verified execution root (the entrypoint's source).
    root: Path
    #: Every verified execution root this component's content was resolved out
    #: of. An import that resolves inside one of them is component-owned: it
    #: must be in ``modules`` or it is refused.
    roots: tuple[Path, ...]
    #: Locations that never carry trust, whatever they contain: content found
    #: there is neither bound nor loadable by component code.
    untrusted: tuple[Path, ...]
    modules: tuple[BoundModule, ...]

    @property
    def entry(self) -> BoundModule:
        """The module the component's deployment is constructed from."""
        return self.modules[0]

    def document(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "kind": self.kind,
            "root": str(self.root),
            "roots": [str(root) for root in self.roots],
            "untrusted": [str(path) for path in self.untrusted],
            "modules": [module.document() for module in self.modules],
        }


#: Suffixes this runtime boundary can execute: a component's code is Python
#: source it imports. Anything else cannot be bound to a process without
#: inventing execution semantics this capability does not own (fail closed).
EXECUTABLE_SUFFIXES = (".py",)


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
    #: The content this element's process is bound to execute, determined by the
    #: engine before anything is launched (ADR-0016 §9–§10). ``None`` until the
    #: deployment binds it, and no process is started from an unbound element.
    execution: ExecutionBinding | None = None
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

    def migrate(self, element: RuntimeElement) -> Mapping[str, Any]:
        """Execute the component-defined migrations the instance requires (§14).

        Deliberately a separate operation from :meth:`start`: the migration
        session is a component-owned execution context for the migrations this
        deployment needs, while ``start`` creates a runtime element of the
        Running Platform. A deployment that only migrates has started nothing.
        """
        ...

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


def declared_module_names(element: RuntimeElement) -> tuple[str, ...]:
    """The component-owned modules this element's processes must load.

    The deployment entrypoint is always part of it; a component that declares
    migrations brings its migration declaration module with it — the migration
    session executes component code too (ADR-0016 §14), so that content is
    bound exactly like the entrypoint.
    """
    binding = element.binding
    names = [binding.deployment_module]
    if binding.migrations is not None:
        names.append(binding.migrations.module)
    ordered: list[str] = []
    for name in names:
        if name and name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def _module_candidates(module: str, root: Path) -> list[Path]:
    """The files a module name could resolve to directly under one root.

    Python source (``x.py``, ``x/__init__.py``) and, when a name carries no
    source, native extension modules (``x.so``, ``x.<tag>.so``): component-owned
    native content is content of the component, so it is subject to the boundary
    exactly like its source and never trusted from an unverified location.
    """
    parts = module.split(".")
    if not parts or not all(part.isidentifier() for part in parts):
        return []
    base = root.joinpath(*parts)
    candidates = [
        candidate
        for candidate in (base.with_suffix(".py"), base / "__init__.py")
        if candidate.is_file()
    ]
    if not candidates and base.parent.is_dir():
        native = {
            candidate
            for pattern in (f"{parts[-1]}.so", f"{parts[-1]}.*.so")
            for candidate in base.parent.glob(pattern)
            if candidate.is_file()
        }
        candidates = sorted(native)
    return candidates


def _resolve_module_under(
    component_id: str, module: str, roots: Sequence[Path]
) -> tuple[Path, Path]:
    """Resolve one module name to the exact file a process will load (§9).

    Resolution follows the order the deployment environment declared, and it
    must yield exactly one file: a name that resolves to nothing cannot be
    executed as bound content, and a name that resolves ambiguously could
    execute content nobody bound. Both are fail-closed.
    """
    for root in roots:
        candidates = _module_candidates(module, root)
        if len(candidates) > 1:
            raise DeploymentExecutionFailed(
                [
                    (
                        f"{component_id}: the module {module!r} resolves ambiguously "
                        f"under {root}; the content a process would execute cannot "
                        "be bound to one file"
                    )
                ]
            )
        if candidates:
            return candidates[0], root
    declared = ", ".join(str(root) for root in roots) or "none"
    raise DeploymentExecutionFailed(
        [
            (
                f"{component_id}: the component's module {module!r} is not present "
                f"in any import root the environment declared ({declared}); the "
                "content a process would execute cannot be bound and no process is "
                "started for it"
            )
        ]
    )


def _relative_base(
    module: str, *, level: int, node_module: str | None, is_package: bool
) -> str:
    """The absolute module a relative ``from``/``import`` is written against."""
    if level == 0:
        return node_module or ""
    parts = module.split(".")
    package_parts = parts if is_package else parts[:-1]
    base = package_parts[: max(len(package_parts) - (level - 1), 0)]
    if node_module:
        base = base + node_module.split(".")
    return ".".join(base)


def _imported_modules(
    source: bytes, module: str, path: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The module names one component module imports, read from its own code.

    Returns the names to resolve and the packages whose every module a wildcard
    import reaches (``from pkg import *``). Reading the code means the boundary
    covers the imports a component actually declares, not only the two files the
    environment happens to name (ADR-0016 §9, §10).
    """
    tree = ast.parse(source, filename=str(path))
    names: set[str] = set()
    wildcards: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                for index in range(1, len(parts) + 1):
                    names.add(".".join(parts[:index]))
        elif isinstance(node, ast.ImportFrom):
            base = _relative_base(
                module,
                level=node.level,
                node_module=node.module,
                is_package=path.name == "__init__.py",
            )
            if base:
                names.add(base)
            for alias in node.names:
                if alias.name == "*":
                    wildcards.add(base)
                elif base:
                    names.add(f"{base}.{alias.name}")
                else:
                    names.add(alias.name)
        elif isinstance(node, ast.Call):
            function = node.func
            called = (
                function.attr
                if isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
                else function.id if isinstance(function, ast.Name) else None
            )
            if called in {"import_module", "__import__"} and len(node.args) == 1:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    names.add(argument.value)
    return tuple(sorted(names)), tuple(sorted(wildcards))


def _package_modules(package: str, path: Path) -> tuple[str, ...]:
    """Every module a wildcard import of a package reaches."""
    if path.name != "__init__.py":
        return ()
    names: list[str] = []
    for candidate in sorted(path.parent.glob("*.py")):
        if candidate.stem != "__init__":
            names.append(f"{package}.{candidate.stem}")
    return tuple(names)


def execution_closure(
    seeds: Sequence[tuple[str, Path, Path]],
    *,
    component_id: str,
    roots: Sequence[Path],
    untrusted: Sequence[Path],
) -> tuple[tuple[BoundModule, ...], tuple[Path, ...]]:
    """Bind the component's whole executable closure (§9, §10, §18).

    Every module reachable from the seeds inside the verified execution roots is
    resolved, read and digested by the engine before anything runs. Two cases
    are refused outright, because content that cannot be bound cannot be
    executed as component code:

    * a name the component imports that is present only in an untrusted
      location — the workspace, whose files the deployment's own process writes;
    * a name that resolves somewhere the engine cannot read as verified content.
    """
    closure: dict[str, tuple[Path, Path]] = {}
    used: list[Path] = []
    queue: list[str] = []

    def add(module: str) -> None:
        """Bind one module, and the packages that must be imported before it."""
        parts = module.split(".")
        for index in range(1, len(parts) + 1):
            name = ".".join(parts[:index])
            if name in closure:
                continue
            resolved = _resolve_optional(component_id, name, roots)
            if resolved is None:
                # A namespace portion carries no file of its own.
                continue
            closure[name] = (resolved[0], resolved[1])
            if resolved[1] not in used:
                used.append(resolved[1])
            queue.append(name)

    for module, _, _ in seeds:
        add(module)
    while queue:
        module = queue.pop(0)
        path, _ = closure[module]
        if path.suffix not in EXECUTABLE_SUFFIXES:
            continue
        try:
            source = path.read_bytes()
            names, wildcards = _imported_modules(source, module, path)
        except (OSError, SyntaxError) as error:
            raise DeploymentExecutionFailed(
                [
                    (
                        f"{component_id}: the content of {module!r} ({path}) cannot "
                        f"be read as component code ({error.__class__.__name__}: "
                        f"{error}); what would execute cannot be bound"
                    )
                ]
            )
        for package in wildcards:
            resolved = _resolve_optional(component_id, package, roots)
            if resolved is not None:
                for reached in _package_modules(package, resolved[0]):
                    names = (*names, reached)
        for name in names:
            if name in closure or not name:
                continue
            resolved = _resolve_optional(component_id, name, roots)
            if resolved is None:
                if _resolve_optional(component_id, name, untrusted) is not None:
                    raise DeploymentExecutionFailed(
                        [
                            (
                                f"{component_id}: the component's content imports "
                                f"{name!r}, which is present only in the runtime "
                                "workspace; content in the workspace is never "
                                "bound and never executed as component code, so "
                                "this deployment refuses before running anything "
                                "(ADR-0016 §7, §10)"
                            )
                        ]
                    )
                continue
            add(name)
    modules = tuple(
        BoundModule(module=module, path=path, digest=compute_content_digest(path))
        for module, (path, _) in closure.items()
    )
    return modules, tuple(used)


def _resolve_optional(
    component_id: str, module: str, roots: Sequence[Path]
) -> tuple[Path, Path] | None:
    """Resolve a module under the given roots, or ``None`` when it is not there.

    Absence is "not component content"; ambiguity is not absence: a name that
    resolves to several files could execute content nobody bound, so it is
    refused exactly as it is for the entrypoint.
    """
    for root in roots:
        candidates = _module_candidates(module, root)
        if len(candidates) > 1:
            raise DeploymentExecutionFailed(
                [
                    (
                        f"{component_id}: the module {module!r} resolves ambiguously "
                        f"under {root}; the content a process would execute cannot "
                        "be bound to one file"
                    )
                ]
            )
        if candidates:
            return candidates[0], root
    return None


def bind_execution(
    element: RuntimeElement,
    *,
    materialized: MaterializedComponent | None = None,
) -> ExecutionBinding:
    """Bind the content one runtime element is started from (§9, §10).

    The engine determines the executable content itself, before anything is
    launched, and computes the digest of those bytes itself. A component with a
    published artifact is bound to the *verified materialized artifact*: what
    runs is what was pinned and verified, or nothing runs at all. A component
    whose declaration is ``artifact_type: none`` has no artifact content, so
    what is bound is the component's own module content, exactly as resolved
    under the import roots the deployment environment declared.
    """
    component_id = element.component.component_id
    module_names = declared_module_names(element)
    if not module_names:
        raise DeploymentExecutionFailed(
            [
                (
                    f"{component_id}: the runtime binding names no component module; "
                    "the content a process would execute cannot be bound"
                ),
            ]
        )

    if element.component.has_published_artifact:
        artifact_path = materialized.artifact_path if materialized is not None else None
        if (
            materialized is None
            or not materialized.artifact_verified
            or artifact_path is None
        ):
            raise DeploymentExecutionFailed(
                [
                    (
                        f"{component_id}: the pinned artifact "
                        f"{element.component.artifact_digest!r} was not verified "
                        "against any content; no process may be started for it"
                    )
                ]
            )
        if (
            artifact_path.suffix not in EXECUTABLE_SUFFIXES
            or not artifact_path.is_file()
        ):
            raise DeploymentExecutionFailed(
                [
                    (
                        f"{component_id}: the verified artifact content "
                        f"{artifact_path.name!r} is not executable component source "
                        "this boundary can bind a process to; refusing rather than "
                        "executing unverified content (ADR-0016 §7, §10)"
                    )
                ]
            )
        if len(module_names) > 1:
            raise DeploymentExecutionFailed(
                [
                    (
                        f"{component_id}: the component declares migrations beside a "
                        "published artifact; the artifact content cannot be bound as "
                        "the migration session's content, so the component's content "
                        "cannot be bound"
                    )
                ]
            )
        digest = compute_content_digest(artifact_path)
        if verify_artifact_digest(element.component, digest):
            raise DeploymentExecutionFailed(
                [
                    (
                        f"{component_id}: the content bound for execution is not the "
                        "verified artifact content; no process is started"
                    )
                ]
            )
        return ExecutionBinding(
            component_id=component_id,
            kind="artifact",
            root=artifact_path.parent,
            roots=(artifact_path.parent,),
            untrusted=(element.workspace,),
            modules=(
                BoundModule(module=module_names[0], path=artifact_path, digest=digest),
            ),
        )

    roots = [*element.source_paths, *element.binding.import_paths]
    untrusted = [element.workspace]
    seeds: list[tuple[str, Path, Path]] = []
    for module_name in module_names:
        path, root = _resolve_module_under(component_id, module_name, roots)
        seeds.append((module_name, path, root))
    closure, verified_roots = execution_closure(
        seeds, component_id=component_id, roots=roots, untrusted=untrusted
    )
    entry_names = {module_name for module_name, _, _ in seeds}
    ordered = tuple(
        sorted(
            closure,
            key=lambda bound: (bound.module not in entry_names, bound.module),
        )
    )
    return ExecutionBinding(
        component_id=component_id,
        kind="component_source",
        root=seeds[0][2],
        roots=verified_roots,
        untrusted=tuple(untrusted),
        modules=ordered,
    )


def verify_bound_content(binding: ExecutionBinding) -> tuple[str, ...]:
    """Re-read bound content and compare it with what the engine bound (§9).

    The engine reads these files itself, so this holds independently of
    anything a running process reported: content that changed after it was
    bound is content no claim may stand on, and the bytes therefore have to
    match the binding for as long as the deployment claims to be deployed.
    """
    errors: list[str] = []
    for module in binding.modules:
        if not module.path.is_file():
            errors.append(
                f"{binding.component_id}: the bound content of {module.module!r} "
                f"({module.path}) is gone; the executed content cannot be "
                "verified"
            )
            continue
        observed = compute_content_digest(module.path)
        if observed != module.digest:
            errors.append(
                f"{binding.component_id}: the bound content of {module.module!r} "
                f"is {observed} but this deployment bound {module.digest}; the "
                "content a claim would rest on changed and is refused"
            )
    return tuple(errors)


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

    # -- migrations --------------------------------------------------------
    def migrate(self, element: RuntimeElement) -> Mapping[str, Any]:
        """Run the component's required migrations in a transient session (§14).

        The session exists only for the component-defined migrations the
        accepted instance requires: it constructs the component's own
        deployment, executes those migrations forward-only, and terminates. It
        is **not** a runtime element of the platform — nothing is left running
        when it ends, and it is never what the ``starting`` stage starts
        (§36–§37). Failures are answered, not raised: the orchestration layer
        decides how an unanswered or refused declaration is recorded (§20).
        """
        self._verify_before_execution(element)
        handle = self._spawn(element, log_name="migration.log")
        try:
            return self.request(handle, OP_MIGRATE)
        finally:
            self.stop(handle)

    # -- runtime -----------------------------------------------------------
    def start(self, element: RuntimeElement) -> RuntimeHandle:
        """Start one runtime element of the platform from its pinned spec."""
        self._verify_before_execution(element)
        return self._spawn(element, log_name="runtime.log")

    def _verify_before_execution(self, element: RuntimeElement) -> None:
        """Re-verify the bound content at the moment it is about to execute.

        Verification and execution have to be the same identity, so the files
        this process is about to load are re-read here, after the binding and
        immediately before the launch: content replaced between verification and
        execution is refused instead of run (§9). A replacement in the remaining
        window is still caught — the process re-digests what it loads and the
        engine re-reads the binding before any claim — but nothing this
        deployment verified is allowed to execute as something else.
        """
        execution = element.execution
        if execution is None:
            return  # _spawn refuses it, with the message that belongs to that
        errors = verify_bound_content(execution)
        if errors:
            raise RuntimeProcessError(
                "the content this deployment verified is not the content about "
                "to execute; refusing to start the process",
                errors=errors,
            )

    def _spawn(self, element: RuntimeElement, *, log_name: str) -> RuntimeHandle:
        """Start one component process bound to the content it must execute.

        The launch carries the engine's execution binding: the process is told
        which file to load for each component-owned module, and it is started
        from nowhere else. There is no unbound launch — an element whose content
        the engine did not determine is not started at all (§9, §10).
        """
        execution = element.execution
        if execution is None:
            message = (
                f"{element.component.component_id}: no execution content was "
                "bound for this element; refusing to start a process whose "
                "content cannot be verified"
            )
            raise RuntimeProcessError(message)
        # The process's search path is the trusted runtime environment and
        # nothing else: the engine's own code root. The component's content is
        # not found by searching — it is handed to the process as the boundary
        # — so no environment-controlled path can add executable content to
        # what this deployment trusts (§17).
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # The working directory is never an import source: not for this
            # process and not for any Python process it starts (§6, §18).
            "PYTHONSAFEPATH": "1",
        }
        system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
        if system_root:
            environment["SystemRoot"] = system_root
        # Secrets are operational inputs injected at the boundary: they reach
        # the component process and are never written anywhere (ADR-0016 §12).
        environment.update({key: value for key, value in element.secrets.items()})

        # The launch instruction names the bound files and never the digests:
        # the process reports the digests of what it actually loaded, and the
        # engine compares that evidence with this binding. A process that is
        # told the expected digest could echo it; one that is told the file
        # cannot load anything else (§9).
        instruction = json.dumps(
            {
                "root": str(execution.root),
                "roots": [str(root) for root in execution.roots],
                "untrusted": [str(path) for path in execution.untrusted],
                "modules": [
                    {"module": module.module, "path": str(module.path)}
                    for module in execution.modules
                ],
            },
            sort_keys=True,
        )

        element.workspace.mkdir(parents=True, exist_ok=True)
        log_path = element.workspace / log_name
        log_stream = log_path.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [
                    element.interpreter,
                    # ``-P`` keeps the working directory off the process's
                    # search path, so the workspace cannot provide startup code
                    # (``sitecustomize``) or shadow module resolution (§18).
                    "-P",
                    "-m",
                    "deployment_operations.runtime_worker",
                    str(element.spec_path),
                    "--binding",
                    instruction,
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

    result: list[str | None] = [None]

    def read() -> None:
        result[0] = process.stdout.readline()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)

    if reader.is_alive():
        return None

    line = result[0]
    if not line or not line.strip():
        return None
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
