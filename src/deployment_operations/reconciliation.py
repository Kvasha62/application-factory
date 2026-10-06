"""Actual-vs-desired reconciliation — the drift signal of the boundary.

ADR-0016 §18 fixes the rule this module implements, and no more than it:

> Unmanaged drift — actual state diverging from desired state without an
> operation — is surfaced as a failure signal, not silently patched.

ADR-0016 §19 places reconciliation results in operational observability,
correlatable with the Platform Instance digest they relate to; ADR-0017 §11 and
§15 place ongoing runtime management, drift surfacing and the full
reconciliation surface after the initial deployment slice; ADR-0017 §44 requires
this later slice to have its own approved work item — this module is that slice.

The operation is deliberately narrow:

* **desired state is exact and already fixed.** It is derived from one existing
  authoritative deployment record — the identity, version and digest that
  deployment pinned and verified. There is no «reconcile the platform called X»,
  no floating selector, no second desired state, and desired state is never
  edited here (ADR-0016 §5, §7, §8).
* **actual state is independently observed.** It is obtained through the
  existing Running Platform observation boundary
  (:class:`~deployment_operations.platform_identity.PlatformIdentityProvider`),
  which receives only an opaque evaluation binding. The binding is specific to
  this observation, so evidence correlated to an earlier evaluation — including
  the deployment's own evidence — is refused instead of being replayed, and the
  expected identity is never passed to the observer (ADR-0016 §18, §19;
  ADR-0019/0020).
* **the comparison reuses the existing contract.** The actual instance digest is
  recomputed by the existing Platform Instance canonicalizer over independently
  observed actual content and compared with the pinned digest by
  :func:`~deployment_operations.platform_identity.establish_identity_correspondence`;
  identity, version and digest are additionally compared field by field so a
  divergence is attributable.
* **fail-closed is the only alternative to proof.** A proven divergence,
  unavailable evidence, an invalid surface or stale evidence is recorded in
  authoritative deployment state and published as a correlated operational
  signal, and the operation then raises. Unproven health is never reported.
* **one observation, one record.** An outcome is appended to the exact
  authoritative record the observation was made against, and that write is
  confirmed before any signal is published: a record that changed while the
  observation was being made — a concurrent reconciliation, any other state
  transition — is refused instead of overwritten, so the history stays
  append-only and no observation number repeats.
* **there is no remediation path.** Reconciliation observes and publishes only.
  It never deploys, upgrades, rolls back, restarts, stops, replaces or
  substitutes anything, never mutates a Manifest or an Instance, and never
  repairs drift; repeating it against unchanged evidence has no deployment or
  runtime side effects (ADR-0016 §7, §20; ADR-0017 §39).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from deployment_operations.deployment import (
    Deployment,
    deployment_record_basis,
    evaluation_binding,
)
from deployment_operations.errors import (
    DeploymentInputRejected,
    DeploymentStateError,
    InvalidDeploymentStateTransition,
    ReconciliationDriftDetected,
    ReconciliationEvidenceUnavailable,
)
from deployment_operations.events import (
    EVENT_RECONCILIATION_DRIFT_DETECTED,
    EVENT_RECONCILIATION_IN_CORRESPONDENCE,
    EVENT_RECONCILIATION_UNVERIFIABLE,
    DeploymentEvent,
    EventJournal,
)
from deployment_operations.platform_identity import (
    ActualEvidence,
    ActualIdentityUnavailable,
    IdentityCorrespondenceResult,
    PlatformIdentityProvider,
    compute_actual_digest,
    establish_identity_correspondence,
    project_actual_identity,
    require_correlated_evidence,
)
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from deployment_operations.state import (
    LIFECYCLE_REALIZED,
    RECONCILIATION_DRIFT,
    RECONCILIATION_IN_CORRESPONDENCE,
    RECONCILIATION_UNVERIFIABLE,
    ComponentRecord,
    DeploymentRecord,
    DeploymentStateStore,
    ReconciliationRecord,
    utc_now,
)
from platform_manifest.lifecycle import LIFECYCLE_STATES
from platform_manifest.validation import (
    ARTIFACT_TYPES,
    COMPONENT_ID_PATTERN,
    MANIFEST_ID_PATTERN,
    SHA256_PATTERN,
    is_floating_selector,
    parse_semver,
)
from running_platform.owner_state import (
    FileRunningPlatformOwnerStateReader,
    OwnerStateSnapshotSource,
)

#: The lifecycle position a reconciliation subject must be in: the record of the
#: operation that realized the Platform Instance and still states that it runs.
_RECONCILABLE_LIFECYCLE = LIFECYCLE_REALIZED

#: The event published for each outcome. One completed observation publishes
#: exactly one signal, and no outcome is ever published as another.
_EVENT_BY_OUTCOME = {
    RECONCILIATION_IN_CORRESPONDENCE: EVENT_RECONCILIATION_IN_CORRESPONDENCE,
    RECONCILIATION_DRIFT: EVENT_RECONCILIATION_DRIFT_DETECTED,
    RECONCILIATION_UNVERIFIABLE: EVENT_RECONCILIATION_UNVERIFIABLE,
}


@dataclass(frozen=True)
class ReconciliationRequest:
    """One reconciliation of one authoritative deployment record.

    ``deployment`` is the existing authoritative record: the exact identity,
    version and digest it pinned and verified are the desired state, and its
    state file is where the outcome is recorded. ``reconciliation_id`` must
    equal :func:`derive_reconciliation_id` of that subject — it names the exact
    deployment operation and Platform Instance being reconciled, never a name.
    """

    deployment: Deployment
    reconciliation_id: str


@dataclass(frozen=True)
class Reconciliation:
    """The completed observation of one reconciliation."""

    deployment: Deployment
    observation: ReconciliationRecord

    @property
    def outcome(self) -> str:
        """The recorded outcome: correspondence, drift or unverifiable."""
        return self.observation.outcome

    @property
    def in_correspondence(self) -> bool:
        """True when the observed platform is the exact desired instance."""
        return self.observation.in_correspondence

    @property
    def drifted(self) -> bool:
        """True when a divergence between desired and actual state was proven."""
        return self.observation.drifted


def derive_reconciliation_id(deployment: Deployment) -> str:
    """Derive the correlation id of the reconciliation subject.

    It names the exact deployment operation, platform identity and instance
    digest — never a human-readable name and never a floating selector — so the
    request cannot be retargeted at another platform (ADR-0016 §8).
    """
    record = deployment.record
    return (
        f"recon:{record.deployment_id}"
        f"@platform:{record.platform_id}"
        f"@instance:{record.instance_digest}"
        f"@{record.environment_id}"
    )


def reconcile(
    request: ReconciliationRequest,
    *,
    identity_provider: PlatformIdentityProvider | None = None,
) -> Reconciliation:
    """Observe the Running Platform and compare it with the exact desired state.

    Observation and publication only: the operation records what it observed in
    authoritative deployment state and publishes one correlated operational
    signal, and it takes no runtime action of any kind. A proven divergence,
    unavailable or stale evidence raises fail-closed after the outcome has been
    recorded — the caller never receives a successful reconciliation of a
    platform that does not correspond to its desired state.
    """
    deployment = request.deployment
    _validate(request)

    record = deployment.record
    observation = _observe(
        identity_provider or _default_identity_provider(deployment),
        request,
        desired=_desired_identity(record),
    )
    recorded = _record_observation(deployment, request, observation)

    if recorded.outcome == RECONCILIATION_DRIFT:
        raise ReconciliationDriftDetected(
            recorded.differences
            or ["the observed actual instance digest differs from the pinned one"],
            instance_digest=record.instance_digest,
            actual_instance_digest=_actual_digest(recorded),
        )
    if recorded.outcome == RECONCILIATION_UNVERIFIABLE:
        raise ReconciliationEvidenceUnavailable(
            recorded.errors or ["actual Running Platform evidence is unavailable"]
        )
    return Reconciliation(deployment=deployment, observation=recorded)


# ---------------------------------------------------------------------------
# Desired state — from the authoritative record only
# ---------------------------------------------------------------------------


def _desired_identity(record: DeploymentRecord) -> dict[str, Any]:
    """Project the exact desired state out of the authoritative record.

    Nothing is invented and nothing is smoothed over: the pinned instance digest,
    the bound Manifest identity/version/digest and every pinned component
    identity/version/artifact digest are read from the record of what was
    verified at deployment time.
    """
    return {
        "platform_id": record.platform_id,
        "instance_digest": record.instance_digest,
        "manifest": {
            "manifest_id": record.manifest_id,
            "manifest_version": record.manifest_version,
            "manifest_digest": record.manifest_digest,
        },
        "manifest_state": record.manifest_state,
        "components": sorted(
            (
                {
                    "component_id": entry.component_id,
                    "component_version": entry.component_version,
                    "artifact_digest": entry.artifact_digest,
                }
                for entry in record.components
            ),
            key=lambda entry: str(entry["component_id"]),
        ),
    }


# ---------------------------------------------------------------------------
# Actual state — through the existing observation boundary
# ---------------------------------------------------------------------------


def _default_identity_provider(deployment: Deployment) -> PlatformIdentityProvider:
    """Read the owner-published actual identity surface of this environment.

    ``DeploymentStateStore`` keeps the record under
    ``<runtime_root>/operations``, so the runtime root of the environment that
    realized the instance — and therefore the owner-published surface
    reconciliation reads — is the state file's grandparent directory. This is
    the same seam the deployment operation reads, with a binding of this
    observation rather than of the deployment: the Running Platform owner
    decides how the fact is produced (ADR-0019/0020).
    """
    runtime_root = deployment.state_path.parent.parent
    return OwnerSuppliedPlatformIdentityProvider(
        OwnerStateSnapshotSource(
            FileRunningPlatformOwnerStateReader(
                runtime_root / "running_platform_identity.json"
            )
        )
    )


#: The authority that establishes the binding of one reconciliation observation.
AUTHORITY_RECONCILE = "deployment-operations/reconcile"


def _binding_token(record: DeploymentRecord, sequence: int) -> str:
    """The opaque binding of one observation — never of an earlier one.

    The binding names the evaluation target the way the deployment path already
    does — the deployment operation of this environment — extended with this
    observation number. It carries no expected Platform Instance document and no
    expected digest for the owner to confirm, and evidence is correlated by the
    owner to the evaluation it was produced for: deployment-time evidence, or
    evidence produced for a previous reconciliation, is therefore refused as
    foreign correlation instead of being replayed as if it were current.
    """
    return f"{record.deployment_id}#observation:{sequence}"


def _observe(
    provider: PlatformIdentityProvider,
    request: ReconciliationRequest,
    *,
    desired: Mapping[str, Any],
) -> ReconciliationRecord:
    """Obtain actual evidence, recompute the actual identity, compare.

    Every failure of the observation boundary — a source that raises, a
    malformed envelope, an invalid surface, stale evidence, or evidence that
    cannot be projected — produces an ``unverifiable`` observation. Unavailable
    evidence is never treated as health, and the expected identity is never
    consulted as a substitute (ADR-0016 §18, §20).
    """
    record = request.deployment.record
    sequence = len(record.reconciliations) + 1
    occurred_at = utc_now()
    binding = evaluation_binding(
        authority=AUTHORITY_RECONCILE,
        token=_binding_token(record, sequence),
        basis=deployment_record_basis(record.deployment_id),
        target=str(record.platform_id),
        scope=record.environment_id,
        sequence=sequence,
        established_at=occurred_at,
    )
    expected = {"instance_digest": desired.get("instance_digest")}
    try:
        evidence = provider.observe_identity(binding)
        if not isinstance(evidence, ActualEvidence):
            raise ActualIdentityUnavailable(
                "the observation surface returned no actual evidence"
            )
        # The observation must belong to this binding before it says anything
        # about this platform (ADR-0019 §26, ADR-0020 §18).
        require_correlated_evidence(binding, evidence)
        # Actual identity is recomputed from what was independently observed,
        # through the existing projection and the existing canonicalizer.
        projected = project_actual_identity(evidence)
        actual_digest = compute_actual_digest(evidence)
        correspondence = establish_identity_correspondence(
            expected, evidence, binding=binding
        )
    except ActualIdentityUnavailable as error:
        return ReconciliationRecord(
            reconciliation_id=request.reconciliation_id,
            sequence=sequence,
            outcome=RECONCILIATION_UNVERIFIABLE,
            occurred_at=occurred_at,
            desired=dict(desired),
            errors=(str(error),),
        )
    except Exception as error:  # noqa: BLE001 - any answer must fail closed
        return ReconciliationRecord(
            reconciliation_id=request.reconciliation_id,
            sequence=sequence,
            outcome=RECONCILIATION_UNVERIFIABLE,
            occurred_at=occurred_at,
            desired=dict(desired),
            errors=(
                (
                    "the Running Platform observation boundary failed closed: "
                    f"{error.__class__.__name__}: {error}"
                ),
            ),
        )

    actual = _actual_identity(projected, actual_digest)
    differences = _differences(desired, actual)
    if correspondence == IdentityCorrespondenceResult.MATCH:
        outcome = (
            RECONCILIATION_IN_CORRESPONDENCE
            if not differences
            else RECONCILIATION_DRIFT
        )
    elif correspondence == IdentityCorrespondenceResult.MISMATCH:
        outcome = RECONCILIATION_DRIFT
    else:
        outcome = RECONCILIATION_UNVERIFIABLE
    return ReconciliationRecord(
        reconciliation_id=request.reconciliation_id,
        sequence=sequence,
        outcome=outcome,
        occurred_at=occurred_at,
        desired=dict(desired),
        actual=actual,
        differences=differences,
        evidence={
            "provenance": evidence.provenance,
            "freshness_current": evidence.freshness.current,
        },
        errors=(
            ()
            if outcome != RECONCILIATION_UNVERIFIABLE
            else (
                (
                    "identity correspondence could not be established from the "
                    "observed evidence"
                ),
            )
        ),
    )


def _actual_identity(
    projected: Mapping[str, Any], actual_digest: str
) -> dict[str, Any]:
    """Shape the independently projected actual identity for comparison."""
    manifest = projected.get("manifest")
    bound: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}
    components = projected.get("components")
    entries: list[dict[str, Any]] = []
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, Mapping):
                continue
            artifact = component.get("artifact")
            pinned: Mapping[str, Any] = (
                artifact if isinstance(artifact, Mapping) else {}
            )
            entries.append(
                {
                    "component_id": component.get("component_id"),
                    "component_version": component.get("component_version"),
                    "artifact_digest": pinned.get("digest"),
                }
            )
    return {
        "platform_id": projected.get("platform_id"),
        "instance_digest": actual_digest,
        "manifest": {
            "manifest_id": bound.get("manifest_id"),
            "manifest_version": bound.get("manifest_version"),
            "manifest_digest": bound.get("manifest_digest"),
        },
        "manifest_state": projected.get("manifest_state"),
        "components": sorted(entries, key=lambda entry: str(entry["component_id"])),
    }


def _differences(
    desired: Mapping[str, Any], actual: Mapping[str, Any]
) -> tuple[str, ...]:
    """Name every identity/version/digest field that provably diverged."""
    fields: list[str] = []
    if desired.get("platform_id") != actual.get("platform_id"):
        fields.append("platform_id")
    if desired.get("manifest_state") != actual.get("manifest_state"):
        fields.append("manifest_state")
    desired_manifest = desired.get("manifest")
    actual_manifest = actual.get("manifest")
    wanted_manifest: Mapping[str, Any] = (
        desired_manifest if isinstance(desired_manifest, Mapping) else {}
    )
    observed_manifest: Mapping[str, Any] = (
        actual_manifest if isinstance(actual_manifest, Mapping) else {}
    )
    for name in ("manifest_id", "manifest_version", "manifest_digest"):
        if wanted_manifest.get(name) != observed_manifest.get(name):
            fields.append(f"manifest.{name}")
    if desired.get("instance_digest") != actual.get("instance_digest"):
        fields.append("instance_digest")

    wanted = _components_by_id(desired)
    observed = _components_by_id(actual)
    for component_id in sorted(wanted.keys() - observed.keys()):
        fields.append(f"components[{component_id}].absent")
    for component_id in sorted(observed.keys() - wanted.keys()):
        fields.append(f"components[{component_id}].unexpected")
    for component_id in sorted(wanted.keys() & observed.keys()):
        if wanted[component_id].get("component_version") != observed[component_id].get(
            "component_version"
        ):
            fields.append(f"components[{component_id}].component_version")
        if wanted[component_id].get("artifact_digest") != observed[component_id].get(
            "artifact_digest"
        ):
            fields.append(f"components[{component_id}].artifact_digest")
    return tuple(fields)


def _components_by_id(identity: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    components = identity.get("components")
    if not isinstance(components, list):
        return {}
    return {
        str(component.get("component_id")): component
        for component in components
        if isinstance(component, Mapping)
    }


def _actual_digest(observation: ReconciliationRecord) -> str | None:
    actual = observation.actual
    if not isinstance(actual, Mapping):
        return None
    digest = actual.get("instance_digest")
    return digest if isinstance(digest, str) else None


# ---------------------------------------------------------------------------
# Publication — state first, then one matching signal
# ---------------------------------------------------------------------------


def _record_observation(
    deployment: Deployment,
    request: ReconciliationRequest,
    observation: ReconciliationRecord,
) -> ReconciliationRecord:
    """Record the observation, then publish exactly its own signal.

    Deployment state is written first and the journal second, so a failed
    publication can never leave a signal about an outcome that state does not
    hold, and a failed state write publishes nothing at all: the operation
    raises without recording a contradiction (ADR-0016 §9, §19, §20).

    The write is a compare-and-swap against the record this observation was made
    against, and the persisted result is confirmed before anything is published.
    An observation is a fact about one observation of one authoritative record:
    if that record changed while the observation was being made — a concurrent
    reconciliation, any other state transition — the observation is refused
    instead of overwriting the other outcome or repeating its number. Nothing
    was then published, and the caller re-reads the authoritative record before
    repeating the operation (which is side-effect free, §20).
    """
    base = deployment.record
    updated = base.with_reconciliation(observation, at=observation.occurred_at)
    _refuse_if_superseded(deployment, base)
    _write_state(deployment, updated)
    _confirm_persisted(deployment, updated)
    deployment.record = updated

    journal = _journal_for(deployment)
    journal.append(
        _event(deployment, request, updated.reconciliations[-1]),
        secrets=deployment._secrets,
    )
    return updated.reconciliations[-1]


def _journal_for(deployment: Deployment) -> EventJournal:
    """The operation's journal, attached to the handle when it has none.

    A handle created without a journal still publishes into the journal of its
    deployment operation — and the handle must then report those signals, or
    ``Deployment.events()`` would hide the outcome reconciliation published.
    """
    if deployment._journal is None:
        deployment._journal = EventJournal(deployment.events_path)
    return deployment._journal


def _persisted_state(deployment: Deployment) -> DeploymentRecord:
    """The persisted authoritative record, or a fail-closed error."""
    try:
        if deployment._store is not None:
            return deployment._store.read()
        return DeploymentStateStore(deployment.state_path).read()
    except DeploymentStateError:
        raise
    except Exception as error:
        raise DeploymentStateError(
            "the persisted deployment state is unavailable: "
            f"{error.__class__.__name__}"
        ) from error


def _write_state(deployment: Deployment, record: DeploymentRecord) -> None:
    """Persist state through the operation's store, never without its secrets.

    The secret-leak guard of the boundary applies to every write of this
    capability (ADR-0016 §12), including a write made through a handle that
    carries no store object of its own.
    """
    if deployment._store is not None:
        deployment._store.write(record, secrets=deployment._secrets)
    else:
        DeploymentStateStore(deployment.state_path).write(
            record, secrets=deployment._secrets
        )


def _refuse_if_superseded(deployment: Deployment, base: DeploymentRecord) -> None:
    """Refuse to append to a record that is no longer the persisted one."""
    if _persisted_state(deployment) != base:
        raise InvalidDeploymentStateTransition(
            "the authoritative deployment state changed while this reconciliation "
            "was observing; the observation is refused and nothing was published"
        )


def _confirm_persisted(deployment: Deployment, expected: DeploymentRecord) -> None:
    """Fail closed when this write did not become the persisted record."""
    if _persisted_state(deployment) != expected:
        raise DeploymentStateError(
            "the reconciliation observation was not persisted as the authoritative "
            "record; nothing was published"
        )


def _event(
    deployment: Deployment,
    request: ReconciliationRequest,
    observation: ReconciliationRecord,
) -> DeploymentEvent:
    """The one signal of this observation, correlated with the pinned instance."""
    record = deployment.record
    return replace(
        DeploymentEvent(
            sequence=0,
            event=_EVENT_BY_OUTCOME[observation.outcome],
            occurred_at=observation.occurred_at,
            deployment_id=record.deployment_id,
            environment_id=record.environment_id,
            platform_id=record.platform_id,
            instance_digest=record.instance_digest,
            manifest_id=record.manifest_id,
            manifest_version=record.manifest_version,
            manifest_digest=record.manifest_digest,
            stage="reconciliation",
            detail={
                "reconciliation_id": request.reconciliation_id,
                "reconciliation_sequence": observation.sequence,
                "outcome": observation.outcome,
                "actual_instance_digest": _actual_digest(observation),
                "differences": list(observation.differences),
                "evidence_provenance": (
                    observation.evidence.get("provenance")
                    if isinstance(observation.evidence, Mapping)
                    else None
                ),
                "errors": list(observation.errors),
            },
        ),
        sequence=_journal_for(deployment).next_sequence(),
    )


# ---------------------------------------------------------------------------
# Input — the exact subject, and nothing else
# ---------------------------------------------------------------------------


def _check_persisted(deployment: Deployment) -> None:
    """Refuse to reconcile anything other than the persisted authoritative record."""
    try:
        persisted = _persisted_state(deployment)
    except Exception as error:
        raise InvalidDeploymentStateTransition(
            "the persisted deployment state is unavailable"
        ) from error
    if persisted != deployment.record:
        raise InvalidDeploymentStateTransition(
            "the persisted deployment state changed or was tampered with"
        )


def _component_errors(components: Sequence[ComponentRecord]) -> list[str]:
    """Every pinned component must carry a complete, self-consistent identity.

    The desired state of a reconciliation is exact or it is not a desired state:
    a component entry whose identity, version or artifact identity is missing or
    contradictory is refused before any observation, exactly as an inexact
    instance digest is.
    """
    errors: list[str] = []
    seen: set[str] = set()
    for entry in components:
        component_id = entry.component_id
        if (
            not isinstance(component_id, str)
            or re.fullmatch(COMPONENT_ID_PATTERN, component_id) is None
        ):
            errors.append(
                f"$.components[{component_id!r}]: a concrete component identity "
                "is required"
            )
            continue
        if component_id in seen:
            errors.append(
                f"$.components[{component_id}]: the record lists this component twice"
            )
        seen.add(component_id)
        if parse_semver(entry.component_version) is None:
            errors.append(
                f"$.components[{component_id}].component_version: a concrete "
                "component version is required"
            )
        errors.extend(_artifact_errors(component_id, entry))
    return errors


def _artifact_errors(component_id: str, entry: ComponentRecord) -> list[str]:
    """The artifact identity of one component is sealed, or explicitly absent."""
    errors: list[str] = []
    artifact_type = entry.artifact_type
    digest = entry.artifact_digest
    if artifact_type == "none":
        if digest is not None:
            errors.append(
                f"$.components[{component_id}].artifact: artifact_type none "
                f"carries the digest {digest!r}"
            )
        return errors
    if artifact_type not in ARTIFACT_TYPES:
        errors.append(
            f"$.components[{component_id}].artifact: the artifact identity is "
            "incomplete"
        )
        return errors
    if not isinstance(digest, str) or re.fullmatch(SHA256_PATTERN, digest) is None:
        errors.append(
            f"$.components[{component_id}].artifact: a sealed artifact digest is "
            "required"
        )
    return errors


def _validate(request: ReconciliationRequest) -> None:
    """Refuse deterministically before any observation (ADR-0016 §8, §20)."""
    deployment = request.deployment
    record = deployment.record
    if request.reconciliation_id != derive_reconciliation_id(deployment):
        raise DeploymentInputRejected(
            ["reconciliation_id does not match the exact deployment and instance"],
            stage="reconciliation",
        )
    _check_persisted(deployment)

    errors: list[str] = []
    if record.lifecycle != _RECONCILABLE_LIFECYCLE:
        errors.append(
            "the record is not a realized deployment; reconciliation applies to "
            "the operation that realized this Platform Instance"
        )
    if not record.identity_verified:
        errors.append(
            "the record carries no verified identity/version/digest "
            "correspondence; there is no exact desired state to reconcile against"
        )
    if record.failure is not None:
        errors.append("the record states a failure, not a realized platform")
    if not record.running:
        errors.append(
            "the record states that the platform's runtime elements are not "
            "running; there is no Running Platform to observe"
        )
    digest = record.instance_digest
    if not isinstance(digest, str) or re.fullmatch(SHA256_PATTERN, digest) is None:
        errors.append(
            "the record does not pin a concrete instance digest; a name-only or "
            "floating desired state cannot be reconciled"
        )
    platform_id = record.platform_id
    if not isinstance(platform_id, str) or not platform_id.strip():
        errors.append("the record does not carry a concrete Platform Instance identity")
    elif is_floating_selector(platform_id):
        errors.append(
            "$.platform_id: the record carries a floating selector, not a stable "
            "Platform Instance identity"
        )
    manifest_id = record.manifest_id
    if (
        not isinstance(manifest_id, str)
        or re.fullmatch(MANIFEST_ID_PATTERN, manifest_id) is None
    ):
        errors.append("the record does not carry a concrete Manifest identity")
    if parse_semver(record.manifest_version) is None:
        errors.append("the record does not carry a concrete Manifest version")
    manifest_digest = record.manifest_digest
    if (
        not isinstance(manifest_digest, str)
        or re.fullmatch(SHA256_PATTERN, manifest_digest) is None
    ):
        errors.append("the record does not pin a concrete Manifest digest")
    if record.manifest_state not in LIFECYCLE_STATES:
        errors.append("the record does not carry a Platform Manifest lifecycle state")
    if not record.components:
        errors.append(
            "the record carries no component identity/version/artifact digest; "
            "the desired state is not exact"
        )
    errors.extend(_component_errors(record.components))
    if errors:
        raise DeploymentInputRejected(errors, stage="reconciliation")


__all__ = [
    "Reconciliation",
    "ReconciliationRequest",
    "derive_reconciliation_id",
    "reconcile",
]
