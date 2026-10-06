"""D&O-side consumer contract for Running Platform identity correspondence.

This module validates independently supplied actual evidence and projects it
through the existing Platform Instance canonicalization. It does not discover
platform state, does not read expected deployment state as actual state, and
does not implement a production Running Platform identity provider.

Correlation of actual evidence to the evaluated binding
------------------------------------------------------

Equal handles are not a proof of actual identity (ADR-0019 §26, ADR-0020 §18).
The correspondence of actual evidence to the Running Platform being evaluated is
established only by the producer-established correlation rule implemented here:

    binding
      -> producer-established binding identity
      -> correlated actual evidence
      -> current Running Platform identity
      -> independently computed D_actual

1. :class:`BindingEvaluationContext` is the evaluation composition's statement
   of the binding it established: which authority established it, from which
   durable basis, for which identity-bearing target, in which binding space
   (``scope``), at which position in that space (``sequence``), and when. A
   binding that states no semantics establishes nothing, and any evaluation that
   does not state them cannot certify anything.
2. :class:`EvidenceCorrelation` is the producer's statement about the
   observation it produced: the evaluation handle it answered (the echo), the
   binding space and position it answered under, the identity-bearing target it
   actually observed, and its own attribution of the observation (authority,
   basis, instant).
3. :func:`require_correlated_evidence` is the single rule that checks agreement
   between the two statements. It is a conjunction: every part is required, a
   missing statement is a refusal, and a disagreement fails closed as
   :class:`ActualIdentityUnavailable`. Equal handles alone establish nothing —
   an observation whose correlation states no binding semantics is refused even
   when its handle equals the evaluation's.
4. Only after the correlation is established is ``D_actual`` computed — from
   actual evidence alone — and compared with ``D_expected``.

The evaluation side therefore never authors a correlation and never copies
expected identity into actual evidence: it checks the producer's facts and
compares digests.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from deployment_operations.environment import IDENTITY_CONFIGURATION_KEYS
from platform_instance.schema import load_schema, validate_structure_against_schema
from platform_manifest.lifecycle import LIFECYCLE_STATES
from platform_manifest.manifest import discover_root
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
class BindingIdentity:
    """The identity-bearing binding one observation belongs to.

    ``token`` is the evaluation handle (the echo), ``scope`` the binding space
    in which an identity-bearing target is unique, ``sequence`` this evaluation's
    position in that space, and ``target`` the identity-bearing platform the
    binding is evaluated for. Two bindings are the same binding exactly when all
    four agree; equal handles of two different bindings are therefore not the
    same binding.
    """

    token: object = ""
    scope: str = ""
    sequence: int = 0
    target: str = ""


@dataclass(frozen=True)
class BindingEvaluationContext:
    """What the evaluation composition established when it bound an evaluation.

    The context is the *semantics* of the evaluation binding: it names the
    authority that established the binding, the durable basis it was established
    from, the identity-bearing ``target`` the evaluation is for, the binding
    ``scope`` in which that target is unique, this evaluation's ``sequence`` in
    that scope, and the instant the binding was established.

    It deliberately carries no expected Platform Instance document and no
    expected digest beyond the identity-bearing target name: the correspondence
    of content is decided by ``D_actual`` against ``D_expected``, never by this
    context.

    Only the binding handle crosses an owner-side transport boundary; the
    producer's answer states the binding identity it answered, and
    :func:`require_correlated_evidence` compares the two statements.
    """

    authority: str
    basis: str
    target: str
    scope: str
    sequence: int
    established_at: str

    def binding_identity(self, token: object) -> BindingIdentity:
        """The identity of the binding this context describes."""
        return BindingIdentity(
            token=token,
            scope=self.scope,
            sequence=self.sequence,
            target=self.target,
        )


@dataclass(frozen=True)
class PlatformIdentityBinding:
    """One evaluation of one identity-bearing binding.

    ``token`` is the evaluation handle: the handle this evaluation established
    for this binding, and the only thing that may cross an owner-side transport
    boundary. It is deliberately not an identity and is never interpreted by
    D&O as one: it means something only together with ``context``, which states
    the binding semantics the evaluation established. A binding without a
    context establishes nothing (:func:`require_correlated_evidence` refuses
    it), so equal handles of two different bindings cannot certify each other.

    The binding carries no expected Platform Instance identity and no digest.
    """

    token: object
    context: BindingEvaluationContext | None = None


@dataclass(frozen=True)
class EvidenceCorrelation:
    """Producer-established correlation of one observation to one binding.

    ``token``/``scope``/``sequence``/``target`` are the producer's statement of
    the binding identity the observation belongs to: the handle it answered (the
    echo), the binding space and position it answered under, and the
    identity-bearing platform it actually observed. ``authority``/``basis``/
    ``established_at`` attribute the observation to the authority that
    established it, the basis it was grounded on and the instant it was
    established (ADR-0020 §17). The producing authority is not the evaluating
    authority, so attribution is required to be stated and is not required to
    equal the evaluation's own.

    The fields default so that a correlation which states nothing is
    constructible — and always refused: a bare handle is never a proof of
    correspondence. See :func:`require_correlated_evidence`.
    """

    token: object = ""
    scope: str = ""
    sequence: int = 0
    target: str = ""
    authority: str = ""
    basis: str = ""
    established_at: str = ""

    def echoes(self, token: object) -> bool:
        """True when this observation is labelled with exactly this handle.

        A handle-echo predicate, and nothing more: it is satisfied by equal
        handles of two different bindings. It is never sufficient to establish
        correspondence — use :func:`require_correlated_evidence`.
        """
        return bool(token) and self.token == token

    def binding_identity(self) -> BindingIdentity:
        """The binding identity this correlation states."""
        return BindingIdentity(
            token=self.token,
            scope=self.scope,
            sequence=self.sequence,
            target=self.target,
        )


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


def require_correlated_evidence(
    binding: PlatformIdentityBinding,
    evidence: ActualEvidence,
) -> EvidenceCorrelation:
    """Establish that one observation belongs to one evaluation binding.

    The correlation rule, as one conjunction. Every step is required; a missing
    statement is a refusal, never a default:

    1. the evaluation binding states its semantics — the authority that
       established it, the durable basis, the identity-bearing ``target``, the
       binding ``scope``, this evaluation's ``sequence`` and the instant — and
       carries a handle;
    2. the observation has a valid envelope: actual evidence, normative
       provenance, current freshness, and an actual surface whose subordinate
       component observations state their own correlation and are correlated to
       the same binding identity;
    3. the observation states its own correlation completely: the handle it
       answered (the echo), the binding space and position it answered under,
       the identity-bearing target it actually observed, and its attribution
       (producing authority, basis, instant);
    4. the two statements agree on the binding identity: handle, scope, sequence
       and target. A disagreement on any of them is evidence of another
       identity-bearing binding and is refused as such — an earlier binding's
       observation, another platform's observation, and an observation of the
       same handle under a different binding are all refused here.

    Equal handles alone satisfy none of this: a correlation that states nothing
    beyond a handle is refused, and so is an evaluation binding that states no
    semantics.

    Returns the established correlation; raises
    :class:`ActualIdentityUnavailable` otherwise.
    """

    if not isinstance(binding, PlatformIdentityBinding):
        raise ActualIdentityUnavailable("the evaluation binding is invalid")
    if not isinstance(binding.token, str) or not binding.token.strip():
        raise ActualIdentityUnavailable("the evaluation binding carries no handle")
    context = binding.context
    if not isinstance(context, BindingEvaluationContext):
        raise ActualIdentityUnavailable(
            "the evaluation binding states no binding semantics, so equal "
            "handles cannot establish correspondence"
        )
    unstated = _unstated_binding_fields(context)
    if unstated:
        raise ActualIdentityUnavailable(
            "the evaluation binding does not state its " + ", ".join(unstated)
        )

    validate_actual_evidence(evidence)

    correlation = evidence.correlation
    unstated = _unstated_correlation_fields(correlation)
    if unstated:
        raise ActualIdentityUnavailable(
            "the actual evidence correlation does not state its " + ", ".join(unstated)
        )
    disagreement = _binding_disagreement(
        correlation.binding_identity(),
        context.binding_identity(binding.token),
    )
    if disagreement:
        raise ActualIdentityUnavailable(
            "actual identity evidence is correlated to another identity-bearing "
            f"binding: {disagreement} does not agree with the evaluation binding"
        )
    return correlation


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
            _project_component(component.value)
            for component in sorted(
                surface.components,
                key=lambda component: component.value.component_id,
            )
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
    _validate_projected_instance_shape(document)
    return document


def compute_actual_digest(evidence: ActualEvidence) -> str:
    """Compute D_actual with the existing Platform Instance canonicalizer."""

    from platform_instance.instance import compute_instance_digest

    return compute_instance_digest(project_actual_identity(evidence))


def establish_identity_correspondence(
    expected_instance: Document,
    evidence: ActualEvidence,
    *,
    binding: PlatformIdentityBinding | None = None,
) -> IdentityCorrespondenceResult:
    """Compare independently computed D_actual with the expected digest only.

    The chain this certifies is, in order:

        binding
          -> producer-established binding identity (the correlation rule)
          -> correlated actual evidence
          -> current Running Platform identity
          -> independently computed D_actual

    ``binding`` is the evaluation binding whose correlation the evidence must
    establish (see :func:`require_correlated_evidence`). Without it — and
    whenever any part of that chain cannot be established — the result is
    ``UNAVAILABLE``: equal handles, equal digests or any other equality of
    caller-supplied values never certify a correspondence on their own.

    No expected field is copied into the actual projection. A missing or
    unusable expected digest is unavailable, not a match.
    """

    if binding is None:
        return IdentityCorrespondenceResult.UNAVAILABLE
    try:
        require_correlated_evidence(binding, evidence)
        actual_digest = compute_actual_digest(evidence)
    except (ActualIdentityUnavailable, TypeError, ValueError):
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


def _unstated_binding_fields(context: BindingEvaluationContext) -> tuple[str, ...]:
    """The semantics an evaluation binding fails to state. Empty when complete."""

    unstated: list[str] = []
    for name in ("authority", "basis", "target", "scope", "established_at"):
        value = getattr(context, name)
        if not isinstance(value, str) or not value.strip():
            unstated.append(name)
    if not _is_sequence(context.sequence):
        unstated.append("sequence")
    return tuple(unstated)


def _unstated_correlation_fields(correlation: EvidenceCorrelation) -> tuple[str, ...]:
    """The facts a correlation fails to state. Empty when complete.

    A correlation that states nothing is constructible and always refused: the
    default values are the refusal, never a silent acceptance.
    """

    unstated: list[str] = []
    if not isinstance(correlation.token, str) or not correlation.token.strip():
        unstated.append("token")
    for name in ("scope", "target", "authority", "basis", "established_at"):
        value = getattr(correlation, name)
        if not isinstance(value, str) or not value.strip():
            unstated.append(name)
    if not _is_sequence(correlation.sequence):
        unstated.append("sequence")
    return tuple(unstated)


def _is_sequence(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _binding_disagreement(
    observed: BindingIdentity,
    expected: BindingIdentity,
) -> str:
    """The first binding-identity field that disagrees; empty when they agree."""

    for name in ("token", "scope", "sequence", "target"):
        if getattr(observed, name) != getattr(expected, name):
            return name
    return ""


def _validate_projected_instance_shape(document: Document) -> None:
    """Validate projected actual identity against the normative Instance schema."""

    candidate = dict(document)
    candidate["instance_digest"] = "sha256:" + "0" * 64
    try:
        schema = load_schema(discover_root())
        errors = validate_structure_against_schema(candidate, schema)
    except (OSError, TypeError, ValueError) as error:
        raise ActualIdentityUnavailable(
            f"authoritative Platform Instance schema is unavailable: {error}"
        ) from error
    if errors:
        raise ActualIdentityUnavailable(
            "projected actual identity violates the authoritative Platform "
            f"Instance schema: {errors[0]}"
        )


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
        raise ActualIdentityUnavailable("actual component membership is empty")
    seen: set[str] = set()
    envelope_identity = (
        envelope_correlation.binding_identity()
        if isinstance(envelope_correlation, EvidenceCorrelation)
        else None
    )
    shared_identity: BindingIdentity | None = None
    for component_evidence in components:
        if not isinstance(component_evidence, ActualEvidence):
            raise ActualIdentityUnavailable("actual component evidence is invalid")
        _require_provenance(component_evidence.provenance)
        correlation = component_evidence.correlation
        if not isinstance(correlation, EvidenceCorrelation):
            raise ActualIdentityUnavailable("actual component correlation is invalid")
        unstated = _unstated_correlation_fields(correlation)
        if unstated:
            raise ActualIdentityUnavailable(
                "actual component correlation does not state its " + ", ".join(unstated)
            )
        identity = correlation.binding_identity()
        if shared_identity is None:
            shared_identity = identity
        elif identity != shared_identity:
            raise ActualIdentityUnavailable("actual component correlation is mixed")
        if envelope_identity is not None and identity != envelope_identity:
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
    "BindingEvaluationContext",
    "BindingIdentity",
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
    "require_correlated_evidence",
    "validate_actual_evidence",
    "validate_actual_surface",
]
