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
* **lifecycle position** — ``in_progress``, ``realized``, ``failed``,
  ``superseded`` or ``rolled_back``;
* **the record** — this document, which is deployment state itself.

``rolled_back`` is recorded only by the explicit Rollback Slice; ``superseded``
continues to record an accepted replacement in the Upgrade Slice.

Reconciliation results are part of this record too (ADR-0016 §9, §18, §19):
:class:`ReconciliationRecord` keeps the append-only history of independent
observations of the Running Platform against the exact desired state this record
pins. That history changes no lifecycle position and no operational condition —
it is how a proven divergence is surfaced honestly (ADR-0016 §18) instead of
being silently patched, and :attr:`DeploymentRecord.in_correspondence` says
plainly that nothing conclusive has been observed yet rather than reporting
unproven health.

Ongoing runtime management is recorded here as well (ADR-0016 §18): every
admitted restart attempt appends one :class:`RestartRecord` with its ordered
phase evidence and the identity/version/artifact state it re-executed. A restart
is an operational action on the Running Platform, so it changes the operational
conditions — and only those: it creates no new deployment operation, invents no
lifecycle position, rewrites no failure record of the deployment operation, and
erases no reconciliation observation.
"""

from __future__ import annotations

import hashlib
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

#: Lifecycle positions of the deployment operation (ADR-0016 §9) implemented
#: by deployment, upgrade and rollback orchestration.
LIFECYCLE_IN_PROGRESS = "in_progress"
LIFECYCLE_REALIZED = "realized"
LIFECYCLE_FAILED = "failed"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_ROLLED_BACK = "rolled_back"

#: Outcomes of one reconciliation observation (ADR-0016 §18–§19). These are the
#: operational names of the existing identity-correspondence result — MATCH is
#: ``in_correspondence``, MISMATCH is ``drift``, UNAVAILABLE is ``unverifiable``
#: — and they introduce no second identity semantics: ``in_correspondence`` means
#: the independently computed actual instance digest equals the pinned one,
#: ``drift`` means it provably does not, and ``unverifiable`` means actual state
#: could not be established and the observation is therefore fail-closed rather
#: than healthy (ADR-0016 §20).
RECONCILIATION_IN_CORRESPONDENCE = "in_correspondence"
RECONCILIATION_DRIFT = "drift"
RECONCILIATION_UNVERIFIABLE = "unverifiable"

#: Every outcome a reconciliation record may carry.
RECONCILIATION_OUTCOMES: tuple[str, ...] = (
    RECONCILIATION_IN_CORRESPONDENCE,
    RECONCILIATION_DRIFT,
    RECONCILIATION_UNVERIFIABLE,
)

#: Outcomes of one restart attempt — the runtime-management operation of
#: ADR-0016 §18. They are **not** lifecycle positions: the lifecycle vocabulary
#: of ADR-0016 §9 stays closed, and a restart neither realizes nor fails the
#: deployment operation it re-executes the runtime of.
RESTART_COMPLETED = "restarted"
RESTART_FAILED = "failed"

#: Every outcome a restart record may carry.
RESTART_OUTCOMES: tuple[str, ...] = (RESTART_COMPLETED, RESTART_FAILED)

#: The observable phases of one restart attempt, in order:
#:
#:     running → stop → execution/content verification → fresh start
#:         → health/readiness verification → identity re-verification → ready
#:
#: This is the observable sequence of the runtime-management operation, not a
#: second initial deployment path: the stages of ADR-0017 §36 stay exactly as
#: they are, and no phase here is one of them.
RESTART_PHASES: tuple[str, ...] = (
    "requested",
    "stop",
    "execution_verification",
    "start",
    "health_check",
    "identity_verification",
    "completed",
)

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
    #: The content this deployment bound to the component's process before it
    #: was started, and the digests the engine computed for it (§9, §10).
    execution: Mapping[str, Any] | None = None
    #: What the running process reported loading — evidence the engine checks
    #: against the binding, never a substitute for it.
    observed_execution: Mapping[str, Any] | None = None
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
            "execution": dict(self.execution) if self.execution else None,
            "observed_execution": (
                dict(self.observed_execution) if self.observed_execution else None
            ),
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
class ReconciliationRecord:
    """One independent observation of the Running Platform against desired state.

    The record keeps what was compared, what was observed and what followed
    (ADR-0016 §9, §18, §19) — never a repair:

    * ``desired`` is the exact identity/version/digest state already fixed in the
      deployment record, and ``actual`` is what independent observation
      established (``None`` when it could not be established at all);
    * ``differences`` names every identity/version/digest field that provably
      diverged, so drift is attributable instead of being flattened into a
      generic failure;
    * ``evidence`` records the normative provenance of the actual evidence and
      that its freshness was current; the opaque owner-side correlation token is
      not copied here;
    * ``errors`` carries the fail-closed reason of an ``unverifiable``
      observation.

    The entry is operational metadata: no secrets, no business or tenant data,
    and nothing that rewrites what the deployment operation did.
    """

    reconciliation_id: str
    sequence: int
    outcome: str
    occurred_at: str
    desired: Mapping[str, Any] = field(default_factory=dict)
    actual: Mapping[str, Any] | None = None
    differences: tuple[str, ...] = ()
    evidence: Mapping[str, Any] | None = None
    errors: tuple[str, ...] = ()

    @property
    def in_correspondence(self) -> bool:
        """True when the observed platform is the exact desired instance."""
        return self.outcome == RECONCILIATION_IN_CORRESPONDENCE

    @property
    def drifted(self) -> bool:
        """True when a divergence between desired and actual state was proven."""
        return self.outcome == RECONCILIATION_DRIFT

    @property
    def unverifiable(self) -> bool:
        """True when actual state could not be established (fail-closed)."""
        return self.outcome == RECONCILIATION_UNVERIFIABLE

    def document(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "sequence": self.sequence,
            "outcome": self.outcome,
            "occurred_at": self.occurred_at,
            "desired": dict(self.desired),
            "actual": dict(self.actual) if self.actual is not None else None,
            "differences": list(self.differences),
            "evidence": dict(self.evidence) if self.evidence is not None else None,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class RestartPhaseRecord:
    """The recorded outcome of one phase of one restart attempt (§18).

    A phase status reuses the stage vocabulary already fixed above
    (``pending`` / ``in_progress`` / ``completed`` / ``failed``) so that the
    order and the outcome of a restart are readable without a second set of
    words. ``detail`` holds operational facts only — which components were
    stopped, which were started, what the fresh verification observed — never
    business data and never secret material (ADR-0016 §6, §12).
    """

    name: str
    status: str = STAGE_PENDING
    occurred_at: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "occurred_at": self.occurred_at,
            "detail": dict(self.detail),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class RestartRecord:
    """One restart attempt of the runtime of one deployment operation (§18).

    The record is the runtime-management evidence of the attempt: what was
    asked for, in which order it was executed, what each phase established, and
    — for a successful attempt — the identity/version/artifact evidence the
    restart acted on and preserved. It never redefines the deployment
    operation's own history: ``lifecycle``, ``failure``, the stages of the
    initial path and the reconciliation history all keep exactly the meaning
    they had (ADR-0016 §9, §18, §20).
    """

    restart_id: str
    sequence: int
    outcome: str
    requested_at: str
    completed_at: str = ""
    reason: str = ""
    #: The identity/version/artifact state the attempt re-executed: the pinned
    #: Platform Instance and, per component, the version, artifact digest and
    #: bound execution content. A restart that preserved identity shows it here
    #: instead of asserting it.
    identity: Mapping[str, Any] = field(default_factory=dict)
    phases: tuple[RestartPhaseRecord, ...] = ()
    #: The phase that failed and why, or ``None`` for a completed attempt.
    failure_phase: str | None = None
    failure_reason: str | None = None
    errors: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        """True when the attempt re-executed the same runtime and verified it."""
        return self.outcome == RESTART_COMPLETED

    @property
    def failed(self) -> bool:
        """True when the attempt stopped fail-closed in one of its phases."""
        return self.outcome == RESTART_FAILED

    def phase(self, name: str) -> RestartPhaseRecord | None:
        for entry in self.phases:
            if entry.name == name:
                return entry
        return None

    def phase_order(self) -> tuple[str, ...]:
        """The names of the phases that were reached, in execution order."""
        return tuple(
            entry.name for entry in self.phases if entry.status != STAGE_PENDING
        )

    def document(self) -> dict[str, Any]:
        return {
            "restart_id": self.restart_id,
            "sequence": self.sequence,
            "outcome": self.outcome,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "reason": self.reason,
            "identity": dict(self.identity),
            "phases": [entry.document() for entry in self.phases],
            "failure_phase": self.failure_phase,
            "failure_reason": self.failure_reason,
            "errors": list(self.errors),
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
    #: Append-only history of independent reconciliation observations (§9, §18).
    #: Never a lifecycle position, never a repair: it says what was observed.
    reconciliations: tuple[ReconciliationRecord, ...] = ()
    #: Append-only history of restart attempts — the runtime-management record
    #: of ongoing operational control (ADR-0016 §18). One entry per admitted
    #: attempt, in order, with its phase evidence; a restart never creates a new
    #: deployment operation and never overwrites an earlier attempt.
    restarts: tuple[RestartRecord, ...] = ()
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

    @property
    def last_reconciliation(self) -> ReconciliationRecord | None:
        """The latest reconciliation observation, or ``None`` if never reconciled."""
        return self.reconciliations[-1] if self.reconciliations else None

    @property
    def last_restart(self) -> RestartRecord | None:
        """The latest restart attempt, or ``None`` if the runtime was never restarted."""
        return self.restarts[-1] if self.restarts else None

    @property
    def restarted(self) -> bool:
        """True when the latest restart attempt completed and was verified.

        This is a fact about the runtime-management operation, not a second
        ``deployed`` claim: whether the platform may be claimed ``deployed``
        keeps depending on the conditions alone — ``realized``, ``running``,
        ``ready`` and ``identity_verified`` together (ADR-0016 §10).
        """
        return self.last_restart is not None and self.last_restart.completed

    @property
    def in_correspondence(self) -> bool | None:
        """Whether the latest conclusive observation found correspondence.

        ``True`` or ``False`` come only from a conclusive observation: identity
        correspondence, or a proven divergence. ``None`` means no conclusive
        observation exists — nothing has been reconciled, or every observation
        was unverifiable. ``None`` is never health: missing or unverifiable
        evidence is fail-closed (ADR-0016 §20), and a reader must not read it as
        «no drift».
        """
        for observation in reversed(self.reconciliations):
            if observation.outcome == RECONCILIATION_IN_CORRESPONDENCE:
                return True
            if observation.outcome == RECONCILIATION_DRIFT:
                return False
        return None

    @property
    def drifted(self) -> bool:
        """True while a proven divergence is not disproved by a later observation.

        This condition is *additional* to ``ready``, ``realized``,
        ``identity_verified`` and the ``deployed`` claim, and it changes none of
        them: it states what independent observation proved about the platform
        now (ADR-0016 §18). ``deployed`` remains what the deployment operation
        did and verified; ``drifted`` is what a later observation found.
        """
        return self.in_correspondence is False

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

    def withdraw_ready(self, *, at: str, reason: str) -> DeploymentRecord:
        """Withdraw ``ready`` without claiming a stop that did not happen (§18, §20).

        ``mark_stopped`` states that the platform's runtime elements are down;
        an operational action that could not complete — a stop one element
        refused, a start that never happened, a health/readiness verification
        that failed — leaves the platform in a state that is *not* a verified
        ready one, while some element may still be up. Claiming ``stopped``
        there would be as false as keeping ``ready``: this transition therefore
        withdraws only the verified operational condition, records why, and
        leaves ``running`` exactly as it was. No lifecycle position changes, no
        ``platform_stopped`` action is invented, and ``identity_verified`` keeps
        stating what the deployment operation verified (§9).
        """
        if not self.ready:
            return self
        withdrawn = replace(self, ready=False, updated_at=at)
        return withdrawn.with_operational_action(
            "readiness_withdrawn", at=at, detail={"reason": reason}
        )

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

    def mark_superseded(
        self, *, at: str, detail: Mapping[str, Any] | None = None
    ) -> DeploymentRecord:
        """Mark this realized deployment as superseded by an accepted upgrade."""
        if not self.deployed:
            raise InvalidDeploymentStateTransition(
                "only an honest deployed record can be superseded"
            )
        record = replace(self, lifecycle=LIFECYCLE_SUPERSEDED, updated_at=at)
        return record.with_operational_action(
            "platform_superseded", at=at, detail=detail
        )

    def mark_rolled_back(
        self, *, at: str, detail: Mapping[str, Any] | None = None
    ) -> DeploymentRecord:
        """Record rollback only after the previous Running Platform has stopped."""
        stopped = any(
            action.name == "platform_stopped" for action in self.operational_actions
        )
        if (
            self.lifecycle != LIFECYCLE_REALIZED
            or self.running
            or self.ready
            or not self.identity_verified
            or self.failure is not None
            or not stopped
        ):
            raise InvalidDeploymentStateTransition(
                "only a verified realized deployment stopped by rollback can be rolled back"
            )
        record = replace(self, lifecycle=LIFECYCLE_ROLLED_BACK, updated_at=at)
        return record.with_operational_action(
            "platform_rolled_back", at=at, detail=detail
        )

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

    def with_reconciliation(
        self, observation: ReconciliationRecord, *, at: str
    ) -> DeploymentRecord:
        """Append one completed reconciliation observation (§9, §18, §19).

        Reconciliation is observational. This transition records what an
        independent observation found, and it changes no lifecycle position and
        no operational condition: ``ready``, ``realized``, ``identity_verified``
        and the ``deployed`` claim keep exactly the meaning they had. A proven
        divergence becomes visible through :attr:`drifted` — it is a signal, not
        a silent rewrite of what the deployment operation did, and not a repair.

        A divergence is deliberately not written into ``failure`` either: the
        deployment operation did not stop and did not fail, and rewriting its
        failure record would misstate where and why an operation stopped
        (ADR-0016 §20). The reconciliation history is where the observation
        lives, and :attr:`drifted` is how it is read.
        """
        if observation.outcome not in RECONCILIATION_OUTCOMES:
            message = (
                f"reconciliation outcome {observation.outcome!r} is not one of "
                f"{', '.join(RECONCILIATION_OUTCOMES)}"
            )
            raise DeploymentStateError(message)
        sequenced = replace(
            observation,
            sequence=len(self.reconciliations) + 1,
            desired=_plain_value(observation.desired),
            actual=_plain_value(observation.actual),
            differences=tuple(str(entry) for entry in observation.differences),
            evidence=_plain_value(observation.evidence),
            errors=tuple(str(entry) for entry in observation.errors),
        )
        return replace(
            self,
            reconciliations=(*self.reconciliations, sequenced),
            updated_at=at,
        )

    def with_restart(self, attempt: RestartRecord, *, at: str) -> DeploymentRecord:
        """Append one completed restart attempt (§9, §18).

        One admitted attempt appends exactly one record, numbered by the
        attempt's position in this record's history, so a controlled retry
        after a failed restart is a *new*, separately identified attempt rather
        than an overwrite of the previous one (ADR-0016 §9, §20).

        The transition changes no lifecycle position and re-defines no
        operational condition: what a restart did to ``running``/``ready`` is
        recorded by the transitions that establish those facts, and the
        reconciliation history is left exactly as it was — a restart neither
        repairs nor hides a proven divergence (ADR-0016 §18).
        """
        if attempt.outcome not in RESTART_OUTCOMES:
            message = (
                f"restart outcome {attempt.outcome!r} is not one of "
                f"{', '.join(RESTART_OUTCOMES)}"
            )
            raise DeploymentStateError(message)
        unknown = [
            entry.name for entry in attempt.phases if entry.name not in RESTART_PHASES
        ]
        if unknown:
            message = (
                f"restart phases {', '.join(sorted(unknown))} are not phases of "
                "the restart operation"
            )
            raise DeploymentStateError(message)
        sequenced = replace(
            attempt,
            sequence=len(self.restarts) + 1,
            identity=_plain_value(attempt.identity),
            phases=tuple(
                replace(
                    entry,
                    detail=_plain_value(entry.detail),
                    errors=tuple(str(error) for error in entry.errors),
                )
                for entry in attempt.phases
            ),
            errors=tuple(str(error) for error in attempt.errors),
        )
        return replace(self, restarts=(*self.restarts, sequenced), updated_at=at)

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
            "reconciliation": {
                "observations": [entry.document() for entry in self.reconciliations],
                "outcome": (
                    self.last_reconciliation.outcome
                    if self.last_reconciliation is not None
                    else None
                ),
                "in_correspondence": self.in_correspondence,
                "drifted": self.drifted,
            },
            # Runtime-management history (ADR-0016 §18): every admitted restart
            # attempt with its ordered phase evidence. Operational metadata only
            # — identity, order and outcome, never business data.
            "restarts": {
                "attempts": [entry.document() for entry in self.restarts],
                "attempt_count": len(self.restarts),
                "outcome": (
                    self.last_restart.outcome if self.last_restart is not None else None
                ),
                "restarted": self.restarted,
            },
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
        safe = hashlib.sha256(deployment_id.encode("utf-8")).hexdigest()
        return operations_dir / f"{safe}.json"

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


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Read a JSON array of strings, ignoring any other shape."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _restart_phases(value: Any) -> tuple[RestartPhaseRecord, ...]:
    """Read the phase evidence of one persisted restart attempt."""
    if not isinstance(value, list):
        return ()
    phases: list[RestartPhaseRecord] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        detail = item.get("detail")
        phases.append(
            RestartPhaseRecord(
                name=str(item.get("name", "")),
                status=str(item.get("status", STAGE_PENDING)),
                occurred_at=str(item.get("occurred_at", "")),
                detail=detail if isinstance(detail, Mapping) else {},
                errors=_string_tuple(item.get("errors")),
            )
        )
    return tuple(phases)


def _plain_value(value: Any) -> Any:
    """Normalize a value to the shape one JSON round trip preserves.

    A reconciliation record is persisted and read back, and deployment state is
    compared with its persisted form before an operation acts on it, so the
    in-memory shape must be exactly the shape a round trip yields: mappings stay
    mappings, sequences are lists. A tuple where the reader produces a list
    would make a freshly written record look tampered with.
    """
    if isinstance(value, Mapping):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    return value


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
    reconciliation = document.get("reconciliation")
    block: Mapping[str, Any] = (
        reconciliation if isinstance(reconciliation, Mapping) else {}
    )
    observations = block.get("observations")
    restarts = document.get("restarts")
    restart_block: Mapping[str, Any] = restarts if isinstance(restarts, Mapping) else {}
    attempts = restart_block.get("attempts")

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
                    # The execution binding and the evidence of what ran are
                    # part of the record: state has to correlate the instance,
                    # the boundary, the execution and the observation, or a
                    # reader cannot tell what a claim stands on (§9, §10).
                    execution=(
                        item.get("execution")
                        if isinstance(item.get("execution"), Mapping)
                        else None
                    ),
                    observed_execution=(
                        item.get("observed_execution")
                        if isinstance(item.get("observed_execution"), Mapping)
                        else None
                    ),
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
        reconciliations=(
            tuple(
                ReconciliationRecord(
                    reconciliation_id=str(item.get("reconciliation_id", "")),
                    sequence=int(item.get("sequence", 0)),
                    outcome=str(item.get("outcome", "")),
                    occurred_at=str(item.get("occurred_at", "")),
                    desired=(
                        item.get("desired")
                        if isinstance(item.get("desired"), Mapping)
                        else {}
                    ),
                    actual=(
                        item.get("actual")
                        if isinstance(item.get("actual"), Mapping)
                        else None
                    ),
                    differences=_string_tuple(item.get("differences")),
                    evidence=(
                        item.get("evidence")
                        if isinstance(item.get("evidence"), Mapping)
                        else None
                    ),
                    errors=_string_tuple(item.get("errors")),
                )
                for item in observations
                if isinstance(item, Mapping)
            )
            if isinstance(observations, list)
            else ()
        ),
        restarts=(
            tuple(
                RestartRecord(
                    restart_id=str(item.get("restart_id", "")),
                    sequence=int(item.get("sequence", 0)),
                    outcome=str(item.get("outcome", "")),
                    requested_at=str(item.get("requested_at", "")),
                    completed_at=str(item.get("completed_at", "")),
                    reason=str(item.get("reason", "")),
                    identity=(
                        item.get("identity")
                        if isinstance(item.get("identity"), Mapping)
                        else {}
                    ),
                    phases=_restart_phases(item.get("phases")),
                    failure_phase=item.get("failure_phase"),
                    failure_reason=item.get("failure_reason"),
                    errors=_string_tuple(item.get("errors")),
                )
                for item in attempts
                if isinstance(item, Mapping)
            )
            if isinstance(attempts, list)
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
    "LIFECYCLE_ROLLED_BACK",
    "LIFECYCLE_SUPERSEDED",
    "RECONCILIATION_DRIFT",
    "RECONCILIATION_IN_CORRESPONDENCE",
    "RECONCILIATION_OUTCOMES",
    "RECONCILIATION_UNVERIFIABLE",
    "RESTART_COMPLETED",
    "RESTART_FAILED",
    "RESTART_OUTCOMES",
    "RESTART_PHASES",
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
    "ReconciliationRecord",
    "RestartPhaseRecord",
    "RestartRecord",
    "StageRecord",
    "derive_deployment_id",
    "load_record",
    "record_from_document",
    "refuse_secret_material",
    "utc_now",
]
