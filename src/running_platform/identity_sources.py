"""Owner-side contracts for independently grounded Running Platform identity.

This module defines the ownership boundary only. It does not discover state,
canonicalize Platform Instance documents, or consume expected deployment state.
Concrete source implementations belong to the Running Platform owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    IdentityField,
    PlatformIdentityBinding,
)


@dataclass(frozen=True)
class ActualMembership:
    """Complete actual component membership for one Running Platform."""

    established: bool
    components: tuple[ActualComponentIdentity, ...]


@dataclass(frozen=True)
class ActualManifest:
    """Actual manifest identity and lifecycle state."""

    manifest: dict[str, object]
    manifest_state: str


@dataclass(frozen=True)
class ActualGoldenBundle:
    """Actual Golden Bundle identity plus independently established inventory."""

    identity: IdentityField
    inventory_established: bool


class MembershipSource(Protocol):
    """Owner-side source for complete actual component membership."""

    def observe_membership(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return evidence whose value is :class:`ActualMembership`."""
        ...


class ManifestSource(Protocol):
    """Owner-side source for actual manifest identity and state."""

    def observe_manifest(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return evidence whose value is :class:`ActualManifest`."""
        ...


class ConfigurationSource(Protocol):
    """Owner-side source for actual identity-bearing configuration."""

    def observe_configuration(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return evidence whose value is :class:`IdentityField`."""
        ...


class GoldenBundleSource(Protocol):
    """Owner-side source for actual Golden Bundle identity and inventory."""

    def observe_golden_bundle(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return evidence whose value is :class:`ActualGoldenBundle`."""
        ...


class ExtensionSource(Protocol):
    """Owner-side source for actual extension inventory."""

    def observe_extensions(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return evidence whose value is :class:`IdentityField`."""
        ...


class BrandingSource(Protocol):
    """Owner-side source for actual branding identity."""

    def observe_branding(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return evidence whose value is :class:`IdentityField`."""
        ...


__all__ = [
    "ActualGoldenBundle",
    "ActualManifest",
    "ActualMembership",
    "BrandingSource",
    "ConfigurationSource",
    "ExtensionSource",
    "GoldenBundleSource",
    "ManifestSource",
    "MembershipSource",
]
