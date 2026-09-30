"""Upgrade orchestration for one realized Platform Instance (ADR-0017)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from deployment_operations.deployment import Deployment, DeploymentRequest, deploy
from deployment_operations.errors import DeploymentInputRejected, InvalidDeploymentStateTransition
from deployment_operations.events import (
    EVENT_OLD_INSTANCE_SUPERSEDED,
    EVENT_NEW_IDENTITY_VERIFIED,
    EVENT_UPGRADE_COMPLETED,
    EVENT_UPGRADE_FAILED,
    EVENT_UPGRADE_REQUESTED,
    DeploymentEvent,
    EventJournal,
)
from deployment_operations.state import DeploymentStateStore, utc_now


@dataclass(frozen=True)
class UpgradeRequest:
    """One explicit old-realized -> new-accepted upgrade."""

    current: Deployment
    replacement: DeploymentRequest
    upgrade_id: str


def derive_upgrade_id(current: Deployment, replacement: DeploymentRequest) -> str:
    """Derive a stable correlation id from both deployment identities."""
    return (
        f"{current.record.deployment_id}"
        f"->platform:{replacement.instance.instance_digest}"
        f"@{replacement.environment.environment_id}"
    )


def _append_upgrade_event(
    journal: EventJournal,
    *,
    deployment: Deployment,
    event: str,
    upgrade_id: str,
    detail: dict[str, Any],
) -> None:
    record = deployment.record
    journal.append(
        DeploymentEvent(
            sequence=journal.next_sequence(),
            event=event,
            occurred_at=utc_now(),
            deployment_id=record.deployment_id,
            environment_id=record.environment_id,
            platform_id=record.platform_id,
            instance_digest=record.instance_digest,
            manifest_id=record.manifest_id,
            manifest_version=record.manifest_version,
            manifest_digest=record.manifest_digest,
            stage="upgrade",
            detail={"upgrade_id": upgrade_id, **detail},
        )
    )


def upgrade(
    request: UpgradeRequest,
    *,
    runtime: Any = None,
    identity_provider: Any = None,
) -> Deployment:
    """Deploy the replacement, accept its actual identity, then supersede the old one."""
    current = request.current
    replacement = request.replacement
    old = current.record
    new_ref = replacement.instance
    upgrade_id = request.upgrade_id

    if not current.deployed:
        raise InvalidDeploymentStateTransition(
            "upgrade requires the current deployment to be an honest deployed state"
        )
    if old.environment_id != replacement.environment.environment_id:
        raise DeploymentInputRejected(
            ["upgrade must keep the same deployment environment"]
        )
    if old.instance_digest == new_ref.instance_digest:
        raise DeploymentInputRejected(
            ["upgrade replacement must identify a different Platform Instance"]
        )
    if not new_ref.instance_digest.strip():
        raise DeploymentInputRejected(
            ["upgrade replacement requires a concrete instance digest"]
        )

    old_journal = EventJournal(
        EventJournal.path_for(
            replacement.environment.operations_dir,
            old.deployment_id,
        )
    )
    _append_upgrade_event(old_journal, deployment=current, event=EVENT_UPGRADE_REQUESTED, upgrade_id=upgrade_id, detail={
        "old_instance_digest": old.instance_digest, "new_instance_digest": new_ref.instance_digest
    })

    try:
        candidate = deploy(replacement, runtime=runtime, identity_provider=identity_provider)
        if not candidate.deployed:
            raise InvalidDeploymentStateTransition("replacement deployment completed without an honest deployed claim")

        new_journal = EventJournal(EventJournal.path_for(replacement.environment.operations_dir, candidate.record.deployment_id))
        _append_upgrade_event(new_journal, deployment=candidate, event=EVENT_NEW_IDENTITY_VERIFIED, upgrade_id=upgrade_id, detail={
            "old_instance_digest": old.instance_digest, "new_instance_digest": candidate.record.instance_digest
        })

        old_store = DeploymentStateStore(current.state_path)
        superseded = old.mark_superseded(at=utc_now(), detail={
            "upgrade_id": upgrade_id,
            "replacement_deployment_id": candidate.record.deployment_id,
            "replacement_instance_digest": candidate.record.instance_digest,
        })
        old_store.write(superseded)
        current.record = superseded

        _append_upgrade_event(old_journal, deployment=replace(current, record=superseded), event=EVENT_OLD_INSTANCE_SUPERSEDED, upgrade_id=upgrade_id, detail={
            "replacement_deployment_id": candidate.record.deployment_id,
            "replacement_instance_digest": candidate.record.instance_digest,
        })
        if hasattr(current, "stop"):
            current.stop()
        _append_upgrade_event(new_journal, deployment=candidate, event=EVENT_UPGRADE_COMPLETED, upgrade_id=upgrade_id, detail={
            "old_deployment_id": old.deployment_id, "old_instance_digest": old.instance_digest
        })
        return candidate
    except Exception:
        _append_upgrade_event(old_journal, deployment=current, event=EVENT_UPGRADE_FAILED, upgrade_id=upgrade_id, detail={
            "old_instance_digest": old.instance_digest, "new_instance_digest": new_ref.instance_digest
        })
        raise


__all__ = ["UpgradeRequest", "derive_upgrade_id", "upgrade"]
