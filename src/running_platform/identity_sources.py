"""Owner-side contracts for independently grounded Running Platform identity.

This module defines the ownership boundary only. It does not discover state,
canonicalize Platform Instance documents, or consume expected deployment state.
Concrete source implementations belong to the Running Platform owner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from deployment_operations.environment import IDENTITY_CONFIGURATION_KEYS
from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    IdentityField,
    PlatformIdentityBinding,
    PresenceState,
)

if TYPE_CHECKING:
    from deployment_operations.platform_identity_source import ActualPlatformSnapshot



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
            raise TypeError("runtime membership registry is unavailable")
        try:
            handles = tuple(self.list_handles(binding))
        except Exception as error:
            raise ValueError("runtime membership registry is unavailable") from error

        components: list[ActualComponentIdentity] = []
        evidence_by_component: dict[str, ActualEvidence] = {}
        correlation = None
        freshness = None
        provenance = None
        for handle in handles:
            evidence = self.observer.observe_runtime_handle(handle)
            if not isinstance(evidence, ActualEvidence):
                raise TypeError("runtime observer returned invalid evidence")
            if not isinstance(evidence.value, ActualComponentIdentity):
                raise TypeError("runtime observer returned invalid component identity")
            if correlation is None:
                correlation = evidence.correlation
                freshness = evidence.freshness
                provenance = evidence.provenance
            elif not evidence.correlation.matches(correlation):
                raise ValueError("runtime membership evidence has mixed correlation")
            if evidence.freshness.current is not True:
                raise ValueError("runtime membership evidence is stale")
            if evidence.provenance != provenance:
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
        if correlation is None or freshness is None or provenance is None:
            raise ValueError("runtime membership has no evidence")

        return ActualEvidence(
            value=ActualMembership(
                established=True,
                components=tuple(components),
            ),
            provenance=provenance,
            correlation=correlation,
            freshness=freshness,
        )


def _platform_id_from_configuration(field: IdentityField) -> str:
    if field.state is not PresenceState.PRESENT or not isinstance(field.value, Mapping):
        raise ValueError("actual platform_id requires identity-bearing configuration")
    values: set[str] = set()
    for section in field.value.values():
        if not isinstance(section, Mapping):
            continue
        for key in IDENTITY_CONFIGURATION_KEYS:
            value = section.get(key)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise ValueError("actual platform_id is invalid")
                values.add(value)
    if len(values) != 1:
        raise ValueError("actual platform_id is unavailable or contradictory")
    return next(iter(values))


@dataclass(frozen=True)
class ComposedRunningPlatformIdentitySource:
    """Compose owner sources into one complete actual platform snapshot."""

    membership: MembershipSource
    manifest: ManifestSource
    configuration: ConfigurationSource
    golden_bundle: GoldenBundleSource
    extensions: ExtensionSource
    branding: BrandingSource

    def observe(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualPlatformSnapshot:

        from deployment_operations.platform_identity_source import (
            ActualPlatformSnapshot,
        )

        evidence = (
            self.membership.observe_membership(binding),
            self.manifest.observe_manifest(binding),
            self.configuration.observe_configuration(binding),
            self.golden_bundle.observe_golden_bundle(binding),
            self.extensions.observe_extensions(binding),
            self.branding.observe_branding(binding),
        )
        membership, manifest, configuration, golden_bundle, extensions, branding = evidence
        correlation = membership.correlation
        provenance = membership.provenance
        freshness = membership.freshness
        for item in evidence:
            if not item.correlation.matches(correlation):
                raise ValueError("owner identity sources returned mixed correlation")
            if item.freshness.current is not True:
                raise ValueError("owner identity sources returned stale evidence")
            if item.provenance != provenance:
                raise ValueError("owner identity sources returned mixed provenance")

        if not isinstance(membership.value, ActualMembership):
            raise TypeError("membership source returned invalid value")
        if not isinstance(manifest.value, ActualManifest):
            raise TypeError("manifest source returned invalid value")
        if not isinstance(configuration.value, IdentityField):
            raise TypeError("configuration source returned invalid value")
        if not isinstance(golden_bundle.value, ActualGoldenBundle):
            raise TypeError("Golden Bundle source returned invalid value")
        if not isinstance(extensions.value, IdentityField):
            raise TypeError("extension source returned invalid value")
        if not isinstance(branding.value, IdentityField):
            raise TypeError("branding source returned invalid value")

        return ActualPlatformSnapshot(
            platform_id=_platform_id_from_configuration(configuration.value),
            manifest=dict(manifest.value.manifest),
            manifest_state=manifest.value.manifest_state,
            components=membership.value.components,
            membership_established=membership.value.established,
            configuration=configuration.value,
            golden_bundle=golden_bundle.value.identity,
            golden_bundle_inventory_established=golden_bundle.value.inventory_established,
            extensions=extensions.value,
            branding=branding.value,
            provenance=provenance,
            correlation_token=correlation.token,
            freshness_current=freshness.current,
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
    "ComposedRunningPlatformIdentitySource",
    "ConfigurationSource",
    "ExtensionSource",
    "GoldenBundleSource",
    "ManifestSource",
    "MembershipSource",
    "RuntimeHandleObserver",
    "RuntimeMembershipSource",
]
