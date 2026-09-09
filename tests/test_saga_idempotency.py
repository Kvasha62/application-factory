"""IS-006 over the IS-005 command-safety boundary (Issue #18, "IS-005 integration").

Every executable step — and every compensation — is delivered through the
existing IS-005 boundary, and IS-006 implements no second idempotency mechanism
of its own. These tests assert that literally: which object executes the
effects, under which command identity, what IS-005 records, and what happens
when the same command identity is delivered with another context or payload.
"""

from __future__ import annotations

import pytest

from idempotency.guard import IdempotencyGuard
from saga.errors import StepFailure
from saga.models import SagaState, StepState, step_idempotency_key
from saga.store import SagaStore
from tests.conftest import SAGA_CREDENTIAL_A, SAGA_CREDENTIAL_C, saga_harness


def test_the_executor_delivers_through_the_is_005_guard_it_was_given():
    guard = IdempotencyGuard(audit_sink=lambda action, details: None)
    calls: list[str] = []
    original = guard.execute

    def observing(key, **kwargs):
        calls.append(key)
        return original(key, **kwargs)

    guard.execute = observing  # type: ignore[method-assign]
    harness = saga_harness(idempotency=guard)
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
    )
    harness.start(definition, saga_id="sag_i1")
    harness.executor.run("sag_i1", subject_credential=SAGA_CREDENTIAL_A)

    # The command identity of a step is the step, not the attempt.
    assert calls == [
        step_idempotency_key("sag_i1", "A", "execute"),
        step_idempotency_key("sag_i1", "B", "execute"),
    ]
    assert harness.guard is guard
    assert harness.reservations.applied_reserves == 2


def test_the_default_command_safety_boundary_is_the_is_005_guard():
    harness = saga_harness()
    assert isinstance(harness.executor.idempotency, IdempotencyGuard)


def test_is_005_records_one_result_per_executed_step_and_per_compensation():
    harness = saga_harness()
    definition = harness.definition(
        "compensated",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2", fail_before=StepFailure("nope")),
    )
    harness.start(definition, saga_id="sag_i2")
    harness.executor.run("sag_i2", subject_credential=SAGA_CREDENTIAL_A)

    # White-box on purpose: this asserts where the deduplication state lives —
    # in the IS-005 owned table, and not in a table of this component.
    records = harness.guard._store
    assert set(records) == {
        step_idempotency_key("sag_i2", "A", "execute"),
        step_idempotency_key("sag_i2", "A", "compensate"),
    }
    assert records[step_idempotency_key("sag_i2", "A", "execute")].tenant_id == "ten_a"
    assert (
        records[step_idempotency_key("sag_i2", "A", "execute")].identity
        == "idn_human_a"
    )
    assert records[step_idempotency_key("sag_i2", "A", "execute")].operation == (
        "reservations.reserve"
    )


def test_a_failed_attempt_records_nothing_so_a_retry_can_execute():
    harness = saga_harness()

    def unavailable(ctx):
        return (
            StepFailure("dependency_unavailable", retryable=True)
            if ctx.attempt == 1
            else None
        )

    from saga.models import RetryPolicy

    definition = harness.definition(
        "retry",
        harness.reserve_step(
            "A", "res_a1", retry=RetryPolicy(max_attempts=2), fail_before=unavailable
        ),
    )
    harness.start(definition, saga_id="sag_i3")
    final = harness.executor.run("sag_i3", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.COMPLETED
    # One record for two attempts: IS-005 records successful effects only.
    assert list(harness.guard._store) == [
        step_idempotency_key("sag_i3", "A", "execute")
    ]
    assert harness.reservations.applied_reserves == 1


def test_a_replayed_step_returns_the_recorded_result_and_applies_nothing():
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_i4")
    harness.executor.deliver_step("sag_i4", "A", subject_credential=SAGA_CREDENTIAL_A)
    assert harness.reservations.applied_reserves == 1

    for _ in range(3):
        snapshot = harness.executor.deliver_step(
            "sag_i4", "A", subject_credential=SAGA_CREDENTIAL_A
        )
        assert snapshot.step("A").state is StepState.COMPLETED
    assert harness.reservations.applied_reserves == 1
    assert harness.actions("sag_i4").count("idempotency_replay") == 3


def test_the_same_step_identity_with_another_payload_is_a_conflict():
    """IS-005 invariant I-003, reached through IS-006: no second effect, no silence."""
    first = saga_harness()
    definition = first.definition(
        "shared-guard",
        first.reserve_step("A", "res_a1", payload={"amount": 10}),
    )
    first.start(definition, saga_id="sag_shared")
    first.executor.run("sag_shared", subject_credential=SAGA_CREDENTIAL_A)

    # A second executor over the *same* command-safety boundary, delivering the
    # same step identity with another payload.
    second = saga_harness(idempotency=first.guard)
    other = second.definition(
        "shared-guard",
        second.reserve_step("A", "res_a1", payload={"amount": 99}),
    )
    second.start(other, saga_id="sag_shared")
    final = second.executor.run("sag_shared", subject_credential=SAGA_CREDENTIAL_A)

    # A conflicting delivery is a hard failure of that step, never a second
    # effect and never a silent success; the workflow still reaches a terminal
    # state instead of being left half-delivered.
    assert final.step("A").state is StepState.FAILED
    assert final.failure is not None
    assert final.failure.reason == "idempotency_conflict"
    assert final.failure.security_sensitive is True
    assert final.state is SagaState.FAILED
    # The conflicting delivery applied nothing anywhere.
    assert first.reservations.applied_reserves == 1
    assert second.reservations.applied_reserves == 0
    conflicts = [
        event
        for event in second.executor.store.audit
        if event.action == "saga.step.conflict"
    ]
    assert conflicts and conflicts[0].security_sensitive is True
    assert conflicts[0].step_id == "A"


def test_the_same_step_identity_from_another_identity_is_a_conflict():
    """IS-005 invariant I-004: a command identity belongs to one identity."""
    first = saga_harness()
    definition = first.definition("shared-guard", first.reserve_step("A", "res_a1"))
    first.start(definition, saga_id="sag_shared2")
    first.executor.run("sag_shared2", subject_credential=SAGA_CREDENTIAL_A)

    second = saga_harness(idempotency=first.guard)
    other = second.definition("shared-guard", second.reserve_step("A", "res_a1"))
    second.start(other, saga_id="sag_shared2", credential=SAGA_CREDENTIAL_C)
    final = second.executor.run("sag_shared2", subject_credential=SAGA_CREDENTIAL_C)
    assert final.failure is not None
    assert final.failure.reason == "idempotency_conflict"
    assert final.failure.security_sensitive is True
    assert second.reservations.applied_reserves == 0
    assert first.reservations.applied_reserves == 1


def test_an_interrupted_compensation_phase_resumes_without_a_second_undo():
    """A crash between two undos leaves COMPENSATING; resuming finishes the recovery."""
    harness = saga_harness()
    interrupted = {"count": 0}

    def crash_once(ctx):
        # A BaseException escapes the executor exactly like a dying process: the
        # workflow is left mid-recovery, with step A already undone.
        if interrupted["count"] == 0:
            interrupted["count"] += 1
            raise KeyboardInterrupt("process died during compensation")

    definition = harness.definition(
        "interrupted-recovery",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2", undo_fails=crash_once),
        harness.reserve_step("C", "res_a3", fail_before=StepFailure("nope")),
    )
    harness.start(definition, saga_id="sag_i5")
    with pytest.raises(KeyboardInterrupt):
        harness.executor.run("sag_i5", subject_credential=SAGA_CREDENTIAL_A)

    broken = harness.executor.snapshot("sag_i5")
    assert broken.state is SagaState.COMPENSATING
    assert broken.step("B").state is StepState.COMPENSATING
    assert broken.step("A").state is StepState.COMPLETED

    resumed = harness.executor.run("sag_i5", subject_credential=SAGA_CREDENTIAL_A)
    assert resumed.state is SagaState.COMPENSATED
    assert resumed.step("A").state is StepState.COMPENSATED
    assert resumed.step("B").state is StepState.COMPENSATED
    # Each undo happened exactly once: no duplicate release on resume.
    assert harness.reservations.applied_releases == 2
    assert harness.reservations.states["res_a1"] == "available"
    assert harness.reservations.states["res_a2"] == "available"


def test_an_undo_that_already_happened_is_not_applied_again_on_resume():
    harness = saga_harness()
    state = {"crashed": False}

    def crash_once(ctx):
        if not state["crashed"]:
            state["crashed"] = True
            raise KeyboardInterrupt("process died during compensation")

    definition = harness.definition(
        "interrupted-recovery-2",
        harness.reserve_step("A", "res_a1", undo_fails=crash_once),
        harness.reserve_step("B", "res_a2"),
        harness.reserve_step("C", "res_a3", fail_before=StepFailure("nope")),
    )
    harness.start(definition, saga_id="sag_i6")
    with pytest.raises(KeyboardInterrupt):
        harness.executor.run("sag_i6", subject_credential=SAGA_CREDENTIAL_A)

    broken = harness.executor.snapshot("sag_i6")
    # B was undone before the crash; A was not.
    assert broken.step("B").state is StepState.COMPENSATED
    assert broken.step("A").state is StepState.COMPENSATING
    assert harness.reservations.applied_releases == 1

    resumed = harness.executor.run("sag_i6", subject_credential=SAGA_CREDENTIAL_A)
    assert resumed.state is SagaState.COMPENSATED
    assert harness.reservations.applied_releases == 2
    assert harness.reservations.states["res_a2"] == "available"
    assert harness.reservations.states["res_a1"] == "available"


def test_the_component_owns_no_second_idempotency_mechanism():
    """The saga store holds workflow state and an audit journal — no key table."""
    store = SagaStore()
    assert set(vars(store)) == {"max_sagas", "sagas", "audit", "_lock"}

    harness = saga_harness()
    definition = harness.definition(
        "two-steps",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
    )
    harness.start(definition, saga_id="sag_i7")
    harness.executor.run("sag_i7", subject_credential=SAGA_CREDENTIAL_A)

    record = harness.executor.store.get("sag_i7")
    assert set(vars(record)) == {
        "context",
        "definition",
        "state",
        "steps",
        "failure",
        "compensation_failures",
        "updated_at",
    }
    for step in record.steps.values():
        assert set(vars(step)) == {
            "step_id",
            "operation",
            "state",
            "attempts",
            "effect_recorded",
            "failure",
        }
    # All deduplication state lives in the IS-005 guard the executor was given.
    assert len(harness.guard._store) == 2
