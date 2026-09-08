"""Owned data of the Saga boundary (logical schema `saga`): workflow state only.

This module is internal. No other component has direct access to it, and — the
part that matters for this slice — it contains nothing but workflow state:

* :class:`SagaRecord` — the state of one workflow, its immutable context and
  its terminal failure information;
* :class:`StepRecord` — the state of one step, its attempt counter and whether
  the command-safety boundary recorded its effect;
* :data:`SagaStore.audit` — the append-only journal of every workflow
  transition, with the correlation context required for observability.

There is deliberately no business data here: no resource, no order, no document
and no copy of anything another component owns. A step's business effect lives
in the component that owns the data, and the value it returned is handed to the
following steps in process instead of being stored (invariant: *Saga owns only
its workflow state*).

The store is bounded (:attr:`SagaStore.max_sagas`) because an in-memory proof
store that grows without limit is not a store but a leak; a full store refuses
a new workflow instead of evicting workflow state silently. The bound applies to
workflow records: the audit journal is append-only and is never truncated,
because an audit record that can disappear is not an audit record.

State changes go through the closed maps of :mod:`saga.models`. A transition
that is not declared there raises :class:`saga.errors.IllegalStateTransition`
and writes nothing, so no undefined partial state can be recorded even by a bug
in the executor (invariant 2).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from saga.errors import (
    IllegalStateTransition,
    SagaNotFound,
    SagaStoreExhausted,
    StepUnknown,
)
from saga.models import (
    COMPONENT_ID,
    COMPONENT_VERSION,
    SAGA_TRANSITIONS,
    STEP_TRANSITIONS,
    TERMINAL_SAGA_STATES,
    UNSETTLED_STEP_STATES,
    FailureInfo,
    SagaAuditEvent,
    SagaContext,
    SagaDefinition,
    SagaRecord,
    SagaSnapshot,
    SagaState,
    StepRecord,
    StepSnapshot,
    StepState,
)

#: Bound of the Level 0 in-memory store: enough for behavioral proof, small
#: enough that "bounded" is a true statement.
DEFAULT_MAX_SAGAS = 256


@dataclass
class SagaStore:
    """Bounded in-memory storage of workflow state and the audit journal."""

    max_sagas: int = DEFAULT_MAX_SAGAS
    sagas: dict[str, SagaRecord] = field(default_factory=dict)
    audit: list[SagaAuditEvent] = field(default_factory=list)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    # ------------------------------------------------------------------ writes
    def create(
        self, context: SagaContext, definition: SagaDefinition, *, now: str
    ) -> SagaRecord:
        """Create one workflow in ``STARTED`` with every step ``PENDING``."""
        with self._lock:
            if len(self.sagas) >= self.max_sagas:
                raise SagaStoreExhausted(self.max_sagas)
            if context.saga_id in self.sagas:
                raise ValueError(f"saga_id {context.saga_id!r} is already in use")
            record = SagaRecord(
                context=context,
                definition=definition,
                state=SagaState.STARTED,
                steps={
                    step.step_id: StepRecord(
                        step_id=step.step_id, operation=step.operation
                    )
                    for step in definition.steps
                },
                updated_at=now,
            )
            self.sagas[context.saga_id] = record
            return record

    def transition_saga(
        self,
        saga_id: str,
        to_state: SagaState,
        *,
        now: str,
        failure: FailureInfo | None = None,
        compensation_failures: tuple[FailureInfo, ...] = (),
    ) -> SagaRecord:
        """Move one workflow to ``to_state`` — only along the declared map."""
        record = self.get(saga_id)
        allowed = SAGA_TRANSITIONS[record.state]
        if to_state not in allowed:
            raise IllegalStateTransition(
                f"saga {saga_id!r}: transition {record.state.value} -> {to_state.value} "
                "is not declared"
            )
        with self._lock:
            # Validate before writing: a refused transition must leave the
            # workflow exactly as it was, not half-written.
            if to_state in TERMINAL_SAGA_STATES:
                self._assert_settled(record, to_state)
            record.state = to_state
            record.updated_at = now
            if failure is not None:
                record.failure = failure
            if compensation_failures:
                record.compensation_failures = compensation_failures
            return record

    def transition_step(
        self,
        saga_id: str,
        step_id: str,
        to_state: StepState,
        *,
        now: str,
        failure: FailureInfo | None = None,
        effect_recorded: bool | None = None,
    ) -> StepRecord:
        """Move one step to ``to_state`` — only along the declared map."""
        record = self.get(saga_id)
        step = record.steps.get(step_id)
        if step is None:
            raise StepUnknown(saga_id, step_id)
        allowed = STEP_TRANSITIONS[step.state]
        if to_state not in allowed:
            raise IllegalStateTransition(
                f"saga {saga_id!r} step {step_id!r}: transition "
                f"{step.state.value} -> {to_state.value} is not declared"
            )
        with self._lock:
            step.state = to_state
            record.updated_at = now
            if failure is not None:
                step.failure = failure
            if effect_recorded is not None:
                step.effect_recorded = effect_recorded
            return step

    def count_attempt(self, saga_id: str, step_id: str) -> int:
        """Record one delivery attempt of a step and return its number."""
        record = self.get(saga_id)
        step = record.steps.get(step_id)
        if step is None:
            raise StepUnknown(saga_id, step_id)
        with self._lock:
            step.attempts += 1
            return step.attempts

    # ------------------------------------------------------------------- reads
    def get(self, saga_id: str) -> SagaRecord:
        record = self.sagas.get(saga_id)
        if record is None:
            raise SagaNotFound(saga_id)
        return record

    def snapshot(self, saga_id: str) -> SagaSnapshot:
        """The published read of one workflow: values only, no store handle."""
        record = self.get(saga_id)
        with self._lock:
            steps = tuple(
                StepSnapshot(
                    step_id=step.step_id,
                    operation=step.operation,
                    state=step.state,
                    attempts=step.attempts,
                    effect_recorded=step.effect_recorded,
                    failure=step.failure,
                )
                for step in record.steps.values()
            )
            return SagaSnapshot(
                saga_id=record.context.saga_id,
                definition=record.context.definition,
                state=record.state,
                tenant_id=record.context.tenant_id,
                identity_id=record.context.identity_id,
                platform_id=record.context.platform_id,
                actor_service_id=record.context.actor_service_id,
                request_id=record.context.request_id,
                correlation_id=record.context.correlation_id,
                trace_id=record.context.trace_id,
                steps=steps,
                failure=record.failure,
                compensation_failures=record.compensation_failures,
            )

    def saga_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self.sagas)

    # ------------------------------------------------------------------- audit
    def append_audit(
        self,
        action: str,
        *,
        now: str,
        saga_id: str | None,
        step_id: str | None = None,
        state: str | None = None,
        reason: str | None = None,
        tenant_id: str | None = None,
        identity_id: str | None = None,
        actor_service_id: str | None = None,
        platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        security_sensitive: bool = False,
    ) -> SagaAuditEvent:
        """Append one immutable workflow event carrying the correlation context.

        The journal is append-only: nothing here rewrites or removes an entry,
        so a refusal that happened stays observable (invariant 11).
        """
        with self._lock:
            event = SagaAuditEvent(
                seq=len(self.audit) + 1,
                timestamp=now,
                action=action,
                component_id=COMPONENT_ID,
                component_version=COMPONENT_VERSION,
                saga_id=saga_id,
                step_id=step_id,
                state=state,
                reason=reason,
                tenant_id=tenant_id,
                identity_id=identity_id,
                actor_service_id=actor_service_id,
                platform_id=platform_id,
                request_id=request_id,
                correlation_id=correlation_id,
                trace_id=trace_id,
                security_sensitive=security_sensitive,
            )
            self.audit.append(event)
            return event

    def audit_trail(self, saga_id: str) -> tuple[SagaAuditEvent, ...]:
        """The audited history of one workflow, in the order it happened."""
        with self._lock:
            return tuple(event for event in self.audit if event.saga_id == saga_id)

    # --------------------------------------------------------------- internals
    @staticmethod
    def _assert_settled(record: SagaRecord, to_state: SagaState) -> None:
        """A terminal workflow must not leave a step "in progress"."""
        unsettled = [
            step.step_id
            for step in record.steps.values()
            if step.state in UNSETTLED_STEP_STATES
        ]
        if unsettled:
            raise IllegalStateTransition(
                f"saga {record.context.saga_id!r} cannot reach {to_state.value} with "
                f"unsettled steps: {', '.join(sorted(unsettled))}"
            )


__all__ = ["DEFAULT_MAX_SAGAS", "SagaStore"]
