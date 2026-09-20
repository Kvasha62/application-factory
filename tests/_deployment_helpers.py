"""Shared helpers for the Deployment & Operations architectural tests.

The helpers build their inputs through the authoritative Factory surfaces —
the Composer, the Platform Manifest and the Platform Instance assembly — so
every test starts from a genuinely accepted desired state rather than from a
hand-written stand-in (ADR-0016 §3: the Factory ends at the Platform Instance,
and this capability consumes exactly that artifact).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from composer import compose_request_document
from composer.request import build_request_document
from deployment_operations import (
    ComponentRuntimeBinding,
    DeploymentEnvironment,
    DeploymentRequest,
    InstanceReference,
    MigrationBinding,
    load_record,
)
from platform_instance import Instance, assemble_document, discover_root
from platform_manifest import compute_manifest_digest, validate_document

#: The repository root, i.e. the factory root of the canonical registry.
ROOT = discover_root()

#: The component used by the canonical tests, with its registry-pinned version.
COMPONENT_ID = "tenant_authority"
COMPONENT_VERSION = "0.1.0"

#: Directory of the runtime doubles used to inject failures at the boundary.
FIXTURE_PATH = Path(__file__).parent / "_runtime_fixtures"

#: The Platform Instance name the canonical tests deploy.
PLATFORM_ID = "deployment-platform"


def validated(
    document: Mapping[str, Any], *, state: str = "validated"
) -> dict[str, Any]:
    """Advance a composed draft manifest to ``state`` deterministically.

    A Platform Manifest lifecycle act (ARCHITECTURE.md §16), performed here
    with fixed values exactly as the Slice C/E/F tests do — the deployment
    capability never advances a manifest.
    """
    advanced = copy.deepcopy(dict(document))
    advanced["lifecycle"] = {"state": state}
    if state != "draft":
        advanced["validation_attestation"] = {
            "validated_by": "deployment-operations-tests",
            "validated_at": "2026-09-16T00:00:00Z",
            "checks": ["schema", "registry", "composition"],
        }
    if state in ("approved", "published", "deployed", "superseded", "retired"):
        advanced["approval"] = {
            "approved_by": "project-owner",
            "approved_at": "2026-09-16T00:00:00Z",
            "approval_id": "approval-1",
        }
    if state in ("published", "deployed", "superseded", "retired"):
        advanced["publication"] = {
            "published_by": "project-owner",
            "published_at": "2026-09-16T00:00:00Z",
            "publication_id": "publication-1",
        }
    advanced["manifest_digest"] = compute_manifest_digest(advanced)
    return advanced


def manifest_for(
    components: Sequence[Mapping[str, Any]] | None = None,
    *,
    root: Path | None = None,
    manifest_id: str = "deployment-platform",
    configuration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose and validate a Platform Manifest through the real Composer."""
    base = root if root is not None else ROOT
    selection = (
        list(components)
        if components is not None
        else [{"component_id": COMPONENT_ID, "component_version": COMPONENT_VERSION}]
    )
    request = build_request_document(
        manifest_id=manifest_id,
        manifest_version="1.0.0",
        components=selection,
        configuration=dict(configuration) if configuration is not None else None,
    )
    document = validated(compose_request_document(request, root=base).document)
    assert validate_document(document, root=base) == []
    return document


def instance_for(
    manifest: Mapping[str, Any],
    *,
    platform_id: str = PLATFORM_ID,
    root: Path | None = None,
) -> Instance:
    """Assemble a Platform Instance from a validated manifest."""
    base = root if root is not None else ROOT
    return assemble_document(manifest, platform_id=platform_id, root=base)


def binding(
    *,
    component_id: str = COMPONENT_ID,
    module: str = "tenant_authority.deployment",
    factory: str = "build_deployment",
    migrations: MigrationBinding | None = None,
    import_fixtures: bool = False,
) -> ComponentRuntimeBinding:
    """A runtime binding of the canonical component in a test environment."""
    if module == "tenant_authority.deployment":
        import_paths: tuple[Path, ...] = ()
    else:
        import_paths = (FIXTURE_PATH,) if import_fixtures else ()
    return ComponentRuntimeBinding(
        component_id=component_id,
        deployment_module=module,
        deployment_factory=factory,
        migrations=migrations,
        import_paths=import_paths,
    )


def fixture_binding(
    module: str,
    *,
    factory: str = "build_component",
    migrations: MigrationBinding | None = None,
    component_id: str = COMPONENT_ID,
) -> ComponentRuntimeBinding:
    """A binding to one of the runtime doubles in ``tests/_runtime_fixtures``."""
    return ComponentRuntimeBinding(
        component_id=component_id,
        deployment_module=module,
        deployment_factory=factory,
        migrations=migrations,
        import_paths=(FIXTURE_PATH,),
    )


def environment_for(
    runtime_root: Path,
    *,
    bindings: Sequence[ComponentRuntimeBinding] | None = None,
    overlay: Mapping[str, Mapping[str, Any]] | None = None,
    secrets: Mapping[str, str] | None = None,
    artifact_cache: Path | None = None,
    environment_id: str = "local-test",
) -> DeploymentEnvironment:
    """A deployment environment for the canonical component."""
    bound = tuple(bindings) if bindings is not None else (binding(),)
    overlay_values = (
        dict(overlay)
        if overlay is not None
        else {COMPONENT_ID: {"platform_id": PLATFORM_ID, "environment": "test"}}
    )
    return DeploymentEnvironment(
        environment_id=environment_id,
        runtime_root=runtime_root,
        bindings=bound,
        configuration_overlay=overlay_values,
        secrets=dict(secrets or {}),
        artifact_cache=artifact_cache,
    )


def request_for(
    instance: Instance,
    manifest: Mapping[str, Any],
    environment: DeploymentEnvironment,
    *,
    attempt: int = 1,
    reference: InstanceReference | None = None,
) -> DeploymentRequest:
    """The canonical deployment request of the accepted instance."""
    return DeploymentRequest(
        instance=reference or InstanceReference.from_document(instance.document),
        instance_document=instance.document,
        manifest_document=manifest,
        environment=environment,
        attempt=attempt,
    )


def operation_identity(
    environment: DeploymentEnvironment, document: object, *, attempt: int = 1
) -> str:
    """The deployment operation identity of one desired-state document."""
    from deployment_operations import derive_deployment_id

    entry = document.document if isinstance(document, Instance) else document
    assert isinstance(entry, Mapping), "an instance document is required"
    return derive_deployment_id(
        entry.get("platform_id"),
        entry.get("instance_digest"),
        environment.environment_id,
        attempt,
    )


def recorded_operations(environment: DeploymentEnvironment) -> tuple[Path, ...]:
    """Every deployment state record this environment holds, in order."""
    if not environment.operations_dir.is_dir():
        return ()
    return tuple(sorted(environment.operations_dir.glob("*.json")))


def only_operation(environment: DeploymentEnvironment):
    """The single deployment state record of an environment.

    Used where the operation was refused before its input could be trusted —
    for a tampered or name-only reference the operation identity cannot be
    derived from the (untrusted) instance, so the record is found by the fact
    that the environment holds exactly one.
    """
    from deployment_operations import load_record

    paths = recorded_operations(environment)
    assert len(paths) == 1, f"expected exactly one deployment operation, found {paths}"
    return load_record(paths[0])


def only_journal(environment: DeploymentEnvironment) -> tuple[dict, ...]:
    """The single operational journal of an environment."""
    if not environment.operations_dir.is_dir():
        return ()
    paths = sorted(environment.operations_dir.glob("*.events.jsonl"))
    assert len(paths) == 1, f"expected exactly one operational journal, found {paths}"
    return tuple(
        json.loads(line)
        for line in paths[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def read_state(
    environment: DeploymentEnvironment, instance: Instance, *, attempt: int = 1
):
    """Read the deployment state record written by an operation."""
    from deployment_operations import DeploymentStateStore, derive_deployment_id

    deployment_id = derive_deployment_id(
        instance.document.get("platform_id"),
        instance.instance_digest,
        environment.environment_id,
        attempt,
    )
    path = DeploymentStateStore.path_for(environment.operations_dir, deployment_id)
    assert path.is_file(), f"deployment state was not written at {path}"
    return load_record(path)


def read_events(
    environment: DeploymentEnvironment, instance: Instance, *, attempt: int = 1
):
    """Read the operational event journal of an operation."""
    from deployment_operations import EventJournal, derive_deployment_id

    deployment_id = derive_deployment_id(
        instance.document.get("platform_id"),
        instance.instance_digest,
        environment.environment_id,
        attempt,
    )
    path = EventJournal.path_for(environment.operations_dir, deployment_id)
    assert path.is_file(), f"the event journal was not written at {path}"
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def tampered_instance(instance: Instance, **changes: Any) -> dict[str, Any]:
    """Return a copy of an instance document with a recomputed digest.

    Recomputing the digest makes the tampering invisible to a digest check
    alone: what must catch it is the exact binding to the authoritative
    manifest and the deployment-boundary verification (AC2, AC13).
    """
    from platform_instance import compute_instance_digest

    document = copy.deepcopy(dict(instance.document))
    document.update(changes)
    document["instance_digest"] = compute_instance_digest(document)
    return document


def file_digests(root: Path) -> dict[str, str]:
    """Content digests of every file under ``root``, for immutability checks.

    Build and cache artifacts are skipped; everything else — authoritative
    factory metadata, component sources, contracts, tests — is covered.
    """
    import hashlib

    ignored = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if ignored & set(path.parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        digests[str(path.relative_to(root))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return digests
