"""Provisioning — the runtime environment required by the instance (§11).

Provisioning prepares what realizing *this* instance requires, and nothing
else. Its requirements are derived from the accepted Platform Instance only
(ADR-0016 §11): the pinned component set with its versions and artifact
identities, the configuration the instance declared, and the platform identity
the components must serve. It never introduces undocumented components,
dependencies or composition changes, and it is fail-closed: an environment
whose properties cannot be verified against those requirements is not used
(ADR-0016 §20).

This is the minimal environment preparation the first slice needs (ADR-0017
§6): an isolated workspace per deployment operation, one materialization slot
per pinned component of the instance, the artifact identity of a published
artifact verified against the environment's artifact source, and the runtime
spec each component process is started from. It is deliberately not a general
provisioning platform, an infrastructure-as-code system or a cloud adapter —
no such technology is selected here (ADR-0016 §23; ADR-0017 §40).

Secrets play no part: they are injected into component processes at start time
by the runtime adapter and are never written into the workspace, deployment
state or events (ADR-0016 §12).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deployment_operations.environment import DeploymentEnvironment
from deployment_operations.errors import ProvisioningFailed
from deployment_operations.verification import (
    ComponentBinding,
    InstanceVerification,
    canonical_digest,
    compute_content_digest,
    verify_artifact_digest,
)

#: Name of the runtime spec file written per component of the instance.
RUNTIME_SPEC_FILENAME = "runtime.json"


@dataclass(frozen=True)
class ArtifactSource:
    """What the environment offers as the content of a pinned artifact.

    ``verified`` says whether an artifact identity was actually checked against
    content. For a component whose declaration is ``artifact_type: none`` no
    artifact exists and none is invented — the observation is explicitly
    unverified rather than silently treated as a match (ADR-0016 §7).
    """

    path: Path | None
    digest: str | None
    verified: bool
    errors: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "digest": self.digest,
            "verified": self.verified,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ProvisionedEnvironment:
    """The prepared runtime environment of one deployment operation."""

    deployment_id: str
    workspace: Path
    requirements: tuple[str, ...]
    configuration: Mapping[str, Mapping[str, Any]]
    runtime_specs: Mapping[str, Path]
    artifact_sources: Mapping[str, ArtifactSource]

    def component_workspace(self, component_id: str) -> Path:
        return self.workspace / "components" / component_id

    def spec_path(self, component_id: str) -> Path:
        return self.runtime_specs[component_id]

    def document(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "workspace": str(self.workspace),
            "requirements": list(self.requirements),
            "components": sorted(self.runtime_specs),
            "artifact_sources": {
                component_id: source.document()
                for component_id, source in sorted(self.artifact_sources.items())
            },
        }


def resolve_artifact(
    cache: Path | None,
    binding: ComponentBinding,
) -> ArtifactSource:
    """Resolve a pinned artifact in the environment's artifact source.

    A published artifact is content selected by immutable digest: the content
    is looked up by that digest and its own digest is computed and compared.
    Absence is a rejection, never a reason to fall back to another version
    (ADR-0016 §7, §20).
    """
    if not binding.has_published_artifact:
        return ArtifactSource(path=None, digest=None, verified=False)

    assert binding.artifact_digest is not None
    if cache is None:
        message = (
            f"{binding.component_id}: no artifact source is configured; the "
            f"pinned artifact {binding.artifact_digest!r} cannot be verified"
        )
        return ArtifactSource(path=None, digest=None, verified=False, errors=(message,))

    expected = str(canonical_digest(binding.artifact_digest))
    candidate = cache / expected
    if not candidate.is_file():
        message = (
            f"{binding.component_id}: the environment's artifact source has no "
            f"content for the pinned artifact {binding.artifact_digest!r}"
        )
        return ArtifactSource(path=None, digest=None, verified=False, errors=(message,))

    observed = compute_content_digest(candidate)
    errors = verify_artifact_digest(binding, observed)
    return ArtifactSource(
        path=candidate,
        digest=observed,
        verified=not errors,
        errors=tuple(errors),
    )


def _requirements(
    verification: InstanceVerification,
    environment: DeploymentEnvironment,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Derive the environment's requirements from the instance alone (§11)."""
    requirements: list[str] = []
    configuration: dict[str, dict[str, Any]] = {}
    for binding in verification.components:
        requirements.append(
            f"component {binding.component_id} {binding.component_version} "
            f"artifact {binding.artifact_type} "
            f"{binding.artifact_digest or 'none'}"
        )
        effective = environment.effective_configuration(
            binding.component_id, verification.instance_configuration
        )
        configuration[binding.component_id] = effective
        keys = ", ".join(sorted(effective)) or "none"
        requirements.append(f"configuration {binding.component_id}: {keys}")
    requirements.append(f"platform identity: {verification.platform_id}")
    requirements.append(f"instance digest: {verification.instance_digest}")
    requirements.append(f"environment: {environment.environment_id}")
    return requirements, configuration


def runtime_spec_document(
    *,
    environment: DeploymentEnvironment,
    verification: InstanceVerification,
    binding: ComponentBinding,
    deployment_id: str,
    configuration: Mapping[str, Any],
    workspace: Path,
    artifact_source: ArtifactSource,
) -> dict[str, Any]:
    """Build the runtime spec a component process is started from.

    The spec pins exactly what the instance pins — component, version, artifact
    identity and digest, platform identity, instance digest — plus the
    operational entrypoint the environment bound and the environment-specific
    values it bound. It contains no secret material (ADR-0016 §12): secrets
    reach a component process only through its process environment.
    """
    runtime_binding = environment.binding_for(binding.component_id)
    assert runtime_binding is not None
    return {
        "$comment": (
            "Runtime spec written by Deployment & Operations for one component of "
            "one deployment operation. It is pinned to the accepted Platform "
            "Instance and is never edited: a different instance is a different "
            "deployment (ADR-0016 §5, §7)."
        ),
        "deployment_id": deployment_id,
        "environment_id": environment.environment_id,
        "platform_id": verification.platform_id,
        "instance_digest": verification.instance_digest,
        "manifest": {
            "manifest_id": verification.manifest_id,
            "manifest_version": verification.manifest_version,
            "manifest_digest": verification.manifest_digest,
        },
        "component": binding.document(),
        "configuration": dict(configuration),
        "artifact_source": artifact_source.document(),
        "runtime": {
            "deployment_module": runtime_binding.deployment_module,
            "deployment_factory": runtime_binding.deployment_factory,
            "migrations": (
                runtime_binding.migrations.document()
                if runtime_binding.migrations
                else None
            ),
            "import_paths": [str(path) for path in runtime_binding.import_paths],
        },
        "workspace": str(workspace),
    }


def provision(
    environment: DeploymentEnvironment,
    verification: InstanceVerification,
    deployment_id: str,
) -> ProvisionedEnvironment:
    """Prepare and verify the runtime environment of one deployment (§11).

    Fail-closed: every requirement derived from the instance must be satisfied
    and verified by the environment, or provisioning fails and the deployment
    operation cannot proceed to realization. Nothing is started here.
    """
    from deployment_operations.environment import validate_environment

    errors = validate_environment(environment, verification)
    if errors:
        raise ProvisioningFailed(errors)

    root = environment.runtime_root
    workspace = environment.deployments_dir / deployment_id
    components_dir = workspace / "components"

    try:
        components_dir.mkdir(parents=True, exist_ok=True)
        environment.operations_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        message = (
            f"runtime_root: {str(root)!r} cannot be prepared "
            f"({error.__class__.__name__}: {error})"
        )
        raise ProvisioningFailed([message]) from error

    for binding in environment.bindings:
        for import_path in binding.import_paths:
            if not import_path.is_dir():
                errors.append(
                    f"bindings[{binding.component_id!r}].import_paths: "
                    f"{str(import_path)!r} is not an existing directory"
                )
    if errors:
        raise ProvisioningFailed(sorted(errors))

    requirements, configuration = _requirements(verification, environment)

    artifact_sources: dict[str, ArtifactSource] = {}
    for binding in verification.components:
        source = resolve_artifact(environment.artifact_cache, binding)
        artifact_sources[binding.component_id] = source
        errors.extend(source.errors)
    if errors:
        raise ProvisioningFailed(sorted(set(errors)))

    runtime_specs: dict[str, Path] = {}
    for binding in verification.components:
        component_dir = components_dir / binding.component_id
        try:
            component_dir.mkdir(parents=True, exist_ok=True)
            spec_path = component_dir / RUNTIME_SPEC_FILENAME
            document = runtime_spec_document(
                environment=environment,
                verification=verification,
                binding=binding,
                deployment_id=deployment_id,
                configuration=configuration[binding.component_id],
                workspace=component_dir,
                artifact_source=artifact_sources[binding.component_id],
            )
            spec_path.write_text(
                json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            message = (
                f"{binding.component_id}: the runtime workspace could not be "
                f"prepared ({error.__class__.__name__}: {error})"
            )
            raise ProvisioningFailed([message]) from error
        runtime_specs[binding.component_id] = spec_path

    verification_errors = _verify_prepared(
        verification, components_dir, runtime_specs, configuration
    )
    if verification_errors:
        raise ProvisioningFailed(sorted(set(verification_errors)))

    return ProvisionedEnvironment(
        deployment_id=deployment_id,
        workspace=workspace,
        requirements=tuple(requirements),
        configuration=configuration,
        runtime_specs=runtime_specs,
        artifact_sources=artifact_sources,
    )


def _verify_prepared(
    verification: InstanceVerification,
    components_dir: Path,
    runtime_specs: Mapping[str, Path],
    configuration: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Verify what was prepared against the instance's requirements (§11).

    Preparation that cannot be verified is not used: the specs are read back
    and their pinned identity is compared with the accepted instance, and the
    workspace must contain exactly the instance's components — no more (an
    undocumented component) and no fewer.
    """
    errors: list[str] = []

    prepared = {path.name for path in components_dir.iterdir() if path.is_dir()}
    expected = set(verification.component_ids)
    for component_id in sorted(prepared - expected):
        errors.append(
            f"workspace: prepared component {component_id!r} is not part of the "
            "accepted instance"
        )
    for component_id in sorted(expected - prepared):
        errors.append(
            f"workspace: component {component_id!r} of the accepted instance was "
            "not prepared"
        )

    for component_id, spec_path in runtime_specs.items():
        try:
            document = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(
                f"{component_id}: the runtime spec is unreadable or invalid "
                f"({error.__class__.__name__})"
            )
            continue
        component = document.get("component") if isinstance(document, Mapping) else None
        entry: Mapping[str, Any] = component if isinstance(component, Mapping) else {}
        binding = verification.component(component_id)
        if binding is None:
            errors.append(
                f"{component_id}: the runtime spec names a component the "
                "accepted instance does not declare"
            )
            continue
        if (
            entry.get("component_id") != binding.component_id
            or entry.get("component_version") != binding.component_version
        ):
            errors.append(
                f"{component_id}: the prepared runtime spec does not pin the "
                "instance's component identity and version"
            )
        pinned_artifact = entry.get("artifact")
        artifact = pinned_artifact if isinstance(pinned_artifact, Mapping) else {}
        if (
            canonical_digest(artifact.get("digest"))
            != canonical_digest(binding.artifact_digest)
            or artifact.get("artifact_type") != binding.artifact_type
        ):
            errors.append(
                f"{component_id}: the prepared runtime spec does not pin the "
                "instance's artifact identity"
            )
        if document.get("instance_digest") != verification.instance_digest:
            errors.append(
                f"{component_id}: the prepared runtime spec is not pinned to the "
                "accepted instance digest"
            )
        if document.get("configuration") != dict(configuration[component_id]):
            errors.append(
                f"{component_id}: the prepared configuration diverges from the "
                "configuration bound for this environment"
            )

    return errors


__all__ = [
    "RUNTIME_SPEC_FILENAME",
    "ArtifactSource",
    "ProvisionedEnvironment",
    "provision",
    "resolve_artifact",
    "runtime_spec_document",
]
