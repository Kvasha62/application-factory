import ast
import importlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

upgrade_module = importlib.import_module("deployment_operations.upgrade")
rollback_module = importlib.import_module("deployment_operations.rollback")
from deployment_operations.deployment import Deployment
from deployment_operations.errors import (
    DeploymentInputRejected,
    DeploymentStateError,
    InvalidDeploymentStateTransition,
)
from deployment_operations.events import EventJournal
from deployment_operations.state import (
    LIFECYCLE_REALIZED,
    LIFECYCLE_SUPERSEDED,
    DeploymentRecord,
    DeploymentStateStore,
)
from deployment_operations.upgrade import UpgradeRequest, derive_upgrade_id, upgrade


def _record(
    tmp_path: Path,
    *,
    instance: str = "a" * 64,
    deployment_id: str = "old-deployment",
) -> DeploymentRecord:
    record = DeploymentRecord.initial(
        deployment_id=deployment_id,
        environment_id="env",
        attempt=1,
        platform_id="platform",
        manifest_id="manifest",
        manifest_version="1",
        manifest_digest="b" * 64,
        manifest_state="approved",
        instance_digest=instance,
        at="2026-09-30T00:00:00Z",
    )
    record = record.mark_ready(at="2026-09-30T00:01:00Z")
    record = record.mark_identity_verified(at="2026-09-30T00:02:00Z")
    record = record.mark_running(at="2026-09-30T00:02:30Z", running=True)
    return record.mark_realized(at="2026-09-30T00:03:00Z")


def _current(tmp_path: Path):
    record = _record(tmp_path)
    state_path = tmp_path / "old.json"
    DeploymentStateStore(state_path).write(record)
    return SimpleNamespace(
        record=record,
        state_path=state_path,
        deployed=record.deployed,
    )


def _request(tmp_path: Path, digest: str = "c" * 64):
    environment = SimpleNamespace(environment_id="env", operations_dir=tmp_path)
    instance = SimpleNamespace(instance_digest=digest)
    replacement = SimpleNamespace(instance=instance, environment=environment)
    return replacement


def test_derive_upgrade_id_is_correlated_to_old_and_new_identity(tmp_path: Path):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    assert derive_upgrade_id(current, replacement) == (
        "old-deployment->platform:" + "c" * 64 + "@env"
    )


def test_upgrade_rejects_non_deployed_current_platform(tmp_path: Path):
    current = _current(tmp_path)
    current.deployed = False
    request = UpgradeRequest(
        current,
        _request(tmp_path),
        derive_upgrade_id(current, _request(tmp_path)),
    )

    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)


def test_upgrade_rejects_same_instance(tmp_path: Path):
    current = _current(tmp_path)
    request = UpgradeRequest(current, _request(tmp_path, "a" * 64), "upgrade-1")

    with pytest.raises(DeploymentInputRejected):
        upgrade(request)


def test_upgrade_rejects_tampered_persisted_old_state(tmp_path: Path):
    current = _current(tmp_path)
    tampered = _record(tmp_path, instance="d" * 64)
    DeploymentStateStore(current.state_path).write(tampered)
    replacement = _request(tmp_path)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )

    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)


def test_upgrade_rejects_wrong_upgrade_correlation(tmp_path: Path):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    request = UpgradeRequest(current, replacement, "forged-upgrade-id")

    with pytest.raises(DeploymentInputRejected):
        upgrade(request)


def test_upgrade_rejects_replacement_digest_drift(tmp_path: Path, monkeypatch):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    candidate = SimpleNamespace(
        record=_record(tmp_path, instance="e" * 64), deployed=True
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)

    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED


def test_failed_replacement_leaves_old_deployed(tmp_path: Path, monkeypatch):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )

    def fail(*args, **kwargs):
        raise RuntimeError("replacement refused")

    monkeypatch.setattr(upgrade_module, "deploy", fail)

    with pytest.raises(RuntimeError, match="replacement refused"):
        upgrade(request)

    assert current.record.deployed is True
    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED


def test_upgrade_rejects_empty_replacement_digest(tmp_path: Path):
    current = _current(tmp_path)
    replacement = _request(tmp_path, "")
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )

    with pytest.raises(DeploymentInputRejected):
        upgrade(request)

    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED


def test_upgrade_rejects_different_environment(tmp_path: Path):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    replacement.environment.environment_id = "other-env"
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )

    with pytest.raises(DeploymentInputRejected):
        upgrade(request)

    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED


def test_upgrade_rejects_unavailable_persisted_state(tmp_path: Path):
    current = _current(tmp_path)
    current.state_path.unlink()
    replacement = _request(tmp_path)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )

    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)

    assert current.record.deployed is True
    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED


def test_upgrade_rejects_replacement_without_honest_deployed_claim(
    tmp_path: Path, monkeypatch
):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    candidate = SimpleNamespace(
        record=_record(tmp_path, instance="c" * 64), deployed=False
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)

    assert current.record.deployed is True
    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED


def test_success_records_correlated_upgrade_events(tmp_path: Path, monkeypatch):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    candidate = SimpleNamespace(
        record=_record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        deployed=True,
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    upgrade(request)

    journal = EventJournal(
        EventJournal.path_for(tmp_path, current.record.deployment_id)
    )
    old_events = journal.events()
    superseded = next(
        event for event in old_events if event.event == "old_instance_superseded"
    )
    assert superseded.detail["upgrade_id"] == request.upgrade_id
    assert superseded.detail["replacement_instance_digest"] == "c" * 64


def test_success_supersedes_old_only_after_new_deployed(tmp_path: Path, monkeypatch):
    current = _current(tmp_path)
    replacement = _request(tmp_path)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    new_record = _record(tmp_path, instance="c" * 64)
    candidate = SimpleNamespace(record=new_record, deployed=True)
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    result = upgrade(request)

    assert result is candidate
    persisted = DeploymentStateStore(current.state_path).read()
    assert persisted.lifecycle == LIFECYCLE_SUPERSEDED
    assert persisted.deployed is False
    assert any(
        action.name == "platform_superseded" for action in persisted.operational_actions
    )


#: A runtime element this synthetic operation holds. An upgrade releases the
#: references of an operation that is **bound** to one: ``Deployment.release``
#: refuses an operation that reaches no runtime, so a handle-less deployment
#: claiming ``running`` would model a state the boundary does not allow. The
#: release is the contract's detach — it never claims a stopped platform.
BOUND_HANDLE = "bound-runtime-handle"


class DetachOnlyRuntime:
    """The production contract's runtime seam: ``stop`` releases a reference.

    It is the detach of ADR-0016 §18 — the member keeps running under its
    owner's policy — so the double records what it was asked and can prove
    afterwards that nothing terminated the member, which is the fact the
    upgrade's state and journal must never contradict. Members named in
    ``refuse`` refuse their release, which is how a *partial* hand-over over
    several references is exercised.
    """

    def __init__(self, *, refuse: frozenset[str] = frozenset()) -> None:
        self.member_alive = True
        self.refuse = refuse
        self.attempted: list[str] = []
        self.detached: list[str] = []

    def stop(self, handle: str) -> dict[str, object]:
        self.attempted.append(handle)
        if handle in self.refuse:
            raise RuntimeError(f"{handle}: the member refused to be released")
        assert self.member_alive, "the member is its owner's, not this operation's"
        self.detached.append(handle)
        return {"status": "detached", "was_running": self.member_alive}


def test_a_detached_upgrade_member_stays_alive_without_a_stop_claim(
    tmp_path: Path, monkeypatch
):
    """AC: detach-only runtime — the released member lives, the state says so."""
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    runtime = DetachOnlyRuntime()
    current = _real_deployment(
        tmp_path,
        current_record,
        runtime=runtime,
        handles=("old-runtime-handle",),
    )
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    result = upgrade(request)

    assert result is candidate
    # the release reached the seam exactly once, and it terminated nothing
    assert runtime.detached == ["old-runtime-handle"]
    assert (
        runtime.member_alive is True
    ), "the upgrade released a reference; the member it released is still up"
    persisted = DeploymentStateStore(current.state_path).read()
    assert persisted.lifecycle == LIFECYCLE_SUPERSEDED
    assert persisted.running is True
    assert persisted.ready is False
    assert persisted.deployed is False
    assert persisted.identity_verified is True
    released = [
        action
        for action in persisted.operational_actions
        if action.name == "platform_released"
    ]
    assert len(released) == 1
    assert released[0].detail["origin"] == "upgrade_current"
    assert not any(
        action.name == "platform_stopped" for action in persisted.operational_actions
    )
    state_text = current.state_path.read_text(encoding="utf-8")
    journal_text = current.events_path.read_text(encoding="utf-8")
    assert "platform_stopped" not in state_text
    assert "platform_stopped" not in journal_text
    assert "platform_released" in state_text
    assert [event.event for event in current.events()] == [
        "upgrade_requested",
        "old_instance_superseded",
    ]


def _real_deployment(
    tmp_path: Path,
    record: DeploymentRecord,
    *,
    runtime=None,
    handles: tuple[str, ...] = (BOUND_HANDLE,),
) -> Deployment:
    state_path = DeploymentStateStore.path_for(tmp_path, record.deployment_id)
    store = DeploymentStateStore(state_path)
    store.write(record)
    journal = EventJournal(EventJournal.path_for(tmp_path, record.deployment_id))
    return Deployment(
        record=record,
        verification=SimpleNamespace(),
        state_path=state_path,
        events_path=journal.path,
        _runtime=runtime,
        _handles=handles,
        _journal=journal,
        _store=store,
    )


def test_a_partial_release_of_current_is_recorded_and_never_claims_a_stop(
    tmp_path: Path, monkeypatch
):
    """One member refusing its release is neither "nothing happened" nor a stop.

    The hand-over over several references can be partial: the reference that
    answered is released, the one that refused is not, and deployment state must
    state exactly that — never a fully ready platform, never a stopped one.
    """
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    runtime = DetachOnlyRuntime(refuse=frozenset({"member-b"}))
    current = _real_deployment(
        tmp_path,
        current_record,
        runtime=runtime,
        handles=("member-a", "member-b"),
    )
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "detached"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    with pytest.raises(
        RuntimeError, match="member-b: the member refused to be released"
    ) as caught:
        upgrade(request)

    # every reference was attempted; the refusing one kept no other bound
    assert runtime.attempted == ["member-a", "member-b"]
    assert runtime.detached == ["member-a"]
    assert runtime.member_alive is True, "a detach terminated nothing"
    # the causal failure is the runtime's own, annotated with the partial outcome
    assert any("partial" in note for note in caught.value.__notes__)
    assert any("member-b" in note for note in caught.value.__notes__)

    persisted = DeploymentStateStore(current.state_path).read()
    assert (persisted.running, persisted.ready, persisted.deployed) == (
        True,
        False,
        False,
    ), "the partial hand-over cannot leave a fully ready/deployed claim standing"
    assert persisted.lifecycle == LIFECYCLE_REALIZED
    assert persisted.identity_verified is True
    released = [
        action
        for action in persisted.operational_actions
        if action.name == "platform_released"
    ]
    assert len(released) == 1
    assert released[0].detail == {
        "origin": "upgrade_current",
        "complete": False,
        "released": ["member-a"],
        "unreleased": ["member-b"],
    }
    action_names = [action.name for action in persisted.operational_actions]
    assert "platform_stopped" not in action_names
    assert "platform_superseded" not in action_names
    assert "platform_stopped" not in current.state_path.read_text(encoding="utf-8")
    assert current.record == persisted
    assert cleanup_calls == ["replacement-runtime-handle"]
    assert [event.event for event in current.events()] == [
        "upgrade_requested",
        "upgrade_failed",
    ]
    # the retry cannot read the operation as untouched and fully bound
    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)
    assert runtime.attempted == ["member-a", "member-b"], (
        "the retry reached no runtime, because the state does not claim a "
        "deployed platform"
    )


def test_a_partial_aborted_candidate_release_states_the_partial_hand_over(
    tmp_path: Path, monkeypatch
):
    """The same policy for candidate cleanup: partial stays partial and kept."""
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    runtime = DetachOnlyRuntime(refuse=frozenset({"candidate-b"}))
    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=runtime,
        handles=("candidate-a", "candidate-b"),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)
    monkeypatch.setattr(
        current,
        "release",
        lambda *, origin: (_ for _ in ()).throw(
            RuntimeError("old runtime refused to release")
        ),
    )

    with pytest.raises(RuntimeError, match="old runtime refused to release"):
        upgrade(request)

    assert runtime.attempted == ["candidate-a", "candidate-b"]
    assert runtime.detached == ["candidate-a"]
    persisted_current = DeploymentStateStore(current.state_path).read()
    assert (
        persisted_current == current_record
    ), "the current deployment keeps its honest claim: nothing was handed over"
    persisted_candidate = DeploymentStateStore(candidate.state_path).read()
    assert (persisted_candidate.running, persisted_candidate.ready) == (True, False)
    assert persisted_candidate.deployed is False
    released = [
        action
        for action in persisted_candidate.operational_actions
        if action.name == "platform_released"
    ]
    assert len(released) == 1
    assert released[0].detail == {
        "origin": "upgrade_aborted_candidate",
        "complete": False,
        "released": ["candidate-a"],
        "unreleased": ["candidate-b"],
    }
    assert "platform_stopped" not in [
        action.name for action in persisted_candidate.operational_actions
    ]
    assert candidate.state_path.exists(), (
        "a partial hand-over is evidence and is kept, never removed as a stale "
        "candidate state file"
    )
    old_events = current.events()
    assert [event.event for event in old_events] == [
        "upgrade_requested",
        "upgrade_failed",
    ]
    assert (
        "candidate_cleanup_persistence_failed" not in old_events[-1].detail
    ), "a partial release is not a persistence failure"


def test_current_release_occurs_before_superseded_state_or_event(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    original_release = current.release
    observed_during_release: list[tuple[str, bool, str, tuple[str, ...]]] = []

    def observe_then_release(*, origin: str):
        persisted_now = DeploymentStateStore(current.state_path).read()
        events_now = tuple(event.event for event in current.events())
        observed_during_release.append(
            (
                current.record.lifecycle,
                current.deployed,
                persisted_now.lifecycle,
                events_now,
            )
        )
        return original_release(origin=origin)

    monkeypatch.setattr(current, "release", observe_then_release)

    result = upgrade(request)

    assert result is candidate
    assert observed_during_release == [
        (
            LIFECYCLE_REALIZED,
            True,
            LIFECYCLE_REALIZED,
            ("upgrade_requested",),
        )
    ], "the replacement was realized and verified before the old was handed over"
    persisted = DeploymentStateStore(current.state_path).read()
    assert persisted.lifecycle == LIFECYCLE_SUPERSEDED
    # The hand-over is a detach: the old record keeps its running claim and
    # withdraws the condition it can no longer verify.
    assert persisted.running is True
    assert persisted.ready is False
    assert persisted.deployed is False
    assert current.deployed is False
    action_names = [action.name for action in persisted.operational_actions]
    assert "platform_released" in action_names
    assert "platform_superseded" in action_names
    assert action_names.index("platform_released") < action_names.index(
        "platform_superseded"
    )
    assert (
        "platform_stopped" not in action_names
    ), "a released reference is not a stopped platform"
    old_event_names = [event.event for event in current.events()]
    assert old_event_names == [
        "upgrade_requested",
        "old_instance_superseded",
    ], "no signal of this upgrade claims the old platform stopped"
    new_event_names = [event.event for event in candidate.events()]
    assert new_event_names == [
        "new_identity_verified",
        "upgrade_completed",
    ]


def test_current_release_failure_preserves_authoritative_state_and_cleans_candidate(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    release_attempts: list[str] = []

    def fail_current_release(*, origin: str):
        release_attempts.append(origin)
        raise RuntimeError("old runtime refused to release")

    monkeypatch.setattr(current, "release", fail_current_release)

    with pytest.raises(RuntimeError, match="old runtime refused to release"):
        upgrade(request)

    persisted_current = DeploymentStateStore(current.state_path).read()
    persisted_candidate = DeploymentStateStore(candidate.state_path).read()
    assert release_attempts == ["upgrade_current"]
    assert cleanup_calls == ["replacement-runtime-handle"]
    assert candidate.deployed is False
    assert persisted_candidate.deployed is False
    # The aborted candidate was released, not stopped: neither its `running`
    # claim nor its readiness says the candidate platform is down.
    assert persisted_candidate.running is True
    assert persisted_candidate.ready is False
    assert persisted_current == current_record
    assert persisted_current.lifecycle == LIFECYCLE_REALIZED
    assert persisted_current.running is True
    assert persisted_current.ready is True
    assert persisted_current.identity_verified is True
    assert persisted_current.deployed is True
    assert current.record == current_record
    assert current.deployed is True
    assert not any(
        action.name == "platform_superseded"
        for action in persisted_current.operational_actions
    )
    assert not any(
        action.name == "platform_stopped"
        for action in persisted_current.operational_actions
    )

    old_events = [event.event for event in current.events()]
    new_events = [event.event for event in candidate.events()]
    assert old_events.count("upgrade_failed") == 1
    assert "old_instance_superseded" not in old_events
    assert "upgrade_completed" not in new_events


def test_final_state_write_failure_restores_released_state_and_cleans_candidate(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    original_write = DeploymentStateStore.write
    superseded_write_attempts: list[DeploymentRecord] = []

    def fail_superseded_commit(store, record, *, secrets=()):
        if (
            store.path == current.state_path
            and record.lifecycle == LIFECYCLE_SUPERSEDED
        ):
            superseded_write_attempts.append(record)
            raise OSError("injected final upgrade state write failure")
        return original_write(store, record, secrets=secrets)

    monkeypatch.setattr(DeploymentStateStore, "write", fail_superseded_commit)

    with pytest.raises(OSError, match="injected final upgrade state write failure"):
        upgrade(request)

    persisted_current = DeploymentStateStore(current.state_path).read()
    persisted_candidate = DeploymentStateStore(candidate.state_path).read()
    assert len(superseded_write_attempts) == 1
    assert cleanup_calls == ["replacement-runtime-handle"]
    assert candidate.deployed is False
    assert persisted_candidate.deployed is False
    assert persisted_current.lifecycle == LIFECYCLE_REALIZED
    # The release had already happened when the superseded write failed, so the
    # old deployment stays represented honestly — still potentially alive, no
    # verified readiness, no stopped-platform claim.
    assert persisted_current.running is True
    assert persisted_current.ready is False
    assert persisted_current.deployed is False
    action_names = [action.name for action in persisted_current.operational_actions]
    assert "platform_released" in action_names
    assert "platform_stopped" not in action_names
    assert "platform_superseded" not in action_names
    assert current.record == persisted_current
    assert current.deployed is False
    assert not any(
        action.name == "platform_superseded"
        for action in persisted_current.operational_actions
    )

    old_events = [event.event for event in current.events()]
    new_events = [event.event for event in candidate.events()]
    assert old_events.count("upgrade_failed") == 1
    assert "old_instance_superseded" not in old_events
    assert "upgrade_completed" not in new_events


@pytest.mark.parametrize(
    "failed_event",
    ["old_instance_superseded", "upgrade_completed"],
)
def test_completion_event_failure_preserves_committed_upgrade_without_failure_event(
    tmp_path: Path, monkeypatch, failed_event: str
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    deploy_calls: list[int] = []
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )

    def fake_deploy(*_args, **_kwargs):
        deploy_calls.append(1)
        return candidate

    monkeypatch.setattr(upgrade_module, "deploy", fake_deploy)
    original_append = EventJournal.append
    failed_appends: list[str] = []

    def fail_selected_event(journal, event, *, secrets=()):
        if event.event == failed_event:
            failed_appends.append(event.event)
            raise OSError(f"injected {failed_event} journal failure")
        return original_append(journal, event, secrets=secrets)

    monkeypatch.setattr(EventJournal, "append", fail_selected_event)

    with pytest.raises(OSError, match=f"injected {failed_event} journal failure"):
        upgrade(request)

    persisted_current = DeploymentStateStore(current.state_path).read()
    persisted_candidate = DeploymentStateStore(candidate.state_path).read()
    assert failed_appends == [failed_event]
    assert deploy_calls == [1]
    assert cleanup_calls == []
    assert candidate.deployed is True
    assert persisted_candidate.deployed is True
    assert persisted_current.lifecycle == LIFECYCLE_SUPERSEDED
    assert persisted_current.deployed is False
    assert current.record == persisted_current
    assert current.deployed is False
    assert any(
        action.name == "platform_superseded"
        for action in persisted_current.operational_actions
    )

    old_events = [event.event for event in current.events()]
    new_events = [event.event for event in candidate.events()]
    assert "upgrade_failed" not in old_events
    assert "upgrade_failed" not in new_events
    assert ("old_instance_superseded" in old_events) is (
        failed_event == "upgrade_completed"
    )
    assert "upgrade_completed" not in new_events

    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)
    assert deploy_calls == [1]
    assert cleanup_calls == []


def test_concurrent_persisted_state_change_during_deploy_fails_closed_and_cleans_candidate(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    competing = current_record.mark_superseded(
        at="2026-09-30T00:04:00Z",
        detail={
            "upgrade_id": "concurrent-upgrade",
            "replacement_deployment_id": "competing-deployment",
            "replacement_instance_digest": "f" * 64,
        },
    ).mark_stopped(at="2026-09-30T00:04:01Z")
    cleanup_calls: list[str] = []
    current_stop_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )

    def deploy_with_concurrent_mutation(*_args, **_kwargs):
        DeploymentStateStore(current.state_path).write(competing)
        return candidate

    monkeypatch.setattr(upgrade_module, "deploy", deploy_with_concurrent_mutation)
    monkeypatch.setattr(current, "stop", lambda: current_stop_calls.append("stop"))

    with pytest.raises(
        InvalidDeploymentStateTransition,
        match="persisted current deployment state changed during upgrade",
    ):
        upgrade(request)

    assert current_stop_calls == []
    assert cleanup_calls == ["replacement-runtime-handle"]
    assert candidate.deployed is False
    assert DeploymentStateStore(candidate.state_path).read().deployed is False
    assert DeploymentStateStore(current.state_path).read() == competing
    old_events = [event.event for event in current.events()]
    new_events = [event.event for event in candidate.events()]
    assert old_events.count("upgrade_failed") == 1
    assert "old_instance_superseded" not in old_events
    assert "upgrade_completed" not in new_events


def test_concurrent_persisted_state_change_during_release_fails_closed_and_cleans_candidate(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    competing = current_record.mark_superseded(
        at="2026-09-30T00:04:00Z",
        detail={
            "upgrade_id": "concurrent-upgrade",
            "replacement_deployment_id": "competing-deployment",
            "replacement_instance_digest": "f" * 64,
        },
    ).mark_stopped(at="2026-09-30T00:04:01Z")
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    original_release = current.release

    def release_with_concurrent_writer(*, origin: str):
        result = original_release(origin=origin)
        DeploymentStateStore(current.state_path).write(competing)
        return result

    monkeypatch.setattr(current, "release", release_with_concurrent_writer)

    with pytest.raises(
        InvalidDeploymentStateTransition,
        match="persisted current deployment state changed during upgrade",
    ):
        upgrade(request)

    assert cleanup_calls == ["replacement-runtime-handle"]
    assert candidate.deployed is False
    assert DeploymentStateStore(candidate.state_path).read().deployed is False
    assert DeploymentStateStore(current.state_path).read() == competing
    old_events = [event.event for event in current.events()]
    new_events = [event.event for event in candidate.events()]
    assert old_events.count("upgrade_failed") == 1
    assert "old_instance_superseded" not in old_events
    assert "upgrade_completed" not in new_events


def test_unconfirmed_superseded_write_replaced_by_concurrent_writer_fails_closed(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    competing = current_record.mark_stopped(at="2026-09-30T00:04:00Z")
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    original_write = DeploymentStateStore.write

    def racing_write(store, record, *, secrets=()):
        original_write(store, record, secrets=secrets)
        if (
            store.path == current.state_path
            and record.lifecycle == LIFECYCLE_SUPERSEDED
        ):
            original_write(store, competing, secrets=secrets)

    monkeypatch.setattr(DeploymentStateStore, "write", racing_write)

    with pytest.raises(
        InvalidDeploymentStateTransition,
        match="persisted current deployment state changed during upgrade",
    ):
        upgrade(request)

    assert cleanup_calls == ["replacement-runtime-handle"]
    assert candidate.deployed is False
    assert DeploymentStateStore(candidate.state_path).read().deployed is False
    assert DeploymentStateStore(current.state_path).read() == competing
    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED
    old_events = [event.event for event in current.events()]
    new_events = [event.event for event in candidate.events()]
    assert old_events.count("upgrade_failed") == 1
    assert "old_instance_superseded" not in old_events
    assert "upgrade_completed" not in new_events


def test_two_concurrent_upgrades_admit_only_one_and_clean_losing_candidate(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current_stop_calls: list[str] = []

    class CurrentRuntime:
        def stop(self, handle: str):
            current_stop_calls.append(handle)
            return {"status": "stopped"}

    current = _real_deployment(
        tmp_path,
        current_record,
        runtime=CurrentRuntime(),
        handles=("old-runtime-handle",),
    )
    replacement_one = _request(tmp_path, "c" * 64)
    replacement_two = _request(tmp_path, "d" * 64)
    request_one = UpgradeRequest(
        current, replacement_one, derive_upgrade_id(current, replacement_one)
    )
    request_two = UpgradeRequest(
        current, replacement_two, derive_upgrade_id(current, replacement_two)
    )

    cleanup_calls: list[str] = []
    cleanup_lock = threading.Lock()

    class CandidateRuntime:
        def stop(self, handle: str):
            with cleanup_lock:
                cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate_one = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="candidate-one"),
        runtime=CandidateRuntime(),
        handles=("candidate-one-handle",),
    )
    candidate_two = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="d" * 64, deployment_id="candidate-two"),
        runtime=CandidateRuntime(),
        handles=("candidate-two-handle",),
    )
    candidates = {
        "c" * 64: candidate_one,
        "d" * 64: candidate_two,
    }
    barrier = threading.Barrier(2, timeout=10.0)

    def synchronized_deploy(replacement_request, **_kwargs):
        chosen = candidates[replacement_request.instance.instance_digest]
        barrier.wait()
        return chosen

    monkeypatch.setattr(upgrade_module, "deploy", synchronized_deploy)

    outcomes: dict[str, object] = {}

    def run_upgrade(name: str, req: UpgradeRequest) -> None:
        try:
            outcomes[name] = upgrade(req)
        except Exception as error:  # noqa: BLE001 - asserted below
            outcomes[name] = error

    thread_one = threading.Thread(
        target=run_upgrade, args=("one", request_one), daemon=True
    )
    thread_two = threading.Thread(
        target=run_upgrade, args=("two", request_two), daemon=True
    )
    thread_one.start()
    thread_two.start()
    thread_one.join(timeout=10.0)
    thread_two.join(timeout=10.0)
    assert not thread_one.is_alive() and not thread_two.is_alive()

    successes = [value for value in outcomes.values() if isinstance(value, Deployment)]
    failures = [value for value in outcomes.values() if isinstance(value, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], InvalidDeploymentStateTransition)

    winner = successes[0]
    loser = candidate_two if winner is candidate_one else candidate_one
    loser_handle = (
        "candidate-two-handle" if winner is candidate_one else "candidate-one-handle"
    )
    assert current_stop_calls == ["old-runtime-handle"]
    assert cleanup_calls == [loser_handle]
    assert winner.deployed is True
    assert DeploymentStateStore(winner.state_path).read().deployed is True
    assert loser.deployed is False
    assert DeploymentStateStore(loser.state_path).read().deployed is False

    persisted_current = DeploymentStateStore(current.state_path).read()
    assert persisted_current.lifecycle == LIFECYCLE_SUPERSEDED
    assert persisted_current.deployed is False
    superseded_actions = [
        action
        for action in persisted_current.operational_actions
        if action.name == "platform_superseded"
    ]
    assert len(superseded_actions) == 1
    assert (
        superseded_actions[0].detail["replacement_deployment_id"]
        == winner.record.deployment_id
    )
    assert (
        superseded_actions[0].detail["replacement_instance_digest"]
        == winner.record.instance_digest
    )


def test_concurrent_stop_of_current_handle_during_deploy_fails_closed_and_cleans_candidate(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )

    def deploy_while_current_stops(*_args, **_kwargs):
        current.stop()
        return candidate

    monkeypatch.setattr(upgrade_module, "deploy", deploy_while_current_stops)

    with pytest.raises(
        InvalidDeploymentStateTransition,
        match="current deployment state changed during upgrade",
    ):
        upgrade(request)

    persisted_current = DeploymentStateStore(current.state_path).read()
    assert cleanup_calls == ["replacement-runtime-handle"]
    assert candidate.deployed is False
    assert DeploymentStateStore(candidate.state_path).read().deployed is False
    assert persisted_current.lifecycle == LIFECYCLE_REALIZED
    assert persisted_current.running is False
    assert persisted_current.ready is False
    assert persisted_current.deployed is False
    assert current.deployed is False
    assert not any(
        action.name == "platform_superseded"
        for action in persisted_current.operational_actions
    )
    old_events = [event.event for event in current.events()]
    new_events = [event.event for event in candidate.events()]
    assert old_events.count("upgrade_failed") == 1
    assert "old_instance_superseded" not in old_events
    assert "upgrade_completed" not in new_events


def test_candidate_cleanup_stop_failure_withdraws_candidate_readiness_and_preserves_causal_error(
    tmp_path: Path, monkeypatch
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )

    class FailingCandidateRuntime:
        def stop(self, _handle: str):
            raise RuntimeError("candidate runtime cleanup failed")

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=FailingCandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)
    monkeypatch.setattr(
        current,
        "release",
        lambda *, origin: (_ for _ in ()).throw(
            RuntimeError("old runtime refused to release")
        ),
    )

    with pytest.raises(RuntimeError, match="old runtime refused to release"):
        upgrade(request)

    persisted_current = DeploymentStateStore(current.state_path).read()
    persisted_candidate = DeploymentStateStore(candidate.state_path).read()
    assert persisted_current == current_record
    assert current.deployed is True
    assert candidate.deployed is False
    assert persisted_candidate.deployed is False
    assert persisted_candidate.ready is False
    assert any(
        action.name == "readiness_withdrawn"
        for action in persisted_candidate.operational_actions
    ), "the failed candidate cleanup states what it knows, never a stop"


@pytest.mark.parametrize("candidate_has_store", [True, False])
def test_candidate_cleanup_state_write_failure_is_recorded_and_removes_false_deployed_claim(
    tmp_path: Path, monkeypatch, candidate_has_store: bool
):
    current_record = _record(
        tmp_path, instance="a" * 64, deployment_id="old-deployment"
    )
    current = _real_deployment(tmp_path, current_record)
    replacement = _request(tmp_path, "c" * 64)
    request = UpgradeRequest(
        current, replacement, derive_upgrade_id(current, replacement)
    )
    cleanup_calls: list[str] = []

    class CandidateRuntime:
        def stop(self, handle: str):
            cleanup_calls.append(handle)
            return {"status": "stopped"}

    candidate = _real_deployment(
        tmp_path,
        _record(tmp_path, instance="c" * 64, deployment_id="new-deployment"),
        runtime=CandidateRuntime(),
        handles=("replacement-runtime-handle",),
    )
    if not candidate_has_store:
        candidate._store = None

    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)
    monkeypatch.setattr(
        current,
        "release",
        lambda *, origin: (_ for _ in ()).throw(
            RuntimeError("old runtime refused to release")
        ),
    )

    original_write = DeploymentStateStore.write
    cleanup_write_attempts: list[DeploymentRecord] = []

    def fail_candidate_cleanup_write(store, record, *, secrets=()):
        if store.path == candidate.state_path and not record.deployed:
            cleanup_write_attempts.append(record)
            raise OSError("injected candidate cleanup state write failure")
        return original_write(store, record, secrets=secrets)

    monkeypatch.setattr(DeploymentStateStore, "write", fail_candidate_cleanup_write)

    with pytest.raises(
        RuntimeError, match="old runtime refused to release"
    ) as exc_info:
        upgrade(request)

    assert len(cleanup_write_attempts) == 1
    assert cleanup_calls == ["replacement-runtime-handle"]
    assert candidate.deployed is False
    assert candidate.record.deployed is False
    assert (
        candidate.record.running is True
    ), "the aborted candidate was released, not stopped"
    assert candidate.record.ready is False
    assert not candidate.state_path.exists()
    with pytest.raises(DeploymentStateError):
        DeploymentStateStore(candidate.state_path).read()

    notes = getattr(exc_info.value, "__notes__", ())
    assert any(
        "candidate cleanup state persistence failed" in note
        and "injected candidate cleanup state write failure" in note
        for note in notes
    )

    persisted_current = DeploymentStateStore(current.state_path).read()
    assert persisted_current == current_record
    assert persisted_current.deployed is True
    assert current.deployed is True

    old_events = current.events()
    new_events = candidate.events()
    assert [event.event for event in old_events] == [
        "upgrade_requested",
        "upgrade_failed",
    ]
    failed_event = old_events[-1]
    assert failed_event.detail["candidate_cleanup_persistence_failed"] is True
    assert failed_event.detail["candidate_cleanup_error_type"] == "OSError"
    assert (
        "injected candidate cleanup state write failure"
        in failed_event.detail["candidate_cleanup_error"]
    )
    assert not any(event.event == "old_instance_superseded" for event in old_events)
    assert not any(event.event == "upgrade_completed" for event in new_events)


def _identifiers_and_literals(module) -> set[str]:
    """Every identifier and string literal a module's code uses."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def test_the_hand_over_paths_never_record_or_publish_a_stop():
    """Structural guard: the detach paths claim no stopped platform.

    An upgrade and a rollback hand the platform over with the detach the runtime
    contract gives them. Neither module may reach for a stop — not the plain
    ``Deployment.stop`` claim, not ``mark_stopped``, not the ``platform_stopped``
    action and not ``EVENT_PLATFORM_STOPPED`` — and both must go through the
    release helper, so this guard cannot pass vacuously.
    """
    for module in (upgrade_module, rollback_module):
        used = _identifiers_and_literals(module)
        assert "stop" not in used, module.__name__
        assert "mark_stopped" not in used, module.__name__
        assert "EVENT_PLATFORM_STOPPED" not in used, module.__name__
        assert "platform_stopped" not in used, module.__name__
        assert "release_references" in used, (
            f"{module.__name__} must hand the platform over through the detach "
            "helper instead of stopping anything"
        )
        assert "platform_released" in used, module.__name__
