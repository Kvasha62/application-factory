"""D&O-side contract for independently established Running Platform identity.

This module is producer-neutral. It defines the evidence and projection
boundary required by ADR-0019/0020 without discovering platform state,
reading expected deployment state as actual state, or introducing a second
digest algorithm.
"""

from collections.abc import Mapping
from enum import StrEnum
from dataclasses import dataclass
from typing import Any, Protocol, Self


Document = Mapping[str, Any]
class EvidenceProvenance(StrEnum):
    """Normative provenance classes from ADR-0019/0020."""

    MEASURED = "MEASURED"
    TRANSITIVE = "TRANSITIVE"
    ATTESTED = "ATTESTED"


class PresenceState(StrEnum):
    """Presence semantics for identity-bearing optional values."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class IdentityCorrespondenceResult(StrEnum):
    """Result of independently establishing actual/expected correspondence."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class IdentityField[T]:
    """An identity-bearing value with explicit presence semantics."""

    state: PresenceState
    value: T | None = None

    @classmethod
    def present(cls, value: T) -> Self:
        if value is None:
            raise ValueError("PRESENT identity field requires a value")
        return cls(PresenceState.PRESENT, value)

    @classmethod
    def absent(cls) -> Self:
        return cls(PresenceState.ABSENT)

    @classmethod
    def unknown(cls) -> Self:
        return cls(PresenceState.UNKNOWN)


@dataclass(frozen=True)
class PlatformIdentityBinding:
    """Opaque target of one Running Platform identity evaluation.

    The token is deliberately not interpreted by D&O. It must not carry
    expected Platform Instance identity or a digest.
    """

    token: object


@dataclass(frozen=True)
class EvidenceCorrelation:
    """Opaque correlation established by the actual identity producer."""

    token: object

    def matches(self, other: Self) -> bool:
        return self.token == other.token


@dataclass(frozen=True)
class EvidenceFreshness:
    """Producer-established freshness result for one identity evaluation.

    The concrete mechanism is intentionally not selected in this slice.
    """

    current: bool


@dataclass(frozen=True)
class ActualEvidence[T]:
    """Actual value plus the proof envelope required by D&O."""

    value: T
    provenance: EvidenceProvenance
    correlation: EvidenceCorrelation
    freshness: EvidenceFreshness


@dataclass(frozen=True)
class ActualComponentIdentity:
    """Minimum identity content for one actual component."""

    component_id: str
    component_version: str
    artifact_identity: Document | None


@dataclass(frozen=True)
class PlatformIdentitySurface:
    """Actual identity-bearing content of one Running Platform."""

    platform_id: str
    manifest: Document
    manifest_state: str
    components: tuple[ActualComponentIdentity, ...]
    configuration: IdentityField[Any]
    golden_bundle: IdentityField[Any]
    extensions: IdentityField[Any]
    branding: IdentityField[Any]


class PlatformIdentityProvider(Protocol):
    """Owner-correct producer boundary consumed by D&O."""

    def observe_identity(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence[PlatformIdentitySurface]:
        """Return actual identity evidence for the bound Running Platform."""
        ...


class ActualIdentityUnavailable(ValueError):
    """Raised when a complete independently grounded D_actual cannot be built."""


def validate_actual_evidence(
    evidence: ActualEvidence[PlatformIdentitySurface],
) -> ActualEvidence[PlatformIdentitySurface]:
    """Validate the evidence envelope and actual surface, failing closed."""

    if not isinstance(evidence, ActualEvidence):
        raise ActualIdentityUnavailable("identity evidence has an invalid envelope")

    if evidence.provenance not in EvidenceProvenance:
        raise ActualIdentityUnavailable("identity evidence has invalid provenance")

    if not isinstance(evidence.correlation, EvidenceCorrelation):
        raise ActualIdentityUnavailable("identity evidence has invalid correlation")

    if not isinstance(evidence.freshness, EvidenceFreshness):
        raise ActualIdentityUnavailable("identity evidence has invalid freshness")

    if not evidence.freshness.current:
        raise ActualIdentityUnavailable("identity evidence is stale")

    validate_actual_surface(evidence.value)
    return evidence


def validate_actual_surface(surface: PlatformIdentitySurface) -> None:
    """Validate actual identity facts before projection."""

    if not isinstance(surface, PlatformIdentitySurface):
        raise ActualIdentityUnavailable("actual identity surface has invalid type")

    if not isinstance(surface.platform_id, str) or not surface.platform_id:
        raise ActualIdentityUnavailable("actual platform_id is unavailable")

    _validate_manifest(surface.manifest)
    if not isinstance(surface.manifest_state, str) or not surface.manifest_state:
        raise ActualIdentityUnavailable("actual manifest_state is unavailable")

    seen: set[str] = set()
    for component in surface.components:
        if not isinstance(component, ActualComponentIdentity):
            raise ActualIdentityUnavailable("actual component record is invalid")
        if not component.component_id:
            raise ActualIdentityUnavailable("actual component_id is unavailable")
        if component.component_id in seen:
            raise ActualIdentityUnavailable(
                f"duplicate actual component_id: {component.component_id!r}"
            )
        seen.add(component.component_id)
        if not component.component_version:
            raise ActualIdentityUnavailable(
                f"actual component_version is unavailable for "
                f"{component.component_id!r}"
            )
        if component.artifact_identity is not None and not isinstance(
            component.artifact_identity, Mapping
        ):
            raise ActualIdentityUnavailable(
                f"artifact identity is invalid for {component.component_id!r}"
            )

    for name in (
        "configuration",
        "golden_bundle",
        "extensions",
        "branding",
    ):
        _validate_identity_field(name, getattr(surface, name))


def _validate_manifest(manifest: Document) -> None:
    if not isinstance(manifest, Mapping):
        raise ActualIdentityUnavailable("actual manifest is unavailable")
    for key in ("manifest_id", "manifest_version", "manifest_digest"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value:
            raise ActualIdentityUnavailable(
                f"actual manifest {key!r} is unavailable"
            )


def _validate_identity_field(name: str, field: IdentityField[Any]) -> None:
    if not isinstance(field, IdentityField):
        raise ActualIdentityUnavailable(f"{name} has invalid presence semantics")
    if field.state in (PresenceState.UNKNOWN, PresenceState.ABSENT):
        if field.value is not None:
            raise ActualIdentityUnavailable(
                f"{name} {field.state.value} must not carry a value"
            )
    elif field.state is PresenceState.PRESENT and field.value is None:
        raise ActualIdentityUnavailable(f"{name} PRESENT requires a value")


def project_actual_identity(
    evidence: ActualEvidence[PlatformIdentitySurface],
) -> dict[str, Any]:
    """Project validated actual identity into the existing Instance shape.

    No expected-side value is consulted. instance_digest is intentionally
    absent because the existing canonicalizer derives it from content.
    """

    surface = validate_actual_evidence(evidence).value

    document: dict[str, Any] = {
        "platform_id": surface.platform_id,
        "manifest": dict(surface.manifest),
        "manifest_state": surface.manifest_state,
        "golden_bundle": _project_required_field(surface.golden_bundle),
        "components": [
            {
                "component_id": component.component_id,
                "component_version": component.component_version,
                "artifact": (
                    dict(component.artifact_identity)
                    if component.artifact_identity is not None
                    else None
                ),
            }
            for component in surface.components
        ],
    }

    for name in ("configuration", "extensions", "branding"):
        field = getattr(surface, name)
        if field.state is PresenceState.PRESENT:
            document[name] = field.value

    return document


def compute_actual_digest(
    evidence: ActualEvidence[PlatformIdentitySurface],
) -> str:
    """Compute D_actual using the existing Platform Instance canonicalizer."""

    from platform_instance.instance import compute_instance_digest

    return compute_instance_digest(project_actual_identity(evidence))


def establish_identity_correspondence(
    expected_instance: Document,
    evidence: ActualEvidence[PlatformIdentitySurface],
) -> IdentityCorrespondenceResult:
    """Compare independently projected D_actual with expected instance digest."""

    try:
        validated = validate_actual_evidence(evidence)
        expected_digest = expected_instance.get("instance_digest")
        if not isinstance(expected_digest, str) or not expected_digest:
            raise ActualIdentityUnavailable(
                "expected instance digest is unavailable"
            )
        actual_digest = compute_actual_digest(validated)
    except ActualIdentityUnavailable:
        return IdentityCorrespondenceResult.UNAVAILABLE

    if actual_digest == expected_digest:
        return IdentityCorrespondenceResult.MATCH
    return IdentityCorrespondenceResult.MISMATCH


def _project_required_field(field: IdentityField[Any]) -> Any:
    if field.state is PresenceState.UNKNOWN:
        raise ActualIdentityUnavailable("required identity field is UNKNOWN")
    if field.state is PresenceState.ABSENT:
        return None
    return field.value


__all__ = [
    "ActualComponentIdentity",
    "ActualEvidence",
    "ActualIdentityUnavailable",
    "EvidenceCorrelation",
    "EvidenceFreshness",
    "EvidenceProvenance",
    "IdentityCorrespondenceResult",
    "IdentityField",
    "PlatformIdentityBinding",
    "PlatformIdentityProvider",
    "PlatformIdentitySurface",
    "PresenceState",
    "compute_actual_digest",
    "establish_identity_correspondence",
    "project_actual_identity",
    "validate_actual_evidence",
    "validate_actual_surface",
]
