from pathlib import Path
from types import SimpleNamespace

import pytest

import deployment_operations.upgrade as upgrade_module
from deployment_operations.errors import (
    DeploymentInputRejected,
    InvalidDeploymentStateTransition,
)
from deployment_operations.state import (
    LIFECYCLE_SUPERSEDED,
    DeploymentRecord,
    DeploymentStateStore,
)
from deployment_operations.upgrade import UpgradeRequest, derive_upgrade_id, upgrade


def _record(tmp_path: Path, *, instance: str = "a" * 64) -> DeploymentRecord:
    record = DeploymentRecord.initial(
        deployment_id="old-deployment",
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
    return record.mark_realized(at="2026-09-30T00:03:00Z")


def _current(tmp_path: Path):
    record = _record(tmp_path)
    return SimpleNamespace(
        record=record,
        state_path=tmp_path / "old.json",
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
    request = UpgradeRequest(current, _request(tmp_path), "upgrade-1")

    with pytest.raises(InvalidDeploymentStateTransition):
        upgrade(request)


def test_upgrade_rejects_same_instance(tmp_path: Path):
    current = _current(tmp_path)
    request = UpgradeRequest(
        current, _request(tmp_path, "a" * 64), "upgrade-1"
    )

    with pytest.raises(DeploymentInputRejected):
        upgrade(request)


def test_failed_replacement_leaves_old_deployed(tmp_path: Path, monkeypatch):
    current = _current(tmp_path)
    request = UpgradeRequest(current, _request(tmp_path), "upgrade-1")

    def fail(*args, **kwargs):
        raise RuntimeError("replacement refused")

    monkeypatch.setattr(upgrade_module, "deploy", fail)

    with pytest.raises(RuntimeError, match="replacement refused"):
        upgrade(request)

    assert current.record.deployed is True
    assert current.record.lifecycle != LIFECYCLE_SUPERSEDED


def test_success_supersedes_old_only_after_new_deployed(tmp_path: Path, monkeypatch):
    current = _current(tmp_path)
    request = UpgradeRequest(current, _request(tmp_path), "upgrade-1")
    new_record = _record(tmp_path, instance="c" * 64)
    candidate = SimpleNamespace(record=new_record, deployed=True)
    monkeypatch.setattr(upgrade_module, "deploy", lambda *args, **kwargs: candidate)

    result = upgrade(request)

    assert result is candidate
    persisted = DeploymentStateStore(current.state_path).read()
    assert persisted.lifecycle == LIFECYCLE_SUPERSEDED
    assert persisted.deployed is False
    assert any(
        action.name == "platform_superseded"
        for action in persisted.operational_actions
    )
