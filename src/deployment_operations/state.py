"""Deployment state — the authoritative operational record of actual state.

Deployment state belongs to this capability and is the only authoritative
answer to «what is actually running where» (ADR-0016 §9). It is operational
metadata, never business data (§6), and it is honest by requirement:

* it never reports ``realized`` for something that was not verified (§9, §10);
* every stage of the initial deployment path (ADR-0017 §36) is recorded with
  its outcome, so a failure is attributable instead of being flattened into a
  generic error;
* it references the Platform Instance by identity, version and digest, and
  every component record carries component, version and artifact identity;
* the transitions themselves enforce the rule — :meth:`DeploymentRecord.mark_realized`
  refuses to fix ``realized`` unless the platform is ``ready`` *and* its
  identity/version/digest verification has confirmed (ADR-0017 §34). A
  premature ``deployed`` is therefore not merely discouraged: it cannot be
  represented.

Three notions are kept apart, exactly as ADR-0017 §35 fixes them:

* **operational condition** — ``ready`` (health/readiness verified) and
  ``running`` (the runtime elements are up);
* **lifecycle position** — ``in_progress``, ``realized`` or ``failed``;
* **the record** — this document, which is deployment state itself.

``superseded`` and ``rolled_back`` belong to ADR-0016 §9 as well but are
deferred to later slices (ADR-0017 §39); this slice never fabricates them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deployment_operations.errors import (
    DeploymentStateError,
    InvalidDeploymentStateTransition,
    SecretLeakRefused,
)

#: The observable stages of the initial deployment path, in order
#: (ADR-0017 §36–§37). This is the minimal observable sequence of one
#: deployment operation — not a new global lifecycle.
STAGES: tuple[str, ...] = (
    "requested",
    "validated",
    "provisioning",
    "deploying",
    "starting",
    "health_check",
    "ready",
)

STAGE_PENDING = "pending"
STAGE_IN_PROGRESS = "in_progress"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"

#: Lifecycle positions of the deployment operation (ADR-0016 §9) that this
#: slice implements. ``superseded`` and ``rolled_back`` are deferred (§39).
LIFECYCLE_IN_PROGRESS = "in_progress"
LIFECYCLE_REALIZED = "realized"
LIFECYCLE_FAILED = "failed"

Clock = Callable[[], str]


def utc_now() -> str:
    """Return the current UTC instant in ISO-8601, second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def derive_deployment_id(
    platform_id: str | None,
    instance_digest: str | None,
    environment_id: str,
    attempt: int,
) -> str:
    """Derive the deterministic identity of one deployment operation (§8).

    The identity is derived from the exact instance digest, the environment
    and the attempt number — never from time or randomness — so the same
    deployment input always names the same operation. ``attempt`` keeps the
    attempt history of ADR-0016 §9 honest instead of overwriting it.
    """
    digest_part = "unverified"
    if isinstance(instance_digest, str) and instance_digest:
        digest_part = instance_digest.removeprefix("sha256:")[:12]
    platform_part = (
        platform_id if isinstance(platform_id, str) and platform_id else "unidentified"
    )
    return f"dep-{platform_part}-{environment_id}-{digest_part}-a{attempt}"


@dataclass(frozen=True)
class StageRecord:
    """The recorded outcome of one observable stage of the path."""

    name: str
    status: str = STAGE_PENDING
    detail: Mapping[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": dict(self.detail)}


@dataclass(frozen=True)
class ComponentRecord:
    """What deployment state knows about one component of the instance.

    ``observed_*`` fields hold what the running component itself reported over
    its published surface — the actual state, not what was requested.
    """

    component_id: str
    component_version: str
    artifact_type: str
    artifact_digest: str | None
    materialized: bool = False
    artifact_verified: bool = False
    runtime_started: bool = False
    observed_component_id: str | None = None
    observed_version: str | None = None
    observed_platform_id: str | None = None
    health: Mapping[str, Any] | None = None
    readiness: Mapping[str, Any] | None = None
    healthy: bool | None = None
    error: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "component_version": self.component_version,
            "artifact": {
                "artifact_type": self.artifact_type,
                "digest": self.artifact_digest,
            },
            "materialized": self.materialized,
            "artifact_verified": self.artifact_verified,
            "runtime_started": self.runtime_started,
            "observed_component_id": self.observed_component_id,
            "observed_version": self.observed_version,
            "observed_platform_id": self.observed_platform_id,
            "health": dict(self.health) if self.health else None,
            "readiness": dict(self.readiness) if self.readiness else None,
            "healthy": self.healthy,
            "error": self.error,
        }


@dataclass(frozen=True)
class MigrationRecord:
    """One component-defined migration executed under orchestration (§14)."""

    component_id: str
    component_version: str
    migration_id: str
    status: str
    error: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "component_version": self.component_version,
            "migration_id": self.migration_id,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class OperationalAction:
    """An operational action reflected in deployment state (ADR-0016 §18)."""

    name: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: str = ""

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "detail": dict(self.detail),
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class FailureRecord:
    """An honest record of where and why the operation stopped (§20)."""

    stage: str
    reason: str
    errors: tuple[str, ...] = ()
    occurred_at: str = ""

    def document(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "reason": self.reason,
            "errors": list(self.errors),
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class DeploymentRecord:
    """Deployment state of exactly one deployment operation."""

    deployment_id: str
    environment_id: str
    attempt: int = 1
    platform_id: str | None = None
    manifest_id: str | None = None
    manifest_version: str | None = None
    manifest_digest: str | None = None
    manifest_state: str | None = None
    instance_digest: str | None = None
    lifecycle: str = LIFECYCLE_IN_PROGRESS
    ready: bool = False
    running: bool = False
    identity_verified: bool = False
    stages: tuple[StageRecord, ...] = ()
    components: tuple[ComponentRecord, ...] = ()
    migrations: tuple[MigrationRecord, ...] = ()
    operational_actions: tuple[OperationalAction, ...] = ()
    failure: FailureRecord | None = None
    created_at: str = ""
    updated_at: str = ""

    # -- construction ------------------------------------------------------
    @classmethod
    def initial(
        cls,
        *,
        deployment_id: str,
        environment_id: str,
        attempt: int,
        platform_id: str | None,
        manifest_id: str | None,
        manifest_version: str | None,
        manifest_digest: str | None,
        manifest_state: str | None,
        instance_digest: str | None,
        at: str,
    ) -> DeploymentRecord:
        return cls(
            deployment_id=deployment_id,
            environment_id=environment_id,
            attempt=attempt,
            platform_id=platform_id,
            manifest_id=manifest_id,
            manifest_version=manifest_version,
            manifest_digest=manifest_digest,
            manifest_state=manifest_state,
            instance_digest=instance_digest,
            lifecycle=LIFECYCLE_IN_PROGRESS,
            ready=False,
            running=False,
            identity_verified=False,
            stages=tuple(StageRecord(name=name) for name in STAGES),
            components=(),
            migrations=(),
            operational_actions=(),
            created_at=at,
            updated_at=at,
        )

    # -- derived facts -----------------------------------------------------
    @property
    def deployed(self) -> bool:
        """True only for a verified, honest ``deployed`` claim (ADR-0016 §10).

        ``deployed`` is a claim about an **actual, observable Running
        Platform**: the instance was realized on it (``lifecycle``), it is
        running now (``running``), the verified operational condition holds
        (``ready``, ADR-0017 §33) and what is actually running was verified
        against the pinned instance (``identity_verified``). Each of these is
        necessary — ``ready`` alone is not sufficient (§34) — and a platform
        whose runtime elements are no longer running holds no such claim.
        """
        return (
            self.lifecycle == LIFECYCLE_REALIZED
            and self.running
            and self.ready
            and self.identity_verified
            and self.failure is None
        )

    def stage(self, name: str) -> StageRecord:
        for entry in self.stages:
            if entry.name == name:
                return entry
        message = f"{name!r} is not a stage of the initial deployment path"
        raise DeploymentStateError(message)

    def component(self, component_id: str) -> ComponentRecord | None:
        for entry in self.components:
            if entry.component_id == component_id:
                return entry
        return None

    # -- transitions -------------------------------------------------------
    def with_stage(
        self,
        name: str,
        status: str,
        *,
        at: str,
        detail: Mapping[str, Any] | None = None,
    ) -> DeploymentRecord:
        """Record the outcome of one stage of the path."""
        entry = self.stage(name)
        updated = replace(
            entry, status=status, detail=dict(detail) if detail else dict(entry.detail)
        )
        stages = tuple(updated if item.name == name else item for item in self.stages)
        return replace(self, stages=stages, updated_at=at)

    def with_components(
        self, components: Sequence[ComponentRecord], *, at: str
    ) -> DeploymentRecord:
        return replace(self, components=tuple(components), updated_at=at)

    def with_component(
        self, component_id: str, *, at: str, **changes: Any
    ) -> DeploymentRecord:
        """Update one component record with verified observations."""
        components: list[ComponentRecord] = []
        found = False
        for entry in self.components:
            if entry.component_id == component_id:
                components.append(replace(entry, **changes))
                found = True
            else:
                components.append(entry)
        if not found:
            message = (
                f"component {component_id!r} is not part of this deployment "
                "operation; the record never invents components"
            )
            raise DeploymentStateError(message)
        return replace(self, components=tuple(components), updated_at=at)

    def with_migration(
        self, migration: MigrationRecord, *, at: str
    ) -> DeploymentRecord:
        return replace(self, migrations=(*self.migrations, migration), updated_at=at)

    def with_operational_action(
        self, name: str, *, at: str, detail: Mapping[str, Any] | None = None
    ) -> DeploymentRecord:
        action = OperationalAction(
            name=name, detail=dict(detail) if detail else {}, occurred_at=at
        )
        return replace(
            self, operational_actions=(*self.operational_actions, action), updated_at=at
        )

    def mark_running(self, *, at: str, running: bool) -> DeploymentRecord:
        return replace(self, running=running, updated_at=at)

    def mark_stopped(self, *, at: str) -> DeploymentRecord:
        """Record that the platform's runtime elements are no longer running (§18).

        An operational action, not a lifecycle transition: ``realized`` states
        what this deployment operation did — it realized the verified instance —
        while the operational conditions state how the platform is **now**. A
        stopped platform is running nothing, holds no verified operational
        condition (``ready`` is a condition *of the Running Platform*, §33), and
        therefore holds no ``deployed`` claim (§10). Nothing here invents a
        lifecycle position: ``stopped`` is not one of the positions established
        by ADR-0016 §9, and this record does not create one.
        """
        if not self.running:
            return self
        stopped = replace(self, running=False, ready=False, updated_at=at)
        return stopped.with_operational_action("platform_stopped", at=at)

    def mark_ready(self, *, at: str) -> DeploymentRecord:
        """Record ``ready`` — a verified operational condition (ADR-0017 §33).

        Reaching ``ready`` fixes no deployment claim by itself: it is recorded
        here and only the identity/version/digest verification may follow
        (ADR-0017 §34).
        """
        return replace(self, ready=True, updated_at=at)

    def mark_identity_verified(self, *, at: str) -> DeploymentRecord:
        """Record that the running platform was verified against the instance."""
        if not self.ready:
            message = (
                "identity/version/digest verification cannot be recorded for a "
                "platform that is not ready"
            )
            raise InvalidDeploymentStateTransition(message)
        return replace(self, identity_verified=True, updated_at=at)

    def mark_realized(self, *, at: str) -> DeploymentRecord:
        """Fix ``realized`` — only after verification (ADR-0016 §9, §10).

        Both dimensions are required simultaneously: the verified operational
        condition and the exact identity/version/digest verification. Neither
        may be dropped, and so neither may be missing here.
        """
        missing: list[str] = []
        if not self.ready:
            missing.append(
                "health/readiness was not verified (the platform is not ready)"
            )
        if not self.identity_verified:
            missing.append(
                "identity/version/digest verification against the instance has "
                "not confirmed"
            )
        if self.failure is not None:
            missing.append("the operation already recorded a failure")
        if missing:
            message = (
                f"deployment {self.deployment_id!r} cannot be marked realized: "
                + "; ".join(missing)
            )
            raise InvalidDeploymentStateTransition(message, errors=missing)
        return replace(self, lifecycle=LIFECYCLE_REALIZED, updated_at=at)

    def mark_failed(
        self,
        *,
        stage: str,
        reason: str,
        errors: Sequence[str],
        at: str,
    ) -> DeploymentRecord:
        """Record a fail-closed failure at the stage where it happened (§20)."""
        if self.lifecycle == LIFECYCLE_REALIZED:
            message = (
                f"deployment {self.deployment_id!r} is already realized; this "
                "slice implements no post-realization failure states"
            )
            raise InvalidDeploymentStateTransition(message)
        failure = FailureRecord(
            stage=stage, reason=reason, errors=tuple(errors), occurred_at=at
        )
        stages = tuple(
            (
                replace(entry, status=STAGE_FAILED)
                if entry.name == stage and entry.status != STAGE_COMPLETED
                else entry
            )
            for entry in self.stages
        )
        return replace(
            self,
            lifecycle=LIFECYCLE_FAILED,
            failure=failure,
            stages=stages,
            updated_at=at,
        )

    # -- serialization -----------------------------------------------------
    def document(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "environment_id": self.environment_id,
            "attempt": self.attempt,
            "platform_instance": {
                "platform_id": self.platform_id,
                "instance_digest": self.instance_digest,
                "manifest_id": self.manifest_id,
                "manifest_version": self.manifest_version,
                "manifest_digest": self.manifest_digest,
                "manifest_state": self.manifest_state,
            },
            "lifecycle": self.lifecycle,
            "conditions": {
                "ready": self.ready,
                "running": self.running,
                "identity_verified": self.identity_verified,
                "deployed": self.deployed,
            },
            "stages": [entry.document() for entry in self.stages],
            "components": [entry.document() for entry in self.components],
            "migrations": [entry.document() for entry in self.migrations],
            "operational_actions": [
                entry.document() for entry in self.operational_actions
            ],
            "failure": self.failure.document() if self.failure else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def render(self) -> str:
        return json.dumps(self.document(), indent=2, ensure_ascii=False, sort_keys=True)


def refuse_secret_material(text: str, secrets: Sequence[str]) -> None:
    """Fail closed rather than persist secret material (ADR-0016 §12)."""
    leaked = [name for name in secrets if name and name in text]
    if leaked:
        message = (
            "the deployment record or event about to be written contains "
            f"{len(leaked)} operational secret(s); secrets are never copied into "
            "records, logs or signals"
        )
        errors = [message]
        raise SecretLeakRefused(errors)


@dataclass(frozen=True)
class DeploymentStateStore:
    """File-backed storage of deployment state (ADR-0016 §9).

    One file per deployment operation, written atomically. The store holds
    operational metadata only: it is never component data, never business data,
    and never secret material.
    """

    path: Path

    @staticmethod
    def path_for(operations_dir: Path, deployment_id: str) -> Path:
        return operations_dir / f"{deployment_id}.json"

    def exists(self) -> bool:
        return self.path.is_file()

    def write(self, record: DeploymentRecord, *, secrets: Sequence[str] = ()) -> None:
        text = record.render() + "\n"
        refuse_secret_material(text, secrets)
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            # An operation that cannot record its own state must refuse loudly
            # rather than leave an unrecorded deployment behind (ADR-0016 §9, §20).
            message = (
                f"deployment state could not be written: {self.path} "
                f"({error.__class__.__name__}: {error})"
            )
            raise DeploymentStateError(message) from error

    def read(self) -> DeploymentRecord:
        if not self.path.is_file():
            message = f"deployment state not found: {self.path}"
            raise DeploymentStateError(message)
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            message = (
                f"deployment state is unreadable: {self.path} "
                f"({error.__class__.__name__})"
            )
            raise DeploymentStateError(message) from error
        if not isinstance(document, Mapping):
            message = f"deployment state is not a JSON object: {self.path}"
            raise DeploymentStateError(message)
        return record_from_document(document)


def record_from_document(document: Mapping[str, Any]) -> DeploymentRecord:
    """Rebuild a deployment record from its persisted document."""
    platform = document.get("platform_instance")
    entry: Mapping[str, Any] = platform if isinstance(platform, Mapping) else {}
    conditions = document.get("conditions")
    state: Mapping[str, Any] = conditions if isinstance(conditions, Mapping) else {}
    failure = document.get("failure")
    stages = document.get("stages")
    components = document.get("components")
    migrations = document.get("migrations")
    actions = document.get("operational_actions")

    return DeploymentRecord(
        deployment_id=str(document.get("deployment_id", "")),
        environment_id=str(document.get("environment_id", "")),
        attempt=int(document.get("attempt", 1)),
        platform_id=entry.get("platform_id"),
        manifest_id=entry.get("manifest_id"),
        manifest_version=entry.get("manifest_version"),
        manifest_digest=entry.get("manifest_digest"),
        manifest_state=entry.get("manifest_state"),
        instance_digest=entry.get("instance_digest"),
        lifecycle=str(document.get("lifecycle", LIFECYCLE_IN_PROGRESS)),
        ready=bool(state.get("ready", False)),
        running=bool(state.get("running", False)),
        identity_verified=bool(state.get("identity_verified", False)),
        stages=(
            tuple(
                StageRecord(
                    name=str(item.get("name", "")),
                    status=str(item.get("status", STAGE_PENDING)),
                    detail=(
                        item.get("detail", {})
                        if isinstance(item.get("detail"), Mapping)
                        else {}
                    ),
                )
                for item in stages
                if isinstance(item, Mapping)
            )
            if isinstance(stages, list)
            else ()
        ),
        components=(
            tuple(
                ComponentRecord(
                    component_id=str(item.get("component_id", "")),
                    component_version=str(item.get("component_version", "")),
                    artifact_type=str(
                        (item.get("artifact") or {}).get("artifact_type", "")
                    ),
                    artifact_digest=(item.get("artifact") or {}).get("digest"),
                    materialized=bool(item.get("materialized", False)),
                    artifact_verified=bool(item.get("artifact_verified", False)),
                    runtime_started=bool(item.get("runtime_started", False)),
                    observed_component_id=item.get("observed_component_id"),
                    observed_version=item.get("observed_version"),
                    observed_platform_id=item.get("observed_platform_id"),
                    health=item.get("health"),
                    readiness=item.get("readiness"),
                    healthy=item.get("healthy"),
                    error=item.get("error"),
                )
                for item in components
                if isinstance(item, Mapping)
            )
            if isinstance(components, list)
            else ()
        ),
        migrations=(
            tuple(
                MigrationRecord(
                    component_id=str(item.get("component_id", "")),
                    component_version=str(item.get("component_version", "")),
                    migration_id=str(item.get("migration_id", "")),
                    status=str(item.get("status", "")),
                    error=item.get("error"),
                )
                for item in migrations
                if isinstance(item, Mapping)
            )
            if isinstance(migrations, list)
            else ()
        ),
        operational_actions=(
            tuple(
                OperationalAction(
                    name=str(item.get("name", "")),
                    detail=(
                        item.get("detail", {})
                        if isinstance(item.get("detail"), Mapping)
                        else {}
                    ),
                    occurred_at=str(item.get("occurred_at", "")),
                )
                for item in actions
                if isinstance(item, Mapping)
            )
            if isinstance(actions, list)
            else ()
        ),
        failure=(
            FailureRecord(
                stage=str(failure.get("stage", "")),
                reason=str(failure.get("reason", "")),
                errors=tuple(str(item) for item in failure.get("errors", [])),
                occurred_at=str(failure.get("occurred_at", "")),
            )
            if isinstance(failure, Mapping)
            else None
        ),
        created_at=str(document.get("created_at", "")),
        updated_at=str(document.get("updated_at", "")),
    )


def load_record(path: Path) -> DeploymentRecord:
    """Read a deployment record from a state file."""
    return DeploymentStateStore(path=path).read()


__all__ = [
    "LIFECYCLE_FAILED",
    "LIFECYCLE_IN_PROGRESS",
    "LIFECYCLE_REALIZED",
    "STAGES",
    "STAGE_COMPLETED",
    "STAGE_FAILED",
    "STAGE_IN_PROGRESS",
    "STAGE_PENDING",
    "ComponentRecord",
    "DeploymentRecord",
    "DeploymentStateStore",
    "FailureRecord",
    "MigrationRecord",
    "OperationalAction",
    "StageRecord",
    "derive_deployment_id",
    "load_record",
    "record_from_document",
    "refuse_secret_material",
    "utc_now",
]
