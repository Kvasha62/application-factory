from __future__ import annotations

import pytest

from deployment_operations.platform_identity import ActualIdentityUnavailable
from running_platform.owner import RunningPlatformOwner, RunningPlatformOwnerError


def test_owner_lifecycle_is_independent_of_expected_platform_instance() -> None:
    owner = RunningPlatformOwner()

    owner.start()

    assert owner.started is True


def test_owner_has_no_identity_until_owner_side_observation_exists() -> None:
    owner = RunningPlatformOwner()
    owner.start()

    with pytest.raises(ActualIdentityUnavailable, match="observation is unavailable"):
        owner.observe_identity()


def test_owner_withdraws_observation_when_stopped() -> None:
    owner = RunningPlatformOwner()
    owner.start()
    owner.stop()

    assert owner.started is False
    with pytest.raises(ActualIdentityUnavailable, match="owner is not established"):
        owner.observe_identity()


def test_identity_cannot_be_published_before_owner_starts() -> None:
    owner = RunningPlatformOwner()

    with pytest.raises(RunningPlatformOwnerError, match="owner starts"):
        owner.publish_identity_surface(object())  # type: ignore[arg-type]
