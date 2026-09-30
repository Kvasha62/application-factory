"""Upgrade orchestration for one realized Platform Instance (ADR-0017)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from deployment_operations.deployment import Deployment, DeploymentRequest, deploy
from deployment_operations.errors import (
    DeploymentInputRejected,
    InvalidDeploymentStateTransition,
)
from deployment_operations.events import (
    EVENT_NEW_IDENTITY_VERIFIED,
    EVENT_OLD_INSTANCE_SUPERSEDED,
    EVENT_UPGRADE_COMPLETED,
    EVENT_UPGRADE_FAILED,
    EVENT_UPGRADE_REQUESTED,
    DeploymentEvent,
    EventJournal,
)
from deployment_operations.state import (
    LIFECYCLE_REALIZED,
    DeploymentRecord,
    DeploymentStateStore,
    utc_now,
)

_TRANSITION_GUARD = threading.Lock()
_TRANSITION_LOCKS: dict[Path, threading.Lock] = {}


def _transition_lock(state_path: Path) -> threading.Lock:
    key = Path(state_path).resolve()
    with _TRANSITION_GUARD:
        return _TRANSITION_LOCKS.setdefault(key, threading.Lock())


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


def _journal_for(
    deployment: Any, operations_dir: Path, deployment_id: str
) -> EventJournal:
    journal = getattr(deployment, "_journal", None)
    if journal is not None:
        return journal
    events_path = getattr(deployment, "events_path", None) or EventJournal.path_for(
        operations_dir, deployment_id
    )
    created = EventJournal(events_path)
    if hasattr(deployment, "_journal"):
        try:
            deployment._journal = created
        except Exception:  # noqa: BLE001, S110
            pass
    return created


def _append_upgrade_event(
    journal: EventJournal,
    *,
    deployment: Deployment,
    event: str,
    upgrade_id: str,
    detail: dict[str, Any],
    record: DeploymentRecord | None = None,
) -> None:
    subject = record if record is not None else deployment.record
    secrets = getattr(deployment, "_secrets", ()) or ()
    journal.append(
        DeploymentEvent(
            sequence=journal.next_sequence(),
            event=event,
            occurred_at=utc_now(),
            deployment_id=subject.deployment_id,
            environment_id=subject.environment_id,
            platform_id=subject.platform_id,
            instance_digest=subject.instance_digest,
            manifest_id=subject.manifest_id,
            manifest_version=subject.manifest_version,
            manifest_digest=subject.manifest_digest,
            stage="upgrade",
            detail={"upgrade_id": upgrade_id, **detail},
        ),
        secrets=secrets,
    )


def _read_persisted(
    state_path: Path,
    *,
    unavailable_message: str = "the persisted current deployment state is unavailable",
) -> DeploymentRecord:
    try:
        return DeploymentStateStore(state_path).read()
    except Exception as error:
        raise InvalidDeploymentStateTransition(unavailable_message) from error


def _write_state(deployment: Any, record: DeploymentRecord) -> None:
    secrets = getattr(deployment, "_secrets", ()) or ()
    store = getattr(deployment, "_store", None)
    if store is not None:
        store.write(record, secrets=secrets)
    else:
        DeploymentStateStore(deployment.state_path).write(record, secrets=secrets)


def _sync_deployed_attr(deployment: Any) -> None:
    if not isinstance(getattr(type(deployment), "deployed", None), property):
        try:
            deployment.deployed = bool(deployment.record.deployed)
        except Exception:  # noqa: BLE001, S110
            pass


def _stop_deployment_runtime(deployment: Any) -> None:
    if hasattr(deployment, "stop") and callable(deployment.stop):
        deployment.stop()
        return
    runtime = getattr(deployment, "_runtime", None)
    if runtime is not None:
        for handle in getattr(deployment, "_handles", ()) or ():
            runtime.stop(handle)


def _cleanup_candidate(
    candidate: Any,
    *,
    causal_error: BaseException | None = None,
) -> Exception | None:
    stop_error: Exception | None = None
    persistence_error: Exception | None = None
    try:
        _stop_deployment_runtime(candidate)
    except Exception as error:  # noqa: BLE001 - classified below
        record_after_stop = getattr(candidate, "record", None)
        runtime_stopped = (
            isinstance(record_after_stop, DeploymentRecord)
            and not record_after_stop.running
            and not record_after_stop.ready
            and any(
                action.name == "platform_stopped"
                for action in record_after_stop.operational_actions
            )
        )
        if runtime_stopped:
            persistence_error = error
        else:
            stop_error = error
    record = getattr(candidate, "record", None)
    if isinstance(record, DeploymentRecord):
        if stop_error is None and (record.running or record.ready):
            record = record.mark_stopped(at=utc_now())
            candidate.record = record
        elif stop_error is not None and record.ready:
            record = record.withdraw_ready(
                at=utc_now(),
                reason="upgrade_aborted_candidate_stop_failed",
            )
            candidate.record = record
        has_state_target = getattr(candidate, "_store", None) is not None or hasattr(
            candidate, "state_path"
        )
        if has_state_target:
            if persistence_error is None:
                try:
                    persisted_candidate = _read_persisted(candidate.state_path)
                except Exception:  # noqa: BLE001
                    persisted_candidate = None
                if persisted_candidate != candidate.record:
                    try:
                        _write_state(candidate, candidate.record)
                        persisted_candidate = _read_persisted(
                            candidate.state_path,
                            unavailable_message=(
                                "the cleaned-up candidate deployment state "
                                "could not be confirmed"
                            ),
                        )
                        if (
                            persisted_candidate != candidate.record
                            or persisted_candidate.deployed
                        ):
                            raise InvalidDeploymentStateTransition(
                                "the cleaned-up candidate deployment state "
                                "still claims deployed"
                            )
                    except Exception as error:  # noqa: BLE001
                        persistence_error = error
            if persistence_error is not None:
                state_path = getattr(candidate, "state_path", None)
                if state_path is not None:
                    try:
                        Path(state_path).unlink(missing_ok=True)
                    except OSError as unlink_error:
                        persistence_error.add_note(
                            "failed to remove stale candidate state file "
                            f"{state_path}: {unlink_error}"
                        )
    _sync_deployed_attr(candidate)
    if persistence_error is not None:
        if causal_error is not None:
            causal_error.add_note(
                "candidate cleanup state persistence failed: "
                f"{persistence_error.__class__.__name__}: {persistence_error}"
            )
            return persistence_error
        raise persistence_error
    return None


def upgrade(
    request: UpgradeRequest,
    *,
    runtime: Any = None,
    identity_provider: Any = None,
) -> Deployment:
    """Deploy the replacement, accept identity, then supersede the old one."""
    current = request.current
    replacement = request.replacement
    old = current.record
    new_ref = replacement.instance
    upgrade_id = request.upgrade_id

    if not current.deployed or not old.deployed:
        raise InvalidDeploymentStateTransition(
            "upgrade requires the current deployment to be an honest deployed state"
        )
    persisted = _read_persisted(current.state_path)
    if persisted != old:
        raise InvalidDeploymentStateTransition(
            "the persisted current deployment state changed or was tampered with"
        )
    if request.upgrade_id != derive_upgrade_id(current, replacement):
        raise DeploymentInputRejected(
            ["upgrade_id does not match the old/new deployment identities"]
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

    operations_dir = replacement.environment.operations_dir
    old_journal = _journal_for(current, operations_dir, old.deployment_id)
    _append_upgrade_event(
        old_journal,
        deployment=current,
        event=EVENT_UPGRADE_REQUESTED,
        upgrade_id=upgrade_id,
        detail={
            "old_instance_digest": old.instance_digest,
            "new_instance_digest": new_ref.instance_digest,
        },
        record=old,
    )

    candidate: Deployment | None = None
    stopped_record: DeploymentRecord | None = None
    concurrent_state_conflict = False
    state_committed = False
    try:
        candidate = deploy(
            replacement,
            runtime=runtime,
            identity_provider=identity_provider,
        )
        if not candidate.deployed or not getattr(candidate.record, "deployed", True):
            raise InvalidDeploymentStateTransition(
                "replacement deployment completed without an honest deployed claim"
            )
        if candidate.record.instance_digest != new_ref.instance_digest:
            raise InvalidDeploymentStateTransition(
                "replacement deployment realized a different Platform Instance"
            )

        new_journal = _journal_for(
            candidate, operations_dir, candidate.record.deployment_id
        )
        _append_upgrade_event(
            new_journal,
            deployment=candidate,
            event=EVENT_NEW_IDENTITY_VERIFIED,
            upgrade_id=upgrade_id,
            detail={
                "old_instance_digest": old.instance_digest,
                "new_instance_digest": candidate.record.instance_digest,
            },
        )

        with _transition_lock(current.state_path):
            if not current.deployed or current.record != old:
                concurrent_state_conflict = True
                raise InvalidDeploymentStateTransition(
                    "the current deployment state changed during upgrade"
                )
            persisted_before_stop = _read_persisted(current.state_path)
            if persisted_before_stop != old:
                concurrent_state_conflict = True
                raise InvalidDeploymentStateTransition(
                    "the persisted current deployment state changed during upgrade"
                )

            # Keep the old deployment's lifecycle and deployed claim authoritative
            # until its runtime elements have stopped successfully.
            _stop_deployment_runtime(current)
            persisted_after_stop = _read_persisted(current.state_path)
            if persisted_after_stop != current.record and persisted_after_stop != old:
                concurrent_state_conflict = True
                raise InvalidDeploymentStateTransition(
                    "the persisted current deployment state changed during upgrade"
                )

            stopped_record = (
                current.record.mark_stopped(at=utc_now())
                if current.record.running or current.record.ready
                else current.record
            )
            at = utc_now()
            superseded = old.mark_superseded(
                at=at,
                detail={
                    "upgrade_id": upgrade_id,
                    "replacement_deployment_id": candidate.record.deployment_id,
                    "replacement_instance_digest": candidate.record.instance_digest,
                },
            )
            superseded = replace(
                stopped_record,
                lifecycle=superseded.lifecycle,
                updated_at=superseded.updated_at,
                operational_actions=(
                    *stopped_record.operational_actions,
                    superseded.operational_actions[-1],
                ),
            )
            _write_state(current, superseded)
            persisted_after_write = _read_persisted(
                current.state_path,
                unavailable_message=(
                    "the superseded current deployment state could not be confirmed"
                ),
            )
            if persisted_after_write != superseded:
                concurrent_state_conflict = True
                raise InvalidDeploymentStateTransition(
                    "the persisted current deployment state changed during upgrade"
                )
            current.record = superseded
            _sync_deployed_attr(current)
            state_committed = True

        _append_upgrade_event(
            old_journal,
            deployment=current,
            event=EVENT_OLD_INSTANCE_SUPERSEDED,
            upgrade_id=upgrade_id,
            detail={
                "replacement_deployment_id": candidate.record.deployment_id,
                "replacement_instance_digest": candidate.record.instance_digest,
            },
            record=superseded,
        )
        _append_upgrade_event(
            new_journal,
            deployment=candidate,
            event=EVENT_UPGRADE_COMPLETED,
            upgrade_id=upgrade_id,
            detail={
                "old_deployment_id": old.deployment_id,
                "old_instance_digest": old.instance_digest,
            },
        )
        return candidate
    except Exception as error:
        if state_committed:
            # Runtime cutover and superseded state write have durably committed.
            # A post-commit completion-signal failure does not undo the upgrade,
            # stop the replacement runtime, or emit a contradictory failure event.
            raise
        if not concurrent_state_conflict:
            observed_record = current.record
            stop_was_recorded = any(
                action.name == "platform_stopped"
                for action in observed_record.operational_actions
            )
            if stopped_record is not None:
                recovery_record = stopped_record
            elif (
                stop_was_recorded
                and observed_record.lifecycle == LIFECYCLE_REALIZED
                and not observed_record.running
                and not observed_record.ready
            ):
                recovery_record = observed_record
            else:
                recovery_record = old
            current.record = recovery_record
            _sync_deployed_attr(current)
            try:
                _write_state(current, recovery_record)
            except Exception:  # noqa: BLE001, S110 - preserve the causal upgrade error
                pass
        cleanup_persistence_error: Exception | None = None
        if candidate is not None:
            cleanup_persistence_error = _cleanup_candidate(
                candidate, causal_error=error
            )
        failure_detail: dict[str, Any] = {
            "old_instance_digest": old.instance_digest,
            "new_instance_digest": new_ref.instance_digest,
        }
        if cleanup_persistence_error is not None:
            failure_detail["candidate_cleanup_persistence_failed"] = True
            failure_detail["candidate_cleanup_error_type"] = (
                cleanup_persistence_error.__class__.__name__
            )
            failure_detail["candidate_cleanup_error"] = str(cleanup_persistence_error)
        try:
            _append_upgrade_event(
                old_journal,
                deployment=current,
                event=EVENT_UPGRADE_FAILED,
                upgrade_id=upgrade_id,
                detail=failure_detail,
                record=old,
            )
        except Exception:  # noqa: BLE001, S110 - preserve the causal upgrade error
            pass
        raise


__all__ = ["UpgradeRequest", "derive_upgrade_id", "upgrade"]
