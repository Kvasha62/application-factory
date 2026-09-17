"""Fail-closed error taxonomy of Deployment & Operations (ADR-0016 §20).

Every error raised by this capability carries the stage of the initial
deployment path (ADR-0017 §36) at which the operation stopped, so a failure is
always attributable and recordable in deployment state (ADR-0016 §9, §20):

    requested → validated → provisioning → deploying → starting
        → health_check → ready

Nothing here is retried silently, repaired or worked around: a failure is a
fail-closed state, never a reason to alter composition, versions, artifacts or
ownership (ADR-0016 §7, §20). These errors are the vocabulary of that failure;
deployment state and the operational event journal are its record.
"""

from __future__ import annotations

from collections.abc import Sequence


class DeploymentOperationsError(Exception):
    """Base class for Deployment & Operations errors."""

    def __init__(
        self,
        message: str,
        *,
        errors: Sequence[str] = (),
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.errors: list[str] = list(errors)
        self.stage = stage

    def diagnostics(self) -> str:
        """Return the failure and every recorded reason, one per line."""
        return "\n".join([str(self), *self.errors])


class DeploymentInputRejected(DeploymentOperationsError):
    """The deployment input was rejected before any action (fail-closed).

    ADR-0016 §8: an unverifiable, name-only or digest-mismatching Platform
    Instance reference is refused deterministically, before provisioning,
    deployment or any runtime action. ADR-0016 §7 / ARCHITECTURE.md §1.3: a
    floating selector anywhere in the deployment input is the same refusal.
    """

    def __init__(self, errors: Sequence[str], *, stage: str = "validated") -> None:
        message = "the deployment input was rejected; no action was taken"
        super().__init__(message, errors=errors, stage=stage)


class ProvisioningFailed(DeploymentOperationsError):
    """The runtime environment could not be prepared for the instance (§11)."""

    def __init__(self, errors: Sequence[str]) -> None:
        message = "provisioning failed; the environment does not satisfy the instance"
        super().__init__(message, errors=errors, stage="provisioning")


class DeploymentExecutionFailed(DeploymentOperationsError):
    """The instance could not be realized into its runtime environment (§10)."""

    def __init__(self, errors: Sequence[str]) -> None:
        message = "deployment execution failed; the instance was not realized"
        super().__init__(message, errors=errors, stage="deploying")


class MigrationOrchestrationFailed(DeploymentOperationsError):
    """A component-defined required migration failed (ADR-0016 §14, LAW-08).

    Migration content belongs to the component; orchestrating its execution
    belongs to this capability. A failed migration is a fail-closed state that
    leaves deployment state honest — never a false ``deployed`` (§20).
    """

    def __init__(
        self,
        errors: Sequence[str],
        *,
        component_id: str,
        migration_id: str | None = None,
    ) -> None:
        message = f"migration orchestration failed for component {component_id!r}"
        super().__init__(message, errors=errors, stage="deploying")
        self.component_id = component_id
        self.migration_id = migration_id


class StartupFailed(DeploymentOperationsError):
    """A runtime element of the platform did not start (§18)."""

    def __init__(self, errors: Sequence[str]) -> None:
        message = "runtime start failed; the platform is not running"
        super().__init__(message, errors=errors, stage="starting")


class HealthCheckFailed(DeploymentOperationsError):
    """Health/readiness verification did not establish ``ready`` (§18, §19)."""

    def __init__(self, errors: Sequence[str]) -> None:
        message = "health/readiness verification failed; the platform is not ready"
        super().__init__(message, errors=errors, stage="health_check")


class IdentityVerificationFailed(DeploymentOperationsError):
    """The running platform does not correspond to the desired state (§8, §10).

    ``ready`` alone fixes nothing: an honest ``realized``/``deployed`` claim
    requires, in addition to the verified operational condition, the exact
    identity/version/digest verification of what is actually running
    (ADR-0017 §34). A mismatch here forbids the claim.
    """

    def __init__(self, errors: Sequence[str], *, stage: str = "ready") -> None:
        message = (
            "identity/version/digest verification failed; the platform is not "
            "deployed"
        )
        super().__init__(message, errors=errors, stage=stage)


class DeploymentStateError(DeploymentOperationsError):
    """Deployment state could not be read, written or trusted (§9).

    Deployment state is the authoritative operational record of actual state:
    a record that cannot be persisted, or one that would overstate reality,
    is a failure, never a silent fallback.
    """

    def __init__(self, message: str, *, errors: Sequence[str] = ()) -> None:
        super().__init__(message, errors=errors)


class InvalidDeploymentStateTransition(DeploymentStateError):
    """An illegal transition of deployment state was attempted (§9, §10).

    Example: fixing ``realized`` for an operation that is not ``ready`` or
    whose identity/version/digest verification has not confirmed. The record
    refuses the transition instead of reporting an unverified claim.
    """

    def __init__(self, message: str, *, errors: Sequence[str] = ()) -> None:
        super().__init__(message, errors=errors)


class SecretLeakRefused(DeploymentOperationsError):
    """Operational secrets were about to be persisted; the write is refused.

    ADR-0016 §12: secrets are operational inputs injected at the boundary.
    They are never Manifest/Instance content and are never copied into
    deployment state records, logs or observability signals. A write that
    would persist secret material fails closed instead of leaking it.
    """

    def __init__(self, errors: Sequence[str]) -> None:
        message = "a deployment record or event would contain secret material"
        super().__init__(message, errors=errors)


__all__ = [
    "DeploymentExecutionFailed",
    "DeploymentInputRejected",
    "DeploymentOperationsError",
    "DeploymentStateError",
    "HealthCheckFailed",
    "IdentityVerificationFailed",
    "InvalidDeploymentStateTransition",
    "MigrationOrchestrationFailed",
    "ProvisioningFailed",
    "SecretLeakRefused",
    "StartupFailed",
]
