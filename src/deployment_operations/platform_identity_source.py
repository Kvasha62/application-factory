"""Owner-side producer seam for Running Platform identity evidence.

This module is an adapter, not an identity authority. The concrete source
belongs to the Running Platform owner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

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
    validate_actual_evidence,
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
    "OwnerSuppliedPlatformIdentityProvider",
    "RunningPlatformIdentitySource",
]
