"""Actual Running Platform identity proof core (ADR-0019 / ADR-0020).

This module deliberately contains no discovery mechanism and no deployment
lifecycle integration. It validates independently supplied actual identity
evidence, projects it into the existing Platform Instance content shape, uses
the existing canonicalization to derive D_actual, and compares that digest
with an externally supplied D_expected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from platform_instance.instance import compute_instance_digest


class EvidenceProvenance(str, Enum):
    """How an actual identity fact was established."""

    MEASURED = "MEASURED"
    TRANSITIVE = "TRANSITIVE"
    ATTESTED = "ATTESTED"


class EvidenceState(str, Enum):
    """State of an independently supplied actual identity fact."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"
    STALE = "STALE"


class ProofStatus(str, Enum):
    """Result of actual-identity proof and correspondence."""

    VALID = "VALID"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


T = TypeVar("T")


@dataclass(frozen=True)
class Evidence(Generic[T]):
    """One independently established actual fact.

    None is never interpreted as "missing" by itself. The explicit state
    distinguishes actual absence from unavailable evidence and from conflict.
    """

    state: EvidenceState
    value: T | None = None
    provenance: EvidenceProvenance | None = None
    source: str | None = None
    correlation_id: str | None = None
    fresh: bool = True

    def valid(self) -> bool:
        """Return whether this fact is usable for identity proof."""
        if self.state is EvidenceState.PRESENT:
            return (
                self.value is not None
                and self.provenance is not None
                and bool(self.source)
                and bool(self.correlation_id)
                and self.fresh
            )
        if self.state is EvidenceState.ABSENT:
            return (
                self.value is None
                and self.provenance is not None
                and bool(self.source)
                and bool(self.correlation_id)
                and self.fresh
            )
        return False


@dataclass(frozen=True)
class ActualIdentityEvidence:
    """Complete actual identity evidence for one Running Platform."""

    platform_id: Evidence[str]
    manifest: Evidence[Mapping[str, Any]]
    manifest_state: Evidence[str]
    components: Evidence[Sequence[Mapping[str, Any]]]
    golden_bundle: Evidence[Any]
    configuration: Evidence[Mapping[str, Any]]
    extensions: Evidence[Any]
    branding: Evidence[Any]


@dataclass(frozen=True)
class IdentityProof:
    """Fail-closed result of deriving and comparing actual identity."""

    status: ProofStatus
    actual_digest: str | None
    expected_digest: str
    actual_projection: Mapping[str, Any] | None
    errors: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status is ProofStatus.VALID


def project_actual_identity(
    evidence: ActualIdentityEvidence,
) -> dict[str, Any]:
    """Validate and project actual evidence into Platform Instance shape."""
    errors = _validate_evidence(evidence)
    if errors:
        raise ValueError(
            "actual identity evidence is unavailable: " + "; ".join(errors)
        )

    components = [dict(component) for component in evidence.components.value or ()]
    manifest = dict(evidence.manifest.value or {})
    return {
        "platform_id": evidence.platform_id.value,
        "manifest": manifest,
        "manifest_state": evidence.manifest_state.value,
        "components": components,
        "golden_bundle": _projection_value(evidence.golden_bundle),
        "configuration": dict(evidence.configuration.value or {}),
        "extensions": _projection_value(evidence.extensions),
        "branding": _projection_value(evidence.branding),
    }


def prove_correspondence(
    evidence: ActualIdentityEvidence,
    *,
    expected_digest: str,
) -> IdentityProof:
    """Derive D_actual independently and compare it with D_expected."""
    if not isinstance(expected_digest, str) or not expected_digest:
        return IdentityProof(
            status=ProofStatus.UNAVAILABLE,
            actual_digest=None,
            expected_digest=expected_digest,
            actual_projection=None,
            errors=("expected digest is unavailable",),
        )

    errors = _validate_evidence(evidence)
    if errors:
        return IdentityProof(
            status=ProofStatus.UNAVAILABLE,
            actual_digest=None,
            expected_digest=expected_digest,
            actual_projection=None,
            errors=tuple(errors),
        )

    projection = project_actual_identity(evidence)
    actual_digest = compute_instance_digest(projection)
    status = (
        ProofStatus.VALID if actual_digest == expected_digest else ProofStatus.MISMATCH
    )
    return IdentityProof(
        status=status,
        actual_digest=actual_digest,
        expected_digest=expected_digest,
        actual_projection=projection,
    )


def _projection_value(evidence: Evidence[Any]) -> Any:
    if evidence.state is EvidenceState.ABSENT:
        return None
    return evidence.value


def _validate_evidence(evidence: ActualIdentityEvidence) -> list[str]:
    errors: list[str] = []
    fields = {
        "platform_id": evidence.platform_id,
        "manifest": evidence.manifest,
        "manifest_state": evidence.manifest_state,
        "components": evidence.components,
        "golden_bundle": evidence.golden_bundle,
        "configuration": evidence.configuration,
        "extensions": evidence.extensions,
        "branding": evidence.branding,
    }
    for name, item in fields.items():
        if not item.valid():
            errors.append(f"{name}: evidence is not independently valid")

    if errors:
        return errors

    if not isinstance(evidence.platform_id.value, str) or not evidence.platform_id.value:
        errors.append("platform_id: actual value is invalid")
    if not isinstance(evidence.manifest.value, Mapping):
        errors.append("manifest: actual value is not an object")
    else:
        for key in ("manifest_id", "manifest_version", "manifest_digest"):
            if not isinstance(evidence.manifest.value.get(key), str):
                errors.append(f"manifest.{key}: actual value is missing or invalid")
    if not isinstance(evidence.manifest_state.value, str):
        errors.append("manifest_state: actual value is invalid")
    if not isinstance(evidence.components.value, Sequence) or isinstance(
        evidence.components.value, str | bytes
    ):
        errors.append("components: actual membership is missing or invalid")
    else:
        errors.extend(_validate_components(evidence.components.value))
    if not isinstance(evidence.configuration.value, Mapping):
        errors.append("configuration: actual identity-bearing configuration is missing")

    return errors


def _validate_components(components: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not components:
        return ["components: actual membership is empty or incomplete"]

    seen: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            errors.append(f"components[{index}]: actual component evidence is invalid")
            continue
        component_id = component.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            errors.append(f"components[{index}]: component_id is missing or invalid")
            continue
        if component_id in seen:
            errors.append(f"components: duplicate actual component {component_id!r}")
        seen.add(component_id)
        if not isinstance(component.get("component_version"), str):
            errors.append(
                f"components[{index}]: component_version is missing or invalid"
            )
        artifact = component.get("artifact")
        if not isinstance(artifact, Mapping):
            errors.append(f"components[{index}]: actual artifact identity is missing")
            continue
        artifact_type = artifact.get("artifact_type")
        if not isinstance(artifact_type, str):
            errors.append(f"components[{index}]: artifact_type is missing or invalid")
        elif artifact_type == "none":
            if artifact.get("digest") is not None:
                errors.append(
                    f"components[{index}]: artifact_type=none requires digest=null"
                )
            if artifact.get("pinned") is not False:
                errors.append(
                    f"components[{index}]: artifact_type=none requires pinned=false"
                )
            if artifact.get("canonical_form") is not None:
                errors.append(
                    f"components[{index}]: artifact_type=none requires canonical_form=null"
                )
        else:
            if not isinstance(artifact.get("digest"), str) or not artifact.get("digest"):
                errors.append(
                    f"components[{index}]: published artifact digest is missing"
                )
            if artifact.get("pinned") is not True:
                errors.append(
                    f"components[{index}]: published artifact is not pinned"
                )
            if artifact.get("canonical_form") not in (
                "source_package/v1",
                "container_image/v1",
            ):
                errors.append(
                    f"components[{index}]: artifact canonical_form is invalid"
                )
    return errors


__all__ = [
    "ActualIdentityEvidence",
    "Evidence",
    "EvidenceProvenance",
    "EvidenceState",
    "IdentityProof",
    "ProofStatus",
    "project_actual_identity",
    "prove_correspondence",
]
