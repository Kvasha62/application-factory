"""IS-006 state model: explicit states, closed transitions, defined terminals.

These tests attack the state machine itself — the part of the component that
makes "no undefined partial state is silently accepted" (invariant 2) and
"every saga reaches a defined terminal state" (invariant 8) checkable facts
rather than intentions.
"""

from __future__ import annotations

import pytest

from saga.errors import (
    IllegalStateTransition,
    SagaDefinitionError,
    SagaNotFound,
    SagaStoreExhausted,
    StepUnknown,
    StepFailure,
)
from saga.models import (
    SAGA_TRANSITIONS,
    STEP_TRANSITIONS,
    TERMINAL_SAGA_STATES,
    UNSETTLED_STEP_STATES,
    RetryPolicy,
    SagaDefinition,
    SagaState,
    StepDefinition,
    StepState,
)
from saga.store import SagaStore
from tests.conftest import SAGA_CREDENTIAL_A, saga_harness


def noop(ctx):  # noqa: ANN001, ANN201 - the smallest possible declared behaviour
    return None


# ------------------------------------------------------------ declarations
def test_a_definition_requires_at_least_one_step():
    with pytest.raises(SagaDefinitionError):
        SagaDefinition("empty", [])


def test_a_step_id_is_unambiguous_inside_one_definition():
    with pytest.raises(SagaDefinitionError) as exc:
        SagaDefinition(
            "ambiguous",
            [
                StepDefinition("A", "op", noop, noop),
                StepDefinition("A", "other", noop, noop),
            ],
        )
    assert "declared twice" in str(exc.value)


def test_compensation_must_be_declared_before_the_step_can_run():
    """ARCHITECTURE.md §6.4: compensation is declared before execution."""
    with pytest.raises(SagaDefinitionError) as exc:
        StepDefinition("A", "op", noop, None)  # type: ignore[arg-type]
    assert "must declare a compensation" in str(exc.value)


def test_a_step_without_an_effect_declares_an_explicit_noop_compensation():
    step = StepDefinition("A", "op", noop, noop)
    assert callable(step.compensate)
    assert step.compensate(None) is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"step_id": "  "}, "step_id"),
        ({"operation": ""}, "operation"),
        ({"execute": None}, "callable execute"),
        ({"timeout_seconds": 0}, "positive timeout"),
        ({"timeout_seconds": -1.0}, "positive timeout"),
    ],
)
def test_a_step_declaration_is_validated(kwargs, message):
    params = {"step_id": "A", "operation": "op", "execute": noop, "compensate": noop}
    params.update(kwargs)
    with pytest.raises(SagaDefinitionError) as exc:
        StepDefinition(**params)  # type: ignore[arg-type]
    assert message in str(exc.value)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": -1}, "max_attempts"),
        ({"delay_seconds": -0.5}, "non-negative delay"),
    ],
)
def test_a_retry_policy_is_validated(kwargs, message):
    with pytest.raises(SagaDefinitionError) as exc:
        RetryPolicy(**kwargs)
    assert message in str(exc.value)


# ------------------------------------------------------------ state machines
def test_the_declared_state_machines_are_the_published_ones():
    assert set(SAGA_TRANSITIONS) == set(SagaState)
    assert set(STEP_TRANSITIONS) == set(StepState)
    assert TERMINAL_SAGA_STATES == frozenset(
        {SagaState.COMPLETED, SagaState.COMPENSATED, SagaState.FAILED}
    )
    for state in TERMINAL_SAGA_STATES:
        assert SAGA_TRANSITIONS[state] == frozenset()
    for state in (
        StepState.FAILED,
        StepState.COMPENSATED,
        StepState.COMPENSATION_FAILED,
    ):
        assert STEP_TRANSITIONS[state] == frozenset()


def test_an_undeclared_saga_transition_is_refused_and_writes_nothing():
    harness = saga_harness()
    harness.start(
        harness.definition("one-step", harness.reserve_step("A", "res_a1")),
        saga_id="sag_sm",
    )
    store = harness.executor.store
    with pytest.raises(IllegalStateTransition):
        store.transition_saga("sag_sm", SagaState.COMPLETED, now="t")
    with pytest.raises(IllegalStateTransition):
        store.transition_saga("sag_sm", SagaState.COMPENSATED, now="t")
    assert store.get("sag_sm").state is SagaState.STARTED


def test_an_undeclared_step_transition_is_refused_and_writes_nothing():
    harness = saga_harness()
    harness.start(
        harness.definition("one-step", harness.reserve_step("A", "res_a1")),
        saga_id="sag_sm2",
    )
    store = harness.executor.store
    with pytest.raises(IllegalStateTransition):
        store.transition_step("sag_sm2", "A", StepState.COMPLETED, now="t")
    with pytest.raises(IllegalStateTransition):
        store.transition_step("sag_sm2", "A", StepState.COMPENSATING, now="t")
    assert store.get("sag_sm2").steps["A"].state is StepState.PENDING


def test_a_terminal_workflow_cannot_be_left_with_an_unsettled_step():
    """The store refuses the write, so the invariant cannot be broken quietly."""
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_sm3")
    store = harness.executor.store

    # Force the situation the invariant forbids: a step left in progress.
    record = store.get("sag_sm3")
    store.transition_saga("sag_sm3", SagaState.RUNNING, now="t")
    store.transition_step("sag_sm3", "A", StepState.RUNNING, now="t")
    with pytest.raises(IllegalStateTransition) as exc:
        store.transition_saga("sag_sm3", SagaState.COMPLETED, now="t")
    assert "unsettled" in str(exc.value)
    assert record.state is SagaState.RUNNING  # nothing was written


# ------------------------------------------------------------- unknown things
def test_an_unknown_saga_is_never_invented():
    harness = saga_harness()
    with pytest.raises(SagaNotFound):
        harness.executor.snapshot("sag_missing")
    with pytest.raises(SagaNotFound):
        harness.executor.run("sag_missing", subject_credential=SAGA_CREDENTIAL_A)


def test_an_undeclared_step_cannot_be_delivered():
    harness = saga_harness()
    harness.start(
        harness.definition("one-step", harness.reserve_step("A", "res_a1")),
        saga_id="sag_su",
    )
    with pytest.raises(StepUnknown):
        harness.executor.deliver_step(
            "sag_su", "Z", subject_credential=SAGA_CREDENTIAL_A
        )
    # The refusal changed nothing.
    assert harness.executor.snapshot("sag_su").step("A").state is StepState.PENDING


def test_a_saga_id_is_stable_and_cannot_be_reused():
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    started = harness.start(definition, saga_id="sag_id")
    assert started.saga_id == "sag_id"
    assert harness.executor.snapshot("sag_id").saga_id == "sag_id"
    with pytest.raises(ValueError):
        harness.start(definition, saga_id="sag_id")
    generated = harness.start(definition)
    assert generated.saga_id and generated.saga_id != "sag_id"
    assert harness.executor.snapshot(generated.saga_id).saga_id == generated.saga_id


# ------------------------------------------------------------------ terminals
def test_the_workflow_store_is_bounded():
    harness = saga_harness()
    harness.executor.store = SagaStore(max_sagas=2)
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_b1")
    harness.start(definition, saga_id="sag_b2")
    with pytest.raises(SagaStoreExhausted):
        harness.start(definition, saga_id="sag_b3")
    assert harness.executor.store.saga_ids() == ("sag_b1", "sag_b2")


@pytest.mark.parametrize(
    "scenario",
    [
        "all_steps_succeed",
        "last_step_fails",
        "first_step_fails",
        "compensation_fails",
        "unclassified_error",
        "timeout",
        "refused_step",
    ],
)
def test_every_driven_workflow_reaches_a_defined_terminal_state(scenario):
    harness = saga_harness()

    def step(step_id, reservation_id, **kwargs):
        return harness.reserve_step(step_id, reservation_id, **kwargs)

    if scenario == "all_steps_succeed":
        definition = harness.definition("s", step("A", "res_a1"), step("B", "res_a2"))
    elif scenario == "last_step_fails":
        definition = harness.definition(
            "s",
            step("A", "res_a1"),
            step("B", "res_a2", fail_before=StepFailure("nope")),
        )
    elif scenario == "first_step_fails":
        definition = harness.definition(
            "s",
            step("A", "res_a1", fail_before=StepFailure("nope")),
            step("B", "res_a2"),
        )
    elif scenario == "compensation_fails":
        definition = harness.definition(
            "s",
            step("A", "res_a1", undo_fails=StepFailure("undo_failed")),
            step("B", "res_a2", fail_before=StepFailure("nope")),
        )
    elif scenario == "unclassified_error":
        definition = harness.definition(
            "s", step("A", "res_a1", fail_before=RuntimeError("unexpected"))
        )
    elif scenario == "timeout":
        harness.executor._clock = _clock_jumping_past(10.0)
        definition = harness.definition(
            "s", step("A", "res_a1", timeout_seconds=1.0), step("B", "res_a2")
        )
    else:  # a business refusal: security-sensitive and not retryable
        definition = harness.definition("s", step("A", "res_b1"))

    harness.start(definition, saga_id="sag_t")
    final = harness.executor.run("sag_t", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state in TERMINAL_SAGA_STATES
    assert final.terminal is True
    # No step is left in progress, whatever happened.
    assert not any(step.state in UNSETTLED_STEP_STATES for step in final.steps)
    # ...and the terminal transition was audited exactly once.
    terminals = [
        event for event in harness.events("sag_t") if event.action == "saga.terminal"
    ]
    assert len(terminals) == 1
    assert terminals[0].state == final.state.value


def test_retrying_is_a_step_state_and_not_a_global_saga_state():
    seen: list[tuple[SagaState, StepState]] = []

    def observe(delay: float) -> None:
        # Called while the step is declared RETRYING, before the next attempt.
        snapshot = harness.executor.snapshot("sag_r")
        seen.append((snapshot.state, snapshot.step("A").state))

    harness = saga_harness(sleep=observe)

    def unavailable(ctx):
        if ctx.attempt == 1:
            return StepFailure("dependency_unavailable", retryable=True)
        return None

    definition = harness.definition(
        "retrying",
        harness.reserve_step(
            "A",
            "res_a1",
            retry=RetryPolicy(max_attempts=2, delay_seconds=0.01),
            fail_before=unavailable,
        ),
    )
    harness.start(definition, saga_id="sag_r")
    final = harness.executor.run("sag_r", subject_credential=SAGA_CREDENTIAL_A)

    assert seen == [(SagaState.RUNNING, StepState.RETRYING)]
    assert final.state is SagaState.COMPLETED
    assert "RETRYING" not in {state.value for state in SagaState}


def test_a_timeout_is_a_failure_and_never_a_silent_success():
    harness = saga_harness()
    harness.executor._clock = _clock_jumping_past(30.0)
    definition = harness.definition(
        "slow-step",
        harness.reserve_step("A", "res_a1", timeout_seconds=1.0),
    )
    harness.start(definition, saga_id="sag_to")
    final = harness.executor.run("sag_to", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.FAILED
    assert final.step("A").state is StepState.FAILED
    assert final.failure is not None
    assert final.failure.kind == "timeout"
    assert final.failure.reason == "step_timeout"
    assert final.failure.retryable is False
    # The effect did happen, which is exactly why the failure must be visible
    # and the recovery path must run instead of reporting success.
    assert harness.reservations.applied_reserves == 1
    assert any(
        event.action == "saga.step.failed" and event.reason == "step_timeout"
        for event in harness.events("sag_to")
    )


def test_a_timeout_after_an_applied_effect_is_compensated():
    harness = saga_harness()
    # The clock jumps 30 units between two reads, so only the step with the
    # tight deadline overruns: A tolerates it, B does not.
    harness.executor._clock = _clock_jumping_past(30.0)
    definition = harness.definition(
        "slow-second-step",
        harness.reserve_step("A", "res_a1", timeout_seconds=1000.0),
        harness.reserve_step("B", "res_a2", timeout_seconds=1.0),
    )
    harness.start(definition, saga_id="sag_to2")
    final = harness.executor.run("sag_to2", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.COMPENSATED
    assert final.step("A").state is StepState.COMPENSATED
    assert final.step("B").state is StepState.FAILED
    assert final.failure.kind == "timeout"
    assert harness.reservations.states["res_a1"] == "available"


def _clock_jumping_past(seconds: float):
    """A clock that reports a jump of ``seconds`` between two reads."""
    reads = {"count": 0}

    def clock() -> float:
        reads["count"] += 1
        return float(reads["count"]) * seconds

    return clock
