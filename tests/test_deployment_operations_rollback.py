from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

rollback_module = importlib.import_module("deployment_operations.rollback")
from deployment_operations.deployment import (
    Deployment,
    DeploymentRequest,
    InstanceReference,
)
from deployment_operations.errors import (
    DeploymentInputRejected,
    InvalidDeploymentStateTransition,
)
from deployment_operations.events import EventJournal
from deployment_operations.rollback import (
    RollbackRequest,
    derive_rollback_id,
    rollback,
)
from deployment_operations.state import (
    LIFECYCLE_REALIZED,
    LIFECYCLE_ROLLED_BACK,
    DeploymentRecord,
    DeploymentStateStore,
    derive_deployment_id,
)


def _record(
    *,
    instance: str,
    deployment_id: str,
    attempt: int = 1,
    environment_id: str = "env",
) -> DeploymentRecord:
    record = DeploymentRecord.initial(
        deployment_id=deployment_id,
        environment_id=environment_id,
        attempt=attempt,
        platform_id="platform",
        manifest_id="manifest-" + instance[0],
        manifest_version="1.0." + instance[0],
        manifest_digest="b" * 64,
        manifest_state="approved",
        instance_digest=instance,
        at="2026-09-30T00:00:00Z",
    )
    record = record.mark_ready(at="2026-09-30T00:01:00Z")
    record = record.mark_identity_verified(at="2026-09-30T00:02:00Z")
    record = record.mark_running(at="2026-09-30T00:02:30Z", running=True)
    return record.mark_realized(at="2026-09-30T00:03:00Z")


#: A runtime element this synthetic operation holds. A rollback releases the
#: references of an operation that is **bound** to one: ``Deployment.release``
#: refuses an operation that reaches no runtime, so a handle-less deployment
#: claiming ``running`` would model a state the boundary does not allow. The
#: release is the contract's detach — it never claims a stopped platform.
BOUND_HANDLE = "bound-runtime-handle"


def _deployment(tmp_path: Path, record: DeploymentRecord) -> Deployment:
    state_path = DeploymentStateStore.path_for(tmp_path, record.deployment_id)
    store = DeploymentStateStore(state_path)
    store.write(record)
    journal = EventJournal(EventJournal.path_for(tmp_path, record.deployment_id))
    return Deployment(
        record=record,
        verification=SimpleNamespace(),
        state_path=state_path,
        events_path=journal.path,
        _handles=(BOUND_HANDLE,),
        _journal=journal,
        _store=store,
    )


class DetachOnlyRuntime:
    """The production contract's runtime seam: ``stop`` releases a reference.

    It is the detach of ADR-0016 §18 — the member keeps running under its
    owner's policy — so the double records what it was asked and can prove
    afterwards that nothing terminated the member.
    """

    def __init__(self) -> None:
        self.member_alive = True
        self.detached: list[str] = []

    def stop(self, handle: str) -> dict[str, object]:
        assert self.member_alive, "the member is its owner's, not this operation's"
        self.detached.append(handle)
        return {"status": "detached", "was_running": self.member_alive}


def test_a_detached_rollback_member_stays_alive_without_a_stop_claim(
    tmp_path: Path, monkeypatch
):
    """AC: detach-only runtime — the old member lives, the state says so."""
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)
    runtime = DetachOnlyRuntime()
    request.current._runtime = runtime
    request.current._handles = ("old-runtime-handle",)
    holder = {}

    def deploy_target(deployment_request, **_kwargs):
        candidate = _candidate_for_request(tmp_path, deployment_request)
        holder["candidate"] = candidate
        return candidate

    monkeypatch.setattr(rollback_module, "deploy", deploy_target)

    result = rollback(request)

    candidate = holder["candidate"]
    assert result is candidate
    assert candidate.deployed is True
    assert runtime.detached == ["old-runtime-handle"]
    assert (
        runtime.member_alive is True
    ), "the rollback released a reference; the member it released is still up"
    record = request.current.record
    assert record.lifecycle == LIFECYCLE_ROLLED_BACK
    assert record.running is True
    assert record.ready is False
    assert record.deployed is False
    assert record.identity_verified is True
    released = [
        action
        for action in record.operational_actions
        if action.name == "platform_released"
    ]
    assert len(released) == 1
    assert released[0].detail["origin"] == "rollback_current"
    assert not any(
        action.name == "platform_stopped" for action in record.operational_actions
    )
    state_text = request.current.state_path.read_text(encoding="utf-8")
    journal_text = request.current.events_path.read_text(encoding="utf-8")
    assert "platform_stopped" not in state_text
    assert "platform_stopped" not in journal_text
    assert "platform_released" in state_text
    current_events = [event.event for event in request.current.events()]
    assert current_events == [
        "rollback_requested",
        "rollback_target_verified",
        "rollback_completed",
    ]
    candidate_events = [event.event for event in candidate.events()]
    assert candidate_events[0] == "rollback_target_realized"
    assert candidate_events[-1] == "rollback_completed"


def _candidate_for_request(
    tmp_path: Path, deployment_request: DeploymentRequest
) -> Deployment:
    deployment_id = derive_deployment_id(
        deployment_request.instance.platform_id,
        deployment_request.instance.instance_digest,
        deployment_request.environment.environment_id,
        deployment_request.attempt,
    )
    record = _record(
        instance=deployment_request.instance.instance_digest,
        deployment_id=deployment_id,
        attempt=deployment_request.attempt,
    )
    return _deployment(tmp_path, record)


def _target_request(
    tmp_path: Path, target_record: DeploymentRecord
) -> DeploymentRequest:
    return DeploymentRequest(
        instance=InstanceReference(
            instance_digest=target_record.instance_digest or "",
            platform_id=target_record.platform_id,
        ),
        instance_document={"instance_digest": target_record.instance_digest},
        manifest_document={"manifest_digest": target_record.manifest_digest},
        environment=SimpleNamespace(
            environment_id=target_record.environment_id,
            operations_dir=tmp_path,
        ),
        attempt=1,
    )


def _request(tmp_path: Path):
    current_record = _record(instance="c" * 64, deployment_id="current-a1")
    target_record = _record(
        instance="a" * 64,
        deployment_id=derive_deployment_id("platform", "a" * 64, "env", 1),
    )
    target_record = target_record.mark_superseded(
        at="2026-09-30T00:04:00Z", detail={"replacement": "current-a1"}
    ).mark_stopped(at="2026-09-30T00:05:00Z")
    current = _deployment(tmp_path, current_record)
    target = _deployment(tmp_path, target_record)
    target_request = _target_request(tmp_path, target_record)
    return RollbackRequest(
        current,
        target,
        target_request,
        derive_rollback_id(current, target),
    )


def _verified_target(_document, _manifest, **_kwargs):
    return SimpleNamespace(
        ok=True,
        platform_id="platform",
        instance_digest="a" * 64,
        manifest_id="manifest-a",
        manifest_version="1.0.a",
        manifest_digest="b" * 64,
        all_errors=lambda: (),
    )


def test_derive_rollback_id_correlates_current_and_exact_target(tmp_path: Path):
    request = _request(tmp_path)
    assert request.rollback_id == derive_rollback_id(request.current, request.target)
    assert "c" * 64 not in request.rollback_id
    assert "a" * 64 in request.rollback_id


def test_rollback_success_realizes_exact_prior_target_then_rolls_back_current(
    tmp_path: Path, monkeypatch
):
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)
    observed = {}

    def fake_deploy(deployment_request, **_kwargs):
        observed["attempt"] = deployment_request.attempt
        observed["digest"] = deployment_request.instance.instance_digest
        observed["candidate"] = _candidate_for_request(tmp_path, deployment_request)
        return observed["candidate"]

    monkeypatch.setattr(rollback_module, "deploy", fake_deploy)

    result = rollback(request)
    candidate = observed["candidate"]

    assert result is candidate
    assert observed["attempt"] == 2
    assert observed["digest"] == "a" * 64
    assert candidate.record.deployed
    assert candidate.record.instance_digest == request.target.record.instance_digest
    assert request.current.record.lifecycle == LIFECYCLE_ROLLED_BACK
    # The hand-over is a detach: the previous record keeps its running claim and
    # withdraws the condition it can no longer verify.
    assert request.current.record.running is True
    assert request.current.record.ready is False
    assert request.current.record.deployed is False
    assert request.current.record.identity_verified is True
    action_names = [
        action.name for action in request.current.record.operational_actions
    ]
    assert action_names.index("rollback_requested") < action_names.index(
        "rollback_target_verified"
    )
    assert action_names.index("rollback_target_verified") < action_names.index(
        "rollback_target_realized"
    )
    assert action_names.index("rollback_target_realized") < action_names.index(
        "platform_released"
    )
    assert action_names.index("platform_released") < action_names.index(
        "platform_rolled_back"
    )
    assert (
        "platform_stopped" not in action_names
    ), "a released reference is not a stopped platform"
    assert action_names.index("platform_rolled_back") < action_names.index(
        "rollback_completed"
    )
    assert (
        DeploymentStateStore(request.current.state_path).read()
        == request.current.record
    )
    names = [event.event for event in request.current.events()]
    assert names.index("rollback_requested") < names.index("rollback_target_verified")
    assert names.index("rollback_target_verified") < names.index("rollback_completed")
    assert any(
        event.event == "rollback_target_realized" for event in candidate.events()
    )
    assert any(event.event == "rollback_completed" for event in candidate.events())
    completed = next(
        event for event in candidate.events() if event.event == "rollback_completed"
    )
    assert completed.instance_digest == "a" * 64
    assert completed.detail["rollback_id"] == request.rollback_id


def test_rollback_rejects_name_only_or_missing_target_digest(tmp_path: Path):
    request = _request(tmp_path)
    invalid_target_request = replace(
        request.target_request,
        instance=InstanceReference(instance_digest="", platform_id="platform"),
        instance_document={"platform_name": "known"},
        manifest_document={},
    )
    request = replace(request, target_request=invalid_target_request)

    with pytest.raises(DeploymentInputRejected):
        rollback(request)

    assert request.current.deployed
    assert request.current.record.lifecycle == LIFECYCLE_REALIZED


@pytest.mark.parametrize(
    ("digest", "platform_id"),
    [("d" * 64, "platform"), ("a" * 64, "other-platform")],
)
def test_rollback_rejects_target_identity_or_digest_mismatch(
    tmp_path: Path, monkeypatch, digest: str, platform_id: str
):
    request = _request(tmp_path)
    request = replace(
        request,
        target_request=replace(
            request.target_request,
            instance=InstanceReference(instance_digest=digest, platform_id=platform_id),
        ),
    )
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)

    with pytest.raises(DeploymentInputRejected):
        rollback(request)

    assert request.current.deployed
    assert request.current.record.lifecycle == LIFECYCLE_REALIZED


def test_rollback_rejects_unverifiable_target_before_runtime_action(
    tmp_path: Path, monkeypatch
):
    request = _request(tmp_path)
    monkeypatch.setattr(
        rollback_module,
        "verify_instance",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False, all_errors=lambda: ["bad digest"]
        ),
    )
    calls = []
    monkeypatch.setattr(rollback_module, "deploy", lambda *_a, **_k: calls.append(1))

    with pytest.raises(DeploymentInputRejected):
        rollback(request)

    assert calls == []
    assert request.current.deployed


def test_failed_target_transition_preserves_current_and_records_failure(
    tmp_path: Path, monkeypatch
):
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)

    calls = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("runtime refused")

    monkeypatch.setattr(rollback_module, "deploy", fail)
    with pytest.raises(RuntimeError, match="runtime refused"):
        rollback(request)

    assert request.current.record.lifecycle == LIFECYCLE_REALIZED
    assert request.current.deployed
    assert (
        request.current.record.running,
        request.current.record.ready,
        request.current.record.deployed,
    ) == (True, True, True), "the current platform stays authoritative and ready"
    assert not any(
        action.name in {"platform_released", "platform_stopped"}
        for action in request.current.record.operational_actions
    ), "no hand-over happened, so none is recorded"
    assert request.current.record.operational_actions[-1].name == "rollback_failed"
    failure = next(
        event for event in request.current.events() if event.event == "rollback_failed"
    )
    assert failure.detail["operation"] == "target_realization"
    assert failure.detail["target_instance_digest"] == "a" * 64
    event_names = [event.event for event in request.current.events()]
    assert event_names.index("rollback_requested") < event_names.index(
        "rollback_target_verified"
    )
    assert event_names.index("rollback_target_verified") < event_names.index(
        "rollback_failed"
    )
    assert (
        DeploymentStateStore(request.current.state_path).read()
        == request.current.record
    )
    event_count = len(request.current.events())
    with pytest.raises(InvalidDeploymentStateTransition):
        rollback(request)
    assert calls == [1]
    assert len(request.current.events()) == event_count


def test_rollback_requires_current_and_target_state_to_be_authoritative(tmp_path: Path):
    request = _request(tmp_path)
    DeploymentStateStore(request.target.state_path).write(
        _record(instance="d" * 64, deployment_id="tampered")
    )

    with pytest.raises(InvalidDeploymentStateTransition, match="tampered"):
        rollback(request)

    assert request.current.deployed


def test_rollback_refuses_repeat_after_success_without_duplicate_effects(
    tmp_path: Path, monkeypatch
):
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)
    calls = []
    candidate_holder = {}

    def fake_deploy(deployment_request, **_kwargs):
        calls.append(1)
        candidate_holder["candidate"] = _candidate_for_request(
            tmp_path, deployment_request
        )
        return candidate_holder["candidate"]

    monkeypatch.setattr(rollback_module, "deploy", fake_deploy)

    rollback(request)
    event_count = len(request.current.events())
    with pytest.raises(InvalidDeploymentStateTransition):
        rollback(request)

    assert calls == [1]
    assert len(request.current.events()) == event_count


def test_rollback_uses_only_forward_migration_deployment_path(
    monkeypatch, tmp_path: Path
):
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)
    calls = []

    def ordinary_deploy(deployment_request, **_kwargs):
        calls.append(
            (deployment_request.attempt, deployment_request.instance.instance_digest)
        )
        return _candidate_for_request(tmp_path, deployment_request)

    monkeypatch.setattr(rollback_module, "deploy", ordinary_deploy)
    rollback(request)

    assert calls == [(2, "a" * 64)]
    assert not hasattr(rollback_module, "downgrade")
    assert request.current.record.lifecycle == LIFECYCLE_ROLLED_BACK
    assert any(
        action.name == "platform_rolled_back"
        for action in request.current.record.operational_actions
    )


def test_current_release_failure_preserves_authoritative_state_and_cleans_candidate(
    tmp_path: Path, monkeypatch
):
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)
    cleanup_calls = []

    class CandidateRuntime:
        def stop(self, handle):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate_holder = {}

    def deploy_target(deployment_request, **_kwargs):
        candidate = _candidate_for_request(tmp_path, deployment_request)
        candidate._runtime = CandidateRuntime()
        candidate._handles = ("target-runtime-handle",)
        candidate_holder["candidate"] = candidate
        return candidate

    monkeypatch.setattr(rollback_module, "deploy", deploy_target)
    release_attempts = []

    def fail_current_release(*, origin):
        release_attempts.append(origin)
        raise RuntimeError("old runtime refused to release")

    monkeypatch.setattr(request.current, "release", fail_current_release)
    original = request.current.record

    with pytest.raises(RuntimeError, match="old runtime refused to release"):
        rollback(request)

    candidate = candidate_holder["candidate"]
    persisted = DeploymentStateStore(request.current.state_path).read()
    assert release_attempts == ["rollback_current"]
    assert cleanup_calls == ["target-runtime-handle"]
    assert not candidate.deployed
    assert (
        candidate.record.running is True
    ), "the aborted target was released, not stopped"
    assert candidate.record.ready is False
    assert not any(
        action.name == "platform_stopped"
        for action in candidate.record.operational_actions
    )
    assert persisted.lifecycle == LIFECYCLE_REALIZED
    assert persisted.running is True
    assert persisted.ready is True
    assert persisted.identity_verified is True
    assert persisted.platform_id == original.platform_id
    assert persisted.instance_digest == original.instance_digest
    assert persisted.deployed
    assert request.current.record == persisted
    action_names = [action.name for action in persisted.operational_actions]
    assert action_names.count("rollback_failed") == 1
    assert "rollback_completed" not in action_names
    assert "platform_rolled_back" not in action_names
    assert "platform_stopped" not in action_names
    assert (
        "platform_released" not in action_names
    ), "the release that failed is not recorded as one that happened"

    current_events = request.current.events()
    assert sum(event.event == "rollback_failed" for event in current_events) == 1
    assert not any(event.event == "rollback_completed" for event in current_events)
    assert not any(event.event == "rollback_completed" for event in candidate.events())


def test_final_state_write_failure_restores_released_state_without_success_claim(
    tmp_path: Path, monkeypatch
):
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)
    cleanup_calls = []

    class CandidateRuntime:
        def stop(self, handle):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate_holder = {}

    def deploy_target(deployment_request, **_kwargs):
        candidate = _candidate_for_request(tmp_path, deployment_request)
        candidate._runtime = CandidateRuntime()
        candidate._handles = ("candidate-handle",)
        candidate_holder["candidate"] = candidate
        return candidate

    monkeypatch.setattr(rollback_module, "deploy", deploy_target)
    initial = request.current.record
    original_write = DeploymentStateStore.write
    final_write_attempts = []

    def fail_rolled_back_commit(store, record, *, secrets=()):
        if (
            store.path == request.current.state_path
            and record.lifecycle == LIFECYCLE_ROLLED_BACK
        ):
            final_write_attempts.append(record)
            raise OSError("injected final rollback state write failure")
        return original_write(store, record, secrets=secrets)

    monkeypatch.setattr(DeploymentStateStore, "write", fail_rolled_back_commit)

    with pytest.raises(OSError, match="injected final rollback state write failure"):
        rollback(request)

    candidate = candidate_holder["candidate"]
    persisted = DeploymentStateStore(request.current.state_path).read()
    assert len(final_write_attempts) == 1
    assert cleanup_calls == ["candidate-handle"]
    assert not candidate.deployed
    assert persisted.lifecycle == initial.lifecycle == LIFECYCLE_REALIZED
    assert persisted.platform_id == initial.platform_id
    assert persisted.instance_digest == initial.instance_digest
    assert persisted.identity_verified is True
    # The references were already released, so preserve the honest actual
    # conditions — still potentially alive, no verified readiness — rather than
    # restoring a stale `deployed` claim or claiming a stop.
    assert persisted.running is True
    assert persisted.ready is False
    assert persisted.deployed is False
    assert any(
        action.name == "platform_released" for action in persisted.operational_actions
    )
    names = [action.name for action in persisted.operational_actions]
    assert "platform_stopped" not in names
    assert names.count("rollback_failed") == 1
    assert "rollback_completed" not in names
    assert "platform_rolled_back" not in names
    assert request.current.record == persisted

    current_events = request.current.events()
    assert sum(event.event == "rollback_failed" for event in current_events) == 1
    assert not any(event.event == "rollback_completed" for event in current_events)
    candidate_actions = [action.name for action in candidate.record.operational_actions]
    assert "rollback_completed" not in candidate_actions
    assert not any(event.event == "rollback_completed" for event in candidate.events())


@pytest.mark.parametrize("failed_journal", ["candidate", "current"])
def test_completion_event_failure_preserves_committed_success_without_failure_event(
    tmp_path: Path, monkeypatch, failed_journal: str
):
    request = _request(tmp_path)
    monkeypatch.setattr(rollback_module, "verify_instance", _verified_target)
    deploy_calls = []
    cleanup_calls = []
    candidate_holder = {}

    class CandidateRuntime:
        def stop(self, handle):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    def deploy_target(deployment_request, **_kwargs):
        deploy_calls.append(1)
        candidate = _candidate_for_request(tmp_path, deployment_request)
        candidate._runtime = CandidateRuntime()
        candidate._handles = ("candidate-handle",)
        candidate_holder["candidate"] = candidate
        return candidate

    monkeypatch.setattr(rollback_module, "deploy", deploy_target)
    original_append = EventJournal.append
    failed_appends = []

    def fail_selected_completion(journal, event, *, secrets=()):
        if event.event == "rollback_completed":
            candidate = candidate_holder["candidate"]
            target_path = (
                candidate.events_path
                if failed_journal == "candidate"
                else request.current.events_path
            )
            if journal.path == target_path:
                failed_appends.append((journal.path, event.event))
                raise OSError(f"injected {failed_journal} completion-event failure")
        return original_append(journal, event, secrets=secrets)

    monkeypatch.setattr(EventJournal, "append", fail_selected_completion)
    with pytest.raises(
        OSError, match=f"injected {failed_journal} completion-event failure"
    ):
        rollback(request)

    candidate = candidate_holder["candidate"]
    current_state = DeploymentStateStore(request.current.state_path).read()
    candidate_state = DeploymentStateStore(candidate.state_path).read()
    assert len(failed_appends) == 1
    assert deploy_calls == [1]
    assert cleanup_calls == []
    assert candidate.deployed
    assert current_state.lifecycle == LIFECYCLE_ROLLED_BACK
    assert current_state.deployed is False
    assert candidate_state.deployed is True
    assert any(
        action.name == "rollback_completed"
        for action in current_state.operational_actions
    )
    assert any(
        action.name == "rollback_completed"
        for action in candidate_state.operational_actions
    )
    assert not any(
        action.name == "rollback_failed" for action in current_state.operational_actions
    )

    current_events = request.current.events()
    candidate_events = candidate.events()
    assert not any(event.event == "rollback_failed" for event in current_events)
    assert not any(event.event == "rollback_failed" for event in candidate_events)
    assert any(event.event == "rollback_completed" for event in candidate_events) is (
        failed_journal == "current"
    )
    assert not any(event.event == "rollback_completed" for event in current_events)

    event_counts = (len(current_events), len(candidate_events))
    with pytest.raises(InvalidDeploymentStateTransition):
        rollback(request)
    assert deploy_calls == [1]
    assert cleanup_calls == []
    assert (len(request.current.events()), len(candidate.events())) == event_counts
