"""Running Platform owner boundary.

This module owns the runtime boundary of the Running Platform. It deliberately
does not discover, reconstruct, or canonicalize Platform Identity. Owner-side
identity producers can publish an independently established
``PlatformIdentitySurface`` later; Deployment & Operations only controls the
owner lifecycle and queries the surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from deployment_operations.platform_identity import (
    ActualIdentityUnavailable,
    EvidenceCorrelation,
    PlatformIdentitySurface,
    validate_actual_surface,
)


class RunningPlatformOwnerError(RuntimeError):
    """The Running Platform owner cannot satisfy an ownership operation."""


@dataclass
class RunningPlatformOwner:
    """The minimal owner boundary for one Running Platform runtime.

    The owner has no dependency on Platform Instance expected state. In
    particular, it never receives an expected instance digest, expected
    component list, DeploymentRecord, or runtime binding.

    Identity data is intentionally absent until an owner-side producer
    establishes it. This class is the ownership/lifecycle seam only; the
    actual seven source capabilities belong to the follow-up Issue #94.
    """

    _started: bool = False
    _surface: PlatformIdentitySurface | None = None
    _observation_correlation: EvidenceCorrelation | None = None

    @property
    def started(self) -> bool:
        """Whether the Running Platform owner lifecycle is established."""
        return self._started

    def start(self) -> None:
        """Establish the Running Platform owner boundary."""
        self._started = True

    def stop(self) -> None:
        """Terminate the owner boundary and withdraw its observation."""
        self._surface = None
        self._observation_correlation = None
        self._started = False

    @property
    def observation_correlation(self) -> EvidenceCorrelation:
        """Return the correlation context of the current owner observation."""
        if not self._started or self._observation_correlation is None:
            raise ActualIdentityUnavailable(
                "Running Platform observation correlation is unavailable"
            )
        return self._observation_correlation

    def observe_identity(self) -> PlatformIdentitySurface:
        """Return the owner-established actual identity surface.

        No expected Platform Instance data is consulted. Until Issue #94
        supplies an owner-side producer, absence of a surface is an explicit
        unavailable result rather than fabricated identity.
        """
        if not self._started:
            raise ActualIdentityUnavailable(
                "Running Platform owner is not established"
            )
        if self._surface is None:
            raise ActualIdentityUnavailable(
                "Running Platform identity observation is unavailable"
            )
        return self._surface

    def publish_identity_surface(self, surface: PlatformIdentitySurface) -> None:
        """Publish an independently established owner-side identity surface.

        This is a producer seam, not a reconstruction mechanism. Validation
        checks the existing identity contract only; no expected identity or
        second digest is introduced here.
        """
        if not self._started:
            raise RunningPlatformOwnerError(
                "cannot publish identity before the Running Platform owner starts"
            )
        validate_actual_surface(surface)
        self._surface = surface
        self._observation_correlation = surface.components[0].correlation


__all__ = ["RunningPlatformOwner", "RunningPlatformOwnerError"]
