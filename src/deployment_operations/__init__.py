"""Deployment & Operations — the first vertical slice (ADR-0016; ADR-0017 §42).

The Factory ends at a deterministic Platform Instance (ADR-0016 §3,
``docs/ARCHITECTURE.md`` §37.1). This capability begins there::

    Factory
        ↓
    Platform Manifest
        ↓
    Platform Instance     (desired state — immutable input)
        ↓
    Deployment & Operations
        ↓
    Running Platform      (actual state — what is deployed, running, observable)

One deployment operation for one accepted Platform Instance traverses the
minimal observable path of ADR-0017 §36 — ``requested`` → ``validated`` →
``provisioning`` → ``deploying`` → ``starting`` → ``health_check`` → ``ready``
— and an honest ``deployed`` / ``realized`` claim is fixed only when the
verified operational condition **and** the exact identity/version/digest
verification of what is actually running hold together (ADR-0017 §33–§35).

Two consequences of that path are deliberate and easy to get wrong:

* the required component-owned migrations run in their own transient session
  before the platform is started: ``deploying`` never starts a runtime element,
  ``starting`` does (§36–§37);
* ``deployed`` is a claim about an **actual, observable Running Platform**
  (§10), so stopping the platform withdraws it — the record keeps what the
  operation did and the verification it performed, and stops claiming a
  platform that is no longer running.

Upgrade and rollback are separate explicit orchestration slices; reconciliation
of actual state against the exact desired state is a separate observational one;
ongoing runtime management — the explicit restart of an already realized runtime
under an explicit stop/start policy — is a separate operational one.
What is deliberately absent here: implicit recovery, automatic remediation or
drift repair, fleet management, autoscaling, scheduling and any general
deployment platform (ADR-0017 §38–§39). Nothing in this package owns business
data, opens a component database, creates an SCS or a new component, or turns
the Factory into a deployment engine (ADR-0016 §6, §21, §22; LAW-03, LAW-04).

Public surface:

* :func:`deploy` — realize one accepted Platform Instance in one environment;
* :func:`attach` — re-bind an already-realized deployment operation to the
  Running Platform that outlived its own process: it binds the runtime elements
  its environment already supervises, re-verifies the actual platform identity,
  and creates, starts, stops, restarts and migrates nothing;
* :func:`rollback` — explicitly re-realize an exact previously verified instance;
* :func:`upgrade` — realize a new accepted instance before superseding current;
* :func:`reconcile` — observe the Running Platform and compare it with the exact
  desired state, recording drift honestly and repairing nothing (ADR-0016 §18);
* :func:`restart` — re-execute the runtime of one realized operation: stop,
  re-verify the execution/content binding, fresh start, fresh health/readiness
  and identity verification, under :class:`StopStartPolicy` (ADR-0016 §18);
* :class:`DeploymentRequest` / :class:`InstanceReference` — the deployment input;
* :class:`Deployment` — the completed operation, its record and its platform;
* :class:`DeploymentEnvironment` / :func:`load_environment` — the operational
  side: where an instance is realized here, and through which entrypoints;
* :func:`verify_instance` — the exact identity/version/digest verification;
* :class:`DeploymentRecord` / :class:`DeploymentStateStore` — deployment state;
* :class:`ReconciliationRecord` — the append-only reconciliation history;
* :class:`RestartRecord` — the append-only runtime-management history;
* :class:`DeploymentEvent` / :class:`EventJournal` — the operational signals;
* :class:`LocalProcessRuntime` — the replaceable runtime adapter of this slice.
"""

from __future__ import annotations

from deployment_operations.deployment import (
    Deployment,
    DeploymentRequest,
    InstanceReference,
    attach,
    default_source_paths,
    deploy,
)
from deployment_operations.environment import (
    IDENTITY_CONFIGURATION_KEYS,
    ComponentRuntimeBinding,
    DependencyEndpoint,
    DeploymentEnvironment,
    MigrationBinding,
    load_environment,
    render_environment,
    validate_environment,
)
from deployment_operations.errors import (
    DeploymentExecutionFailed,
    DeploymentInputRejected,
    DeploymentOperationsError,
    DeploymentStateError,
    HealthCheckFailed,
    IdentityVerificationFailed,
    InvalidDeploymentStateTransition,
    MigrationOrchestrationFailed,
    ProvisioningFailed,
    ReconciliationDriftDetected,
    ReconciliationEvidenceUnavailable,
    RestartFailed,
    RestartInProgress,
    SecretLeakRefused,
    StartupFailed,
)
from deployment_operations.events import DeploymentEvent, EventJournal
from deployment_operations.health import (
    ComponentObservation,
    HealthEvaluation,
    evaluate_health,
    verify_identity,
)
from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    ActualIdentityUnavailable,
    BindingEvaluationContext,
    BindingIdentity,
    EvidenceCorrelation,
    EvidenceFreshness,
    EvidenceProvenance,
    IdentityCorrespondenceResult,
    IdentityField,
    PlatformIdentityBinding,
    PlatformIdentityProvider,
    PlatformIdentitySurface,
    PresenceState,
    compute_actual_digest,
    establish_identity_correspondence,
    project_actual_identity,
    require_correlated_evidence,
    validate_actual_evidence,
    validate_actual_surface,
)
from deployment_operations.platform_identity_source import (
    ActualPlatformSnapshot,
    OwnerSuppliedPlatformIdentityProvider,
    RunningPlatformIdentitySource,
)
from deployment_operations.provisioning import (
    ArtifactSource,
    ProvisionedEnvironment,
    provision,
    resolve_artifact,
)
from deployment_operations.reconciliation import (
    Reconciliation,
    ReconciliationRequest,
    derive_reconciliation_id,
    reconcile,
)
from deployment_operations.restart import (
    STRICT_STOP_START_POLICY,
    Restart,
    RestartRequest,
    StopStartPolicy,
    derive_restart_id,
    restart,
    validate_stop_start_policy,
)
from deployment_operations.rollback import (
    RollbackRequest,
    derive_rollback_id,
    rollback,
)
from deployment_operations.runtime import (
    LocalProcessRuntime,
    MaterializedComponent,
    RuntimeAdapter,
    RuntimeElement,
    RuntimeHandle,
    RuntimeProcessError,
    build_elements,
)
from deployment_operations.state import (
    LIFECYCLE_FAILED,
    LIFECYCLE_IN_PROGRESS,
    LIFECYCLE_REALIZED,
    LIFECYCLE_ROLLED_BACK,
    LIFECYCLE_SUPERSEDED,
    RECONCILIATION_DRIFT,
    RECONCILIATION_IN_CORRESPONDENCE,
    RECONCILIATION_OUTCOMES,
    RECONCILIATION_UNVERIFIABLE,
    RESTART_COMPLETED,
    RESTART_FAILED,
    RESTART_OUTCOMES,
    RESTART_PHASES,
    STAGES,
    ComponentRecord,
    DeploymentRecord,
    DeploymentStateStore,
    FailureRecord,
    MigrationRecord,
    OperationalAction,
    ReconciliationRecord,
    RestartPhaseRecord,
    RestartRecord,
    derive_deployment_id,
    load_record,
    utc_now,
)
from deployment_operations.upgrade import UpgradeRequest, derive_upgrade_id, upgrade
from deployment_operations.verification import (
    DEPLOYABLE_INSTANCE_STATES,
    ComponentBinding,
    InstanceVerification,
    canonical_digest,
    verify_artifact_digest,
    verify_input_unchanged,
    verify_instance,
)

__all__ = [
    "DEPLOYABLE_INSTANCE_STATES",
    "IDENTITY_CONFIGURATION_KEYS",
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
    "STRICT_STOP_START_POLICY",
    "ActualComponentIdentity",
    "ActualEvidence",
    "ActualIdentityUnavailable",
    "ActualPlatformSnapshot",
    "ArtifactSource",
    "BindingEvaluationContext",
    "BindingIdentity",
    "ComponentBinding",
    "ComponentObservation",
    "ComponentRecord",
    "ComponentRuntimeBinding",
    "DependencyEndpoint",
    "Deployment",
    "DeploymentEnvironment",
    "DeploymentEvent",
    "DeploymentExecutionFailed",
    "DeploymentInputRejected",
    "DeploymentOperationsError",
    "DeploymentRecord",
    "DeploymentRequest",
    "DeploymentStateError",
    "DeploymentStateStore",
    "EventJournal",
    "EvidenceCorrelation",
    "EvidenceFreshness",
    "EvidenceProvenance",
    "FailureRecord",
    "HealthCheckFailed",
    "HealthEvaluation",
    "IdentityCorrespondenceResult",
    "IdentityField",
    "IdentityVerificationFailed",
    "InstanceReference",
    "InstanceVerification",
    "InvalidDeploymentStateTransition",
    "LocalProcessRuntime",
    "MaterializedComponent",
    "MigrationBinding",
    "MigrationOrchestrationFailed",
    "MigrationRecord",
    "OperationalAction",
    "OwnerSuppliedPlatformIdentityProvider",
    "PlatformIdentityBinding",
    "PlatformIdentityProvider",
    "PlatformIdentitySurface",
    "PresenceState",
    "ProvisionedEnvironment",
    "ProvisioningFailed",
    "Reconciliation",
    "ReconciliationDriftDetected",
    "ReconciliationEvidenceUnavailable",
    "ReconciliationRecord",
    "ReconciliationRequest",
    "Restart",
    "RestartFailed",
    "RestartInProgress",
    "RestartPhaseRecord",
    "RestartRecord",
    "RestartRequest",
    "RollbackRequest",
    "RunningPlatformIdentitySource",
    "RuntimeAdapter",
    "RuntimeElement",
    "RuntimeHandle",
    "RuntimeProcessError",
    "SecretLeakRefused",
    "StartupFailed",
    "StopStartPolicy",
    "UpgradeRequest",
    "attach",
    "build_elements",
    "canonical_digest",
    "compute_actual_digest",
    "default_source_paths",
    "deploy",
    "derive_deployment_id",
    "derive_reconciliation_id",
    "derive_restart_id",
    "derive_rollback_id",
    "derive_upgrade_id",
    "establish_identity_correspondence",
    "evaluate_health",
    "load_environment",
    "load_record",
    "project_actual_identity",
    "provision",
    "reconcile",
    "render_environment",
    "require_correlated_evidence",
    "resolve_artifact",
    "restart",
    "rollback",
    "upgrade",
    "utc_now",
    "validate_actual_evidence",
    "validate_actual_surface",
    "validate_environment",
    "validate_stop_start_policy",
    "verify_artifact_digest",
    "verify_identity",
    "verify_input_unchanged",
    "verify_instance",
]
