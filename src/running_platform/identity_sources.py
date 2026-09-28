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


RuntimeHandle = object


class RuntimeHandleObserver(Protocol):
    """Owner-side observer of one already-running runtime handle."""

    def observe_runtime_handle(
        self,
        handle: RuntimeHandle,
    ) -> ActualEvidence:
        """Return independently observed component identity evidence."""
        ...


@dataclass(frozen=True)
class RuntimeMembershipSource:
    """Observe complete membership from an owner-controlled runtime registry.

    The registry callback is the source of the *complete* set of running
    handles. The observer callback turns each handle into independently
    observed component identity evidence. Neither callback receives expected
    deployment state; the source only preserves and validates actual facts.
    """

    list_handles: object
    observer: RuntimeHandleObserver

    def observe_membership(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return complete actual membership or fail closed."""
        if not callable(self.list_handles):
            raise ValueError("runtime membership registry is unavailable")
        try:
            handles = tuple(self.list_handles(binding))
        except Exception as error:
            raise ValueError("runtime membership registry is unavailable") from error

        components: list[ActualComponentIdentity] = []
        evidence_by_component: dict[str, ActualEvidence] = {}
        correlation = None
        freshness = None
        for handle in handles:
            evidence = self.observer.observe_runtime_handle(handle)
            if not isinstance(evidence, ActualEvidence):
                raise ValueError("runtime observer returned invalid evidence")
            if not isinstance(evidence.value, ActualComponentIdentity):
                raise ValueError("runtime observer returned invalid component identity")
            if correlation is None:
                correlation = evidence.correlation
                freshness = evidence.freshness
            elif not evidence.correlation.matches(correlation):
                raise ValueError("runtime membership evidence has mixed correlation")
            if evidence.freshness.current is not True:
                raise ValueError("runtime membership evidence is stale")
            if evidence.provenance != next(iter(evidence_by_component.values())).provenance if evidence_by_component else False:
                raise ValueError("runtime membership evidence has mixed provenance")
            component = evidence.value
            if component.component_id in evidence_by_component:
                raise ValueError(
                    f"runtime membership contains duplicate component "
                    f"{component.component_id!r}"
                )
            evidence_by_component[component.component_id] = evidence
            components.append(component)

        if not handles:
            raise ValueError("runtime membership is empty")
        if correlation is None or freshness is None:
            raise ValueError("runtime membership has no evidence")

        return ActualEvidence(
            value=ActualMembership(
                established=True,
                components=tuple(components),
            ),
            provenance=next(iter(evidence_by_component.values())).provenance,
            correlation=correlation,
            freshness=freshness,
        )


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
    "RuntimeHandleObserver",
    "RuntimeMembershipSource",
]
