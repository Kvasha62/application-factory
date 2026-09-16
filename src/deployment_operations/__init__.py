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

What is deliberately absent: upgrade, rollback, ``superseded`` handling, drift
reconciliation, fleet management, autoscaling, scheduling and any general
deployment platform (ADR-0017 §38–§39). Nothing in this package owns business
data, opens a component database, creates an SCS or a new component, or turns
the Factory into a deployment engine (ADR-0016 §6, §21, §22; LAW-03, LAW-04).

Public surface:

* :func:`deploy` — realize one accepted Platform Instance in one environment;
* :class:`DeploymentRequest` / :class:`InstanceReference` — the deployment input;
* :class:`Deployment` — the completed operation, its record and its platform;
* :class:`DeploymentEnvironment` / :func:`load_environment` — the operational
  side: where an instance is realized here, and through which entrypoints;
* :func:`verify_instance` — the exact identity/version/digest verification;
* :class:`DeploymentRecord` / :class:`DeploymentStateStore` — deployment state;
* :class:`DeploymentEvent` / :class:`EventJournal` — the operational signals;
* :class:`LocalProcessRuntime` — the replaceable runtime adapter of this slice.
"""

from __future__ import annotations

from deployment_operations.deployment import (
    Deployment,
    DeploymentRequest,
    InstanceReference,
    default_source_paths,
    deploy,
)
from deployment_operations.environment import (
    IDENTITY_CONFIGURATION_KEYS,
    ComponentRuntimeBinding,
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
from deployment_operations.provisioning import (
    ArtifactSource,
    ProvisionedEnvironment,
    provision,
    resolve_artifact,
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
    STAGES,
    ComponentRecord,
    DeploymentRecord,
    DeploymentStateStore,
    FailureRecord,
    MigrationRecord,
    OperationalAction,
    derive_deployment_id,
    load_record,
    utc_now,
)
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
    "STAGES",
    "ArtifactSource",
    "ComponentBinding",
    "ComponentObservation",
    "ComponentRecord",
    "ComponentRuntimeBinding",
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
    "FailureRecord",
    "HealthCheckFailed",
    "HealthEvaluation",
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
    "ProvisionedEnvironment",
    "ProvisioningFailed",
    "RuntimeAdapter",
    "RuntimeElement",
    "RuntimeHandle",
    "RuntimeProcessError",
    "SecretLeakRefused",
    "StartupFailed",
    "build_elements",
    "canonical_digest",
    "default_source_paths",
    "deploy",
    "derive_deployment_id",
    "evaluate_health",
    "load_environment",
    "load_record",
    "provision",
    "render_environment",
    "resolve_artifact",
    "utc_now",
    "validate_environment",
    "verify_artifact_digest",
    "verify_identity",
    "verify_input_unchanged",
    "verify_instance",
]
