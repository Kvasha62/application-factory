"""Owner-side producer seam for Running Platform identity evidence.

This module is an adapter, not an identity authority. The concrete source
belongs to the Running Platform owner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from running_platform.identity_sources import (
    ActualGoldenBundle,
    ActualManifest,
    ActualMembership,
    BrandingSource,
    ConfigurationSource,
    ExtensionSource,
    GoldenBundleSource,
    ManifestSource,
    MembershipSource,
)
from deployment_operations.environment import IDENTITY_CONFIGURATION_KEYS
from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    ActualIdentityUnavailable,
    EvidenceCorrelation,
    EvidenceFreshness,
    IdentityField,
    PlatformIdentityBinding,
    PlatformIdentityProvider,
    PlatformIdentitySurface,
    PresenceState,
    validate_actual_evidence,
)
from running_platform.identity_sources import (
    ActualGoldenBundle,
    ActualManifest,
    ActualMembership,
    BrandingSource,
    ConfigurationSource,
    ExtensionSource,
    GoldenBundleSource,
    ManifestSource,
    MembershipSource,
)

Document = Mapping[str, object]


@dataclass(frozen=True)
class ActualPlatformSnapshot:
    """Owner-supplied actual identity snapshot.

    Components remain a tuple so multiplicity is preserved until validation.
    No expected Platform Instance or instance digest is part of this model.
    """

    platform_id: str
    manifest: Document
    manifest_state: str
    components: tuple[ActualComponentIdentity, ...]
    membership_established: bool
    configuration: IdentityField
    golden_bundle: IdentityField
    golden_bundle_inventory_established: bool
    extensions: IdentityField
    branding: IdentityField
    provenance: str
    correlation_token: object
    freshness_current: bool


@dataclass(frozen=True)
class ComposedRunningPlatformIdentitySource:
    """Compose the six owner sources into one complete actual snapshot.

    Every source is queried against the same opaque binding. The first source
    establishes the evaluation envelope; all other sources must carry the same
    correlation and current freshness. No source can supply expected state.
    """

    membership: MembershipSource
    manifest: ManifestSource
    configuration: ConfigurationSource
    golden_bundle: GoldenBundleSource
    extensions: ExtensionSource
    branding: BrandingSource

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        membership = self.membership.observe_membership(binding)
        manifest = self.manifest.observe_manifest(binding)
        configuration = self.configuration.observe_configuration(binding)
        golden_bundle = self.golden_bundle.observe_golden_bundle(binding)
        extensions = self.extensions.observe_extensions(binding)
        branding = self.branding.observe_branding(binding)

        evidence = (
            membership,
            manifest,
            configuration,
            golden_bundle,
            extensions,
            branding,
        )
        correlation = membership.correlation
        freshness = membership.freshness
        provenance = membership.provenance
        for item in evidence:
            if not item.correlation.matches(correlation):
                raise ActualIdentityUnavailable(
                    "owner identity sources returned mixed correlation"
                )
            if item.freshness.current is not True:
                raise ActualIdentityUnavailable(
                    "owner identity sources returned stale evidence"
                )
            if item.provenance != provenance:
                raise ActualIdentityUnavailable(
                    "owner identity sources returned mixed provenance"
                )

        membership_value = membership.value
        manifest_value = manifest.value
        golden_value = golden_bundle.value
        if not isinstance(membership_value, ActualMembership):
            raise ActualIdentityUnavailable("membership source returned invalid value")
        if not isinstance(manifest_value, ActualManifest):
            raise ActualIdentityUnavailable("manifest source returned invalid value")
        if not isinstance(manifest_value.manifest, Mapping):
            raise ActualIdentityUnavailable("manifest source returned invalid manifest")
        if not isinstance(golden_value, ActualGoldenBundle):
            raise ActualIdentityUnavailable(
                "Golden Bundle source returned invalid value"
            )
        if not isinstance(configuration.value, IdentityField):
            raise ActualIdentityUnavailable(
                "configuration source returned invalid value"
            )
        if not isinstance(extensions.value, IdentityField):
            raise ActualIdentityUnavailable("extension source returned invalid value")
        if not isinstance(branding.value, IdentityField):
            raise ActualIdentityUnavailable("branding source returned invalid value")

        return ActualPlatformSnapshot(
            platform_id=_platform_id_from_configuration(configuration.value),
            manifest=dict(manifest_value.manifest),
            manifest_state=manifest_value.manifest_state,
            components=membership_value.components,
            membership_established=membership_value.established,
            configuration=configuration.value,
            golden_bundle=golden_value.identity,
            golden_bundle_inventory_established=golden_value.inventory_established,
            extensions=extensions.value,
            branding=branding.value,
            provenance=provenance,
            correlation_token=correlation.token,
            freshness_current=freshness.current,
        )


def _platform_id_from_configuration(field: IdentityField) -> str:
    if field.state is not PresenceState.PRESENT or not isinstance(field.value, Mapping):
        raise ActualIdentityUnavailable(
            "actual platform_id requires identity-bearing configuration"
        )

    values: set[str] = set()
    for section in field.value.values():
        if not isinstance(section, Mapping):
            continue
        for key in IDENTITY_CONFIGURATION_KEYS:
            value = section.get(key)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise ActualIdentityUnavailable(
                        "actual platform_id is invalid"
                    )
                values.add(value)

    if len(values) != 1:
        raise ActualIdentityUnavailable(
            "actual platform_id is unavailable or contradictory"
        )
    return next(iter(values))


class RunningPlatformIdentitySource(Protocol):
    """Owner-side source of actual Running Platform identity facts."""

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        """Return one complete actual snapshot for the bound platform."""
        ...


class OwnerSuppliedPlatformIdentityProvider(PlatformIdentityProvider):
    """Adapt an owner-side snapshot source to the existing D&O contract."""

    def __init__(self, source: RunningPlatformIdentitySource) -> None:
        self._source = source

    def observe_identity(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        try:
            snapshot = self._source.observe(binding)
            if not isinstance(snapshot, ActualPlatformSnapshot):
                raise ActualIdentityUnavailable(
                    "owner identity source returned an invalid snapshot"
                )
            correlation = EvidenceCorrelation(snapshot.correlation_token)
            freshness = EvidenceFreshness(snapshot.freshness_current)
            components = tuple(
                ActualEvidence(
                    value=component,
                    provenance=snapshot.provenance,
                    correlation=correlation,
                    freshness=freshness,
                )
                for component in snapshot.components
            )
            surface = PlatformIdentitySurface(
                platform_id=snapshot.platform_id,
                manifest=snapshot.manifest,
                manifest_state=snapshot.manifest_state,
                components=components,
                membership_established=snapshot.membership_established,
                configuration=snapshot.configuration,
                golden_bundle=snapshot.golden_bundle,
                golden_bundle_inventory_established=(
                    snapshot.golden_bundle_inventory_established
                ),
                extensions=snapshot.extensions,
                branding=snapshot.branding,
            )
            return validate_actual_evidence(
                ActualEvidence(
                    value=surface,
                    provenance=snapshot.provenance,
                    correlation=correlation,
                    freshness=freshness,
                )
            )
        except ActualIdentityUnavailable:
            raise
        except Exception as error:
            raise ActualIdentityUnavailable(
                "owner identity source returned invalid actual evidence"
            ) from error


__all__ = [
    "ActualPlatformSnapshot",
    "ComposedRunningPlatformIdentitySource",
    "OwnerSuppliedPlatformIdentityProvider",
    "RunningPlatformIdentitySource",
]
