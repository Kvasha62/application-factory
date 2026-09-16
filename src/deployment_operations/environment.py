"""The deployment environment: where and how an instance is realized.

The Platform Instance is the desired state and is immutable (ADR-0016 §5, §7);
the environment is the operational side of the same operation. It declares
what this capability needs in order to realize an instance here:

* ``runtime_root`` — the directory owned by this capability in which the
  deployment workspace and the deployment state of this environment live
  (ADR-0016 §9);
* one **runtime binding** per component of the instance — the operational
  entrypoint through which that component's own deployment is constructed in
  this environment, and the location of its component-owned migrations, if it
  declares any (ADR-0016 §14);
* an optional **configuration overlay** — environment-specific values bound to
  the runtime environment through the existing configuration contract
  (ADR-0016 §13), never through composition changes;
* optional **secrets**, which are operational inputs injected at the boundary:
  they are passed to the component processes and are never persisted into
  deployment state, logs or events (ADR-0016 §12).

The binding is deliberately narrow: this capability contains no per-component
knowledge, selects no deployment technology (ADR-0017 §40) and owns no
component internals. It is a replaceable adapter seam — a different environment
may bind the same instance to a different runtime without the instance, the
Manifest or any contract changing (ADR-0016 §11, §23).

None of this is desired state. Editing an environment never changes what a
platform *is*: composition identity — component identities, versions, artifact
identities, the Manifest and the instance — stays exactly what the factory
assembled (ADR-0016 §7, §13).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deployment_operations.verification import InstanceVerification, canonical_digest
from platform_manifest.validation import is_floating_selector

Document = Mapping[str, Any]

#: Component configuration keys that name the Platform Instance a component
#: serves. An environment may never overlay them with a different value: that
#: would alter the identity of the deployed platform, not its environment.
IDENTITY_CONFIGURATION_KEYS = ("platform_id", "current_platform_id")

_DOTTED_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MigrationBinding:
    """Where a component declares its own required migrations (ADR-0016 §14).

    Migration content belongs to the component (LAW-03) and develops forward
    only (LAW-08). This binding names the component-owned declaration; this
    capability never holds a migration of its own and never touches component
    data directly.
    """

    module: str
    attribute: str

    def document(self) -> dict[str, Any]:
        return {"module": self.module, "attribute": self.attribute}


@dataclass(frozen=True)
class ComponentRuntimeBinding:
    """The operational entrypoint of one component in one environment."""

    component_id: str
    deployment_module: str
    deployment_factory: str
    migrations: MigrationBinding | None = None
    import_paths: tuple[Path, ...] = ()

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "component_id": self.component_id,
            "deployment_module": self.deployment_module,
            "deployment_factory": self.deployment_factory,
            "import_paths": [str(path) for path in self.import_paths],
            "migrations": self.migrations.document() if self.migrations else None,
        }
        return document


@dataclass(frozen=True)
class DeploymentEnvironment:
    """Operational inputs of one deployment operation in one environment."""

    environment_id: str
    runtime_root: Path
    bindings: tuple[ComponentRuntimeBinding, ...]
    configuration_overlay: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)
    artifact_cache: Path | None = None
    python_executable: str = ""
    probe_timeout_seconds: float = 60.0
    keep_running_after_stop: bool = False

    # -- derived locations -------------------------------------------------
    @property
    def operations_dir(self) -> Path:
        """Directory holding deployment state records and event journals."""
        return self.runtime_root / "operations"

    @property
    def deployments_dir(self) -> Path:
        """Directory holding the runtime workspaces of realized instances."""
        return self.runtime_root / "deployments"

    def interpreter(self) -> str:
        """The interpreter used to start component runtime processes."""
        return self.python_executable or sys.executable

    # -- lookups -----------------------------------------------------------
    def binding_for(self, component_id: str) -> ComponentRuntimeBinding | None:
        for binding in self.bindings:
            if binding.component_id == component_id:
                return binding
        return None

    def overlay_for(self, component_id: str) -> Mapping[str, Any]:
        overlay = self.configuration_overlay.get(component_id)
        return overlay if isinstance(overlay, Mapping) else {}

    def effective_configuration(
        self,
        component_id: str,
        instance_configuration: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind environment-specific settings to the instance's configuration.

        The instance's declared configuration is the desired state and wins;
        the overlay only supplies environment-specific values for keys the
        instance does not pin. Contradicting the instance is refused by
        :func:`validate_environment` before this is ever called.
        """
        declared = instance_configuration.get(component_id)
        effective: dict[str, Any] = (
            dict(declared) if isinstance(declared, Mapping) else {}
        )
        for key, value in self.overlay_for(component_id).items():
            if key not in effective:
                effective[key] = value
        return effective

    def document(self) -> dict[str, Any]:
        """Return the environment as a document, with secrets left out.

        Secrets are operational inputs, not content: they are never written
        into records, logs or signals (ADR-0016 §12), so the serialisable form
        of an environment names only the fact that these components receive
        secret material.
        """
        return {
            "environment_id": self.environment_id,
            "runtime_root": str(self.runtime_root),
            "artifact_cache": str(self.artifact_cache) if self.artifact_cache else None,
            "python_executable": self.interpreter(),
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "bindings": [binding.document() for binding in self.bindings],
            "configuration_overlay": {
                component_id: dict(values)
                for component_id, values in sorted(self.configuration_overlay.items())
            },
            "secret_keys": sorted(self.secrets),
        }


def _path(value: object, base_dir: Path | None) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    return candidate


def load_environment(
    document: object, *, base_dir: Path | None = None
) -> DeploymentEnvironment:
    """Load a deployment environment from a JSON document.

    Secrets are deliberately not readable from this document: they are
    operational inputs supplied by the operator at run time (process
    environment or an explicit argument), never stored beside the environment
    (ADR-0016 §12). Relative paths resolve against ``base_dir``.
    """
    from deployment_operations.errors import DeploymentStateError

    if not isinstance(document, Mapping):
        message = "a deployment environment document must be a JSON object"
        raise DeploymentStateError(message)

    environment_id = document.get("environment_id")
    if not isinstance(environment_id, str) or not environment_id.strip():
        message = "environment_id is required and must be a non-empty string"
        raise DeploymentStateError(message)

    runtime_root = _path(document.get("runtime_root"), base_dir)
    if runtime_root is None:
        message = "runtime_root is required: the environment must own a directory"
        raise DeploymentStateError(message)

    raw_bindings = document.get("bindings", [])
    if not isinstance(raw_bindings, list) or not raw_bindings:
        message = "bindings is required: one runtime binding per component is needed"
        raise DeploymentStateError(message)

    bindings: list[ComponentRuntimeBinding] = []
    for entry in raw_bindings:
        if not isinstance(entry, Mapping):
            message = "each binding must be a JSON object"
            raise DeploymentStateError(message)
        migrations: MigrationBinding | None = None
        raw_migrations = entry.get("migrations")
        if isinstance(raw_migrations, Mapping):
            migrations = MigrationBinding(
                module=str(raw_migrations.get("module", "")),
                attribute=str(raw_migrations.get("attribute", "")),
            )
        raw_paths = entry.get("import_paths", [])
        import_paths: list[Path] = []
        if isinstance(raw_paths, list):
            for item in raw_paths:
                resolved = _path(item, base_dir)
                if resolved is not None:
                    import_paths.append(resolved)
        bindings.append(
            ComponentRuntimeBinding(
                component_id=str(entry.get("component_id", "")),
                deployment_module=str(entry.get("deployment_module", "")),
                deployment_factory=str(entry.get("deployment_factory", "")),
                migrations=migrations,
                import_paths=tuple(import_paths),
            )
        )

    overlay: dict[str, dict[str, Any]] = {}
    raw_overlay = document.get("configuration_overlay", {})
    if isinstance(raw_overlay, Mapping):
        for component_id, values in raw_overlay.items():
            if isinstance(values, Mapping):
                overlay[str(component_id)] = dict(values)

    timeout = document.get("probe_timeout_seconds", 60.0)
    if not isinstance(timeout, int | float) or timeout <= 0:
        message = "probe_timeout_seconds must be a positive number"
        raise DeploymentStateError(message)

    return DeploymentEnvironment(
        environment_id=environment_id,
        runtime_root=runtime_root,
        bindings=tuple(bindings),
        configuration_overlay=overlay,
        artifact_cache=_path(document.get("artifact_cache"), base_dir),
        python_executable=str(document.get("python_executable", "") or ""),
        probe_timeout_seconds=float(timeout),
    )


def _binding_errors(environment: DeploymentEnvironment) -> list[str]:
    errors: list[str] = []

    if not environment.environment_id.strip():
        errors.append("environment_id: a non-empty environment identity is required")
    elif is_floating_selector(environment.environment_id):
        errors.append(
            f"environment_id: {environment.environment_id!r} is a floating "
            "selector, not a concrete environment"
        )

    if not environment.runtime_root.is_absolute():
        errors.append(
            f"runtime_root: {str(environment.runtime_root)!r} must be an absolute "
            "path; the runtime environment is a concrete location, not a guess"
        )

    if environment.probe_timeout_seconds <= 0:
        errors.append("probe_timeout_seconds: must be positive")

    if not Path(environment.interpreter()).exists():
        errors.append(
            f"python_executable: {environment.interpreter()!r} does not exist"
        )

    seen: set[str] = set()
    for binding in environment.bindings:
        prefix = f"bindings[{binding.component_id!r}]"
        if not binding.component_id:
            errors.append("bindings: every binding must name a component_id")
            continue
        if binding.component_id in seen:
            errors.append(f"{prefix}: the component is bound more than once")
        seen.add(binding.component_id)
        if is_floating_selector(binding.component_id):
            errors.append(f"{prefix}: a floating selector is not a component identity")
        if not _DOTTED_NAME_RE.fullmatch(binding.deployment_module):
            errors.append(
                f"{prefix}.deployment_module: {binding.deployment_module!r} is not "
                "a dotted module name"
            )
        if not _IDENTIFIER_RE.fullmatch(binding.deployment_factory):
            errors.append(
                f"{prefix}.deployment_factory: {binding.deployment_factory!r} is not "
                "a factory name"
            )
        if binding.migrations is not None:
            if not _DOTTED_NAME_RE.fullmatch(binding.migrations.module):
                errors.append(
                    f"{prefix}.migrations.module: {binding.migrations.module!r} is "
                    "not a dotted module name"
                )
            if not _IDENTIFIER_RE.fullmatch(binding.migrations.attribute):
                errors.append(
                    f"{prefix}.migrations.attribute: "
                    f"{binding.migrations.attribute!r} is not an attribute name"
                )

    return errors


def validate_environment(
    environment: DeploymentEnvironment,
    verification: InstanceVerification,
) -> list[str]:
    """Verify that this environment can satisfy this instance (§11, §13).

    Provisioning derives its requirements only from the instance, so an
    environment that cannot satisfy it — a component without a runtime binding,
    a binding for a component the instance does not declare, an overlay that
    would alter the instance's configuration or identity, or a missing artifact
    source for a component whose artifact is pinned — is a fail-closed
    rejection. It is never an occasion to improvise a component, substitute a
    version or skip a check (ADR-0016 §7, §20).
    """
    errors = _binding_errors(environment)

    instance_components = set(verification.component_ids)
    bound = {binding.component_id for binding in environment.bindings}

    for component_id in sorted(instance_components - bound):
        errors.append(
            f"bindings: the environment provides no runtime binding for "
            f"component {component_id!r}; the instance cannot be realized here"
        )
    for component_id in sorted(bound - instance_components):
        errors.append(
            f"bindings[{component_id!r}]: the environment binds a component the "
            "accepted instance does not declare; provisioning never introduces "
            "undocumented components"
        )

    needs_artifact_source = any(
        binding.has_published_artifact for binding in verification.components
    )
    if needs_artifact_source:
        cache = environment.artifact_cache
        if cache is None:
            errors.append(
                "artifact_cache: the instance pins published artifacts, but the "
                "environment provides no artifact source to verify them against"
            )
        elif not cache.exists():
            errors.append(
                f"artifact_cache: {str(cache)!r} does not exist; artifact "
                "identity cannot be verified against an absent source"
            )

    for component_id, overlay in sorted(environment.configuration_overlay.items()):
        if component_id not in instance_components:
            errors.append(
                f"configuration_overlay[{component_id!r}]: the environment "
                "overlays configuration of a component the instance does not declare"
            )
            continue
        binding = verification.component(component_id)
        declared_keys = set(binding.configuration_keys) if binding else set()
        for key, value in overlay.items():
            path = f"configuration_overlay[{component_id!r}].{key}"
            if is_floating_selector(key):
                errors.append(f"{path}: the configuration key is a floating selector")
            if is_floating_selector(value):
                errors.append(
                    f"{path}: {value!r} is a floating selector; environment "
                    "configuration also carries concrete values"
                )
            if key in IDENTITY_CONFIGURATION_KEYS and value != verification.platform_id:
                errors.append(
                    f"{path}: the environment may not restate the identity of "
                    f"the deployed platform as {value!r}; the instance is "
                    f"{verification.platform_id!r}"
                )
            if key in declared_keys:
                errors.append(
                    f"{path}: the accepted instance pins this configuration key; "
                    "environment-specific configuration cannot alter what the "
                    "desired state fixed"
                )

    return sorted(set(errors))


def render_environment(environment: DeploymentEnvironment) -> str:
    """Serialize an environment deterministically, secrets excluded."""
    return json.dumps(
        environment.document(), indent=2, ensure_ascii=False, sort_keys=True
    )


__all__ = [
    "IDENTITY_CONFIGURATION_KEYS",
    "ComponentRuntimeBinding",
    "DeploymentEnvironment",
    "MigrationBinding",
    "canonical_digest",
    "load_environment",
    "render_environment",
    "validate_environment",
]
