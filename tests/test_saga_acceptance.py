"""IS-006 acceptance scenarios A–H (Issue #18), proven against composed components.

Every scenario runs the real executor over the real composition: the tenant
context comes from IS-001, the business effect goes through the published
contract of a data owner that asks IS-003, and every step effect and every
compensation is delivered through the IS-005 command-safety boundary. "No
business effect" is always a measured counter of the data owner, never an
assumption about the workflow state.
"""

from __future__ import annotations

import threading

import pytest

from records_service.deployment import build_deployment as build_records
from saga.errors import (
    SagaContextViolation,
    SagaRefused,
    SagaTerminalState,
    StepFailure,
)
from saga.models import RetryPolicy, SagaState, StepState
from tests.conftest import (
    SAGA_CREDENTIAL_A,
    SAGA_CREDENTIAL_B,
    SAGA_CREDENTIAL_C,
    SAGA_CREDENTIAL_SERVICE,
    saga_harness,
)


# ----------------------------------------------------------------- A: success
def test_scenario_a_all_steps_succeed_and_the_saga_completes():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
        harness.reserve_step("C", "res_a3"),
    )
    started = harness.start(
        definition,
        saga_id="sag_a",
        request_id="req-a1",
        correlation_id="cor-a",
        trace_id="tr-a",
    )
    assert started.state is SagaState.STARTED
    assert [step.state for step in started.steps] == [StepState.PENDING] * 3

    final = harness.executor.run(
        "sag_a",
        subject_credential=SAGA_CREDENTIAL_A,
        request_id="req-a2",
        correlation_id="cor-a",
        trace_id="tr-a",
    )

    assert final.state is SagaState.COMPLETED
    assert final.terminal is True
    assert [step.state for step in final.steps] == [StepState.COMPLETED] * 3
    assert all(step.effect_recorded for step in final.steps)
    # Three business effects, applied in order, and nothing undone.
    assert harness.reservations.applied_reserves == 3
    assert harness.reservations.applied_releases == 0
    assert harness.executor.snapshot("sag_a").state is SagaState.COMPLETED


# ------------------------------------------------- B: failure with compensation
def test_scenario_b_failing_step_is_compensated_in_reverse_order():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
        harness.reserve_step(
            "C", "res_a3", fail_before=StepFailure("payment_declined")
        ),
    )
    harness.start(definition, saga_id="sag_b")
    final = harness.executor.run("sag_b", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.COMPENSATED
    assert final.step("A").state is StepState.COMPENSATED
    assert final.step("B").state is StepState.COMPENSATED
    assert final.step("C").state is StepState.FAILED
    assert final.failure is not None
    assert final.failure.reason == "payment_declined"
    # Both applied effects were undone by real business operations.
    assert harness.reservations.applied_reserves == 2
    assert harness.reservations.applied_releases == 2
    assert harness.reservations.states["res_a1"] == "available"
    assert harness.reservations.states["res_a2"] == "available"
    # Compensation ran in reverse declaration order: B before A.
    compensated = [
        event.step_id
        for event in harness.events("sag_b")
        if event.action == "saga.step.compensating"
    ]
    assert compensated == ["B", "A"]


# ------------------------------------------------- C: repeated step delivery
def test_scenario_c_repeated_delivery_applies_the_effect_once():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
    )
    harness.start(definition, saga_id="sag_c")

    first = harness.executor.deliver_step(
        "sag_c", "A", subject_credential=SAGA_CREDENTIAL_A
    )
    second = harness.executor.deliver_step(
        "sag_c", "A", subject_credential=SAGA_CREDENTIAL_A
    )
    third = harness.executor.deliver_step(
        "sag_c", "A", subject_credential=SAGA_CREDENTIAL_A
    )

    assert harness.reservations.applied_reserves == 1
    assert harness.reservations.applied_releases == 0
    for snapshot in (first, second, third):
        assert snapshot.step("A").state is StepState.COMPLETED
        assert snapshot.step("A").attempts == 1
        assert snapshot.state is SagaState.RUNNING
    # The determinism comes from IS-005, and the journal shows it: the second
    # and third delivery are replays at the command-safety boundary.
    assert harness.actions("sag_c").count("idempotency_replay") == 2
    assert harness.actions("sag_c").count("saga.step.redelivered") == 2
    # Replaying the whole workflow is safe for the same reason.
    final = harness.executor.run("sag_c", subject_credential=SAGA_CREDENTIAL_A)
    assert final.state is SagaState.COMPLETED
    assert harness.reservations.applied_reserves == 2  # step B, applied once


def test_scenario_c_concurrent_delivery_of_one_step_applies_one_effect():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order", harness.reserve_step("A", "res_a1")
    )
    harness.start(definition, saga_id="sag_cc")

    outcomes: list[object] = [None, None, None, None]

    def deliver(index: int) -> None:
        try:
            outcomes[index] = harness.executor.deliver_step(
                "sag_cc", "A", subject_credential=SAGA_CREDENTIAL_A
            )
        except Exception as exc:
            outcomes[index] = exc

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert harness.reservations.applied_reserves == 1
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert not failures, failures
    assert all(outcome.step("A").state is StepState.COMPLETED for outcome in outcomes)
    # Every delivery got a defined answer: one execution, the rest replays.
    assert harness.actions("sag_cc").count("saga.step.completed") == 1


# ------------------------------------------------- D: compensation failure
def test_scenario_d_failed_compensation_ends_in_terminal_failed_with_evidence():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step(
            "B", "res_a2", undo_fails=StepFailure("undo_refused_by_owner")
        ),
        harness.reserve_step("C", "res_a3", fail_before=StepFailure("boom")),
    )
    harness.start(definition, saga_id="sag_d")
    final = harness.executor.run("sag_d", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.FAILED
    assert final.terminal is True
    assert final.step("A").state is StepState.COMPENSATED
    assert final.step("B").state is StepState.COMPENSATION_FAILED
    assert final.step("C").state is StepState.FAILED
    # The failure information is preserved, not swallowed.
    assert final.failure is not None and final.failure.reason == "boom"
    assert [item.reason for item in final.compensation_failures] == [
        "undo_refused_by_owner"
    ]
    assert final.step("B").failure is not None
    assert final.step("B").failure.kind == "compensation_failed"
    # The undo that failed really did not happen: the reservation is still held.
    assert harness.reservations.states["res_a2"] == "reserved"
    assert harness.reservations.states["res_a1"] == "available"
    # ...and the terminal failure is observable.
    terminal = [
        event for event in harness.events("sag_d") if event.action == "saga.terminal"
    ]
    assert len(terminal) == 1
    assert terminal[0].state == "FAILED"
    assert "B:undo_refused_by_owner" in (terminal[0].reason or "")


# ------------------------------------------------------- E: tenant mismatch
def test_scenario_e_step_for_another_tenant_is_denied_without_effect():
    harness = saga_harness()
    definition = harness.definition(
        "cross-tenant-attempt",
        harness.reserve_step("A", "res_a1"),
        # res_b1 belongs to Tenant B; the workflow's tenant is Tenant A.
        harness.reserve_step("X", "res_b1"),
    )
    harness.start(definition, saga_id="sag_e")
    final = harness.executor.run("sag_e", subject_credential=SAGA_CREDENTIAL_A)

    assert final.step("X").state is StepState.FAILED
    assert final.failure is not None
    assert final.failure.reason == "resource_tenant_mismatch"
    assert final.failure.security_sensitive is True
    # No business effect on the foreign resource — measured at the data owner.
    assert harness.reservations.states["res_b1"] == "available"
    assert harness.reservations.applied_reserves == 1  # step A only
    # The security-relevant refusal is audited as such.
    refusals = [
        event
        for event in harness.events("sag_e")
        if event.action == "saga.step.failed" and event.step_id == "X"
    ]
    assert refusals and refusals[0].security_sensitive is True
    assert refusals[0].reason == "resource_tenant_mismatch"
    assert refusals[0].tenant_id == "ten_a"


def test_scenario_e_a_workflow_cannot_claim_another_tenant_at_start():
    """The tenant of a workflow comes from the verified identity, not the caller."""
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))

    with pytest.raises(SagaRefused) as exc:
        harness.start(definition, saga_id="sag_e2", claimed_tenant_id="ten_b")
    assert exc.value.reason == "tenant_mismatch"
    # Nothing was created and no effect was applied.
    assert "sag_e2" not in harness.executor.store.sagas
    assert harness.reservations.applied_effects == 0
    refused = [
        event
        for event in harness.executor.store.audit
        if event.action == "saga.start_refused"
    ]
    assert refused and refused[0].saga_id is None
    assert refused[0].reason == "tenant_mismatch"
    assert refused[0].security_sensitive is True


def test_scenario_e_a_delivery_from_another_tenant_is_refused_by_is_001():
    """The tenant of a workflow is re-verified on every delivery, by IS-001."""
    harness = saga_harness()
    definition = harness.definition(
        "cross-tenant-attempt", harness.reserve_step("A", "res_a1")
    )
    harness.start(definition, saga_id="sag_e3")

    with pytest.raises(SagaContextViolation) as exc:
        harness.executor.run("sag_e3", subject_credential=SAGA_CREDENTIAL_B)
    # IS-001 refuses the claim itself: the workflow tenant is a cross-check.
    assert exc.value.reason == "tenant_mismatch"
    assert harness.reservations.applied_effects == 0
    assert harness.executor.snapshot("sag_e3").state is SagaState.STARTED
    assert [step.state for step in harness.executor.snapshot("sag_e3").steps] == [
        StepState.PENDING
    ]
    refusals = [
        event
        for event in harness.events("sag_e3")
        if event.action == "saga.delivery_refused"
    ]
    assert refusals and refusals[0].reason == "tenant_mismatch"
    assert refusals[0].security_sensitive is True


def test_scenario_e_a_delivery_from_another_identity_is_refused_before_any_step():
    """`token-human-c` acts in the same tenant but is not the identity of this workflow."""
    harness = saga_harness()
    definition = harness.definition(
        "cross-tenant-attempt", harness.reserve_step("A", "res_a1")
    )
    harness.start(definition, saga_id="sag_e4")

    with pytest.raises(SagaContextViolation) as exc:
        harness.executor.run("sag_e4", subject_credential=SAGA_CREDENTIAL_C)
    assert exc.value.reason == "identity_mismatch"
    assert harness.reservations.applied_effects == 0
    assert harness.executor.snapshot("sag_e4").state is SagaState.STARTED


# --------------------------------------------------- F: authorization denial
def test_scenario_f_denied_step_never_executes_the_business_operation():
    """`token-service` is verified but holds no grant for the reservation operations."""
    harness = saga_harness()
    definition = harness.definition(
        "unauthorized-step", harness.reserve_step("A", "res_a1")
    )
    harness.start(definition, saga_id="sag_f", credential=SAGA_CREDENTIAL_SERVICE)
    final = harness.executor.run("sag_f", subject_credential=SAGA_CREDENTIAL_SERVICE)

    assert final.step("A").state is StepState.FAILED
    assert final.failure is not None
    assert final.failure.reason == "permission_not_granted"
    assert final.failure.security_sensitive is True
    # The denied operation was not executed: the owner applied nothing.
    assert harness.reservations.applied_effects == 0
    assert harness.reservations.states["res_a1"] == "available"
    # Nothing was applied, so the workflow ends FAILED without a compensation phase.
    assert final.state is SagaState.FAILED
    assert "saga.step.compensating" not in harness.actions("sag_f")


def test_scenario_f_denial_at_the_real_records_owner_is_denied_too():
    """The same guarantee against the IS-004 data owner, over its published contract."""
    harness = saga_harness()
    definition = harness.definition(
        "records-transition",
        harness.records_step("A", "rec_a2", transition="activate"),
    )
    harness.start(definition, saga_id="sag_f2", credential=SAGA_CREDENTIAL_SERVICE)
    final = harness.executor.run("sag_f2", subject_credential=SAGA_CREDENTIAL_SERVICE)

    assert final.step("A").state is StepState.FAILED
    assert final.failure.reason == "permission_not_granted"
    assert harness.records_store.applied_transitions == 0


# ---------------------------------------------------- G: dependency failure
def test_scenario_g_dependency_failure_is_retried_and_never_reported_as_success():
    sleeps: list[float] = []
    harness = saga_harness(sleep=sleeps.append)  # deterministic schedule, no waiting

    def unavailable(ctx):
        return (
            StepFailure("dependency_unavailable", retryable=True)
            if ctx.attempt < 3
            else None
        )

    definition = harness.definition(
        "flaky-dependency",
        harness.reserve_step(
            "A",
            "res_a1",
            retry=RetryPolicy(max_attempts=3, delay_seconds=0.25),
            fail_before=unavailable,
        ),
    )
    harness.start(definition, saga_id="sag_g")
    final = harness.executor.run("sag_g", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.COMPLETED
    assert final.step("A").attempts == 3
    assert harness.reservations.applied_reserves == 1
    assert sleeps == [0.25, 0.25]
    actions = harness.actions("sag_g")
    assert actions.count("saga.step.retrying") == 2
    assert actions.count("saga.step.completed") == 1


def test_scenario_g_exhausted_retries_follow_the_compensation_path():
    harness = saga_harness()
    definition = harness.definition(
        "dead-dependency",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step(
            "B",
            "res_a2",
            retry=RetryPolicy(max_attempts=2),
            fail_before=StepFailure("dependency_unavailable", retryable=True),
        ),
    )
    harness.start(definition, saga_id="sag_g2")
    final = harness.executor.run("sag_g2", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.COMPENSATED
    assert final.step("B").attempts == 2
    assert final.step("B").state is StepState.FAILED
    assert final.failure.reason == "dependency_unavailable"
    assert final.failure.retryable is True
    # The step that had applied an effect was undone.
    assert harness.reservations.applied_releases == 1
    assert harness.reservations.states["res_a1"] == "available"


def test_scenario_g_a_failing_records_dependency_is_not_a_success():
    """IS-004 fails closed when its decision dependency does not answer; so does the saga."""
    harness = saga_harness()

    class BrokenDecisionPort:
        """A decision port that cannot answer: the dependency is unavailable."""

        def decide(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("authorization dependency is down")

    broken_records = build_records(
        {
            "platform_id": harness.instance.authority.current_platform_id,
            "environment": harness.instance.authority.config.environment,
        },
        authorization=BrokenDecisionPort(),
        seed_demo=True,
        with_http=True,
    )
    harness.records_client = broken_records.publish()

    definition = harness.definition("records-read", harness.records_step("A", "rec_a1"))
    harness.start(definition, saga_id="sag_g3")
    final = harness.executor.run("sag_g3", subject_credential=SAGA_CREDENTIAL_A)

    # Fail closed at the data owner -> a refusal, never a served resource.
    assert final.step("A").state is StepState.FAILED
    assert final.failure.reason == "authorization_unavailable"
    assert final.state is SagaState.FAILED


# ------------------------------------------------------ H: interruption/retry
def test_scenario_h_interrupted_workflow_resumes_without_a_duplicate_effect():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
    )
    harness.start(definition, saga_id="sag_h")
    # The driver is interrupted after the first step: the workflow is left
    # RUNNING with one recorded effect and no terminal state.
    interrupted = harness.executor.deliver_step(
        "sag_h", "A", subject_credential=SAGA_CREDENTIAL_A
    )
    assert interrupted.state is SagaState.RUNNING
    assert interrupted.step("A").state is StepState.COMPLETED
    assert harness.reservations.applied_reserves == 1

    resumed = harness.executor.run("sag_h", subject_credential=SAGA_CREDENTIAL_A)

    assert resumed.state is SagaState.COMPLETED
    assert resumed.step("A").attempts == 1  # not executed a second time
    assert resumed.step("B").attempts == 1
    assert harness.reservations.applied_reserves == 2  # A once, B once
    assert "saga.step.skipped" in harness.actions("sag_h")


def test_scenario_h_step_interrupted_before_its_effect_is_applied_once_on_retry():
    harness = saga_harness()
    interrupted = lambda ctx: (
        StepFailure("dependency_unavailable", retryable=True)
        if ctx.attempt == 1
        else None
    )
    definition = harness.definition(
        "interrupted-step",
        harness.reserve_step(
            "A", "res_a1", retry=RetryPolicy(max_attempts=2), fail_before=interrupted
        ),
    )
    harness.start(definition, saga_id="sag_h2")
    final = harness.executor.run("sag_h2", subject_credential=SAGA_CREDENTIAL_A)

    assert final.state is SagaState.COMPLETED
    assert harness.reservations.applied_reserves == 1
    assert harness.reservations.applied_releases == 0


def test_scenario_h_a_terminal_workflow_accepts_no_further_delivery():
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_h3")
    harness.executor.run("sag_h3", subject_credential=SAGA_CREDENTIAL_A)

    with pytest.raises(SagaTerminalState):
        harness.executor.run("sag_h3", subject_credential=SAGA_CREDENTIAL_A)
    with pytest.raises(SagaTerminalState):
        harness.executor.deliver_step(
            "sag_h3", "A", subject_credential=SAGA_CREDENTIAL_A
        )
    assert harness.reservations.applied_reserves == 1
    refusals = [
        event
        for event in harness.events("sag_h3")
        if event.action == "saga.delivery_refused"
    ]
    assert len(refusals) == 2
    assert all(event.reason == "terminal_state" for event in refusals)
    assert all(event.security_sensitive for event in refusals)
