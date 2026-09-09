"""Workflow state model of the Saga / Workflow Consistency Boundary (IS-006).

This module is the whole state model of the component, and the state model is
the whole component: IS-006 owns **workflow state and nothing else**. It owns
no identity, no tenant registry, no permission grant, no idempotency table and
no copy of any business data. A step's business effect belongs to the
component that owns the data and is reached only through that component's
published contract (``Saga -> component contract -> component data owner``).

Two consequences are visible in the types below:

* a step record states *that* an effect was recorded, by whom and with which
  outcome — it never holds the business value the effect returned. Results
  travel in process, inside one run, and are not part of the saga's owned
  state, so a snapshot of this store can never serve business data;
* :class:`SagaContext` is frozen and carries the effective Tenant resolved by
  IS-001. There is no operation anywhere in this component that produces a
  second :class:`SagaContext` with another ``tenant_id``: the tenant of a saga
  is immutable for the whole workflow (invariant 9, LAW-16, LAW-16a).

State machines are closed maps (:data:`SAGA_TRANSITIONS`,
:data:`STEP_TRANSITIONS`). A transition that is not in the map is refused by
the store, so no undefined partial state can be written silently
(invariant 2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from saga.errors import SagaDefinitionError

#: Identity of this component, as published in its Component Contract.
COMPONENT_ID = "saga"
COMPONENT_VERSION = "0.1.0"
COMPONENT_CLASS = "platform_service"

#: The two phases of a step that can carry a business effect. Both go through
#: the IS-005 command-safety boundary with their own key, which is what makes
#: a compensation idempotent as well (invariant 7).
PHASE_EXECUTE = "execute"
PHASE_COMPENSATE = "compensate"


class SagaState(StrEnum):
    """Explicit state of one workflow — no implicit and no partial state.

    ``STARTED`` — created, tenant context resolved, no step executed yet.
    ``RUNNING`` — steps are being executed in declaration order.
    ``COMPENSATING`` — a step failed and the completed steps are being undone
    in reverse declaration order.
    ``COMPLETED`` / ``COMPENSATED`` / ``FAILED`` — terminal (invariant 8).
    """

    STARTED = "STARTED"
    RUNNING = "RUNNING"
    COMPENSATING = "COMPENSATING"
    COMPLETED = "COMPLETED"
    COMPENSATED = "COMPENSATED"
    FAILED = "FAILED"


#: Terminal states: once reached, a saga accepts no further delivery.
TERMINAL_SAGA_STATES: frozenset[SagaState] = frozenset(
    {SagaState.COMPLETED, SagaState.COMPENSATED, SagaState.FAILED}
)

#: The closed state machine of a saga. Anything not listed here is refused by
#: the store instead of being written, so an undefined partial state cannot
#: appear even by accident (invariant 2).
SAGA_TRANSITIONS: dict[SagaState, frozenset[SagaState]] = {
    SagaState.STARTED: frozenset({SagaState.RUNNING}),
    SagaState.RUNNING: frozenset(
        {SagaState.COMPLETED, SagaState.COMPENSATING, SagaState.FAILED}
    ),
    SagaState.COMPENSATING: frozenset({SagaState.COMPENSATED, SagaState.FAILED}),
    SagaState.COMPLETED: frozenset(),
    SagaState.COMPENSATED: frozenset(),
    SagaState.FAILED: frozenset(),
}


class StepState(StrEnum):
    """Explicit state of one step, including its recovery progress.

    ``RETRYING`` lives at step level, as the Issue requires: it says "this
    attempt failed and another attempt is allowed", not "the whole workflow is
    retrying". ``COMPENSATING`` / ``COMPENSATED`` / ``COMPENSATION_FAILED``
    make the undo of a completed step observable, so a compensation is never
    an invisible side effect.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


#: Step states a terminal saga must not be left in: a workflow that reached a
#: terminal state with a step still "in progress" would be an undefined partial
#: state, and the executor refuses to write one.
UNSETTLED_STEP_STATES: frozenset[StepState] = frozenset(
    {StepState.RUNNING, StepState.RETRYING, StepState.COMPENSATING}
)

STEP_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset({StepState.RUNNING}),
    StepState.RUNNING: frozenset(
        {StepState.COMPLETED, StepState.RETRYING, StepState.FAILED}
    ),
    StepState.RETRYING: frozenset({StepState.RUNNING}),
    StepState.COMPLETED: frozenset({StepState.COMPENSATING}),
    StepState.COMPENSATING: frozenset(
        {StepState.COMPENSATED, StepState.COMPENSATION_FAILED}
    ),
    StepState.FAILED: frozenset(),
    StepState.COMPENSATED: frozenset(),
    StepState.COMPENSATION_FAILED: frozenset(),
}


#: Kinds of failure the component distinguishes. They are workflow facts, not
#: business semantics: the reason string itself belongs to the component that
#: refused or failed.
class FailureKind(StrEnum):
    #: The step stated its own failure (a refusal of a business boundary, a
    #: dependency that did not answer, ...).
    STEP_FAILURE = "step_failure"
    #: The step raised something it did not classify. Never read as retryable
    #: and never read as success.
    UNCLASSIFIED = "unclassified"
    #: The attempt returned past its declared timeout. A timeout is a failure,
    #: never a silent success (ARCHITECTURE.md §6.4).
    TIMEOUT = "timeout"
    #: The undo of a completed step failed: the saga cannot reach a consistent
    #: state on its own and ends in the terminal ``FAILED`` state.
    COMPENSATION_FAILED = "compensation_failed"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Deterministic retry declaration of one step.

    ``max_attempts`` counts the first attempt: ``1`` means "no retry". Only a
    failure that the step itself declared retryable is retried — a security
    refusal is never retried, because repeating a denied operation is not a
    recovery path. ``delay_seconds`` is honoured through the executor's
    injected sleeper, so a test can assert the schedule without waiting for it.
    """

    max_attempts: int = 1
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise SagaDefinitionError("retry policy requires max_attempts >= 1")
        if self.delay_seconds < 0:
            raise SagaDefinitionError("retry policy requires a non-negative delay")


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """One declared step: its effect, its undo and its recovery declaration.

    ``compensate`` is **required** and is therefore always declared before the
    step executes (ARCHITECTURE.md §6.4). A step with nothing to undo declares
    that explicitly with a compensation that does nothing, so the absence of an
    undo is a declaration and never an oversight.

    ``execute`` and ``compensate`` receive a :class:`StepContext` and return
    whatever the business contract returned. The saga hands that value to the
    following steps of the same run and does not store it: workflow state only.
    """

    step_id: str
    operation: str
    execute: Callable[[StepContext], Any]
    compensate: Callable[[StepContext], Any]
    payload: Mapping[str, Any] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise SagaDefinitionError("a step requires a non-empty step_id")
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise SagaDefinitionError("a step requires a non-empty operation")
        if not callable(self.execute):
            raise SagaDefinitionError(
                f"step {self.step_id!r} requires a callable execute"
            )
        if not callable(self.compensate):
            # Compensation is declared before execution — always.
            raise SagaDefinitionError(
                f"step {self.step_id!r} must declare a compensation; "
                "a step with nothing to undo declares an explicit no-op"
            )
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise SagaDefinitionError(
                f"step {self.step_id!r} requires a positive timeout"
            )

    @property
    def frozen_payload(self) -> Mapping[str, Any]:
        """The declared payload, read-only: a step cannot restate its inputs."""
        return MappingProxyType(dict(self.payload))


@dataclass(frozen=True, slots=True)
class SagaDefinition:
    """An ordered, validated list of steps — the workflow declaration.

    A definition is a value: holding one starts nothing, and the executor
    refuses a definition that is empty or that repeats a ``step_id``, because
    a repeated identity would make a step's state ambiguous (invariant 3).
    """

    name: str
    steps: Sequence[StepDefinition]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SagaDefinitionError("a saga definition requires a non-empty name")
        declared = tuple(self.steps)
        if not declared:
            raise SagaDefinitionError("a saga definition requires at least one step")
        seen: set[str] = set()
        for step in declared:
            if not isinstance(step, StepDefinition):
                raise SagaDefinitionError(
                    "a saga definition requires StepDefinition values"
                )
            if step.step_id in seen:
                raise SagaDefinitionError(f"step_id {step.step_id!r} is declared twice")
            seen.add(step.step_id)
        object.__setattr__(self, "steps", declared)

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps)

    def step(self, step_id: str) -> StepDefinition:
        for declared in self.steps:
            if declared.step_id == step_id:
                return declared
        raise KeyError(step_id)


@dataclass(frozen=True, slots=True)
class SagaContext:
    """Immutable correlation and tenant context of one workflow.

    ``tenant_id``, ``identity_id`` and ``platform_id`` come from the verified
    identity context of IS-001 — never from a caller claim, and never
    re-derived later in the workflow. The tracing fields are the correlation
    context every saga/step transition is audited with (invariant 11).

    ``subject_credential`` is deliberately **not** part of this context: a
    credential is presented per delivery and is never persisted, so the store
    and the audit journal hold identities, not secrets (ARCHITECTURE.md §27).
    """

    saga_id: str
    definition: str
    tenant_id: str
    identity_id: str
    platform_id: str
    actor_service_id: str
    request_id: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class StepContext:
    """What a step handler is given: workflow context, never another component.

    A handler receives values and its own callable dependencies; it holds no
    reference to the saga store, the executor or the workflow state, and it
    cannot change the tenant it operates in — ``tenant_id`` is the immutable
    tenant of the saga, and a business contract receives it as a cross-check
    exactly as any other caller would.

    ``results`` maps the ``step_id`` of an earlier step of *this run* to the
    value its business contract returned. It is run-scoped on purpose: results
    are not owned saga state, so a resumed run does not carry them and steps
    are addressable through their declared payload instead.
    """

    saga_id: str
    step_id: str
    operation: str
    attempt: int
    phase: str
    tenant_id: str
    identity_id: str
    actor_service_id: str
    subject_credential: str | None
    request_id: str | None
    correlation_id: str | None
    trace_id: str | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    results: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(self, "results", MappingProxyType(dict(self.results)))


@dataclass(frozen=True, slots=True)
class FailureInfo:
    """One recorded failure — errors are never silently lost (invariant 5)."""

    step_id: str
    phase: str
    kind: str
    reason: str
    attempts: int
    retryable: bool = False
    security_sensitive: bool = False


@dataclass
class StepRecord:
    """Workflow state of one step. Contains no business data."""

    step_id: str
    operation: str
    state: StepState = StepState.PENDING
    attempts: int = 0
    #: Set once the command-safety boundary recorded the effect of this step:
    #: a workflow fact, not a copy of what the effect returned.
    effect_recorded: bool = False
    failure: FailureInfo | None = None


@dataclass
class SagaRecord:
    """Workflow state of one saga. Contains no business data."""

    context: SagaContext
    definition: SagaDefinition
    state: SagaState = SagaState.STARTED
    steps: dict[str, StepRecord] = field(default_factory=dict)
    failure: FailureInfo | None = None
    compensation_failures: tuple[FailureInfo, ...] = ()
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class StepSnapshot:
    """Published read of one step's workflow state."""

    step_id: str
    operation: str
    state: StepState
    attempts: int
    effect_recorded: bool
    failure: FailureInfo | None = None


@dataclass(frozen=True, slots=True)
class SagaSnapshot:
    """Published read of one saga: workflow state, correlation context, failures.

    A snapshot is a value: it grants nothing, exposes no store handle and
    carries no business data of any component.
    """

    saga_id: str
    definition: str
    state: SagaState
    tenant_id: str
    identity_id: str
    platform_id: str
    actor_service_id: str
    request_id: str | None
    correlation_id: str | None
    trace_id: str | None
    steps: tuple[StepSnapshot, ...]
    failure: FailureInfo | None = None
    compensation_failures: tuple[FailureInfo, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_SAGA_STATES

    def step(self, step_id: str) -> StepSnapshot:
        for snapshot in self.steps:
            if snapshot.step_id == step_id:
                return snapshot
        raise KeyError(step_id)


@dataclass(frozen=True, slots=True)
class SagaAuditEvent:
    """One audited workflow transition with the full correlation context.

    The field set is the observability context of ARCHITECTURE.md §26 plus the
    saga identity: ``saga_id``, ``tenant_id``, ``request_id``,
    ``correlation_id``, ``trace_id`` and the acting service/subject identity.
    Security-sensitive refusals and terminal transitions are recorded here, so
    they are observable after the fact (invariant 11).
    """

    seq: int
    timestamp: str
    action: str
    component_id: str
    component_version: str
    saga_id: str | None
    step_id: str | None
    state: str | None
    reason: str | None
    tenant_id: str | None
    identity_id: str | None
    actor_service_id: str | None
    platform_id: str | None
    request_id: str | None
    correlation_id: str | None
    trace_id: str | None
    security_sensitive: bool = False


def step_fingerprint(
    *,
    definition: str,
    saga_id: str,
    step_id: str,
    operation: str,
    phase: str,
    payload: Mapping[str, Any] | None,
) -> str:
    """Stable payload fingerprint of one step delivery, for the IS-005 boundary.

    The fingerprint covers the step identity, the phase and the declared
    payload: a re-delivery of the same step is a replay, while the same step
    identity carrying another payload is a conflict that IS-005 refuses
    (its invariants I-003..I-005). IS-006 computes the fingerprint and owns no
    second idempotency mechanism of its own (invariant 10).
    """
    canonical = json.dumps(
        {
            "definition": definition,
            "saga_id": saga_id,
            "step_id": step_id,
            "operation": operation,
            "phase": phase,
            "payload": dict(payload or {}),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def step_idempotency_key(saga_id: str, step_id: str, phase: str) -> str:
    """The IS-005 command identity of one step of one saga.

    Deliberately attempt-independent: the identity of the command is the step,
    not the try, so a retry after a failed attempt re-executes (IS-005 records
    successful effects only) while a re-delivery after a recorded effect
    replays. Compensation has its own key, so undoing a step is its own
    exactly-once command.
    """
    return f"saga:{saga_id}:step:{step_id}:{phase}"


__all__ = [
    "COMPONENT_CLASS",
    "COMPONENT_ID",
    "COMPONENT_VERSION",
    "PHASE_COMPENSATE",
    "PHASE_EXECUTE",
    "SAGA_TRANSITIONS",
    "STEP_TRANSITIONS",
    "TERMINAL_SAGA_STATES",
    "UNSETTLED_STEP_STATES",
    "FailureInfo",
    "FailureKind",
    "RetryPolicy",
    "SagaAuditEvent",
    "SagaContext",
    "SagaDefinition",
    "SagaRecord",
    "SagaSnapshot",
    "SagaState",
    "StepContext",
    "StepDefinition",
    "StepRecord",
    "StepSnapshot",
    "StepState",
    "step_fingerprint",
    "step_idempotency_key",
]
