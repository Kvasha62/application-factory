"""Concrete owner-side composition of actual Running Platform identity facts.

This module composes the six existing owner-side source contracts into the
existing :class:`ActualPlatformSnapshot` seam. It contains no expected
deployment state and introduces no second canonical identity or digest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
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
    PresenceState,
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


class FileRunningPlatformOwnerStateReader:
    """Read the owner-published actual identity surface for one evaluation.

    The document is Running Platform state, not deployment state. It contains
    no expected instance digest and is correlated to the opaque binding token.
    A missing, malformed, stale, or differently correlated surface fails closed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ActualIdentityUnavailable(
                "running platform identity surface is unavailable"
            ) from error
        if not isinstance(document, dict):
            raise ActualIdentityUnavailable("running platform identity surface is invalid")
        if "instance_digest" in document or "expected_instance" in document:
            raise ActualIdentityUnavailable(
                "running platform identity surface contains expected identity"
            )
        if document.get("correlation_token") != binding.token:
            raise ActualIdentityUnavailable(
                "running platform identity surface has stale or foreign correlation"
            )
        if document.get("freshness_current") is not True:
            raise ActualIdentityUnavailable("running platform identity surface is stale")
        provenance = document.get("provenance")
        if provenance not in (
            EvidenceProvenance.MEASURED,
            EvidenceProvenance.TRANSITIVE,
            EvidenceProvenance.ATTESTED,
        ):
            raise ActualIdentityUnavailable("running platform identity surface has invalid provenance")

        def field(name: str) -> IdentityField:
            value = document.get(name)
            if not isinstance(value, dict):
                raise ActualIdentityUnavailable(f"actual {name} evidence is unavailable")
            state = value.get("state")
            if state == PresenceState.PRESENT:
                return IdentityField.present(value.get("value"))
            if state == PresenceState.ABSENT:
                return IdentityField.absent()
            raise ActualIdentityUnavailable(f"actual {name} evidence is unavailable")

        raw_components = document.get("components")
        if not isinstance(raw_components, list):
            raise ActualIdentityUnavailable("actual component membership is unavailable")
        components: list[ActualComponentIdentity] = []
        for item in raw_components:
            if not isinstance(item, dict):
                raise ActualIdentityUnavailable("actual component membership is malformed")
            component_id = item.get("component_id")
            version = item.get("component_version")
            artifact = item.get("artifact_identity")
            if not isinstance(component_id, str) or not component_id:
                raise ActualIdentityUnavailable("actual component_id is unavailable")
            if not isinstance(version, str) or not version:
                raise ActualIdentityUnavailable("actual component_version is unavailable")
            if artifact is not None and not isinstance(artifact, dict):
                raise ActualIdentityUnavailable("actual artifact identity is malformed")
            components.append(ActualComponentIdentity(component_id, version, artifact))

        manifest = document.get("manifest")
        manifest_state = document.get("manifest_state")
        if not isinstance(manifest, dict) or not isinstance(manifest_state, str):
            raise ActualIdentityUnavailable("actual Manifest identity is unavailable")
        return RunningPlatformOwnerState(
            platform_id=str(document.get("platform_id", "")),
            membership=ActualMembership(
                document.get("membership_established") is True,
                tuple(components),
            ),
            manifest=ActualManifest(manifest, manifest_state),
            configuration=field("configuration"),
            golden_bundle=ActualGoldenBundle(
                field("golden_bundle"),
                document.get("golden_bundle_inventory_established") is True,
            ),
            extensions=field("extensions"),
            branding=field("branding"),
            provenance=provenance,
            correlation_token=document["correlation_token"],
            freshness_current=True,
        )


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
            if field.state is PresenceState.UNKNOWN:
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
    "FileRunningPlatformOwnerStateReader",
    "OwnerStateReader",
    "OwnerStateSnapshotSource",
    "OwnerStateSource",
    "RunningPlatformOwnerState",
]
