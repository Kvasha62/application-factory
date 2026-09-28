"""D&O-side consumer contract for Running Platform identity correspondence.

This module validates independently supplied actual evidence and projects it
through the existing Platform Instance canonicalization. It does not discover
platform state, does not read expected deployment state as actual state, and
does not implement a production Running Platform identity provider.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from deployment_operations.environment import IDENTITY_CONFIGURATION_KEYS
from platform_manifest.lifecycle import LIFECYCLE_STATES
from platform_manifest.validation import (
    ARTIFACT_TYPES,
    MANIFEST_ID_PATTERN,
    SHA256_NULLABLE_PATTERN,
    SHA256_PATTERN,
    parse_semver,
)

T = TypeVar("T")
Document = Mapping[str, Any]
_MEASURED_DIGEST = re.compile(SHA256_NULLABLE_PATTERN)
_ARTIFACT_IDENTITY_KEYS = frozenset(
    {"artifact_type", "digest", "pinned", "canonical_form"}
)
_SEALED_CANONICAL_FORMS = {
    "source_package": "source_package/v1",
    "container_image": "container_image/v1",
}


class EvidenceProvenance:
    """Normative provenance classes. DERIVED is not a provenance class."""

    MEASURED = "MEASURED"
    TRANSITIVE = "TRANSITIVE"
    ATTESTED = "ATTESTED"


class PresenceState:
    """Explicit presence. UNKNOWN is never absence and never an expected value."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class IdentityCorrespondenceResult:
    """Correspondence of an independently computed D_actual to D_expected."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


class ActualIdentityUnavailable(Exception):
    """Complete independently grounded D_actual cannot be established."""


@dataclass(frozen=True)
class IdentityField:
    """An identity-bearing value with explicit presence semantics."""

    state: PresenceState
    value: Any = None

    @classmethod
    def present(cls, value: Any) -> IdentityField:
        if value is None:
            raise ValueError("PRESENT identity field requires a value")
        return cls(PresenceState.PRESENT, value)

    @classmethod
    def absent(cls) -> IdentityField:
        return cls(PresenceState.ABSENT, None)

    @classmethod
    def unknown(cls) -> IdentityField:
        return cls(PresenceState.UNKNOWN, None)


@dataclass(frozen=True)
class PlatformIdentityBinding:
    """Opaque evaluation target.

    D&O does not interpret the token. The binding carries no expected
    Platform Instance identity and no digest.
    """

    token: object


@dataclass(frozen=True)
class EvidenceCorrelation:
    """Opaque correlation of evidence to one Running Platform evaluation."""

    token: object

    def matches(self, other: EvidenceCorrelation) -> bool:
        return self.token == other.token


@dataclass(frozen=True)
class EvidenceFreshness:
    """Producer-established freshness. The mechanism is not selected here."""

    current: bool


@dataclass(frozen=True)
class ActualEvidence:
    """Actual value plus the proof envelope required before projection."""

    value: T
    provenance: EvidenceProvenance
    correlation: EvidenceCorrelation
    freshness: EvidenceFreshness


@dataclass(frozen=True)
class ActualComponentIdentity:
    """Minimum actual identity of one component. No health or runtime handle."""

    component_id: str
    component_version: str
    artifact_identity: Document | None


@dataclass(frozen=True)
class PlatformIdentitySurface:
    """Actual identity-bearing content. Not a Platform Instance document."""

    platform_id: str
    manifest: Document
    manifest_state: str
    components: tuple[ActualEvidence, ...]
    membership_established: bool
    configuration: IdentityField
    golden_bundle: IdentityField
    golden_bundle_inventory_established: bool
    extensions: IdentityField
    branding: IdentityField


class PlatformIdentityProvider(Protocol):
    """Consumer-side protocol. No production provider is supplied in this slice."""

    def observe_identity(
        self,
        binding: PlatformIdentityBinding,
    ) -> ActualEvidence:
        """Return actual identity evidence for the bound Running Platform."""
        ...


def validate_actual_evidence(evidence: ActualEvidence) -> ActualEvidence:
    """Validate the envelope and surface, failing closed."""

    if not isinstance(evidence, ActualEvidence):
        raise ActualIdentityUnavailable("identity evidence has an invalid envelope")
    _require_provenance(evidence.provenance)
    if not isinstance(evidence.correlation, EvidenceCorrelation):
        raise ActualIdentityUnavailable("identity evidence has invalid correlation")
    if not isinstance(evidence.freshness, EvidenceFreshness):
        raise ActualIdentityUnavailable("identity evidence has invalid freshness")
    if evidence.freshness.current is not True:
        raise ActualIdentityUnavailable("identity evidence is stale")
    validate_actual_surface(evidence.value, evidence.correlation)
    return evidence


def validate_actual_surface(
    surface: PlatformIdentitySurface,
    envelope_correlation: EvidenceCorrelation | None = None,
) -> None:
    """Validate actual identity facts before projection."""

    if not isinstance(surface, PlatformIdentitySurface):
        raise ActualIdentityUnavailable("actual identity surface has invalid type")
    if not isinstance(surface.platform_id, str) or not surface.platform_id:
        raise ActualIdentityUnavailable("actual platform_id is unavailable")
    _validate_manifest(surface.manifest)
    _validate_manifest_state(surface.manifest_state)
    if surface.membership_established is not True:
        raise ActualIdentityUnavailable(
            "actual component membership is not established"
        )
    _validate_components(surface.components, envelope_correlation)
    for name in ("configuration", "golden_bundle", "extensions", "branding"):
        _validate_identity_field(name, getattr(surface, name))
    _validate_configuration_consistency(surface)
    _validate_golden_bundle_inventory(surface)


def project_actual_identity(evidence: ActualEvidence) -> dict[str, Any]:
    """Project validated actual evidence into existing Instance content shape.

    Expected values are not consulted. ``instance_digest`` is not added.
    Source evidence is not mutated.
    """

    surface = validate_actual_evidence(evidence).value
    document: dict[str, Any] = {
        "platform_id": surface.platform_id,
        "manifest": _copy_value(dict(surface.manifest)),
        "manifest_state": surface.manifest_state,
        "golden_bundle": _project_required_field(
            "golden_bundle", surface.golden_bundle
        ),
        "components": [
            _project_component(component.value) for component in surface.components
        ],
    }
    for name in ("configuration", "extensions", "branding"):
        field = getattr(surface, name)
        if field.state is PresenceState.UNKNOWN:
            raise ActualIdentityUnavailable(
                f"{name} is UNKNOWN and is not actual absence"
            )
        if field.state is PresenceState.PRESENT:
            document[name] = _copy_value(field.value)
    return document


def compute_actual_digest(evidence: ActualEvidence) -> str:
    """Compute D_actual with the existing Platform Instance canonicalizer."""

    from platform_instance.instance import compute_instance_digest

    return compute_instance_digest(project_actual_identity(evidence))


def establish_identity_correspondence(
    expected_instance: Document,
    evidence: ActualEvidence,
) -> IdentityCorrespondenceResult:
    """Compare independently computed D_actual with the expected digest only.

    No expected field is copied into the actual projection. A missing or
    unusable expected digest is unavailable, not a match.
    """

    try:
        actual_digest = compute_actual_digest(evidence)
    except (ActualIdentityUnavailable, TypeError, ValueError, KeyError):
        return IdentityCorrespondenceResult.UNAVAILABLE
    expected_digest = expected_instance.get("instance_digest")
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(SHA256_PATTERN, expected_digest) is None
    ):
        return IdentityCorrespondenceResult.UNAVAILABLE
    if actual_digest == expected_digest:
        return IdentityCorrespondenceResult.MATCH
    return IdentityCorrespondenceResult.MISMATCH


def _require_provenance(provenance: object) -> None:
    if provenance not in (
        EvidenceProvenance.MEASURED,
        EvidenceProvenance.TRANSITIVE,
        EvidenceProvenance.ATTESTED,
    ):
        raise ActualIdentityUnavailable(
            "identity evidence has invalid or non-normative provenance"
        )


def _validate_manifest(manifest: object) -> None:
    if not isinstance(manifest, Mapping):
        raise ActualIdentityUnavailable("actual manifest is unavailable")
    manifest_id = manifest.get("manifest_id")
    if (
        not isinstance(manifest_id, str)
        or re.fullmatch(MANIFEST_ID_PATTERN, manifest_id) is None
    ):
        raise ActualIdentityUnavailable("actual manifest_id is invalid")
    manifest_version = manifest.get("manifest_version")
    if parse_semver(manifest_version) is None:
        raise ActualIdentityUnavailable("actual manifest_version is invalid")
    manifest_digest = manifest.get("manifest_digest")
    if (
        not isinstance(manifest_digest, str)
        or re.fullmatch(SHA256_PATTERN, manifest_digest) is None
    ):
        raise ActualIdentityUnavailable("actual manifest_digest is invalid")


def _validate_components(
    components: object,
    envelope_correlation: EvidenceCorrelation | None,
) -> None:
    if not isinstance(components, tuple):
        raise ActualIdentityUnavailable(
            "actual components must preserve multiplicity until validation"
        )
    if not components:
        raise ActualIdentityUnavailable("actual components must not be empty")
    seen: set[str] = set()
    shared_correlation: EvidenceCorrelation | None = None
    for component_evidence in components:
        if not isinstance(component_evidence, ActualEvidence):
            raise ActualIdentityUnavailable("actual component evidence is invalid")
        _require_provenance(component_evidence.provenance)
        if not isinstance(component_evidence.correlation, EvidenceCorrelation):
            raise ActualIdentityUnavailable("actual component correlation is invalid")
        if shared_correlation is None:
            shared_correlation = component_evidence.correlation
        elif not component_evidence.correlation.matches(shared_correlation):
            raise ActualIdentityUnavailable("actual component correlation is mixed")
        if (
            envelope_correlation is not None
            and not component_evidence.correlation.matches(envelope_correlation)
        ):
            raise ActualIdentityUnavailable(
                "actual component correlation does not match the evaluation"
            )
        if (
            not isinstance(component_evidence.freshness, EvidenceFreshness)
            or component_evidence.freshness.current is not True
        ):
            raise ActualIdentityUnavailable(
                "required actual component evidence is stale"
            )
        identity = component_evidence.value
        if not isinstance(identity, ActualComponentIdentity):
            raise ActualIdentityUnavailable("actual component record is invalid")
        if not isinstance(identity.component_id, str) or not identity.component_id:
            raise ActualIdentityUnavailable("actual component_id is unavailable")
        if identity.component_id in seen:
            raise ActualIdentityUnavailable(
                f"duplicate actual component_id: {identity.component_id}"
            )
        seen.add(identity.component_id)
        if (
            not isinstance(identity.component_version, str)
            or not identity.component_version
        ):
            raise ActualIdentityUnavailable(
                f"actual component_version is unavailable for {identity.component_id}"
            )
        _require_artifact(identity.component_id, identity.artifact_identity)


def _require_artifact(component_id: str, artifact: object) -> None:
    if not isinstance(artifact, Mapping):
        raise ActualIdentityUnavailable(
            f"artifact identity is unavailable for {component_id}"
        )
    identity = {
        key: value for key, value in artifact.items() if key != "execution_digest"
    }
    if set(identity) != _ARTIFACT_IDENTITY_KEYS:
        raise ActualIdentityUnavailable(
            f"artifact identity is incomplete for {component_id}"
        )
    artifact_type = identity["artifact_type"]
    if artifact_type == "none":
        if (
            identity["digest"] is not None
            or identity["pinned"] is not False
            or identity["canonical_form"] is not None
        ):
            raise ActualIdentityUnavailable(
                f"artifact_type none is not proved absence for {component_id}"
            )
        return
    if artifact_type not in ARTIFACT_TYPES:
        raise ActualIdentityUnavailable(
            f"artifact identity is contradictory for {component_id}"
        )
    digest = identity["digest"]
    if not isinstance(digest, str) or _MEASURED_DIGEST.fullmatch(digest) is None:
        raise ActualIdentityUnavailable(
            f"sealed artifact digest is unavailable for {component_id}"
        )
    if identity["pinned"] is not True or (
        identity["canonical_form"] != _SEALED_CANONICAL_FORMS.get(artifact_type)
    ):
        raise ActualIdentityUnavailable(
            f"sealed artifact identity is contradictory for {component_id}"
        )


def _validate_identity_field(name: str, field: object) -> None:
    if not isinstance(field, IdentityField) or field.state not in (
        PresenceState.PRESENT,
        PresenceState.ABSENT,
        PresenceState.UNKNOWN,
    ):
        raise ActualIdentityUnavailable(f"{name} has invalid presence semantics")
    if field.state in (PresenceState.UNKNOWN, PresenceState.ABSENT):
        if field.value is not None:
            raise ActualIdentityUnavailable(
                f"{name} {field.state} must not carry a value"
            )
        return
    if field.state is PresenceState.PRESENT and field.value is None:
        raise ActualIdentityUnavailable(f"{name} PRESENT requires a value")


def _validate_manifest_state(state: object) -> None:
    if not isinstance(state, str) or state not in LIFECYCLE_STATES:
        raise ActualIdentityUnavailable(
            "actual manifest_state is not a Platform Manifest lifecycle state"
        )


def _validate_configuration_consistency(surface: PlatformIdentitySurface) -> None:
    field = surface.configuration
    if field.state is not PresenceState.PRESENT:
        return
    if not isinstance(field.value, Mapping):
        raise ActualIdentityUnavailable("actual configuration is incomplete")
    for section in field.value.values():
        if not isinstance(section, Mapping):
            continue
        for key in IDENTITY_CONFIGURATION_KEYS:
            if key not in section:
                continue
            if section[key] != surface.platform_id:
                raise ActualIdentityUnavailable(
                    "actual configuration identity contradicts actual platform_id"
                )


def _validate_golden_bundle_inventory(surface: PlatformIdentitySurface) -> None:
    if surface.golden_bundle_inventory_established is not True:
        raise ActualIdentityUnavailable("actual Golden Bundle inventory is incomplete")
    if surface.golden_bundle.state is PresenceState.UNKNOWN:
        raise ActualIdentityUnavailable(
            "golden_bundle is UNKNOWN and is not actual absence"
        )


def _project_required_field(name: str, field: IdentityField) -> Any:
    if field.state is PresenceState.UNKNOWN:
        raise ActualIdentityUnavailable(f"{name} is UNKNOWN and is not actual absence")
    if field.state is PresenceState.ABSENT:
        return None
    return _copy_value(field.value)


def _project_component(identity: ActualComponentIdentity) -> dict[str, Any]:
    artifact = identity.artifact_identity
    if not isinstance(artifact, Mapping):
        raise ActualIdentityUnavailable(
            f"artifact identity is unavailable for {identity.component_id}"
        )
    projected_artifact = {
        key: _copy_value(value)
        for key, value in artifact.items()
        if key != "execution_digest"
    }
    return {
        "component_id": identity.component_id,
        "component_version": identity.component_version,
        "artifact": projected_artifact,
    }


def _copy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, tuple):
        return [_copy_value(item) for item in value]
    return value


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
