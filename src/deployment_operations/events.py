"""Operational observability of the deployment boundary (ADR-0016 §19).

Every operational signal produced here is correlatable with the Platform
Instance it relates to: the operation identity, the environment, the platform
identity, the Manifest identity/version/digest and the instance digest are on
every event, and every component-scoped event additionally carries the
component, its version and its artifact identity (ADR-0016 §8, §19; AC15).

The journal is append-only JSON lines: one event per line, in the order the
deployment path produced it, so a failure is diagnosable from the recorded
sequence alone. Events are operational metadata — never business data, never
component content, and never secret material (ADR-0016 §6, §12): the journal
refuses a write that would contain a secret the boundary injected.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deployment_operations.errors import DeploymentStateError
from deployment_operations.state import refuse_secret_material

#: Lifecycle of one deployment operation, as observed through events.
EVENT_DEPLOYMENT_REQUESTED = "deployment_requested"
EVENT_STAGE_STARTED = "stage_started"
EVENT_STAGE_COMPLETED = "stage_completed"
EVENT_COMPONENT_MATERIALIZED = "component_materialized"
EVENT_MIGRATION_STARTED = "migration_started"
EVENT_MIGRATION_COMPLETED = "migration_completed"
EVENT_MIGRATION_FAILED = "migration_failed"
EVENT_RUNTIME_STARTED = "runtime_started"
EVENT_HEALTH_CHECKED = "health_checked"
EVENT_READY_REACHED = "ready_reached"
EVENT_IDENTITY_VERIFIED = "identity_verified"
EVENT_DEPLOYMENT_REALIZED = "deployment_realized"
EVENT_DEPLOYMENT_FAILED = "deployment_failed"
EVENT_PLATFORM_STOPPED = "platform_stopped"


@dataclass(frozen=True)
class DeploymentEvent:
    """One operational signal of the deployment boundary (ADR-0016 §19)."""

    sequence: int
    event: str
    occurred_at: str
    deployment_id: str
    environment_id: str
    platform_id: str | None = None
    instance_digest: str | None = None
    manifest_id: str | None = None
    manifest_version: str | None = None
    manifest_digest: str | None = None
    stage: str | None = None
    component_id: str | None = None
    component_version: str | None = None
    artifact_type: str | None = None
    artifact_digest: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def correlation(self) -> dict[str, Any]:
        """The correlation tuple every signal carries (AC15, ADR-0016 §19)."""
        return {
            "deployment_id": self.deployment_id,
            "platform_id": self.platform_id,
            "environment_id": self.environment_id,
            "instance_digest": self.instance_digest,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_digest": self.manifest_digest,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "artifact_type": self.artifact_type,
            "artifact_digest": self.artifact_digest,
        }

    def document(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event": self.event,
            "occurred_at": self.occurred_at,
            "deployment_id": self.deployment_id,
            "environment_id": self.environment_id,
            "platform_instance": {
                "platform_id": self.platform_id,
                "instance_digest": self.instance_digest,
            },
            "manifest": {
                "manifest_id": self.manifest_id,
                "manifest_version": self.manifest_version,
                "manifest_digest": self.manifest_digest,
            },
            "stage": self.stage,
            "component": {
                "component_id": self.component_id,
                "component_version": self.component_version,
                "artifact_type": self.artifact_type,
                "artifact_digest": self.artifact_digest,
            },
            "detail": dict(self.detail),
        }

    def render(self) -> str:
        return json.dumps(self.document(), ensure_ascii=False, sort_keys=True)


@dataclass
class EventJournal:
    """Append-only journal of the operational signals of one deployment."""

    path: Path

    @staticmethod
    def path_for(operations_dir: Path, deployment_id: str) -> Path:
        return operations_dir / f"{deployment_id}.events.jsonl"

    def next_sequence(self) -> int:
        if not self.path.is_file():
            return 1
        try:
            with self.path.open(encoding="utf-8") as stream:
                return sum(1 for line in stream if line.strip()) + 1
        except OSError as error:
            message = (
                f"the operational journal is unreadable: {self.path} "
                f"({error.__class__.__name__}: {error})"
            )
            raise DeploymentStateError(message) from error

    def append(self, event: DeploymentEvent, *, secrets: Sequence[str] = ()) -> None:
        line = event.render() + "\n"
        refuse_secret_material(line, secrets)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            message = (
                f"the operational journal could not be written: {self.path} "
                f"({error.__class__.__name__}: {error})"
            )
            raise DeploymentStateError(message) from error

    def events(self) -> tuple[DeploymentEvent, ...]:
        if not self.path.is_file():
            return ()
        collected: list[DeploymentEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            document = json.loads(line)
            collected.append(event_from_document(document))
        return tuple(collected)


def event_from_document(document: Mapping[str, Any]) -> DeploymentEvent:
    """Rebuild an event from its journal line."""
    platform = document.get("platform_instance")
    instance: Mapping[str, Any] = platform if isinstance(platform, Mapping) else {}
    manifest = document.get("manifest")
    bound: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}
    component = document.get("component")
    entry: Mapping[str, Any] = component if isinstance(component, Mapping) else {}
    detail = document.get("detail")
    return DeploymentEvent(
        sequence=int(document.get("sequence", 0)),
        event=str(document.get("event", "")),
        occurred_at=str(document.get("occurred_at", "")),
        deployment_id=str(document.get("deployment_id", "")),
        environment_id=str(document.get("environment_id", "")),
        platform_id=instance.get("platform_id"),
        instance_digest=instance.get("instance_digest"),
        manifest_id=bound.get("manifest_id"),
        manifest_version=bound.get("manifest_version"),
        manifest_digest=bound.get("manifest_digest"),
        stage=document.get("stage"),
        component_id=entry.get("component_id"),
        component_version=entry.get("component_version"),
        artifact_type=entry.get("artifact_type"),
        artifact_digest=entry.get("artifact_digest"),
        detail=dict(detail) if isinstance(detail, Mapping) else {},
    )


__all__ = [
    "EVENT_COMPONENT_MATERIALIZED",
    "EVENT_DEPLOYMENT_FAILED",
    "EVENT_DEPLOYMENT_REALIZED",
    "EVENT_DEPLOYMENT_REQUESTED",
    "EVENT_HEALTH_CHECKED",
    "EVENT_IDENTITY_VERIFIED",
    "EVENT_MIGRATION_COMPLETED",
    "EVENT_MIGRATION_FAILED",
    "EVENT_MIGRATION_STARTED",
    "EVENT_PLATFORM_STOPPED",
    "EVENT_READY_REACHED",
    "EVENT_RUNTIME_STARTED",
    "EVENT_STAGE_COMPLETED",
    "EVENT_STAGE_STARTED",
    "DeploymentEvent",
    "EventJournal",
    "event_from_document",
]
