"""Concrete owner-side composition of actual Running Platform identity facts.

This module contains no expected deployment state and no Platform Instance
canonicalization. It composes observations from the six existing owner-side
source contracts into the existing :class:`ActualPlatformSnapshot` seam.

The state held here is owner state, not a second canonical identity model.
Expected identity is intentionally absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    ActualIdentityUnavailable,
    EvidenceCorrelation,
    EvidenceFreshness,
    EvidenceProvenance,
    IdentityField,
    PlatformIdentityBinding,
)
from deployment_operations.platform_identity_source import ActualPlatformSnapshot
from running_platform.identity_sources import (
    ActualGoldenBundle,
    ActualManifest,
    ActualMembership,
)


@dataclass(frozen=True)
class RunningPlatformOwnerState:
    """Actual owner-observed facts for one Running Platform evaluation.

    The fields deliberately reuse the existing source-contract value types.
    No expected instance, expected digest, deployment record, or D&O object is
    represented here.
    """

    platform_id: str
    membership: ActualMembership
    manifest: ActualManifest
    configuration: IdentityField
    golden_bundle: ActualGoldenBundle
    extensions: IdentityField
    branding: IdentityField
    provenance: EvidenceProvenance
    correlation_token: object
    freshness_current: bool


class OwnerStateReader(Protocol):
    """Owner-side reader for the current actual state."""

    def read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
        """Read actual owner state for the opaque evaluation binding."""
        ...


class OwnerStateSource:
    """Expose one concrete owner state through all six source contracts."""

    def __init__(self, reader: OwnerStateReader) -> None:
        self._reader = reader

    def _read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
        state = self._reader.read(binding)
        if not isinstance(state, RunningPlatformOwnerState):
            raise ActualIdentityUnavailable(
                "owner state reader returned invalid actual state"
            )
        return state

    def _evidence(
        self,
        state: RunningPlatformOwnerState,
        value: object,
    ) -> ActualEvidence:
        return ActualEvidence(
            value=value,
            provenance=state.provenance,
            correlation=EvidenceCorrelation(state.correlation_token),
            freshness=EvidenceFreshness(state.freshness_current),
        )

    def observe_membership(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        state = self._read(binding)
        return self._evidence(state, state.membership)

    def observe_manifest(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        state = self._read(binding)
        return self._evidence(state, state.manifest)

    def observe_configuration(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        state = self._read(binding)
        return self._evidence(state, state.configuration)

    def observe_golden_bundle(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        state = self._read(binding)
        return self._evidence(state, state.golden_bundle)

    def observe_extensions(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        state = self._read(binding)
        return self._evidence(state, state.extensions)

    def observe_branding(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        state = self._read(binding)
        return self._evidence(state, state.branding)


class OwnerStateSnapshotSource:
    """Build the existing snapshot seam from the six owner-side observations."""

    def __init__(self, sources: OwnerStateSource) -> None:
        self._sources = sources

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        membership = self._sources.observe_membership(binding)
        manifest = self._sources.observe_manifest(binding)
        configuration = self._sources.observe_configuration(binding)
        golden_bundle = self._sources.observe_golden_bundle(binding)
        extensions = self._sources.observe_extensions(binding)
        branding = self._sources.observe_branding(binding)

        evidence = (
            membership,
            manifest,
            configuration,
            golden_bundle,
            extensions,
            branding,
        )
        self._validate_shared_envelope(evidence)

        membership_value = membership.value
        manifest_value = manifest.value
        golden_value = golden_bundle.value
        if not isinstance(membership_value, ActualMembership):
            raise ActualIdentityUnavailable("membership source returned invalid state")
        if not isinstance(manifest_value, ActualManifest):
            raise ActualIdentityUnavailable("manifest source returned invalid state")
        if not isinstance(golden_value, ActualGoldenBundle):
            raise ActualIdentityUnavailable(
                "Golden Bundle source returned invalid state"
            )
        if not isinstance(manifest_value.manifest, dict):
            raise ActualIdentityUnavailable(
                "manifest source did not establish a concrete actual manifest"
            )

        platform_id = self._platform_id(membership_value.components, configuration)

        return ActualPlatformSnapshot(
            platform_id=platform_id,
            manifest=manifest_value.manifest,
            manifest_state=manifest_value.manifest_state,
            components=tuple(
                component.value
                for component in membership_value.components
                if isinstance(component, ActualComponentIdentity)
            ),
            membership_established=membership_value.established,
            configuration=configuration.value,
            golden_bundle=golden_value.identity,
            golden_bundle_inventory_established=golden_value.inventory_established,
            extensions=extensions.value,
            branding=branding.value,
            provenance=membership.provenance,
            correlation_token=membership.correlation.token,
            freshness_current=membership.freshness.current,
        )

    @staticmethod
    def _validate_shared_envelope(evidence: tuple[ActualEvidence, ...]) -> None:
        if not evidence:
            raise ActualIdentityUnavailable("owner identity source returned no evidence")

        first = evidence[0]
        for item in evidence:
            if not isinstance(item, ActualEvidence):
                raise ActualIdentityUnavailable("owner source returned invalid evidence")
            if item.provenance not in (
                EvidenceProvenance.MEASURED,
                EvidenceProvenance.TRANSITIVE,
                EvidenceProvenance.ATTESTED,
            ):
                raise ActualIdentityUnavailable(
                    "owner sources returned non-normative provenance"
                )
            if not item.correlation.matches(first.correlation):
                raise ActualIdentityUnavailable(
                    "owner sources returned mixed correlation"
                )
            if item.freshness.current is not True:
                raise ActualIdentityUnavailable("owner sources returned stale evidence")

    @staticmethod
    def _platform_id(
        components: tuple[ActualComponentIdentity, ...],
        configuration: ActualEvidence,
    ) -> str:
        if not components:
            raise ActualIdentityUnavailable("owner membership contains no components")
        if not isinstance(configuration.value, IdentityField):
            raise ActualIdentityUnavailable(
                "configuration source returned invalid identity field"
            )
        if configuration.value.state is IdentityField.absent().state:
            raise ActualIdentityUnavailable(
                "owner configuration does not establish platform identity"
            )
        if not isinstance(configuration.value.value, dict):
            raise ActualIdentityUnavailable(
                "owner configuration does not establish platform identity"
            )

        values: set[str] = set()
        for section in configuration.value.value.values():
            if not isinstance(section, dict):
                continue
            for key in ("platform_id", "current_platform_id"):
                value = section.get(key)
                if isinstance(value, str):
                    values.add(value)

        if len(values) != 1:
            raise ActualIdentityUnavailable(
                "owner configuration does not establish one actual platform_id"
            )
        return values.pop()


__all__ = [
    "OwnerStateReader",
    "OwnerStateSnapshotSource",
    "OwnerStateSource",
    "RunningPlatformOwnerState",
]
