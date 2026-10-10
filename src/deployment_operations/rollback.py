"""Explicit rollback orchestration for one realized Platform Instance.

Rollback re-realizes a previously verified exact Platform Instance through the
normal deployment path. That path keeps component migrations forward-only; this
module has no schema downgrade or tenant-data mutation operation.

The current platform is handed over, not stopped: once the target is realized
and verified, the rollback releases *its own* references to the current
platform — the runtime contract's **detach** (``RuntimeAdapter.stop``,
ADR-0016 §18; S4 runbook §7) — and only then commits the rolled-back state. A
released reference is not evidence that the platform stopped, so no state or
signal of this module claims one: the record keeps its ``running`` claim and
withdraws the operational condition it can no longer verify. Whether the
released runtime element continues is its owner's decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from deployment_operations.deployment import (
    Deployment,
    DeploymentRequest,
    deploy,
    release_references,
)
from deployment_operations.errors import (
    DeploymentInputRejected,
    InvalidDeploymentStateTransition,
)
from deployment_operations.events import (
    EVENT_ROLLBACK_COMPLETED,
    EVENT_ROLLBACK_FAILED,
    EVENT_ROLLBACK_REQUESTED,
    EVENT_ROLLBACK_TARGET_REALIZED,
    EVENT_ROLLBACK_TARGET_VERIFIED,
    DeploymentEvent,
    EventJournal,
)
from deployment_operations.state import (
    LIFECYCLE_REALIZED,
    LIFECYCLE_ROLLED_BACK,
    LIFECYCLE_SUPERSEDED,
    DeploymentRecord,
    DeploymentStateStore,
    derive_deployment_id,
    utc_now,
)
from deployment_operations.verification import verify_instance


@dataclass(frozen=True)
class RollbackRequest:
    """One explicit transition from a currently deployed instance to a prior one.

    ``target`` is the deployment record that proves the target was previously
    realized and verified; ``target_request`` supplies its immutable Factory
    inputs so the normal deployment verification and migration path can run
    again. No name or floating selector is accepted.
    """

    current: Deployment
    target: Deployment
    target_request: DeploymentRequest
    rollback_id: str


def derive_rollback_id(current: Deployment, target: Deployment) -> str:
    """Derive an operation correlation id from both deployment identities."""
    return (
        f"rollback:{current.record.deployment_id}"
        f"->platform:{target.record.instance_digest}"
        f"@{current.record.environment_id}"
    )


def _event(
    journal: EventJournal,
    deployment: Deployment,
    name: str,
    rollback_id: str,
    *,
    operation: str,
    target_digest: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    record = deployment.record
    journal.append(
        DeploymentEvent(
            sequence=journal.next_sequence(),
            event=name,
            occurred_at=utc_now(),
            deployment_id=record.deployment_id,
            environment_id=record.environment_id,
            platform_id=record.platform_id,
            instance_digest=record.instance_digest,
            manifest_id=record.manifest_id,
            manifest_version=record.manifest_version,
            manifest_digest=record.manifest_digest,
            stage="rollback",
            detail={
                "rollback_id": rollback_id,
                "operation": operation,
                "target_instance_digest": target_digest,
                **(detail or {}),
            },
        )
    )


def _journal_for(deployment: Deployment) -> EventJournal:
    if deployment._journal is not None:
        return deployment._journal
    return EventJournal(deployment.events_path)


def _record_action(
    deployment: Deployment, name: str, *, rollback_id: str, detail: dict[str, Any]
) -> None:
    updated = deployment.record.with_operational_action(
        name,
        at=utc_now(),
        detail={"rollback_id": rollback_id, **detail},
    )
    deployment.record = updated
    if deployment._store is not None:
        deployment._store.write(updated, secrets=deployment._secrets)
    else:
        DeploymentStateStore(deployment.state_path).write(updated)


def _fresh_attempt(request: DeploymentRequest) -> DeploymentRequest:
    """Select an unused attempt; never overwrite a previous operation record."""
    attempt = request.attempt
    while True:
        deployment_id = derive_deployment_id(
            request.instance.platform_id,
            request.instance.instance_digest,
            request.environment.environment_id,
            attempt,
        )
        state_path = DeploymentStateStore.path_for(
            request.environment.operations_dir, deployment_id
        )
        if not state_path.exists():
            return replace(request, attempt=attempt)
        attempt += 1


def _check_persisted(deployment: Deployment, label: str) -> None:
    try:
        persisted = DeploymentStateStore(deployment.state_path).read()
    except Exception as error:
        raise InvalidDeploymentStateTransition(
            f"the persisted {label} deployment state is unavailable"
        ) from error
    if persisted != deployment.record:
        raise InvalidDeploymentStateTransition(
            f"the persisted {label} deployment state changed or was tampered with"
        )


def _validate(request: RollbackRequest) -> DeploymentRequest:
    current = request.current
    target = request.target
    target_request = request.target_request
    current_record = current.record
    target_record = target.record

    if request.rollback_id != derive_rollback_id(current, target):
        raise DeploymentInputRejected(
            ["rollback_id does not match the current and target identities"]
        )
    _check_persisted(current, "current")
    _check_persisted(target, "rollback target")
    if any(
        action.detail.get("rollback_id") == request.rollback_id
        for action in current_record.operational_actions
    ):
        raise InvalidDeploymentStateTransition(
            "this explicit rollback operation has already been recorded"
        )
    if not current.deployed:
        raise InvalidDeploymentStateTransition(
            "rollback requires the current deployment to be honestly deployed"
        )

    if current_record.environment_id != target_record.environment_id:
        raise DeploymentInputRejected(
            ["rollback must keep the same deployment environment"]
        )
    if target_request.environment.environment_id != current_record.environment_id:
        raise DeploymentInputRejected(
            ["rollback target request must use the current deployment environment"]
        )
    if (
        not target_record.instance_digest
        or target_record.instance_digest == current_record.instance_digest
    ):
        raise DeploymentInputRejected(
            ["rollback target must identify a different exact Platform Instance"]
        )
    if target_record.lifecycle not in {LIFECYCLE_REALIZED, LIFECYCLE_SUPERSEDED}:
        raise DeploymentInputRejected(
            ["rollback target has no prior successful realization"]
        )
    if not target_record.identity_verified or target_record.failure is not None:
        raise DeploymentInputRejected(
            ["rollback target lacks prior successful identity verification"]
        )
    if target_request.instance.instance_digest != target_record.instance_digest:
        raise DeploymentInputRejected(
            ["rollback request digest does not match the known-good target"]
        )
    if (
        target_request.instance.platform_id is not None
        and target_request.instance.platform_id != target_record.platform_id
    ):
        raise DeploymentInputRejected(
            ["rollback request platform identity does not match the known-good target"]
        )

    verified = verify_instance(
        target_request.instance_document,
        target_request.manifest_document,
        root=target_request.factory_root,
        environment_id=target_request.environment.environment_id,
    )
    if not verified.ok:
        raise DeploymentInputRejected(
            ["rollback target input could not be verified", *verified.all_errors()]
        )
    identity = (
        verified.platform_id,
        verified.instance_digest,
        verified.manifest_id,
        verified.manifest_version,
        verified.manifest_digest,
    )
    known_good = (
        target_record.platform_id,
        target_record.instance_digest,
        target_record.manifest_id,
        target_record.manifest_version,
        target_record.manifest_digest,
    )
    if identity != known_good:
        raise DeploymentInputRejected(
            ["rollback target identity/version/digest does not match prior state"]
        )

    return _fresh_attempt(target_request)


def rollback(
    request: RollbackRequest,
    *,
    runtime: Any = None,
    identity_provider: Any = None,
) -> Deployment:
    """Explicitly realize a previously known-good target, then supersede current.

    The target is fully validated before runtime action. The current deployment
    remains authoritative and running if target realization fails. A successful
    candidate is returned only after the existing deployment path has verified
    readiness and Running Platform identity correspondence.
    """
    current = request.current
    target = request.target
    target_digest = target.record.instance_digest
    current_journal = _journal_for(current)
    operation = "target_verification"
    candidate: Deployment | None = None
    original_current_record = current.record
    released_record: DeploymentRecord | None = None
    candidate_precompletion_record: DeploymentRecord | None = None
    candidate_completion_attempted = False
    state_committed = False
    try:
        deployment_request = _validate(request)
        operation = "target_realization"
        _record_action(
            current,
            "rollback_requested",
            rollback_id=request.rollback_id,
            detail={"target_instance_digest": target_digest},
        )
        _event(
            current_journal,
            current,
            EVENT_ROLLBACK_REQUESTED,
            request.rollback_id,
            operation="requested",
            target_digest=target_digest,
            detail={"target_deployment_id": target.record.deployment_id},
        )
        _record_action(
            current,
            "rollback_target_verified",
            rollback_id=request.rollback_id,
            detail={"target_instance_digest": target_digest},
        )
        _event(
            current_journal,
            current,
            EVENT_ROLLBACK_TARGET_VERIFIED,
            request.rollback_id,
            operation="target_verification",
            target_digest=target_digest,
            detail={"target_deployment_id": target.record.deployment_id},
        )

        candidate = deploy(
            deployment_request,
            runtime=runtime,
            identity_provider=identity_provider,
        )
        expected_deployment_id = derive_deployment_id(
            deployment_request.instance.platform_id,
            deployment_request.instance.instance_digest,
            deployment_request.environment.environment_id,
            deployment_request.attempt,
        )
        if (
            candidate.record.deployment_id != expected_deployment_id
            or candidate.record.attempt != deployment_request.attempt
        ):
            raise InvalidDeploymentStateTransition(
                "rollback target was recorded under a different deployment attempt"
            )
        if not candidate.deployed:
            raise InvalidDeploymentStateTransition(
                "rollback target completed without an honest deployed claim"
            )
        if (
            candidate.record.platform_id != target.record.platform_id
            or candidate.record.instance_digest != target.record.instance_digest
            or candidate.record.manifest_id != target.record.manifest_id
            or candidate.record.manifest_version != target.record.manifest_version
            or candidate.record.manifest_digest != target.record.manifest_digest
        ):
            raise InvalidDeploymentStateTransition(
                "rollback realized an identity/version/digest other than its target"
            )

        _record_action(
            current,
            "rollback_target_realized",
            rollback_id=request.rollback_id,
            detail={
                "target_instance_digest": target_digest,
                "target_deployment_id": candidate.record.deployment_id,
            },
        )
        candidate_journal = _journal_for(candidate)
        _event(
            candidate_journal,
            candidate,
            EVENT_ROLLBACK_TARGET_REALIZED,
            request.rollback_id,
            operation="target_realized",
            target_digest=target_digest,
            detail={"previous_deployment_id": current.record.deployment_id},
        )

        # Keep the current deployment's lifecycle and deployed claim authoritative
        # until the target is realized and verified; only then release this
        # operation's references to the current platform. The release is the
        # contract's detach, so the record keeps its ``running`` claim and
        # withdraws the condition it can no longer verify (ADR-0016 §18).
        operation = "current_runtime_release"
        released_record = release_references(current, origin="rollback_current")

        operation = "final_state_persistence"
        rolled_back = current.record.mark_rolled_back(
            at=utc_now(),
            detail={
                "rollback_id": request.rollback_id,
                "replacement_deployment_id": candidate.record.deployment_id,
                "replacement_instance_digest": candidate.record.instance_digest,
                "reason": "explicit_rollback",
            },
        )
        completed_record = rolled_back.with_operational_action(
            "rollback_completed",
            at=utc_now(),
            detail={
                "rollback_id": request.rollback_id,
                "target_instance_digest": target_digest,
                "replacement_deployment_id": candidate.record.deployment_id,
            },
        )
        if current._store is not None:
            current._store.write(completed_record, secrets=current._secrets)
        else:
            DeploymentStateStore(current.state_path).write(completed_record)
        current.record = completed_record

        candidate_precompletion_record = candidate.record
        candidate_record = candidate.record.with_operational_action(
            "rollback_completed",
            at=utc_now(),
            detail={
                "rollback_id": request.rollback_id,
                "previous_deployment_id": current.record.deployment_id,
                "target_deployment_id": target.record.deployment_id,
                "target_instance_digest": target_digest,
            },
        )
        candidate_completion_attempted = True
        if candidate._store is not None:
            candidate._store.write(candidate_record, secrets=candidate._secrets)
        else:
            DeploymentStateStore(candidate.state_path).write(candidate_record)
        candidate.record = candidate_record
        state_committed = True
        operation = "candidate_completion_event"
        _event(
            candidate_journal,
            candidate,
            EVENT_ROLLBACK_COMPLETED,
            request.rollback_id,
            operation="completed",
            target_digest=target_digest,
            detail={"previous_deployment_id": current.record.deployment_id},
        )
        operation = "current_completion_event"
        _event(
            current_journal,
            current,
            EVENT_ROLLBACK_COMPLETED,
            request.rollback_id,
            operation="completed",
            target_digest=target_digest,
            detail={"replacement_deployment_id": candidate.record.deployment_id},
        )
        return candidate
    except Exception as error:
        if state_committed:
            # Runtime transition and both authoritative state writes committed.
            # A completion-signal failure is not a failed rollback: retain the
            # deployed candidate and successful state, and surface the journal
            # error without emitting a contradictory rollback_failed signal.
            raise
        # Before the release succeeds, restore the original record. After a
        # successful release, retain its honest conditions — running stands, the
        # verified readiness is withdrawn — rather than resurrecting a stale
        # deployed claim or claiming a stop that nobody established.
        observed_record = current.record
        release_recorded = any(
            action.name == "platform_released"
            for action in observed_record.operational_actions
        )
        if released_record is not None:
            # The references were released and the record says exactly that: the
            # platform may still be up, and the honest state keeps it reachable
            # instead of claiming a stop nobody established.
            recovery_record = released_record
        elif release_recorded and not observed_record.ready:
            recovery_record = observed_record
        else:
            recovery_record = original_current_record
        current.record = recovery_record
        try:
            if current._store is not None:
                current._store.write(recovery_record, secrets=current._secrets)
            else:
                DeploymentStateStore(current.state_path).write(recovery_record)
        except Exception:  # noqa: BLE001, S110 - preserve the causal rollback error
            pass
        if (
            candidate is not None
            and candidate_completion_attempted
            and candidate_precompletion_record is not None
        ):
            candidate.record = candidate_precompletion_record
            try:
                if candidate._store is not None:
                    candidate._store.write(
                        candidate_precompletion_record,
                        secrets=candidate._secrets,
                    )
                else:
                    DeploymentStateStore(candidate.state_path).write(
                        candidate_precompletion_record
                    )
            except Exception:  # noqa: BLE001, S110 - best-effort restore
                pass
        if candidate is not None and candidate.deployed:
            try:
                release_references(candidate, origin="rollback_failed_candidate")
            except Exception:  # noqa: BLE001, S110 - preserve the causal rollback error
                pass
        # A failed rollback is recorded without asserting that its target was
        # realized. The current deployment's prior lifecycle claim is retained.
        try:
            already_terminal = any(
                action.detail.get("rollback_id") == request.rollback_id
                and action.name in {"rollback_completed", "rollback_failed"}
                for action in current.record.operational_actions
            )
            if not already_terminal:
                if current.record.lifecycle in {
                    LIFECYCLE_REALIZED,
                    LIFECYCLE_ROLLED_BACK,
                }:
                    failed_record = current.record.with_operational_action(
                        "rollback_failed",
                        at=utc_now(),
                        detail={
                            "rollback_id": request.rollback_id,
                            "target_instance_digest": target_digest,
                            "operation": operation,
                            "error_type": error.__class__.__name__,
                        },
                    )
                    current.record = failed_record
                    if current._store is not None:
                        current._store.write(failed_record, secrets=current._secrets)
                    else:
                        DeploymentStateStore(current.state_path).write(failed_record)
                _event(
                    current_journal,
                    current,
                    EVENT_ROLLBACK_FAILED,
                    request.rollback_id,
                    operation=operation,
                    target_digest=target_digest,
                    detail={"error_type": error.__class__.__name__},
                )
        except Exception:  # noqa: BLE001, S110 - preserve the causal rollback error
            # Preserve the causal exception; persistence failures are surfaced by
            # the next operation's mandatory state check rather than disguising
            # the runtime/verification failure that initiated this path.
            pass
        raise


__all__ = ["RollbackRequest", "derive_rollback_id", "rollback"]
