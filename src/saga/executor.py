"""The in-process Saga executor of IS-006 — workflow state, nothing else.

This is the whole runtime of the component: one bounded in-memory store
(:mod:`saga.store`), one deterministic executor, and two consumed boundaries —
IS-001 for the effective tenant context and IS-005 for the exactly-once
delivery of every step effect and every compensation. There is no broker, no
worker pool, no scheduler, no external workflow engine and no distributed lock:
a Level 0 proof slice of the Saga boundary (Issue #18, non-goals).

What the executor guarantees, and where:

* **explicit state** — every saga and every step moves only along the closed
  maps of :mod:`saga.models`, and every saga reaches ``COMPLETED``,
  ``COMPENSATED`` or ``FAILED`` (invariants 2, 3, 8);
* **exactly one business effect per step** — each attempt is delivered through
  the IS-005 guard under a stable, attempt-independent command identity, so a
  re-delivery replays instead of re-applying. For a step the saga already
  recorded as completed the effect is never handed to the guard again at all:
  the guard is asked to replay, and if it cannot, that is an explicit error and
  not a second effect (invariants 4, 10);
* **a defined recovery path** — a failure the step declared retryable is retried
  within its declared policy; anything else moves the workflow into
  compensation of the completed steps in reverse order, and a compensation that
  fails ends the workflow in the terminal ``FAILED`` state with the failure
  preserved (invariants 6, 7, 8);
* **no silent loss** — every transition, refusal, retry, timeout and
  compensation failure is written to the audit journal with ``saga_id``,
  ``tenant_id``, ``request_id``, ``correlation_id``, ``trace_id`` and the acting
  service/subject identity (invariants 5, 11);
* **one immutable tenant** — the tenant of a workflow is resolved once from the
  verified identity by IS-001 and is re-verified on every delivery; a delivery
  presenting another identity or tenant is refused before any step runs
  (invariant 9).

One workflow is delivered by one thread at a time inside this executor
(:meth:`SagaExecutor._saga_lock`). That is ownership of the saga's own state by
the component that owns it — steps of one workflow are ordered, so two
concurrent deliveries of the same workflow are serialized rather than
interleaved. It is an in-process lock over this component's own store; IS-006
has no cross-process recovery and claims none.

A timeout is never a silent success: an attempt that returns past its declared
``timeout_seconds`` is recorded as a failure with a recovery path, exactly like
any other failure (ARCHITECTURE.md §6.4). At Level 0 a hung handler cannot be
preempted — there is no worker thread to cancel in a proof slice — so the
timeout is evaluated when the attempt returns; that limitation is stated here
rather than hidden behind a claim of cancellation that does not exist.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from idempotency.errors import IdempotencyConflict
from idempotency.guard import IdempotencyGuard
from saga.consumed import TenantContextAnswer, TenantContextRefusal
from saga.errors import (
    IllegalStateTransition,
    SagaContextViolation,
    SagaDefinitionError,
    SagaRefused,
    SagaTerminalState,
    StepConflict,
    StepFailure,
    StepUnknown,
)
from saga.models import (
    COMPONENT_ID,
    COMPONENT_VERSION,
    PHASE_COMPENSATE,
    PHASE_EXECUTE,
    TERMINAL_SAGA_STATES,
    FailureInfo,
    FailureKind,
    SagaAuditEvent,
    SagaContext,
    SagaDefinition,
    SagaRecord,
    SagaSnapshot,
    SagaState,
    StepContext,
    StepDefinition,
    StepState,
    step_fingerprint,
    step_idempotency_key,
)
from saga.ports import CommandSafetyPort, TenantContextPort
from saga.store import SagaStore

#: The service identity this executor acts with, recorded in every audit event.
DEFAULT_ACTOR_SERVICE_ID = "svc-saga"

#: Audit actions of this component. They are part of the observable behaviour of
#: the slice — tests assert on them, so renaming one is a behaviour change.
ACTION_CREATED = "saga.created"
ACTION_START_REFUSED = "saga.start_refused"
ACTION_TRANSITION = "saga.transition"
ACTION_TERMINAL = "saga.terminal"
ACTION_DELIVERY_REFUSED = "saga.delivery_refused"
ACTION_STEP_STARTED = "saga.step.started"
ACTION_STEP_ATTEMPT = "saga.step.attempt"
ACTION_STEP_COMPLETED = "saga.step.completed"
ACTION_STEP_REDELIVERED = "saga.step.redelivered"
ACTION_STEP_SKIPPED = "saga.step.skipped"
ACTION_STEP_RETRYING = "saga.step.retrying"
ACTION_STEP_FAILED = "saga.step.failed"
ACTION_STEP_CONFLICT = "saga.step.conflict"
ACTION_STEP_COMPENSATING = "saga.step.compensating"
ACTION_STEP_COMPENSATED = "saga.step.compensated"
ACTION_STEP_COMPENSATION_FAILED = "saga.step.compensation_failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class _Observation:
    """Correlation context of one delivery, carried into every event it writes."""

    request_id: str | None
    correlation_id: str | None
    trace_id: str | None


class SagaExecutor:
    """Deterministic, in-process executor of multi-step business operations.

    The executor is composition-internal: a composition root builds it, wires
    the two consumed boundaries and hands it workflow declarations whose steps
    call the published contracts of the components that own the data. The
    executor itself knows no business component: it holds a tenant-context port,
    the IS-005 guard, its own store and a clock — and nothing that could reach
    another component's storage or internals (invariant 12).
    """

    def __init__(
        self,
        *,
        tenant_context: TenantContextPort,
        idempotency: CommandSafetyPort | None = None,
        store: SagaStore | None = None,
        actor_service_id: str = DEFAULT_ACTOR_SERVICE_ID,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        if not isinstance(actor_service_id, str) or not actor_service_id.strip():
            raise ValueError("the executor requires a non-empty actor service identity")
        self.tenant_context = tenant_context
        # No second idempotency mechanism: the default is the IS-005 guard
        # itself, auditing into this component's journal.
        self.idempotency: CommandSafetyPort = idempotency or IdempotencyGuard(
            audit_sink=self.audit_sink
        )
        self.store = store or SagaStore()
        self.actor_service_id = actor_service_id
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------ public reads
    def snapshot(self, saga_id: str) -> SagaSnapshot:
        """The published read of one workflow: values only."""
        return self.store.snapshot(saga_id)

    def audit_trail(self, saga_id: str) -> tuple[SagaAuditEvent, ...]:
        """The audited history of one workflow, in the order it happened."""
        return self.store.audit_trail(saga_id)

    def audit_sink(self, action: str, details: dict[str, Any]) -> None:
        """Audit sink handed to the IS-005 guard.

        The command-safety boundary reports its replays and conflicts here, so
        the workflow journal shows *why* a re-delivery produced no second
        effect instead of merely asserting it.
        """
        key = details.get("key")
        saga_id, step_id = _parse_step_key(key if isinstance(key, str) else None)
        self.store.append_audit(
            action,
            now=self._now(),
            saga_id=saga_id,
            step_id=step_id,
            reason=action,
            tenant_id=_as_str(details.get("tenant_id")),
            identity_id=_as_str(details.get("identity")),
            actor_service_id=self.actor_service_id,
            request_id=_as_str(details.get("request_id")),
            correlation_id=_as_str(details.get("correlation_id")),
            security_sensitive=action == "idempotency_conflict",
        )

    # ------------------------------------------------------------------ start
    def start(
        self,
        definition: SagaDefinition,
        *,
        subject_credential: str | None,
        claimed_tenant_id: str | None = None,
        saga_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> SagaSnapshot:
        """Create one workflow in ``STARTED`` — or refuse, creating nothing.

        The effective tenant comes from IS-001 (:class:`TenantContextPort`);
        ``claimed_tenant_id`` is forwarded as the cross-check that contract
        defines and can never select the tenant. When IS-001 refuses, no saga
        record, no step record and no business effect exist afterwards, and the
        refusal itself is audited (invariant 9).
        """
        if not isinstance(definition, SagaDefinition):
            raise SagaDefinitionError("a SagaDefinition is required")
        obs = _Observation(request_id, correlation_id, trace_id)
        answer = self._resolve(
            subject_credential, claimed_tenant_id=claimed_tenant_id, obs=obs
        )
        if isinstance(answer, TenantContextRefusal):
            self.store.append_audit(
                ACTION_START_REFUSED,
                now=self._now(),
                saga_id=None,
                state=SagaState.STARTED.value,
                reason=answer.reason_code,
                actor_service_id=self.actor_service_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                trace_id=obs.trace_id,
                security_sensitive=True,
            )
            raise SagaRefused(answer.reason_code or "tenant_context_unavailable")

        context = SagaContext(
            saga_id=saga_id or f"sag_{uuid4().hex[:16]}",
            definition=definition.name,
            tenant_id=answer.tenant_id,
            identity_id=answer.identity_id,
            platform_id=answer.platform_id,
            actor_service_id=self.actor_service_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            trace_id=obs.trace_id,
            created_at=self._now(),
        )
        self.store.create(context, definition, now=context.created_at)
        record = self.store.get(context.saga_id)
        self._audit(
            ACTION_CREATED,
            record,
            obs,
            state=SagaState.STARTED.value,
            reason=definition.name,
        )
        return self.store.snapshot(context.saga_id)

    # -------------------------------------------------------------------- run
    def run(
        self,
        saga_id: str,
        *,
        subject_credential: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> SagaSnapshot:
        """Drive one workflow to a terminal state.

        Steps already completed by an earlier delivery are not executed again:
        an interrupted workflow resumes where it stopped, and a step whose
        effect was recorded replays through IS-005 instead of re-applying. That
        is what makes delivering the same workflow twice safe (invariant 4).
        """
        with self._saga_lock(saga_id):
            record, obs = self._accept_delivery(
                saga_id,
                subject_credential,
                request_id=request_id,
                correlation_id=correlation_id,
                trace_id=trace_id,
            )
            if record.state is SagaState.COMPENSATING:
                # Interrupted inside the compensation phase: finish the undo.
                self._compensate(record, record.failure, subject_credential, obs)
                return self.store.snapshot(saga_id)

            self._begin(record, obs)
            results: dict[str, Any] = {}
            for step in record.definition.steps:
                if record.steps[step.step_id].state is StepState.COMPLETED:
                    self._audit(
                        ACTION_STEP_SKIPPED,
                        record,
                        obs,
                        step_id=step.step_id,
                        state=StepState.COMPLETED.value,
                        reason="effect_recorded",
                    )
                    continue
                failure = self._execute_step(
                    record, step, subject_credential, obs, results
                )
                if failure is not None:
                    self._recover(record, failure, subject_credential, obs)
                    break
            else:
                self._finish(
                    record, SagaState.COMPLETED, obs, reason="all_steps_completed"
                )
            return self.store.snapshot(saga_id)

    def deliver_step(
        self,
        saga_id: str,
        step_id: str,
        *,
        subject_credential: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> SagaSnapshot:
        """Deliver (or re-deliver) exactly one step of one workflow.

        This is the delivery boundary the acceptance scenarios attack: the same
        step delivered twice performs its business effect once, because the
        second delivery is a replay at the IS-005 boundary. A step that fails
        here follows the same recovery path as inside :meth:`run`.

        Declaration order decides the order :meth:`run` executes steps in and
        the order compensations run in; it is not a constraint on an explicit
        delivery, which addresses one declared step by its identity.
        """
        with self._saga_lock(saga_id):
            record, obs = self._accept_delivery(
                saga_id,
                subject_credential,
                request_id=request_id,
                correlation_id=correlation_id,
                trace_id=trace_id,
            )
            try:
                step = record.definition.step(step_id)
            except KeyError as exc:
                raise StepUnknown(saga_id, step_id) from exc
            if record.state is SagaState.COMPENSATING:
                self._compensate(record, record.failure, subject_credential, obs)
                return self.store.snapshot(saga_id)

            self._begin(record, obs)
            failure = self._execute_step(record, step, subject_credential, obs, {})
            if failure is not None:
                self._recover(record, failure, subject_credential, obs)
            return self.store.snapshot(saga_id)

    # -------------------------------------------------------------- deliveries
    def _accept_delivery(
        self,
        saga_id: str,
        subject_credential: str | None,
        *,
        request_id: str | None,
        correlation_id: str | None,
        trace_id: str | None,
    ) -> tuple[SagaRecord, _Observation]:
        """Refuse a delivery that cannot run; otherwise return the record.

        Two refusals, both before any business effect and both audited: a
        terminal workflow accepts nothing more, and a delivery must present the
        identity and tenant the workflow was started with — the tenant of a saga
        is immutable (invariant 9).
        """
        obs = _Observation(request_id, correlation_id, trace_id)
        record = self.store.get(saga_id)
        if record.state in TERMINAL_SAGA_STATES:
            self._audit(
                ACTION_DELIVERY_REFUSED,
                record,
                obs,
                state=record.state.value,
                reason="terminal_state",
                security_sensitive=True,
            )
            raise SagaTerminalState(saga_id, record.state.value)

        answer = self._resolve(
            subject_credential, claimed_tenant_id=record.context.tenant_id, obs=obs
        )
        reason = _context_violation(record, answer)
        if reason is not None:
            self._audit(
                ACTION_DELIVERY_REFUSED,
                record,
                obs,
                state=record.state.value,
                reason=reason,
                security_sensitive=True,
            )
            raise SagaContextViolation(saga_id, reason)
        return record, obs

    def _resolve(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None,
        obs: _Observation,
    ) -> TenantContextAnswer | TenantContextRefusal:
        """Ask IS-001 for the effective tenant context; never derive one here."""
        try:
            return self.tenant_context.resolve(
                subject_credential,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:  # a dependency that cannot answer is not a context
            return TenantContextRefusal(
                f"tenant_context_unavailable:{type(exc).__name__}"
            )

    def _begin(self, record: SagaRecord, obs: _Observation) -> None:
        if record.state is SagaState.STARTED:
            self.store.transition_saga(
                record.context.saga_id, SagaState.RUNNING, now=self._now()
            )
            self._audit(
                ACTION_TRANSITION,
                record,
                obs,
                state=SagaState.RUNNING.value,
                reason="started",
            )

    def _execute_step(
        self,
        record: SagaRecord,
        step: StepDefinition,
        subject_credential: str | None,
        obs: _Observation,
        results: dict[str, Any],
    ) -> FailureInfo | None:
        """Deliver one step; return ``None`` on success, the failure otherwise."""
        if record.steps[step.step_id].state is StepState.COMPLETED:
            self._replay(record, step, subject_credential, obs)
            return None

        saga_id = record.context.saga_id
        self.store.transition_step(
            saga_id, step.step_id, StepState.RUNNING, now=self._now()
        )
        self._audit(
            ACTION_STEP_STARTED,
            record,
            obs,
            step_id=step.step_id,
            state=StepState.RUNNING.value,
            reason=step.operation,
        )
        key = step_idempotency_key(saga_id, step.step_id, PHASE_EXECUTE)
        fingerprint = self._fingerprint(record, step, PHASE_EXECUTE)
        attempts_allowed = max(1, step.retry.max_attempts)

        for attempt in range(1, attempts_allowed + 1):
            self.store.count_attempt(saga_id, step.step_id)
            context = self._step_context(
                record, step, subject_credential, obs, attempt, PHASE_EXECUTE, results
            )
            self._audit(
                ACTION_STEP_ATTEMPT,
                record,
                obs,
                step_id=step.step_id,
                state=StepState.RUNNING.value,
                reason=f"attempt:{attempt}",
            )
            started = self._clock()
            try:
                result = self._deliver_effect(
                    key,
                    fingerprint=fingerprint,
                    record=record,
                    operation=step.operation,
                    step_id=step.step_id,
                    obs=obs,
                    effect=lambda: step.execute(context),
                )
            except StepConflict as conflict:
                # The same step identity was delivered with another context or
                # payload: a hard failure of this step, never a second effect.
                outcome = FailureInfo(
                    step_id=step.step_id,
                    phase=PHASE_EXECUTE,
                    kind=FailureKind.STEP_FAILURE.value,
                    reason=conflict.reason,
                    attempts=attempt,
                    retryable=False,
                    security_sensitive=True,
                )
            except StepFailure as failure:
                outcome = FailureInfo(
                    step_id=step.step_id,
                    phase=PHASE_EXECUTE,
                    kind=FailureKind.STEP_FAILURE.value,
                    reason=failure.reason,
                    attempts=attempt,
                    retryable=failure.retryable,
                    security_sensitive=failure.security_sensitive,
                )
            except Exception as exc:  # an unclassified failure is never a success
                outcome = FailureInfo(
                    step_id=step.step_id,
                    phase=PHASE_EXECUTE,
                    kind=FailureKind.UNCLASSIFIED.value,
                    reason=type(exc).__name__,
                    attempts=attempt,
                    retryable=False,
                    security_sensitive=False,
                )
            else:
                elapsed = self._clock() - started
                if step.timeout_seconds is not None and elapsed > step.timeout_seconds:
                    # A timeout is a failure with a recovery path, never a
                    # silent success (ARCHITECTURE.md §6.4).
                    outcome = FailureInfo(
                        step_id=step.step_id,
                        phase=PHASE_EXECUTE,
                        kind=FailureKind.TIMEOUT.value,
                        reason="step_timeout",
                        attempts=attempt,
                        retryable=False,
                        security_sensitive=False,
                    )
                else:
                    self.store.transition_step(
                        saga_id,
                        step.step_id,
                        StepState.COMPLETED,
                        now=self._now(),
                        effect_recorded=True,
                    )
                    results[step.step_id] = result
                    self._audit(
                        ACTION_STEP_COMPLETED,
                        record,
                        obs,
                        step_id=step.step_id,
                        state=StepState.COMPLETED.value,
                        reason=step.operation,
                    )
                    return None

            if outcome.retryable and attempt < attempts_allowed:
                self.store.transition_step(
                    saga_id,
                    step.step_id,
                    StepState.RETRYING,
                    now=self._now(),
                    failure=outcome,
                )
                self._audit(
                    ACTION_STEP_RETRYING,
                    record,
                    obs,
                    step_id=step.step_id,
                    state=StepState.RETRYING.value,
                    reason=outcome.reason,
                    security_sensitive=outcome.security_sensitive,
                )
                if step.retry.delay_seconds:
                    self._sleep(step.retry.delay_seconds)
                self.store.transition_step(
                    saga_id, step.step_id, StepState.RUNNING, now=self._now()
                )
                continue

            self.store.transition_step(
                saga_id,
                step.step_id,
                StepState.FAILED,
                now=self._now(),
                failure=outcome,
            )
            self._audit(
                ACTION_STEP_FAILED,
                record,
                obs,
                step_id=step.step_id,
                state=StepState.FAILED.value,
                reason=outcome.reason,
                security_sensitive=outcome.security_sensitive,
            )
            return outcome

        raise IllegalStateTransition(  # pragma: no cover - the loop always returns
            f"saga {saga_id!r} step {step.step_id!r}: attempt loop ended without a state"
        )

    def _replay(
        self,
        record: SagaRecord,
        step: StepDefinition,
        subject_credential: str | None,
        obs: _Observation,
    ) -> None:
        """Re-deliver a step the saga already recorded as completed.

        The business effect is **not** handed to the guard again: the saga asks
        IS-005 for the recorded result and the effect callable refuses to run. A
        replay therefore cannot apply a second effect even if the recorded
        result were missing — the divergence surfaces as an explicit error
        instead (invariant 4).
        """
        saga_id = record.context.saga_id
        # No StepContext is built: a replay runs no handler, so there is
        # nothing to hand one to.
        self._deliver_effect(
            step_idempotency_key(saga_id, step.step_id, PHASE_EXECUTE),
            fingerprint=self._fingerprint(record, step, PHASE_EXECUTE),
            record=record,
            operation=step.operation,
            step_id=step.step_id,
            obs=obs,
            effect=_refuse_reexecution(saga_id, step.step_id),
        )
        self._audit(
            ACTION_STEP_REDELIVERED,
            record,
            obs,
            step_id=step.step_id,
            state=StepState.COMPLETED.value,
            reason="replayed",
        )

    def _deliver_effect(
        self,
        key: str,
        *,
        fingerprint: str,
        record: SagaRecord,
        operation: str,
        step_id: str,
        obs: _Observation,
        effect: Callable[[], Any],
    ) -> Any:
        """Deliver one effect through the IS-005 command-safety boundary.

        The command identity is the step, not the attempt, and the context is
        the immutable one of the saga: identity, tenant, operation, resource and
        payload fingerprint. IS-005 decides between executing, replaying and
        refusing a conflict — IS-006 owns none of those rules (invariant 10).
        """
        try:
            return self.idempotency.execute(
                key,
                identity=record.context.identity_id,
                tenant_id=record.context.tenant_id,
                operation=operation,
                resource=f"{record.context.saga_id}:{step_id}",
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=effect,
            )
        except IdempotencyConflict as exc:
            self._audit(
                ACTION_STEP_CONFLICT,
                record,
                obs,
                step_id=step_id,
                reason="idempotency_conflict",
                security_sensitive=True,
            )
            raise StepConflict(record.context.saga_id, step_id, exc.details) from exc

    # --------------------------------------------------------------- recovery
    def _recover(
        self,
        record: SagaRecord,
        failure: FailureInfo,
        subject_credential: str | None,
        obs: _Observation,
    ) -> None:
        """The defined recovery path after a step failure.

        If nothing was applied there is no step to undo, so the workflow ends in
        the terminal ``FAILED`` state directly, carrying the failure. Otherwise
        the completed steps are compensated in reverse declaration order:
        ``COMPENSATING`` and then ``COMPENSATED`` when every undo succeeded, or
        ``FAILED`` when one did not (invariants 6, 7, 8).
        """
        saga_id = record.context.saga_id
        applied = [
            step
            for step in record.definition.steps
            if record.steps[step.step_id].state
            in (StepState.COMPLETED, StepState.COMPENSATING)
        ]
        if not applied:
            self._finish(
                record,
                SagaState.FAILED,
                obs,
                reason=failure.reason,
                failure=failure,
                security_sensitive=failure.security_sensitive,
            )
            return

        if record.state is SagaState.RUNNING:
            self.store.transition_saga(saga_id, SagaState.COMPENSATING, now=self._now())
            self._audit(
                ACTION_TRANSITION,
                record,
                obs,
                state=SagaState.COMPENSATING.value,
                reason=failure.reason,
                security_sensitive=failure.security_sensitive,
            )

        compensation_failures = self._compensate_applied(
            record, subject_credential, obs
        )
        if compensation_failures:
            self._finish(
                record,
                SagaState.FAILED,
                obs,
                reason=";".join(
                    f"{i.step_id}:{i.reason}" for i in compensation_failures
                ),
                failure=failure,
                compensation_failures=tuple(compensation_failures),
                security_sensitive=any(
                    i.security_sensitive for i in compensation_failures
                ),
            )
            return

        self._finish(
            record,
            SagaState.COMPENSATED,
            obs,
            reason=failure.reason,
            failure=failure,
            security_sensitive=failure.security_sensitive,
        )

    def _compensate_applied(
        self,
        record: SagaRecord,
        subject_credential: str | None,
        obs: _Observation,
    ) -> list[FailureInfo]:
        """Undo every applied step, in reverse declaration order."""
        failures: list[FailureInfo] = []
        for step in reversed(record.definition.steps):
            outcome = self._compensate_step(record, step, subject_credential, obs)
            if outcome is not None:
                failures.append(outcome)
        return failures

    def _compensate_step(
        self,
        record: SagaRecord,
        step: StepDefinition,
        subject_credential: str | None,
        obs: _Observation,
    ) -> FailureInfo | None:
        """Undo one completed step through the IS-005 boundary.

        A compensation is its own exactly-once command: delivered twice it is a
        replay, so an undo can never be applied twice (invariant 7). It runs
        under the same tenant and identity as the step it undoes and reaches the
        business component through the same published contract, so it is
        authorized and enforced exactly like the step itself.
        """
        saga_id = record.context.saga_id
        step_id = step.step_id
        state = record.steps[step_id].state
        if state is StepState.COMPENSATED:
            return None
        if state is StepState.COMPENSATION_FAILED:
            return record.steps[step_id].failure
        if state not in (StepState.COMPLETED, StepState.COMPENSATING):
            # The step applied nothing, so there is nothing to undo. A
            # compensation is never executed for a step that never ran.
            return None
        if state is StepState.COMPLETED:
            self.store.transition_step(
                saga_id, step_id, StepState.COMPENSATING, now=self._now()
            )
        self._audit(
            ACTION_STEP_COMPENSATING,
            record,
            obs,
            step_id=step_id,
            state=StepState.COMPENSATING.value,
            reason=step.operation,
        )
        context = self._step_context(
            record, step, subject_credential, obs, 1, PHASE_COMPENSATE, {}
        )
        try:
            self._deliver_effect(
                step_idempotency_key(saga_id, step_id, PHASE_COMPENSATE),
                fingerprint=self._fingerprint(record, step, PHASE_COMPENSATE),
                record=record,
                operation=f"{step.operation}.compensate",
                step_id=step_id,
                obs=obs,
                effect=lambda: step.compensate(context),
            )
        except StepConflict as exc:
            outcome = self._compensation_failure(
                step_id, exc.reason, security_sensitive=True
            )
        except StepFailure as failure:
            outcome = self._compensation_failure(
                step_id, failure.reason, security_sensitive=failure.security_sensitive
            )
        except Exception as exc:
            outcome = self._compensation_failure(step_id, type(exc).__name__)
        else:
            self.store.transition_step(
                saga_id, step_id, StepState.COMPENSATED, now=self._now()
            )
            self._audit(
                ACTION_STEP_COMPENSATED,
                record,
                obs,
                step_id=step_id,
                state=StepState.COMPENSATED.value,
                reason=step.operation,
            )
            return None

        self.store.transition_step(
            saga_id,
            step_id,
            StepState.COMPENSATION_FAILED,
            now=self._now(),
            failure=outcome,
        )
        self._audit(
            ACTION_STEP_COMPENSATION_FAILED,
            record,
            obs,
            step_id=step_id,
            state=StepState.COMPENSATION_FAILED.value,
            reason=outcome.reason,
            security_sensitive=outcome.security_sensitive,
        )
        return outcome

    def _compensate(
        self,
        record: SagaRecord,
        failure: FailureInfo | None,
        subject_credential: str | None,
        obs: _Observation,
    ) -> None:
        """Resume an interrupted compensation phase to its terminal state."""
        known = failure or FailureInfo(
            step_id="-",
            phase=PHASE_EXECUTE,
            kind=FailureKind.STEP_FAILURE.value,
            reason="interrupted_compensation",
            attempts=0,
        )
        failures = self._compensate_applied(record, subject_credential, obs)
        if failures:
            self._finish(
                record,
                SagaState.FAILED,
                obs,
                reason=";".join(f"{i.step_id}:{i.reason}" for i in failures),
                failure=known,
                compensation_failures=tuple(failures),
                security_sensitive=any(i.security_sensitive for i in failures),
            )
            return
        self._finish(
            record, SagaState.COMPENSATED, obs, reason=known.reason, failure=known
        )

    @staticmethod
    def _compensation_failure(
        step_id: str, reason: str, *, security_sensitive: bool = False
    ) -> FailureInfo:
        return FailureInfo(
            step_id=step_id,
            phase=PHASE_COMPENSATE,
            kind=FailureKind.COMPENSATION_FAILED.value,
            reason=reason,
            attempts=1,
            retryable=False,
            security_sensitive=security_sensitive,
        )

    # -------------------------------------------------------------- terminals
    def _finish(
        self,
        record: SagaRecord,
        state: SagaState,
        obs: _Observation,
        *,
        reason: str,
        failure: FailureInfo | None = None,
        compensation_failures: tuple[FailureInfo, ...] = (),
        security_sensitive: bool = False,
    ) -> None:
        """Write the terminal state of a workflow and audit it.

        Every saga reaches exactly one of ``COMPLETED``, ``COMPENSATED`` or
        ``FAILED`` (invariant 8), and the terminal transition is the event an
        operator looks for — so it carries the reason and the full correlation
        context.
        """
        self.store.transition_saga(
            record.context.saga_id,
            state,
            now=self._now(),
            failure=failure,
            compensation_failures=compensation_failures,
        )
        self._audit(
            ACTION_TRANSITION,
            record,
            obs,
            state=state.value,
            reason=reason,
            security_sensitive=security_sensitive,
        )
        self._audit(
            ACTION_TERMINAL,
            record,
            obs,
            state=state.value,
            reason=reason,
            security_sensitive=security_sensitive,
        )

    # -------------------------------------------------------------- internals
    def _saga_lock(self, saga_id: str) -> threading.Lock:
        """The in-process delivery lock of one workflow (its own state, its own lock)."""
        with self._locks_guard:
            lock = self._locks.get(saga_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[saga_id] = lock
            return lock

    @staticmethod
    def _fingerprint(record: SagaRecord, step: StepDefinition, phase: str) -> str:
        return step_fingerprint(
            definition=record.definition.name,
            saga_id=record.context.saga_id,
            step_id=step.step_id,
            operation=step.operation,
            phase=phase,
            payload=step.payload,
        )

    def _step_context(
        self,
        record: SagaRecord,
        step: StepDefinition,
        subject_credential: str | None,
        obs: _Observation,
        attempt: int,
        phase: str,
        results: Mapping[str, Any],
    ) -> StepContext:
        """The values one step handler is given — never a handle on this component."""
        return StepContext(
            saga_id=record.context.saga_id,
            step_id=step.step_id,
            operation=step.operation,
            attempt=attempt,
            phase=phase,
            tenant_id=record.context.tenant_id,
            identity_id=record.context.identity_id,
            actor_service_id=record.context.actor_service_id,
            subject_credential=subject_credential,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            trace_id=obs.trace_id,
            payload=step.payload,
            results=results,
        )

    def _audit(
        self,
        action: str,
        record: SagaRecord,
        obs: _Observation,
        *,
        step_id: str | None = None,
        state: str | None = None,
        reason: str | None = None,
        security_sensitive: bool = False,
    ) -> None:
        context = record.context
        self.store.append_audit(
            action,
            now=self._now(),
            saga_id=context.saga_id,
            step_id=step_id,
            state=state,
            reason=reason,
            tenant_id=context.tenant_id,
            identity_id=context.identity_id,
            actor_service_id=context.actor_service_id,
            platform_id=context.platform_id,
            request_id=obs.request_id or context.request_id,
            correlation_id=obs.correlation_id or context.correlation_id,
            trace_id=obs.trace_id or context.trace_id,
            security_sensitive=security_sensitive,
        )


def _context_violation(
    record: SagaRecord, answer: TenantContextAnswer | TenantContextRefusal
) -> str | None:
    """Why a delivery does not match the immutable context of the saga, if at all."""
    if isinstance(answer, TenantContextRefusal):
        return answer.reason_code or "tenant_context_unavailable"
    context = record.context
    if answer.identity_id != context.identity_id:
        return "identity_mismatch"
    if answer.tenant_id != context.tenant_id:
        return "tenant_mismatch"
    if answer.platform_id != context.platform_id:
        return "platform_mismatch"
    return None


def _refuse_reexecution(saga_id: str, step_id: str) -> Callable[[], Any]:
    """An effect that fails loudly instead of applying a second business effect."""

    def effect() -> Any:
        raise IllegalStateTransition(
            f"saga {saga_id!r} step {step_id!r}: the effect was delivered again "
            "after the workflow recorded it"
        )

    return effect


def _parse_step_key(key: str | None) -> tuple[str | None, str | None]:
    """Best-effort ``saga:<id>:step:<id>:<phase>`` split, for audit correlation."""
    if not key or not key.startswith("saga:"):
        return None, None
    parts = key.split(":")
    if len(parts) < 5:
        return (parts[1] if len(parts) > 1 else None), None
    return parts[1], parts[3]


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "ACTION_CREATED",
    "ACTION_DELIVERY_REFUSED",
    "ACTION_START_REFUSED",
    "ACTION_STEP_ATTEMPT",
    "ACTION_STEP_COMPENSATED",
    "ACTION_STEP_COMPENSATING",
    "ACTION_STEP_COMPENSATION_FAILED",
    "ACTION_STEP_COMPLETED",
    "ACTION_STEP_CONFLICT",
    "ACTION_STEP_FAILED",
    "ACTION_STEP_REDELIVERED",
    "ACTION_STEP_RETRYING",
    "ACTION_STEP_SKIPPED",
    "ACTION_STEP_STARTED",
    "ACTION_TERMINAL",
    "ACTION_TRANSITION",
    "COMPONENT_ID",
    "COMPONENT_VERSION",
    "DEFAULT_ACTOR_SERVICE_ID",
    "SagaExecutor",
]
