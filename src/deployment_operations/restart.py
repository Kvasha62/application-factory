"""Ongoing runtime management — the explicit restart and the stop/start policy.

Runtime management is part of the full responsibility of this capability
(ADR-0016 §18; ADR-0017 §4, §11). The first implementation slice deliberately
held only the minimum needed to start a platform and evaluate its
health/readiness inside one deployment operation; this module is the later
stage ADR-0017 §39 defers and §44 requires an approved work item of its own
for: **controlled re-execution of an already realized runtime**, as its own
explicit operation.

One restart attempt is one controlled cycle over the runtime elements of one
existing deployment operation::

    running
      ↓
    stop                                    (the references are released: detach)
      ↓
    execution/content verification          (the same binding, the same bytes)
      ↓
    fresh start                             (the same elements, re-executed)
      ↓
    health/readiness verification           (fresh evidence, never inherited)
      ↓
    identity re-verification                (the same instance, confirmed again)
      ↓
    ready / running

What the operation is — and what it is not:

* **it is not a new deployment.** No Platform Instance is assembled, selected,
  re-resolved or replaced: the attempt re-executes the runtime elements the
  deployment operation already bound, from the runtime spec it already wrote,
  under the identity, version, artifact digest and execution/content binding it
  already pinned and verified (ADR-0016 §5, §7, §8).
* **it is not a desired-state operation.** Nothing here edits a Manifest, an
  Instance, a pinned component identity or any other authoritative desired
  state, and no new deployment operation is created: the attempt is appended to
  the *existing* authoritative record of the operation whose runtime it
  re-executes (ADR-0016 §7, §9).
* **it is not a repair.** A proven divergence stays a fact of the reconciliation
  history: a restart neither erases nor rewrites it, records no reconciliation
  observation of its own, and never substitutes content to make a platform
  correspond (ADR-0016 §18, §20; ADR-0017 §39).
* **it is not a lifecycle transition.** ``realized`` states what the deployment
  operation did and verified; the operational conditions state how the platform
  is *now*. A restart therefore moves ``running``/``ready`` and appends
  runtime-management evidence, and it invents no lifecycle position
  (ADR-0016 §9; ADR-0017 §35).
* **it is honest about every phase.** ``RuntimeAdapter.stop`` is the runtime
  contract's **detach**: it releases this operation's references to the runtime
  elements and leaves their fate to their owner (S4 runbook §7), so a detached
  reference is *not* evidence that the platform stopped. After the stop the
  record therefore claims no verified readiness and no ``deployed`` platform,
  while the ``running`` claim stands as the conservative statement that a
  Running Platform may still be up — and, precisely because the platform is not
  claimed stopped, a later ``attach`` can re-bind it against the owner's own
  evidence. After the start the mere fact that a process exists justifies
  nothing — ``ready`` is established only by a *fresh* health/readiness
  verification (ADR-0017 §33–§34) — and a failure of the stop, of the
  execution/content verification, of the start, of the health/readiness
  verification or of the identity re-verification fails the whole attempt, is
  recorded with its phase and its reasons, is published as such (§20), and
  never asserts a stopped platform that no evidence establishes.

The stop/start policy is explicit and closed (:class:`StopStartPolicy`): a plain
stop never starts anything again, a failure never triggers an automatic restart,
one attempt at a time is admitted per deployment operation, and a restart never
changes identity or claims readiness without verification. A request that would
weaken any of those rules is refused before anything is touched, because each of
them is an architectural rule and not a preference (ADR-0016 §18, §20).

No technology is selected here either: the operation drives the same
:class:`~deployment_operations.runtime.RuntimeAdapter` seam the deployment path
uses, so no orchestrator, runtime technology or deployment engine is chosen by
it (ADR-0016 §23; ADR-0017 §40).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

# The capability's own health probe and its own single crossing of the
# owner-side identity boundary are reused rather than re-implemented: a restart
# establishes ``ready`` and identity correspondence exactly the way the
# deployment path does, with no second semantics of its own (ADR-0016 §9–§10,
# §19; ADR-0017 §33–§34).
from deployment_operations.deployment import (
    Deployment,
    _probe,
    _verify_running_platform_identity,
    deployment_record_basis,
    evaluation_binding,
    new_evaluation_handle,
)
from deployment_operations.errors import (
    DeploymentInputRejected,
    DeploymentStateError,
    IdentityVerificationFailed,
    InvalidDeploymentStateTransition,
    RestartFailed,
    RestartInProgress,
)
from deployment_operations.events import (
    EVENT_IDENTITY_VERIFIED,
    EVENT_RESTART_COMPLETED,
    EVENT_RESTART_EXECUTION_VERIFIED,
    EVENT_RESTART_FAILED,
    EVENT_RESTART_HEALTH_CHECKED,
    EVENT_RESTART_REQUESTED,
    EVENT_RESTART_STARTED,
    EVENT_RESTART_STOPPED,
    EVENT_RUNTIME_STARTED,
    DeploymentEvent,
    EventJournal,
)
from deployment_operations.health import (
    ComponentObservation,
    evaluate_health,
    verify_identity,
)
from deployment_operations.platform_identity import (
    PlatformIdentityBinding,
    PlatformIdentityProvider,
)
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from deployment_operations.runtime import (
    OP_START,
    ExecutionBinding,
    RuntimeAdapter,
    RuntimeElement,
    RuntimeHandle,
    verify_bound_content,
)
from deployment_operations.state import (
    LIFECYCLE_REALIZED,
    RESTART_COMPLETED,
    RESTART_FAILED,
    RESTART_PHASES,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_PENDING,
    ComponentRecord,
    DeploymentRecord,
    DeploymentStateStore,
    RestartPhaseRecord,
    RestartRecord,
    utc_now,
)
from deployment_operations.verification import (
    ComponentBinding,
    InstanceVerification,
    compute_content_digest,
    verify_artifact_digest,
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

Clock = Callable[[], str]

#: What a restart treats as a managed-runtime failure. The owner's runtime seam
#: is external code: the RuntimeAdapter contract names its operations, not the
#: exception types an implementation raises, so an unexpected exception is
#: still a failure of the phase that was running — recorded, never left as an
#: uncontrolled traceback with elements still started. ``BaseException`` (an
#: interrupt, a process exit) is deliberately not caught: it leaves the record
#: stating exactly what had been reached, and the next process attaches or
#: refuses instead of inventing a recovery (ADR-0016 §18, §20).
_RUNTIME_ERRORS = Exception

#: The operational actions a restart attempt records in deployment state
#: (ADR-0016 §18: every operational action is reflected in deployment state).
ACTION_RESTARTED = "platform_restarted"
ACTION_RESTART_FAILED = "platform_restart_failed"

#: The checks the pre-start verification applies, named for the evidence they
#: produce. They are the *existing* guarantees of the boundary, re-applied —
#: no restart-specific identity, digest or content semantics is invented here.
EXECUTION_CHECKS: tuple[str, ...] = (
    "instance_identity",
    "component_identity",
    "component_version",
    "artifact_digest",
    "execution_binding",
    "bound_content",
    "runtime_spec",
)

#: Serialization of restart attempts, keyed by the authoritative state file of
#: the deployment operation. The runtime elements of a Running Platform are
#: owned by the operation that started them — they are live handles inside that
#: owner — so the owner is also the only place where two attempts at the *same*
#: platform can meet, and exactly one attempt at a time is admitted there. A
#: second, concurrent attempt is refused rather than queued: nothing is then
#: done twice, and no uncontrolled runtime element can be left behind.
_ATTEMPT_GUARD = threading.Lock()
_ATTEMPT_LOCKS: dict[Path, threading.Lock] = {}


#: The authority that establishes the binding of one restart attempt.
AUTHORITY_RESTART = "deployment-operations/restart"


@dataclass(frozen=True)
class StopStartPolicy:
    """The explicit stop/start rules of ongoing runtime management (§18).

    These are the rules ADR-0016 §18 and §20 already fix, written down as an
    input of the operation instead of being left implicit in code paths:

    * ``restart_after_stop`` — a plain ``stop`` never starts a runtime again.
      Stopping is stopping; starting again is a separate, explicit operation.
    * ``restart_on_failure`` — a failure never triggers an automatic restart.
      This boundary implements no implicit recovery and no automatic «fix».
    * ``allow_identity_change`` — a restart never selects another version,
      another artifact, a floating selector or another Platform Instance.
    * ``readiness_requires_verification`` — ``ready`` after a restart is
      established only by a fresh health/readiness verification, never by the
      fact that a process was started again.
    * ``max_in_flight_attempts`` — how many restart attempts of one deployment
      operation may be in flight. More than one could leave several
      uncontrolled runtime elements behind, so a further attempt is refused.

    The default is the only policy this capability accepts
    (:data:`STRICT_STOP_START_POLICY`); :func:`validate_stop_start_policy`
    refuses a weakened one fail-closed, because every rule it states is an
    architectural rule.
    """

    restart_after_stop: bool = False
    restart_on_failure: bool = False
    allow_identity_change: bool = False
    readiness_requires_verification: bool = True
    max_in_flight_attempts: int = 1

    def document(self) -> dict[str, Any]:
        return {
            "restart_after_stop": self.restart_after_stop,
            "restart_on_failure": self.restart_on_failure,
            "allow_identity_change": self.allow_identity_change,
            "readiness_requires_verification": self.readiness_requires_verification,
            "max_in_flight_attempts": self.max_in_flight_attempts,
        }


#: The stop/start policy of this capability. It is a stated value, not a knob:
#: the rules it carries are the rules of ADR-0016 §18 and §20.
STRICT_STOP_START_POLICY = StopStartPolicy()


def validate_stop_start_policy(policy: StopStartPolicy) -> list[str]:
    """Every rule a policy weakens is a refusal, not a configuration choice."""
    if not isinstance(policy, StopStartPolicy):
        return ["$.policy: a stop/start policy of this capability is required"]
    errors: list[str] = []
    if policy.restart_after_stop:
        errors.append(
            "$.policy.restart_after_stop: a plain stop may not start a runtime "
            "again; starting again is the separate restart operation "
            "(ADR-0016 §18)"
        )
    if policy.restart_on_failure:
        errors.append(
            "$.policy.restart_on_failure: a failure may not trigger an automatic "
            "restart; this boundary implements no implicit recovery and no "
            "automatic fix (ADR-0016 §20)"
        )
    if policy.allow_identity_change:
        errors.append(
            "$.policy.allow_identity_change: a restart may not select another "
            "version, artifact or Platform Instance; different desired state is "
            "a new instance from the Factory (ADR-0016 §5, §7)"
        )
    if not policy.readiness_requires_verification:
        errors.append(
            "$.policy.readiness_requires_verification: readiness after a restart "
            "is established only by a fresh health/readiness verification, never "
            "by a started process (ADR-0017 §33–§34)"
        )
    attempts = policy.max_in_flight_attempts
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        errors.append(
            "$.policy.max_in_flight_attempts: a concrete attempt count is required"
        )
    elif attempts != 1:
        errors.append(
            "$.policy.max_in_flight_attempts: exactly one restart attempt of one "
            "deployment operation may be in flight; parallel attempts could leave "
            "uncontrolled runtime elements behind (ADR-0016 §18, §20)"
        )
    return errors


@dataclass(frozen=True)
class RestartRequest:
    """One explicit restart of the runtime of one realized deployment operation.

    ``deployment`` is the existing authoritative operation whose runtime
    elements are re-executed: its record pins the identity, version, artifact
    digest and execution/content binding the attempt must preserve, and its
    state file is where the attempt is recorded. ``restart_id`` must equal
    :func:`derive_restart_id` of that subject — it names the exact deployment
    operation, Platform Instance and attempt number, never a platform name and
    never a floating selector.
    """

    deployment: Deployment
    restart_id: str
    #: Why the operation was asked for. Operational metadata only: it is
    #: recorded and published as given and changes no rule of the attempt.
    reason: str = "explicit_runtime_management_request"
    policy: StopStartPolicy = STRICT_STOP_START_POLICY


@dataclass(frozen=True)
class Restart:
    """One completed restart attempt and the runtime it re-executed."""

    deployment: Deployment
    attempt: RestartRecord

    @property
    def outcome(self) -> str:
        """The recorded outcome of the attempt: ``restarted``, or a raised failure."""
        return self.attempt.outcome

    @property
    def completed(self) -> bool:
        """True only when every phase of the cycle completed and was verified."""
        return self.attempt.completed

    @property
    def record(self) -> DeploymentRecord:
        """The authoritative deployment record the attempt was written to."""
        return self.deployment.record

    @property
    def phases(self) -> tuple[str, ...]:
        """The phases the attempt reached, in the order it executed them."""
        return self.attempt.phase_order()


def derive_restart_id(deployment: Deployment) -> str:
    """Derive the correlation id of one restart attempt of one operation.

    The id names the exact deployment operation, its platform identity, the
    instance digest it pinned and the attempt number within that record — never
    a human-readable platform name and never a floating selector (ADR-0016 §8).
    The attempt number comes from the record's own restart history, so a
    controlled retry after a failed attempt is a separately identified attempt
    instead of a rewrite of the previous one.
    """
    record = deployment.record
    return (
        f"restart:{record.deployment_id}"
        f"@platform:{record.platform_id}"
        f"@instance:{record.instance_digest}"
        f"@{record.environment_id}"
        f"#attempt:{len(record.restarts) + 1}"
    )


def restart(
    request: RestartRequest,
    *,
    runtime: RuntimeAdapter | None = None,
    identity_provider: PlatformIdentityProvider | None = None,
    clock: Clock | None = None,
) -> Restart:
    """Re-execute the runtime of one realized deployment operation.

    Fail-closed at every phase: an attempt either completes the whole cycle and
    is recorded as ``restarted``, or it stops where it stopped, leaves no
    uncontrolled runtime element behind and is recorded as ``failed`` with its
    phase and its reasons. A refused attempt — an inexact subject, a weakened
    policy, a concurrent attempt — changes nothing and publishes nothing.
    """
    deployment = request.deployment
    _validate(request)
    now: Clock = clock or deployment._clock or utc_now
    adapter = runtime if runtime is not None else deployment._runtime
    provider = identity_provider or _default_identity_provider(deployment)
    lock = _attempt_lock(deployment.state_path)
    if not lock.acquire(blocking=False):
        raise RestartInProgress(
            [
                (
                    f"{deployment.record.deployment_id}: a restart attempt is "
                    "already in flight; this attempt was refused before any "
                    "runtime action"
                )
            ]
        )
    try:
        return _Attempt(
            deployment=deployment,
            request=request,
            adapter=adapter,
            provider=provider,
            clock=now,
        ).run()
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Admission — refused before anything is touched
# ---------------------------------------------------------------------------


def _attempt_lock(state_path: Path) -> threading.Lock:
    """The one attempt lock of one authoritative deployment record."""
    key = Path(state_path).resolve()
    with _ATTEMPT_GUARD:
        return _ATTEMPT_LOCKS.setdefault(key, threading.Lock())


def _validate(request: RestartRequest) -> None:
    """Refuse deterministically before any runtime action (ADR-0016 §8, §20)."""
    deployment = request.deployment
    errors = validate_stop_start_policy(request.policy)
    if request.restart_id != derive_restart_id(deployment):
        errors.append(
            "$.restart_id: the id does not name this exact deployment operation, "
            "Platform Instance and restart attempt"
        )
    if not isinstance(request.reason, str) or not request.reason.strip():
        errors.append("$.reason: an operational reason is required")
    if errors:
        raise DeploymentInputRejected(errors, stage="restart")

    _check_persisted(deployment)

    errors = _subject_errors(deployment.record)
    errors.extend(_runtime_errors(deployment))
    if errors:
        raise DeploymentInputRejected(errors, stage="restart")

    evidence = _execution_evidence_errors(deployment)
    if evidence:
        raise DeploymentInputRejected(evidence, stage="restart")


def _check_persisted(deployment: Deployment) -> None:
    """Refuse to act on anything but the persisted authoritative record (§9)."""
    try:
        persisted = _read_persisted(deployment)
    except DeploymentStateError as error:
        raise InvalidDeploymentStateTransition(
            "the persisted deployment state is unavailable; a restart re-executes "
            "the runtime of a recorded operation and invents no record of its own"
        ) from error
    if persisted != deployment.record:
        raise InvalidDeploymentStateTransition(
            "the persisted deployment state changed or was tampered with; a "
            "restart acts on the authoritative record or not at all"
        )


def _read_persisted(deployment: Deployment) -> DeploymentRecord:
    if deployment._store is not None:
        return deployment._store.read()
    return DeploymentStateStore(deployment.state_path).read()


def _subject_errors(record: DeploymentRecord) -> list[str]:
    """The subject of a restart is one exact, realized, verified operation."""
    errors: list[str] = []
    if record.lifecycle != LIFECYCLE_REALIZED:
        errors.append(
            "the record is not a realized deployment operation; a restart "
            "re-executes the runtime of the operation that realized this Platform "
            "Instance, and it resurrects no superseded or rolled back one"
        )
    if not record.identity_verified:
        errors.append(
            "the record carries no verified identity/version/digest "
            "correspondence; there is nothing a restart could preserve"
        )
    if record.failure is not None:
        errors.append("the record states a failure, not a realized platform")
    digest = record.instance_digest
    if not isinstance(digest, str) or re.fullmatch(SHA256_PATTERN, digest) is None:
        errors.append(
            "the record does not pin a concrete instance digest; a name-only or "
            "floating desired state cannot be re-executed"
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
            "the runtime to re-execute is not exact"
        )
    errors.extend(_component_errors(record.components))
    return errors


def _component_errors(components: Sequence[ComponentRecord]) -> list[str]:
    """Every component the attempt would re-execute must be exactly pinned."""
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
    artifact_digest = entry.artifact_digest
    if artifact_type == "none":
        if artifact_digest is not None:
            errors.append(
                f"$.components[{component_id}].artifact: artifact_type none "
                f"carries the digest {artifact_digest!r}"
            )
        return errors
    if artifact_type not in ARTIFACT_TYPES:
        errors.append(
            f"$.components[{component_id}].artifact: the artifact identity is "
            "incomplete"
        )
        return errors
    if (
        not isinstance(artifact_digest, str)
        or re.fullmatch(SHA256_PATTERN, artifact_digest) is None
    ):
        errors.append(
            f"$.components[{component_id}].artifact: a sealed artifact digest is "
            "required"
        )
    return errors


def _runtime_errors(deployment: Deployment) -> list[str]:
    """The attempt manages the runtime elements this operation owns — or none."""
    errors: list[str] = []
    record = deployment.record
    handles = tuple(deployment._handles)
    if not handles:
        errors.append(
            "the deployment operation holds no runtime elements; a restart "
            "re-executes the elements of the Running Platform it was given and "
            "never rebuilds them from desired state"
        )
        return errors
    if deployment._runtime is None:
        errors.append(
            "the deployment operation holds no runtime adapter; its runtime "
            "elements cannot be managed"
        )
    held = {handle.component_id for handle in handles}
    pinned = {entry.component_id for entry in record.components}
    for component_id in sorted(pinned - held):
        errors.append(
            f"{component_id}: the record pins this component but the operation "
            "holds no runtime element for it"
        )
    for component_id in sorted(held - pinned):
        errors.append(
            f"{component_id}: the operation holds a runtime element the record "
            "does not pin; a restart never manages an element of another instance"
        )
    components = getattr(deployment.verification, "components", None)
    if not isinstance(components, Sequence) or not components:
        errors.append(
            "the deployment operation carries no verified instance identity; the "
            "identity a restart must preserve cannot be established"
        )
    return errors


# ---------------------------------------------------------------------------
# Identity, version, artifact and execution/content evidence
# ---------------------------------------------------------------------------


def _elements(deployment: Deployment) -> dict[str, RuntimeElement]:
    """The runtime elements of the operation, by the component they serve."""
    return {handle.component_id: handle.element for handle in deployment._handles}


def _execution_evidence_errors(deployment: Deployment) -> list[str]:
    """Everything a restart must preserve, checked before it acts (§7–§10).

    The same evidence is checked twice: here, before anything is touched — so a
    platform is never torn down for a restart that cannot preserve its identity
    — and again after the stop, immediately before the fresh start, so content
    replaced inside that window is refused there. Content replaced immediately
    before execution is refused by the runtime adapter's own launch
    verification. Nothing this deployment verified may execute as something
    else, whenever the substitution happens.
    """
    record = deployment.record
    verification = deployment.verification
    elements = _elements(deployment)
    errors = _pinned_identity_errors(record, verification)
    for component_id in sorted(elements):
        entry = record.component(component_id)
        if entry is None:  # pragma: no cover - refused by _runtime_errors
            continue
        errors.extend(_element_errors(record, entry, elements[component_id]))
    return errors


def _pinned_identity_errors(
    record: DeploymentRecord, verification: InstanceVerification
) -> list[str]:
    """The verified instance and the authoritative record must still agree."""
    errors: list[str] = []
    for name in (
        "platform_id",
        "instance_digest",
        "manifest_id",
        "manifest_version",
        "manifest_digest",
        "manifest_state",
    ):
        pinned = getattr(record, name)
        verified = getattr(verification, name, None)
        if pinned != verified:
            errors.append(
                f"{name}: the authoritative record pins {pinned!r} while the "
                f"verified instance is {verified!r}; a restart re-executes one "
                "exact Platform Instance and refuses a substituted one"
            )
    verified_components = {
        binding.component_id: binding for binding in verification.components
    }
    pinned_components = {entry.component_id: entry for entry in record.components}
    for component_id in sorted(pinned_components):
        entry = pinned_components[component_id]
        binding = verified_components.get(component_id)
        if binding is None:
            errors.append(
                f"{component_id}: the verified instance does not include this "
                "component of the record"
            )
            continue
        if binding.component_version != entry.component_version:
            errors.append(
                f"{component_id}: the record pins version "
                f"{entry.component_version!r} and the verified instance pins "
                f"{binding.component_version!r}; a restart selects no version"
            )
        if binding.artifact_digest != entry.artifact_digest:
            errors.append(
                f"{component_id}: the record pins artifact digest "
                f"{entry.artifact_digest!r} and the verified instance pins "
                f"{binding.artifact_digest!r}; a restart selects no artifact"
            )
    for component_id in sorted(set(verified_components) - set(pinned_components)):
        errors.append(
            f"{component_id}: the verified instance includes a component the "
            "record does not pin"
        )
    return errors


def _element_errors(
    record: DeploymentRecord, entry: ComponentRecord, element: RuntimeElement
) -> list[str]:
    """One runtime element must still be the element this operation bound."""
    component_id = entry.component_id
    errors: list[str] = []
    if element.deployment_id != record.deployment_id:
        errors.append(
            f"{component_id}: the runtime element belongs to deployment "
            f"{element.deployment_id!r}, not to {record.deployment_id!r}"
        )
    if element.platform_id != record.platform_id:
        errors.append(
            f"{component_id}: the runtime element was built for platform "
            f"{element.platform_id!r}; the record pins {record.platform_id!r}"
        )
    if element.instance_digest != record.instance_digest:
        errors.append(
            f"{component_id}: the runtime element was built for instance "
            f"{element.instance_digest!r}; the record pins "
            f"{record.instance_digest!r}"
        )
    binding = element.component
    if binding.component_id != component_id:
        errors.append(
            f"{component_id}: the runtime element binds component "
            f"{binding.component_id!r}"
        )
    if binding.component_version != entry.component_version:
        errors.append(
            f"{component_id}: the runtime element binds version "
            f"{binding.component_version!r}; the record pins "
            f"{entry.component_version!r}"
        )
    if binding.artifact_digest != entry.artifact_digest:
        errors.append(
            f"{component_id}: the runtime element binds artifact digest "
            f"{binding.artifact_digest!r}; the record pins "
            f"{entry.artifact_digest!r}"
        )
    execution = element.execution
    if execution is None:
        errors.append(
            f"{component_id}: no execution content was bound for this element; a "
            "restart never starts a process whose content cannot be verified"
        )
        return errors
    persisted = entry.execution
    if not isinstance(persisted, Mapping):
        errors.append(
            f"{component_id}: the record holds no execution binding; the content a "
            "restart would execute cannot be verified against it"
        )
    elif _canonical(execution.document()) != _canonical(dict(persisted)):
        errors.append(
            f"{component_id}: the execution content bound for this restart is not "
            "the execution content this deployment recorded; the execution "
            "boundary no longer confirms the runtime content"
        )
    errors.extend(_bound_content_errors(execution))
    errors.extend(_artifact_content_errors(binding, execution))
    errors.extend(_runtime_spec_errors(record, entry, element))
    return errors


def _error_details(error: BaseException) -> tuple[str, ...]:
    """The diagnostics an error of this boundary carries with it (§20).

    A failure of a restart must be diagnosable from what is recorded: the
    runtime, verification and state errors of the boundary state *why* they
    refused, and those reasons belong to the attempt's errors as well.
    """
    details = getattr(error, "errors", ())
    if isinstance(details, str | bytes) or not isinstance(details, Sequence):
        return ()
    return tuple(str(entry) for entry in details if str(entry).strip())


def _canonical(document: Mapping[str, Any]) -> str:
    """A deterministic rendering of a document, for exact comparison."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, default=str)


def _jsonable(value: Any) -> Any:
    """Normalize an adapter answer to what deployment state can persist."""
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        )
    except (TypeError, ValueError):  # pragma: no cover - a str() always serializes
        return {"answer": str(value)}


def _bound_content_errors(execution: ExecutionBinding) -> list[str]:
    """Re-read the bound bytes: what would execute must still be what was bound."""
    errors = list(verify_bound_content(execution))
    if not execution.modules:
        errors.append(
            f"{execution.component_id}: the execution binding names no module; the "
            "content a restart would execute is not bound"
        )
    return errors


def _artifact_content_errors(
    binding: ComponentBinding, execution: ExecutionBinding
) -> list[str]:
    """The pinned artifact identity is re-verified against the content that runs."""
    if execution.kind != "artifact":
        return []
    path = Path(execution.entry.path)
    if not path.is_file():
        return [
            (
                f"{binding.component_id}: the verified artifact content "
                f"{path.name!r} is gone; the pinned artifact "
                f"{binding.artifact_digest!r} cannot be re-verified"
            )
        ]
    return list(verify_artifact_digest(binding, compute_content_digest(path)))


def _runtime_spec_errors(
    record: DeploymentRecord, entry: ComponentRecord, element: RuntimeElement
) -> list[str]:
    """The prepared runtime spec must still pin exactly this instance (§8).

    The spec is what a component process is started from. It was written by the
    deployment operation and is never edited; a spec that now names another
    deployment, platform, instance, version or artifact digest is a substituted
    Platform Instance, and the restart refuses it instead of starting from it.
    """
    component_id = entry.component_id
    path = element.spec_path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return [
            (
                f"{component_id}: the prepared runtime spec {path} is unreadable "
                f"({error.__class__.__name__}); what a restart would execute "
                "cannot be confirmed"
            )
        ]
    if not isinstance(document, Mapping):
        return [f"{component_id}: the prepared runtime spec is not a JSON object"]

    errors: list[str] = []
    for name, expected in (
        ("deployment_id", record.deployment_id),
        ("platform_id", record.platform_id),
        ("instance_digest", record.instance_digest),
    ):
        if document.get(name) != expected:
            errors.append(
                f"{component_id}: the prepared runtime spec names {name} "
                f"{document.get(name)!r}; this operation pins {expected!r}"
            )
    manifest = document.get("manifest")
    bound: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}
    for name, expected in (
        ("manifest_id", record.manifest_id),
        ("manifest_version", record.manifest_version),
        ("manifest_digest", record.manifest_digest),
    ):
        if bound.get(name) != expected:
            errors.append(
                f"{component_id}: the prepared runtime spec names {name} "
                f"{bound.get(name)!r}; this operation pins {expected!r}"
            )
    component = document.get("component")
    declared: Mapping[str, Any] = component if isinstance(component, Mapping) else {}
    if declared.get("component_id") != component_id:
        errors.append(
            f"{component_id}: the prepared runtime spec names component "
            f"{declared.get('component_id')!r}"
        )
    if declared.get("component_version") != entry.component_version:
        errors.append(
            f"{component_id}: the prepared runtime spec names component version "
            f"{declared.get('component_version')!r}; this operation pins "
            f"{entry.component_version!r}"
        )
    artifact = declared.get("artifact")
    pinned: Mapping[str, Any] = artifact if isinstance(artifact, Mapping) else {}
    if pinned.get("digest") != entry.artifact_digest:
        errors.append(
            f"{component_id}: the prepared runtime spec names artifact digest "
            f"{pinned.get('digest')!r}; this operation pins "
            f"{entry.artifact_digest!r}"
        )
    return errors


def _identity_evidence(
    record: DeploymentRecord, elements: Mapping[str, RuntimeElement]
) -> dict[str, Any]:
    """The identity/version/artifact state one attempt re-executes (§19).

    Recorded with the attempt so a restart *shows* that it preserved identity
    instead of asserting it: the pinned Platform Instance and Manifest identity
    and, per component, the pinned version, the pinned artifact digest and the
    digests of the bound execution content.
    """
    components: list[dict[str, Any]] = []
    for entry in record.components:
        element = elements.get(entry.component_id)
        execution = element.execution if element is not None else None
        modules = execution.modules if execution is not None else ()
        components.append(
            {
                "component_id": entry.component_id,
                "component_version": entry.component_version,
                "artifact_type": entry.artifact_type,
                "artifact_digest": entry.artifact_digest,
                "execution_kind": execution.kind if execution is not None else None,
                "bound_modules": {module.module: module.digest for module in modules},
            }
        )
    return {
        "platform_id": record.platform_id,
        "instance_digest": record.instance_digest,
        "manifest_id": record.manifest_id,
        "manifest_version": record.manifest_version,
        "manifest_digest": record.manifest_digest,
        "manifest_state": record.manifest_state,
        "components": sorted(
            components, key=lambda component: str(component["component_id"])
        ),
    }


def _default_identity_provider(deployment: Deployment) -> PlatformIdentityProvider:
    """The owner-published actual identity surface of this environment.

    The same seam the deployment path and reconciliation read: the Running
    Platform owner decides how the fact is produced (ADR-0019/0020), and a
    restart re-confirms correspondence through it instead of inventing a second
    identity source.
    """
    runtime_root = deployment.state_path.parent.parent
    return OwnerSuppliedPlatformIdentityProvider(
        OwnerStateSnapshotSource(
            FileRunningPlatformOwnerStateReader(
                runtime_root / "running_platform_identity.json"
            )
        )
    )


def _persisted_identity_errors(
    deployment: Deployment, acted_on: DeploymentRecord
) -> list[str]:
    """The authoritative record must still pin what this attempt re-executed.

    Read back after the fresh start, so a desired-state or record substitution
    made *during* the attempt is caught before any claim is fixed: a restart
    changes no pinned identity, and it refuses to verify against one that moved.
    """
    try:
        persisted = _read_persisted(deployment)
    except DeploymentStateError as error:
        return [
            (
                "the persisted deployment state could not be read back while the "
                f"restart was verifying it ({error.__class__.__name__})"
            )
        ]
    errors: list[str] = []
    for name in (
        "deployment_id",
        "platform_id",
        "instance_digest",
        "manifest_id",
        "manifest_version",
        "manifest_digest",
        "manifest_state",
    ):
        if getattr(persisted, name) != getattr(acted_on, name):
            errors.append(
                f"{name}: the authoritative record now pins "
                f"{getattr(persisted, name)!r}; this restart acted on "
                f"{getattr(acted_on, name)!r} and changes no desired state"
            )
    current = {entry.component_id: entry for entry in persisted.components}
    for entry in acted_on.components:
        pinned = current.get(entry.component_id)
        if pinned is None:
            errors.append(
                f"{entry.component_id}: the authoritative record no longer pins "
                "this component"
            )
            continue
        if pinned.component_version != entry.component_version:
            errors.append(
                f"{entry.component_id}: the authoritative record now pins version "
                f"{pinned.component_version!r}; this restart re-executed "
                f"{entry.component_version!r}"
            )
        if pinned.artifact_digest != entry.artifact_digest:
            errors.append(
                f"{entry.component_id}: the authoritative record now pins artifact "
                f"digest {pinned.artifact_digest!r}; this restart re-executed "
                f"{entry.artifact_digest!r}"
            )
    return errors


# ---------------------------------------------------------------------------
# One admitted attempt
# ---------------------------------------------------------------------------


class _PhaseFailure(Exception):
    """Internal signal: one phase of the attempt stopped fail-closed."""

    def __init__(self, phase: str, reason: str, errors: Sequence[str]) -> None:
        super().__init__(reason)
        self.phase = phase
        self.reason = reason
        self.errors = list(errors)


@dataclass
class _Attempt:
    """One admitted restart attempt: its phases, its writes and its signals.

    Deployment state is written before the matching signal is published at every
    phase, so the journal never claims a fact deployment state does not hold —
    and a write that fails publishes nothing at all (ADR-0016 §9, §19, §20).
    """

    deployment: Deployment
    request: RestartRequest
    adapter: RuntimeAdapter
    provider: PlatformIdentityProvider
    clock: Clock
    record: DeploymentRecord = field(init=False)
    store: DeploymentStateStore = field(init=False)
    journal: EventJournal = field(init=False)
    secrets: tuple[str, ...] = field(init=False)
    elements: dict[str, RuntimeElement] = field(init=False)
    #: The handle of every component now, whether this attempt started it or the
    #: deployment operation did: it is what the operation keeps managing.
    current: dict[str, RuntimeHandle] = field(init=False)
    #: The handles this attempt started — and therefore must not leave behind.
    fresh: dict[str, RuntimeHandle] = field(init=False)
    #: The components whose runtime element is known to be up.
    up: set[str] = field(init=False)
    #: The components whose references this attempt released: a released
    #: reference is not an active reference any more, so it is dropped from
    #: ``current``/``fresh`` and never detached a second time, and the failure
    #: signals can state what was and was not handed back (ADR-0016 §18).
    released: set[str] = field(init=False, default_factory=set)
    phases: list[RestartPhaseRecord] = field(init=False)
    observations: tuple[ComponentObservation, ...] = field(init=False, default=())
    requested_at: str = field(init=False, default="")
    #: The number of this attempt within the record's history, fixed when the
    #: attempt is admitted: every phase, signal and the attempt record itself
    #: correlate to *this* attempt, including the one that appends it.
    attempt_number: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        deployment = self.deployment
        self.record = deployment.record
        self.store = (
            deployment._store
            if deployment._store is not None
            else DeploymentStateStore(deployment.state_path)
        )
        if deployment._journal is None:
            # A handle created without a journal still publishes into the journal
            # of its operation — and must then report the signals it published.
            deployment._journal = EventJournal(deployment.events_path)
        self.journal = deployment._journal
        self.secrets = tuple(deployment._secrets)
        self.elements = _elements(deployment)
        self.current = {handle.component_id: handle for handle in deployment._handles}
        self.fresh = {}
        self.up = set(self.current) if self.record.running else set()
        self.phases = [RestartPhaseRecord(name=name) for name in RESTART_PHASES]
        self.attempt_number = len(self.record.restarts) + 1
        # The handle of this attempt, issued once: the seam and the journal of
        # the attempt name the same evaluation, and it is nobody's earlier one.
        self.handle = new_evaluation_handle()

    # -- the identity of this attempt ---------------------------------------
    @property
    def sequence(self) -> int:
        """The position of this attempt in the record's restart history."""
        return self.attempt_number

    @property
    def evaluation_binding(self) -> PlatformIdentityBinding:
        """The binding of *this* attempt, with its established semantics.

        The binding is not an opaque string: it states which authority
        established it (this restart operation), the durable record it is
        derived from, the identity-bearing platform it is for, the binding space
        (this environment) and this attempt's position in it, and the instant it
        was established. Only its handle crosses the owner-side boundary.
        """
        return evaluation_binding(
            authority=AUTHORITY_RESTART,
            token=self.binding_token,
            basis=deployment_record_basis(self.record.deployment_id),
            target=str(self.record.platform_id),
            scope=self.record.environment_id,
            sequence=self.sequence,
            established_at=self.now(),
        )

    @property
    def binding_token(self) -> str:
        """The opaque evaluation handle of *this* attempt.

        A fresh handle, issued for this attempt and for no other: it names no
        platform, no environment, no attempt number and no digest, and nothing
        is read out of it. Evidence correlated to the deployment itself, or to
        an earlier attempt, is evidence of another evaluation — an answer to a
        handle this attempt never issued — so the owner-side boundary is asked
        freshly, under a handle of this attempt, exactly as reconciliation asks
        under a handle of its own observation (ADR-0019/0020).
        """
        return self.handle

    def identity_evidence(self) -> dict[str, Any]:
        return _identity_evidence(self.record, self.elements)

    # -- state and signals ---------------------------------------------------
    def now(self) -> str:
        return self.clock()

    def update(self, record: DeploymentRecord) -> None:
        """Persist one honest change of the authoritative record (§9).

        The write comes first. A record that *cannot* be written — because the
        state is broken, or because secret material appeared in what a runtime
        reported — is not a record this attempt may carry forward either: the
        attempt keeps acting on the last state it durably wrote, and reports the
        refusal (§12, §20). Nothing is then claimed in memory that deployment
        state does not hold.
        """
        self.store.write(record, secrets=self.secrets)
        self.record = record
        self.deployment.record = record

    def publish(
        self,
        name: str,
        phase: str,
        detail: Mapping[str, Any],
        *,
        component: ComponentBinding | None = None,
    ) -> None:
        """Publish one correlated operational signal of this attempt (§19)."""
        self.journal.append(
            DeploymentEvent(
                sequence=self.journal.next_sequence(),
                event=name,
                occurred_at=self.now(),
                deployment_id=self.record.deployment_id,
                environment_id=self.record.environment_id,
                platform_id=self.record.platform_id,
                instance_digest=self.record.instance_digest,
                manifest_id=self.record.manifest_id,
                manifest_version=self.record.manifest_version,
                manifest_digest=self.record.manifest_digest,
                stage="restart",
                component_id=component.component_id if component else None,
                component_version=component.component_version if component else None,
                artifact_type=component.artifact_type if component else None,
                artifact_digest=component.artifact_digest if component else None,
                detail={
                    "restart_id": self.request.restart_id,
                    "restart_sequence": self.sequence,
                    "phase": phase,
                    **_jsonable(dict(detail)),
                },
            ),
            secrets=self.secrets,
        )

    def complete(self, phase: str, detail: Mapping[str, Any]) -> None:
        self._mark_phase(phase, STAGE_COMPLETED, detail)

    def failed_phase(self, phase: str, reason: str, errors: Sequence[str]) -> None:
        self._mark_phase(
            phase, STAGE_FAILED, {"reason": reason, "errors": list(errors)}, errors
        )

    def _mark_phase(
        self,
        phase: str,
        status: str,
        detail: Mapping[str, Any],
        errors: Sequence[str] = (),
    ) -> None:
        at = self.now()
        self.phases = [
            (
                RestartPhaseRecord(
                    name=entry.name,
                    status=status,
                    occurred_at=at,
                    detail=_jsonable(dict(detail)),
                    errors=tuple(errors),
                )
                if entry.name == phase
                else entry
            )
            for entry in self.phases
        ]

    def reached_phases(self) -> tuple[str, ...]:
        """The phases this attempt reached, in the order it executed them."""
        return tuple(
            entry.name for entry in self.phases if entry.status != STAGE_PENDING
        )

    # -- the cycle -----------------------------------------------------------
    def run(self) -> Restart:
        """Execute the whole cycle, or stop honestly in the phase that failed."""
        phase = "requested"
        try:
            self._requested_phase()
            phase = "stop"
            self._stop_phase()
            phase = "execution_verification"
            self._execution_verification_phase()
            phase = "start"
            self._start_phase()
            phase = "health_check"
            self._health_phase()
            phase = "identity_verification"
            self._identity_phase()
        except DeploymentStateError:
            # A state write that failed is refused loudly: no outcome is
            # invented for it, nothing is published, and the elements this
            # attempt started are stopped so it leaves nothing uncontrolled.
            # The operation then keeps only the references this attempt still
            # holds — a released reference is not an active one, whatever the
            # state file says.
            self._abandon_fresh_elements()
            self._hand_over()
            raise
        except _PhaseFailure as failure:
            return self._terminate_failed(failure.phase, failure.reason, failure.errors)
        except _RUNTIME_ERRORS as error:
            reason = f"{error.__class__.__name__}: {error}"
            return self._terminate_failed(
                phase,
                "the restart attempt stopped unexpectedly",
                [reason, *_error_details(error)],
            )
        return self._terminate_completed()

    def _requested_phase(self) -> None:
        self.requested_at = self.now()
        detail = {
            "reason": self.request.reason,
            "policy": self.request.policy.document(),
            "platform_running": self.record.running,
            "identity": self.identity_evidence(),
        }
        self.complete("requested", detail)
        self.publish(EVENT_RESTART_REQUESTED, "requested", detail)

    def _stop_phase(self) -> None:
        """Release this operation's references to the runtime — a detach (§18).

        ``RuntimeAdapter.stop`` is the runtime contract's **detach**: it releases
        this operation's reference to a runtime element and leaves that element's
        fate to the runtime's owner (S4 runbook §7). A detach that answered is
        therefore **not evidence that the platform stopped**: the member this
        operation released may still be running under its owner's policy. The
        record keeps the ``running`` claim it carried — which is what keeps the
        platform reachable through ``attach`` — and withdraws only the verified
        operational condition this operation can no longer check, the same
        conservative transition every fail-closed path of this module uses
        (§9, §10, §20). Nothing here claims a stopped platform, in deployment
        state or in the operational journal.
        """
        errors: list[str] = []
        answers: dict[str, Any] = {}
        for component_id in sorted(self.current):
            try:
                answers[component_id] = _jsonable(
                    self.adapter.stop(self.current[component_id])
                )
            except _RUNTIME_ERRORS as error:
                errors.append(
                    f"{component_id}: the runtime element could not be stopped "
                    f"({error.__class__.__name__}: {error})"
                )
                errors.extend(
                    f"{component_id}: {detail}" for detail in _error_details(error)
                )
                continue
            # The reference was released: it is no longer an active reference of
            # this operation, so the attempt keeps only the components whose
            # release the seam refused. Nothing is detached twice, and the
            # operation is never handed a released handle back.
            self.released.add(component_id)
            self.current.pop(component_id, None)
            self.up.discard(component_id)
            self._component_state(component_id, runtime_started=False)
        if errors:
            # An element was not released: the platform is not detached, and
            # saying so is the only honest state. The conservative claim — still
            # running, no verified readiness — is recorded instead of a false
            # stop. What *was* released is kept as evidence, and the components
            # whose release refused stay this attempt's references.
            self.update(
                self.record.withdraw_ready(
                    at=self.now(), reason="a restart stop phase did not complete"
                )
            )
            raise _PhaseFailure(
                "stop", "one or more runtime elements refused to stop", errors
            )

        claimed_running = self.record.running
        withdrawn = self.record.withdraw_ready(
            at=self.now(),
            reason=(
                "a restart released this operation's references to the Running "
                "Platform; a detach is the runtime contract's reference release "
                "and is not evidence that the platform stopped"
            ),
        )
        if withdrawn is not self.record:
            self.update(withdrawn)
        detail = {
            # The references this phase released — never a claim that what they
            # pointed at is down.
            "detached": sorted(answers),
            "answers": answers,
            "claimed_running": claimed_running,
            "running": self.record.running,
            "ready": self.record.ready,
            "physical_stop_established": False,
        }
        self.complete("stop", detail)
        self.publish(EVENT_RESTART_STOPPED, "stop", detail)

    def _execution_verification_phase(self) -> None:
        """Re-apply the execution/content guarantees before the fresh start (§9)."""
        errors = _execution_evidence_errors(self.deployment)
        if errors:
            raise _PhaseFailure(
                "execution_verification",
                "the content this deployment verified is not the content a restart "
                "would execute; no runtime element was started again",
                errors,
            )
        detail = {
            "verified": list(EXECUTION_CHECKS),
            "identity": self.identity_evidence(),
        }
        self.complete("execution_verification", detail)
        self.publish(EVENT_RESTART_EXECUTION_VERIFIED, "execution_verification", detail)

    def _start_phase(self) -> None:
        """Start the same elements again — a started process claims nothing yet."""
        errors: list[str] = []
        for component_id in sorted(self.elements):
            element = self.elements[component_id]
            try:
                handle = self.adapter.start(element)
            except _RUNTIME_ERRORS as error:
                errors.append(
                    f"{component_id}: the runtime element could not be started "
                    f"again ({error.__class__.__name__}: {error})"
                )
                errors.extend(
                    f"{component_id}: {detail}" for detail in _error_details(error)
                )
                continue
            self.fresh[component_id] = handle
            self.current[component_id] = handle
            try:
                answer = self.adapter.request(handle, OP_START)
            except _RUNTIME_ERRORS as error:
                errors.append(
                    f"{component_id}: the started runtime element did not confirm "
                    f"its start ({error.__class__.__name__}: {error})"
                )
                errors.extend(
                    f"{component_id}: {detail}" for detail in _error_details(error)
                )
                continue
            if answer.get("status") != "ok":
                errors.append(
                    f"{component_id}: "
                    + str(answer.get("error", "the component runtime refused to start"))
                )
                continue
            self.up.add(component_id)
            self._component_state(component_id, runtime_started=True)
            self.publish(
                EVENT_RUNTIME_STARTED,
                "start",
                {"workspace": str(element.workspace), "restart": True},
                component=element.component,
            )
        if errors:
            raise _PhaseFailure("start", "the platform did not start again", errors)
        self.update(self.record.mark_running(at=self.now(), running=True))
        detail = {"started": sorted(self.fresh), "running": True}
        self.complete("start", detail)
        self.publish(EVENT_RESTART_STARTED, "start", detail)

    def _health_phase(self) -> None:
        """Fresh health/readiness evidence — never inherited from before (§33)."""
        verification = self.deployment.verification
        observations, probe_errors = _probe(self.adapter, verification, self.current)
        self.observations = tuple(observations)
        evaluation = evaluate_health(
            verification.components, observations, probe_errors
        )
        self._record_observations(observations)
        detail = {**_jsonable(evaluation.document()), "ready": evaluation.ready}
        if evaluation.ready:
            # ``ready`` is a fresh verified condition of the restarted platform,
            # never a condition inherited from before the stop (ADR-0017 §33).
            self.update(self.record.mark_ready(at=self.now()))
            self.complete("health_check", detail)
        self.publish(EVENT_RESTART_HEALTH_CHECKED, "health_check", detail)
        if not evaluation.ready:
            raise _PhaseFailure(
                "health_check",
                "health/readiness verification did not establish ready",
                list(evaluation.errors),
            )

    def _identity_phase(self) -> None:
        """Re-confirm that what now runs is the same instance (§8, §10)."""
        verification = self.deployment.verification
        executions = {
            component_id: element.execution.document()
            for component_id, element in self.elements.items()
            if element.execution is not None
        }
        errors = list(
            verify_identity(
                verification.components,
                self.observations,
                platform_id=self.record.platform_id,
                instance_digest=self.record.instance_digest,
                executions=executions,
            )
        )
        for component_id in sorted(self.elements):
            execution = self.elements[component_id].execution
            if execution is not None:
                errors.extend(verify_bound_content(execution))
        errors.extend(_persisted_identity_errors(self.deployment, self.record))
        if not errors:
            try:
                _verify_running_platform_identity(
                    self.provider,
                    {"instance_digest": self.record.instance_digest},
                    binding=self.evaluation_binding,
                )
            except IdentityVerificationFailed as error:
                errors.extend(error.errors or [str(error)])
        if errors:
            raise _PhaseFailure(
                "identity_verification",
                "the restarted runtime does not correspond to the pinned Platform "
                "Instance",
                sorted(set(errors)),
            )
        self.update(self.record.mark_identity_verified(at=self.now()))
        detail = {
            "instance_digest": self.record.instance_digest,
            "identity_verified": True,
            "identity": self.identity_evidence(),
            "binding_token": self.binding_token,
        }
        self.complete("identity_verification", detail)
        self.publish(EVENT_IDENTITY_VERIFIED, "identity_verification", detail)

    # -- terminal outcomes ----------------------------------------------------
    def _terminate_completed(self) -> Restart:
        at = self.now()
        detail = {
            "outcome": RESTART_COMPLETED,
            "ready": self.record.ready,
            "running": self.record.running,
            "deployed": self.record.deployed,
            "identity": self.identity_evidence(),
        }
        self.complete("completed", detail)
        record = self.record.with_operational_action(
            ACTION_RESTARTED,
            at=at,
            detail={
                "restart_id": self.request.restart_id,
                "reason": self.request.reason,
                "phases": list(self.reached_phases()),
            },
        )
        record = record.with_restart(self._attempt_record(RESTART_COMPLETED, at), at=at)
        self.update(record)
        self.publish(EVENT_RESTART_COMPLETED, "completed", detail)
        self._hand_over()
        attempt = self.record.last_restart
        if attempt is None:  # pragma: no cover - with_restart always appends
            raise DeploymentStateError("the restart attempt was not recorded")
        return Restart(deployment=self.deployment, attempt=attempt)

    def _terminate_failed(
        self, phase: str, reason: str, errors: Sequence[str]
    ) -> NoReturn:
        """Leave nothing uncontrolled behind, then record and publish the failure."""
        self.failed_phase(phase, reason, errors)
        cleanup = self._stop_fresh_elements()
        at = self.now()
        # The record never claims the platform stopped, whatever phase failed:
        # everything this attempt performed against the runtime is the contract's
        # detach (``RuntimeAdapter.stop``), and a detach does not establish that
        # the owner's runtime is down. The conservative claim therefore stands —
        # a Running Platform this operation can no longer verify — and only the
        # verified operational condition is withdrawn, exactly as in the stop
        # phase (§9, §18, §20).
        record = self.record.withdraw_ready(
            at=at, reason=f"the restart attempt failed in its {phase} phase"
        )
        all_errors = [*errors, *cleanup]
        record = record.with_operational_action(
            ACTION_RESTART_FAILED,
            at=at,
            detail={
                "restart_id": self.request.restart_id,
                "phase": phase,
                "reason": reason,
                "phases": list(self.reached_phases()),
                # What this attempt handed back, and what it still holds: a
                # partial hand-over is stated as one, so nothing in deployment
                # state can be read as an untouched operation or as a stopped
                # platform.
                "released": sorted(self.released),
                "references_held": sorted(self.up),
            },
        )
        attempt = self._attempt_record(
            RESTART_FAILED,
            at,
            failure_phase=phase,
            failure_reason=reason,
            errors=all_errors,
        )
        record = record.with_restart(attempt, at=at)
        self.update(record)
        self.publish(
            EVENT_RESTART_FAILED,
            phase,
            {
                "outcome": RESTART_FAILED,
                "failure_phase": phase,
                "reason": reason,
                "errors": all_errors,
                # What the record still claims, and the references this attempt
                # could not release: the failure states both, and claims no
                # platform that stopped.
                "running": record.running,
                "ready": record.ready,
                "deployed": record.deployed,
                "released": sorted(self.released),
                "references_held": sorted(self.up),
            },
        )
        self._hand_over()
        raise RestartFailed(all_errors, phase=phase, reason=reason)

    def _attempt_record(
        self,
        outcome: str,
        at: str,
        *,
        failure_phase: str | None = None,
        failure_reason: str | None = None,
        errors: Sequence[str] = (),
    ) -> RestartRecord:
        return RestartRecord(
            restart_id=self.request.restart_id,
            sequence=self.sequence,
            outcome=outcome,
            requested_at=self.requested_at or at,
            completed_at=at,
            reason=self.request.reason,
            identity=self.identity_evidence(),
            phases=tuple(self.phases),
            failure_phase=failure_phase,
            failure_reason=failure_reason,
            errors=tuple(errors),
        )

    def _stop_fresh_elements(self) -> list[str]:
        """Release what this attempt started: no uncontrolled reference survives.

        The call is the contract's detach, so it releases this attempt's
        references and never destroys the elements themselves — their lifecycle
        stays with the runtime's owner (§18).
        """
        errors: list[str] = []
        for component_id in sorted(self.fresh):
            handle = self.fresh[component_id]
            try:
                self.adapter.stop(handle)
            except _RUNTIME_ERRORS as error:
                errors.append(
                    f"{component_id}: the runtime element this attempt started "
                    f"could not be stopped ({error.__class__.__name__}: {error})"
                )
                continue
            # Released: dropped from what this attempt still holds, so a repeated
            # cleanup detaches nothing twice and ``_hand_over`` never hands the
            # operation a released handle.
            self.released.add(component_id)
            self.fresh.pop(component_id, None)
            self.current.pop(component_id, None)
            self.up.discard(component_id)
            self._component_state(component_id, runtime_started=False)
        return errors

    def _abandon_fresh_elements(self) -> None:
        """Release this attempt's references without recording what cannot be.

        Used when a state write failed: the attempt keeps acting on the last
        state it durably wrote, so only the reference release is performed —
        and what it released is dropped from the references it still holds.
        """
        for component_id in sorted(self.fresh):
            handle = self.fresh[component_id]
            try:
                self.adapter.stop(handle)
            except _RUNTIME_ERRORS:
                # Nothing can be recorded about it: the state write that failed
                # is the failure this operation reports. The reference stays this
                # attempt's, because the seam did not release it.
                continue
            self.released.add(component_id)
            self.fresh.pop(component_id, None)
            self.current.pop(component_id, None)
            self.up.discard(component_id)

    def _component_state(self, component_id: str, **changes: Any) -> None:
        """Record one per-component runtime fact, never inventing a component."""
        self.update(self.record.with_component(component_id, at=self.now(), **changes))

    def _record_observations(
        self, observations: Sequence[ComponentObservation]
    ) -> None:
        """Record what the restarted components reported about themselves (§19)."""
        at = self.now()
        record = self.record
        for observation in observations:
            component_id, version, platform_id = observation.observed_identity()
            record = record.with_component(
                observation.component_id,
                at=at,
                observed_component_id=component_id,
                observed_version=version,
                observed_platform_id=platform_id,
                observed_execution=(
                    dict(observation.execution) if observation.execution else None
                ),
                health=dict(observation.health) if observation.health else None,
                readiness=(
                    dict(observation.readiness) if observation.readiness else None
                ),
                healthy=observation.healthy,
                error=observation.error,
            )
        self.update(record)

    def _hand_over(self) -> None:
        """The operation keeps managing the references this attempt still holds.

        Everything released during the attempt is gone from ``current``, so the
        operation is left exactly with the runtime references that exist for it
        — never with a stale handle to an element whose reference was already
        released (§18).
        """
        self.deployment._handles = tuple(
            self.current[component_id] for component_id in sorted(self.current)
        )
        self.deployment._runtime = self.adapter


__all__ = [
    "ACTION_RESTARTED",
    "ACTION_RESTART_FAILED",
    "EXECUTION_CHECKS",
    "STRICT_STOP_START_POLICY",
    "Restart",
    "RestartRequest",
    "StopStartPolicy",
    "derive_restart_id",
    "restart",
    "validate_stop_start_policy",
]
