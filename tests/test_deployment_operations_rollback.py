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
        _journal=journal,
        _store=store,
    )


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
    assert request.current.record.deployed is False
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
