"""Concrete owner-side composition of actual Running Platform identity facts.

This module composes the six existing owner-side source contracts into the
existing :class:`ActualPlatformSnapshot` seam. It contains no expected
deployment state and introduces no second canonical identity or digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from deployment_operations.platform_identity import (
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
    """Owner-observed facts for one Running Platform evaluation."""

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
    """Owner-side reader for current actual state."""

    def read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
        """Read actual owner state for the opaque evaluation binding."""
        ...


class OwnerStateSource:
    """Expose one owner state through the six existing source contracts."""

    def __init__(self, reader: OwnerStateReader) -> None:
        self._reader = reader

    def _read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
        state = self._reader.read(binding)
        if not isinstance(state, RunningPlatformOwnerState):
            raise ActualIdentityUnavailable(
                "owner state reader returned invalid actual state"
            )
        return state

    @staticmethod
    def _evidence(
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
    """Build the existing snapshot seam from one consistent owner observation."""

    def __init__(self, reader: OwnerStateReader) -> None:
        self._reader = reader

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        state = self._reader.read(binding)
        if not isinstance(state, RunningPlatformOwnerState):
            raise ActualIdentityUnavailable(
                "owner state reader returned invalid actual state"
            )

        source = OwnerStateSource(lambda_reader(state))
        observations = (
            source.observe_membership(binding),
            source.observe_manifest(binding),
            source.observe_configuration(binding),
            source.observe_golden_bundle(binding),
            source.observe_extensions(binding),
            source.observe_branding(binding),
        )
        self._validate_shared_envelope(observations)

        for name, observation in zip(
            ("configuration", "extensions", "branding"),
            observations[2:3] + observations[4:6],
        ):
            field = observation.value
            if not isinstance(field, IdentityField):
                raise ActualIdentityUnavailable(
                    f"{name} source returned invalid identity field"
                )
            if field.state.name == "UNKNOWN":
                raise ActualIdentityUnavailable(f"{name} identity evidence is unknown")

        membership = observations[0].value
        manifest = observations[1].value
        configuration = observations[2].value
        golden_bundle = observations[3].value

        if not isinstance(membership, ActualMembership):
            raise ActualIdentityUnavailable("membership source returned invalid state")
        if not isinstance(manifest, ActualManifest):
            raise ActualIdentityUnavailable("manifest source returned invalid state")
        if not isinstance(golden_bundle, ActualGoldenBundle):
            raise ActualIdentityUnavailable(
                "Golden Bundle source returned invalid state"
            )
        if any(
            not hasattr(component, "component_id")
            for component in membership.components
        ):
            raise ActualIdentityUnavailable(
                "membership source returned malformed component identity"
            )

        return ActualPlatformSnapshot(
            platform_id=state.platform_id,
            manifest=manifest.manifest,
            manifest_state=manifest.manifest_state,
            components=tuple(membership.components),
            membership_established=membership.established,
            configuration=configuration,
            golden_bundle=golden_bundle.identity,
            golden_bundle_inventory_established=golden_bundle.inventory_established,
            extensions=observations[4].value,
            branding=observations[5].value,
            provenance=observations[0].provenance,
            correlation_token=observations[0].correlation.token,
            freshness_current=observations[0].freshness.current,
        )

    @staticmethod
    def _validate_shared_envelope(evidence: tuple[ActualEvidence, ...]) -> None:
        if not evidence:
            raise ActualIdentityUnavailable(
                "owner identity source returned no evidence"
            )

        first = evidence[0]
        if first.provenance not in (
            EvidenceProvenance.MEASURED,
            EvidenceProvenance.TRANSITIVE,
            EvidenceProvenance.ATTESTED,
        ):
            raise ActualIdentityUnavailable(
                "owner sources returned non-normative provenance"
            )

        for item in evidence:
            if not isinstance(item, ActualEvidence):
                raise ActualIdentityUnavailable(
                    "owner source returned invalid evidence"
                )
            if item.provenance != first.provenance:
                raise ActualIdentityUnavailable(
                    "owner sources returned mixed provenance"
                )
            if not item.correlation.matches(first.correlation):
                raise ActualIdentityUnavailable(
                    "owner sources returned mixed correlation"
                )
            if item.freshness.current is not True:
                raise ActualIdentityUnavailable("owner sources returned stale evidence")


def lambda_reader(state: RunningPlatformOwnerState) -> OwnerStateReader:
    """Return a reader pinned to one already observed owner state."""

    class _Reader:
        def read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
            return state

    return _Reader()


__all__ = [
    "OwnerStateReader",
    "OwnerStateSnapshotSource",
    "OwnerStateSource",
    "RunningPlatformOwnerState",
]
