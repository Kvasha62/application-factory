"""IS-006 observability and audit (Issue #18, "Observability / audit").

Every saga and step transition must retain enough context to be investigated
after the fact: ``saga_id``, ``tenant_id``, ``request_id``, ``correlation_id``,
``trace_id`` and the acting service/subject identity. Security-sensitive
refusals and terminal failure/recovery transitions must be observable, and a
credential must never be part of what is stored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saga.errors import SagaContextViolation, SagaRefused, StepFailure
from saga.executor import (
    ACTION_STEP_COMPENSATED,
    ACTION_STEP_COMPENSATION_FAILED,
    ACTION_STEP_FAILED,
    ACTION_STEP_RETRYING,
    ACTION_TERMINAL,
    ACTION_TRANSITION,
)
from saga.models import (
    COMPONENT_ID,
    COMPONENT_VERSION,
    RetryPolicy,
    SagaState,
    StepState,
)
from tests.conftest import SAGA_CREDENTIAL_A, SAGA_CREDENTIAL_B, saga_harness

REQUIRED_CONTEXT = (
    "saga_id",
    "tenant_id",
    "request_id",
    "correlation_id",
    "trace_id",
    "identity_id",
    "actor_service_id",
)


def test_every_workflow_event_carries_the_required_correlation_context():
    harness = saga_harness(actor_service_id="svc-saga-proof")
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
    )
    harness.start(
        definition,
        saga_id="sag_obs",
        request_id="req-start",
        correlation_id="cor-obs",
        trace_id="tr-obs",
    )
    harness.executor.run(
        "sag_obs",
        subject_credential=SAGA_CREDENTIAL_A,
        request_id="req-run",
        correlation_id="cor-obs",
        trace_id="tr-obs",
    )

    events = harness.events("sag_obs")
    assert events, "the workflow wrote no audit trail"
    for event in events:
        assert event.saga_id == "sag_obs"
        assert event.tenant_id == "ten_a"
        assert event.correlation_id == "cor-obs"
        assert event.trace_id == "tr-obs"
        assert event.identity_id == "idn_human_a"
        assert event.actor_service_id == "svc-saga-proof"
        assert event.platform_id == "plt_demo"
        assert event.component_id == COMPONENT_ID
        assert event.component_version == COMPONENT_VERSION
        assert event.timestamp
        assert event.action
    # The delivery's own request id is kept; the one from start is the fallback.
    assert {event.request_id for event in events} == {"req-start", "req-run"}
    started = [event for event in events if event.action == "saga.created"]
    assert started[0].request_id == "req-start"


def test_step_events_name_the_step_and_its_state():
    harness = saga_harness()
    definition = harness.definition(
        "provision-order",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2", fail_before=StepFailure("nope")),
    )
    harness.start(definition, saga_id="sag_steps", correlation_id="cor-steps")
    harness.executor.run("sag_steps", subject_credential=SAGA_CREDENTIAL_A)

    step_events = [event for event in harness.events("sag_steps") if event.step_id]
    assert {event.step_id for event in step_events} == {"A", "B"}
    for event in step_events:
        assert event.state  # every step event states the step state it refers to
    completed = [
        event for event in step_events if event.action == "saga.step.completed"
    ]
    assert [event.step_id for event in completed] == ["A"]
    assert completed[0].state == StepState.COMPLETED.value


def test_the_terminal_transition_is_observable_with_its_reason():
    harness = saga_harness()
    definition = harness.definition(
        "compensated",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2", fail_before=StepFailure("nope")),
    )
    harness.start(definition, saga_id="sag_term", correlation_id="cor-term")
    harness.executor.run("sag_term", subject_credential=SAGA_CREDENTIAL_A)

    transitions = [
        event
        for event in harness.events("sag_term")
        if event.action == ACTION_TRANSITION
    ]
    assert [event.state for event in transitions] == [
        SagaState.RUNNING.value,
        SagaState.COMPENSATING.value,
        SagaState.COMPENSATED.value,
    ]
    terminal = [
        event for event in harness.events("sag_term") if event.action == ACTION_TERMINAL
    ]
    assert len(terminal) == 1
    assert terminal[0].state == SagaState.COMPENSATED.value
    assert terminal[0].reason == "nope"
    # The recovery itself is observable step by step.
    assert [
        event.step_id
        for event in harness.events("sag_term")
        if event.action == ACTION_STEP_COMPENSATED
    ] == ["A"]


def test_a_failed_recovery_is_observable_with_its_evidence():
    harness = saga_harness()
    definition = harness.definition(
        "broken-recovery",
        harness.reserve_step("A", "res_a1", undo_fails=StepFailure("undo_refused")),
        harness.reserve_step("B", "res_a2", fail_before=StepFailure("boom")),
    )
    harness.start(definition, saga_id="sag_broken")
    final = harness.executor.run("sag_broken", subject_credential=SAGA_CREDENTIAL_A)

    failures = [
        event
        for event in harness.events("sag_broken")
        if event.action == ACTION_STEP_COMPENSATION_FAILED
    ]
    assert [event.step_id for event in failures] == ["A"]
    assert failures[0].reason == "undo_refused"
    assert failures[0].state == StepState.COMPENSATION_FAILED.value
    assert final.compensation_failures and final.compensation_failures[0].step_id == "A"
    terminal = [
        event
        for event in harness.events("sag_broken")
        if event.action == ACTION_TERMINAL
    ]
    assert terminal[0].state == SagaState.FAILED.value
    assert "A:undo_refused" in terminal[0].reason


def test_retries_are_observable_and_distinguishable_from_success():
    harness = saga_harness()

    def unavailable(ctx):
        return (
            StepFailure("dependency_unavailable", retryable=True)
            if ctx.attempt < 3
            else None
        )

    definition = harness.definition(
        "retry-observability",
        harness.reserve_step(
            "A", "res_a1", retry=RetryPolicy(max_attempts=3), fail_before=unavailable
        ),
    )
    harness.start(definition, saga_id="sag_retry")
    final = harness.executor.run("sag_retry", subject_credential=SAGA_CREDENTIAL_A)

    retries = [
        event
        for event in harness.events("sag_retry")
        if event.action == ACTION_STEP_RETRYING
    ]
    assert len(retries) == 2
    assert all(event.reason == "dependency_unavailable" for event in retries)
    assert all(event.state == StepState.RETRYING.value for event in retries)
    attempts = [
        event
        for event in harness.events("sag_retry")
        if event.action == "saga.step.attempt"
    ]
    assert [event.reason for event in attempts] == [
        "attempt:1",
        "attempt:2",
        "attempt:3",
    ]
    assert final.state is SagaState.COMPLETED


def test_security_sensitive_refusals_are_marked_and_auditable():
    harness = saga_harness()
    definition = harness.definition(
        "refusals",
        harness.reserve_step("A", "res_b1"),  # a resource of Tenant B
    )
    harness.start(definition, saga_id="sag_sec")
    harness.executor.run("sag_sec", subject_credential=SAGA_CREDENTIAL_A)

    refused = [
        event
        for event in harness.events("sag_sec")
        if event.action == ACTION_STEP_FAILED
    ]
    assert refused and refused[0].security_sensitive is True
    assert refused[0].reason == "resource_tenant_mismatch"

    # A refused start is auditable even though no workflow exists yet.
    with pytest.raises(SagaRefused):
        harness.start(definition, saga_id="sag_sec2", claimed_tenant_id="ten_b")
    start_refusals = [
        event
        for event in harness.executor.store.audit
        if event.action == "saga.start_refused"
    ]
    assert start_refusals and start_refusals[-1].saga_id is None
    assert start_refusals[-1].security_sensitive is True

    # A delivery from another tenant is refused and audited before any step.
    harness.start(
        harness.definition("one", harness.reserve_step("A", "res_a1")),
        saga_id="sag_sec3",
    )
    try:
        harness.executor.run("sag_sec3", subject_credential=SAGA_CREDENTIAL_B)
    except SagaContextViolation:
        pass
    delivery_refusals = [
        event
        for event in harness.events("sag_sec3")
        if event.action == "saga.delivery_refused"
    ]
    assert delivery_refusals and delivery_refusals[0].security_sensitive is True
    assert delivery_refusals[0].reason == "tenant_mismatch"


def test_replays_and_conflicts_of_the_command_boundary_share_the_journal():
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_replay")
    harness.executor.deliver_step(
        "sag_replay", "A", subject_credential=SAGA_CREDENTIAL_A
    )
    harness.executor.deliver_step(
        "sag_replay", "A", subject_credential=SAGA_CREDENTIAL_A
    )

    replays = [
        event
        for event in harness.events("sag_replay")
        if event.action == "idempotency_replay"
    ]
    assert len(replays) == 1
    # The IS-005 event is correlated with the workflow and the step it belongs to.
    assert replays[0].saga_id == "sag_replay"
    assert replays[0].step_id == "A"
    assert replays[0].tenant_id == "ten_a"


def test_the_journal_is_append_only_and_ordered():
    harness = saga_harness()
    definition = harness.definition(
        "two-steps",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step("B", "res_a2"),
    )
    harness.start(definition, saga_id="sag_journal")
    harness.executor.deliver_step(
        "sag_journal", "A", subject_credential=SAGA_CREDENTIAL_A
    )
    before = list(harness.executor.store.audit)
    harness.executor.run("sag_journal", subject_credential=SAGA_CREDENTIAL_A)
    after = harness.executor.store.audit

    assert after[: len(before)] == before  # nothing was rewritten or removed
    assert [event.seq for event in after] == list(range(1, len(after) + 1))


def test_no_credential_is_persisted_in_the_workflow_state_or_the_journal():
    harness = saga_harness()
    definition = harness.definition("one-step", harness.reserve_step("A", "res_a1"))
    harness.start(definition, saga_id="sag_nosecret", correlation_id="cor-nosecret")
    harness.executor.run("sag_nosecret", subject_credential=SAGA_CREDENTIAL_A)

    store = harness.executor.store
    record = store.get("sag_nosecret")
    persisted: list[object] = [record.context, *record.steps.values(), *store.audit]
    fields: list[object] = []
    for value in persisted:
        fields.extend(getattr(value, "__dict__", {}).values())
    assert SAGA_CREDENTIAL_A not in [
        field for field in fields if isinstance(field, str)
    ]
    # The identity is what is stored, not the secret that proved it.
    assert record.context.identity_id == "idn_human_a"
    assert all(
        event.identity_id == "idn_human_a" for event in store.audit if event.saga_id
    )


def test_required_context_fields_are_declared_by_the_component():
    """The contract states the observability context the implementation writes."""
    contract = json.loads(
        Path("components/saga/contract/component_contract.json").read_text(
            encoding="utf-8"
        )
    )
    declared = set(contract["observability"]["context_fields"])
    assert set(REQUIRED_CONTEXT) <= declared
    actions = set(contract["observability"]["audited_actions"])
    harness = saga_harness()
    definition = harness.definition(
        "everything",
        harness.reserve_step("A", "res_a1"),
        harness.reserve_step(
            "B",
            "res_a2",
            retry=RetryPolicy(max_attempts=2),
            fail_before=lambda ctx: (
                StepFailure("flaky", retryable=True) if ctx.attempt == 1 else None
            ),
        ),
        harness.reserve_step("C", "res_a3", fail_before=StepFailure("nope")),
    )
    harness.start(definition, saga_id="sag_declared")
    harness.executor.run("sag_declared", subject_credential=SAGA_CREDENTIAL_A)
    used = {event.action for event in harness.events("sag_declared")}
    assert used <= actions, sorted(used - actions)
    assert ACTION_STEP_RETRYING in used
