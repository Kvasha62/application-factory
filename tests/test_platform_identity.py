"""Contract tests for the D&O-side Platform Identity consumer.

Test doubles supply actual evidence. They are not a production provider.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

import pytest

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    ActualIdentityUnavailable,
    EvidenceCorrelation,
    EvidenceFreshness,
    EvidenceProvenance,
    IdentityCorrespondenceResult,
    IdentityField,
    PlatformIdentityBinding,
    PlatformIdentityProvider,
    PlatformIdentitySurface,
    compute_actual_digest,
    establish_identity_correspondence,
    project_actual_identity,
    validate_actual_evidence,
    validate_actual_surface,
)
from deployment_operations.state import (
    LIFECYCLE_FAILED,
    LIFECYCLE_IN_PROGRESS,
    LIFECYCLE_REALIZED,
    STAGES,
)
from platform_instance.instance import compute_instance_digest


def _component(
    component_id: str = "authorization",
    version: str = "0.1.0",
    *,
    token: object = "evaluation-1",
    current: bool = True,
    execution_digest: str | None = None,
    artifact: dict[str, object] | None = None,
) -> ActualEvidence:
    if artifact is None:
        artifact = {
            "artifact_type": "none",
            "canonical_form": None,
            "digest": None,
            "pinned": False,
        }
    else:
        artifact = dict(artifact)
    if execution_digest is not None:
        artifact["execution_digest"] = execution_digest
    return ActualEvidence(
        value=ActualComponentIdentity(component_id, version, artifact),
        provenance=EvidenceProvenance.MEASURED,
        correlation=EvidenceCorrelation(token),
        freshness=EvidenceFreshness(current),
    )


def _surface(
    *,
    platform_id: str = "example-platform",
    manifest_state: str = "validated",
    branding: IdentityField | None = None,
    extensions: IdentityField | None = None,
    golden_bundle: IdentityField | None = None,
    configuration: IdentityField | None = None,
    components: tuple[ActualEvidence, ...] | None = None,
    membership_established: bool = True,
    golden_bundle_inventory_established: bool = True,
) -> PlatformIdentitySurface:
    return PlatformIdentitySurface(
        platform_id=platform_id,
        manifest={
            "manifest_id": "example-platform",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:manifest",
        },
        manifest_state=manifest_state,
        components=components if components is not None else (_component(),),
        membership_established=membership_established,
        configuration=(
            configuration
            if configuration is not None
            else IdentityField.present({"authorization": {"platform_id": platform_id}})
        ),
        golden_bundle=(
            golden_bundle if golden_bundle is not None else IdentityField.absent()
        ),
        golden_bundle_inventory_established=golden_bundle_inventory_established,
        extensions=extensions if extensions is not None else IdentityField.absent(),
        branding=branding if branding is not None else IdentityField.absent(),
    )


def _evidence(
    surface: PlatformIdentitySurface,
    *,
    provenance: EvidenceProvenance = EvidenceProvenance.MEASURED,
    current: bool = True,
    token: object = "evaluation-1",
) -> ActualEvidence:
    return ActualEvidence(
        value=surface,
        provenance=provenance,
        correlation=EvidenceCorrelation(token),
        freshness=EvidenceFreshness(current),
    )


def _expected_from_surface(surface: PlatformIdentitySurface) -> dict[str, object]:
    document = project_actual_identity(_evidence(surface))
    document["instance_digest"] = compute_instance_digest(document)
    return document


class _RecordingProvider:
    """Test double. Not a production PlatformIdentityProvider."""

    def __init__(self, evidence: ActualEvidence) -> None:
        self.evidence = evidence

    def observe_identity(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        assert isinstance(binding, PlatformIdentityBinding)
        return self.evidence


def test_platform_identity_binding_carries_no_expected_identity() -> None:
    binding = PlatformIdentityBinding(token=object())

    assert set(binding.__dataclass_fields__) == {"token"}
    assert not hasattr(binding, "instance_digest")
    assert not hasattr(binding, "manifest")
    assert not hasattr(binding, "components")


def test_provider_protocol_is_consumer_side_only() -> None:
    provider: PlatformIdentityProvider = _RecordingProvider(_evidence(_surface()))

    observed = provider.observe_identity(PlatformIdentityBinding(token="bound"))

    assert observed.value.platform_id == "example-platform"
    assert not hasattr(provider, "deploy")


@pytest.mark.parametrize(
    "provenance",
    [
        EvidenceProvenance.MEASURED,
        EvidenceProvenance.TRANSITIVE,
        EvidenceProvenance.ATTESTED,
    ],
)
def test_normative_provenance_classes_are_accepted(
    provenance: EvidenceProvenance,
) -> None:
    evidence = _evidence(_surface(), provenance=provenance)

    assert validate_actual_evidence(evidence) is evidence


def test_missing_provenance_is_unavailable() -> None:
    evidence = ActualEvidence(
        value=_surface(),
        provenance=None,  # type: ignore[arg-type]
        correlation=EvidenceCorrelation("evaluation-1"),
        freshness=EvidenceFreshness(True),
    )

    with pytest.raises(ActualIdentityUnavailable, match="provenance"):
        validate_actual_evidence(evidence)


def test_ambiguous_provenance_is_unavailable() -> None:
    evidence = ActualEvidence(
        value=_surface(),
        provenance=("MEASURED", "ATTESTED"),  # type: ignore[arg-type]
        correlation=EvidenceCorrelation("evaluation-1"),
        freshness=EvidenceFreshness(True),
    )

    with pytest.raises(ActualIdentityUnavailable, match="provenance"):
        validate_actual_evidence(evidence)


def test_incomplete_evidence_envelope_is_unavailable() -> None:
    evidence = ActualEvidence(
        value=_surface(),
        provenance=EvidenceProvenance.MEASURED,
        correlation="evaluation-1",  # type: ignore[arg-type]
        freshness=EvidenceFreshness(True),
    )

    with pytest.raises(ActualIdentityUnavailable, match="correlation"):
        validate_actual_evidence(evidence)


def test_non_normative_provenance_is_unavailable() -> None:
    evidence = ActualEvidence(
        value=_surface(),
        provenance="DERIVED",  # type: ignore[arg-type]
        correlation=EvidenceCorrelation("evaluation-1"),
        freshness=EvidenceFreshness(True),
    )

    with pytest.raises(ActualIdentityUnavailable, match="provenance"):
        validate_actual_evidence(evidence)


def test_distinct_actual_identities_have_distinct_digests() -> None:
    first = compute_actual_digest(_evidence(_surface(platform_id="alpha")))
    second = compute_actual_digest(_evidence(_surface(platform_id="beta")))

    assert first != second
    assert first.startswith("sha256:")


@pytest.mark.parametrize(
    ("field_name", "first", "second"),
    [
        ("branding", {"name": "Alpha"}, {"name": "Beta"}),
        ("extensions", [{"extension_id": "a"}], [{"extension_id": "b"}]),
        ("golden_bundle", {"bundle_id": "certified-a"}, {"bundle_id": "certified-b"}),
        (
            "configuration",
            {"authorization": {"platform_id": "example-platform", "environment": "a"}},
            {"authorization": {"platform_id": "example-platform", "environment": "b"}},
        ),
    ],
)
def test_identity_bearing_difference_changes_actual_digest(
    field_name: str,
    first: object,
    second: object,
) -> None:
    digest_a = compute_actual_digest(
        _evidence(_surface(**{field_name: IdentityField.present(first)}))
    )
    digest_b = compute_actual_digest(
        _evidence(_surface(**{field_name: IdentityField.present(second)}))
    )

    assert digest_a != digest_b


def test_manifest_state_difference_changes_actual_digest() -> None:
    validated = compute_actual_digest(_evidence(_surface(manifest_state="validated")))
    published = compute_actual_digest(_evidence(_surface(manifest_state="published")))

    assert validated != published


def test_extra_actual_component_is_mismatch() -> None:
    expected_surface = _surface()
    expected = _expected_from_surface(expected_surface)
    extra = _component("booking")
    actual = _surface(components=expected_surface.components + (extra,))

    assert (
        establish_identity_correspondence(expected, _evidence(actual))
        is IdentityCorrespondenceResult.MISMATCH
    )


def test_missing_expected_component_is_mismatch() -> None:
    expected_surface = _surface(
        components=(_component("authorization"), _component("booking"))
    )
    expected = _expected_from_surface(expected_surface)
    actual = _surface(components=(_component("authorization"),))

    assert (
        establish_identity_correspondence(expected, _evidence(actual))
        is IdentityCorrespondenceResult.MISMATCH
    )


def test_expected_digest_echo_is_not_actual_identity() -> None:
    expected = _expected_from_surface(_surface())
    other = _surface(platform_id="another-platform")

    assert compute_actual_digest(_evidence(other)) != expected["instance_digest"]
    assert (
        establish_identity_correspondence(expected, _evidence(other))
        is IdentityCorrespondenceResult.MISMATCH
    )
    assert (
        establish_identity_correspondence(expected, _evidence(_surface()))
        is IdentityCorrespondenceResult.MATCH
    )


def test_deployment_record_digest_is_not_copied_as_actual() -> None:
    expected = _expected_from_surface(_surface())
    record_digest = expected["instance_digest"]

    assert (
        compute_actual_digest(_evidence(_surface(platform_id="other"))) != record_digest
    )


def test_stale_evidence_is_unavailable() -> None:
    expected = _expected_from_surface(_surface())

    with pytest.raises(ActualIdentityUnavailable, match="stale"):
        validate_actual_evidence(_evidence(_surface(), current=False))
    assert (
        establish_identity_correspondence(
            expected,
            _evidence(_surface(), current=False),
        )
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_stale_required_component_evidence_is_unavailable() -> None:
    surface = _surface(components=(_component(current=False),))

    with pytest.raises(ActualIdentityUnavailable, match="stale"):
        validate_actual_evidence(_evidence(surface))


def test_valid_execution_digest_does_not_establish_platform_identity() -> None:
    execution_digest = "sha256:" + sha256(b"loaded-module").hexdigest()
    expected = _expected_from_surface(_surface())
    actual = _surface(
        components=(_component(execution_digest=execution_digest),),
        branding=IdentityField.present({"name": "different"}),
    )

    projected = project_actual_identity(
        _evidence(_surface(components=(_component(execution_digest=execution_digest),)))
    )
    assert "execution_digest" not in projected["components"][0]["artifact"]
    assert compute_actual_digest(_evidence(actual)) != execution_digest
    assert (
        establish_identity_correspondence(expected, _evidence(actual))
        is IdentityCorrespondenceResult.MISMATCH
    )


def test_execution_digest_does_not_change_platform_digest() -> None:
    without = compute_actual_digest(_evidence(_surface()))
    with_execution = compute_actual_digest(
        _evidence(_surface(components=(_component(execution_digest="sha256:abcd"),)))
    )

    assert without == with_execution


def test_unknown_is_not_absent() -> None:
    assert IdentityField.unknown().state is not IdentityField.absent().state
    absent = project_actual_identity(
        _evidence(_surface(branding=IdentityField.absent()))
    )
    assert "branding" not in absent

    with pytest.raises(ActualIdentityUnavailable, match="UNKNOWN"):
        project_actual_identity(_evidence(_surface(branding=IdentityField.unknown())))


def test_unknown_golden_bundle_is_not_null() -> None:
    with pytest.raises(ActualIdentityUnavailable, match="UNKNOWN"):
        project_actual_identity(
            _evidence(_surface(golden_bundle=IdentityField.unknown()))
        )


def test_proven_empty_membership_is_not_unknown_emptiness() -> None:
    empty = _surface(components=(), membership_established=True)
    assert project_actual_identity(_evidence(empty))["components"] == []

    with pytest.raises(ActualIdentityUnavailable, match="membership"):
        validate_actual_evidence(
            _evidence(_surface(components=(), membership_established=False))
        )


def test_duplicate_component_preserves_failure_before_collapse() -> None:
    duplicated = (_component("authorization"), _component("authorization", "0.2.0"))

    with pytest.raises(ActualIdentityUnavailable, match="duplicate"):
        validate_actual_evidence(_evidence(_surface(components=duplicated)))


def test_mixed_correlation_is_unavailable() -> None:
    surface = _surface(
        components=(_component(token="other-evaluation"),),
    )

    with pytest.raises(ActualIdentityUnavailable, match="correlation"):
        validate_actual_evidence(_evidence(surface, token="evaluation-1"))


def test_projection_does_not_mutate_source_evidence() -> None:
    surface = _surface(branding=IdentityField.present({"name": "actual"}))
    evidence = _evidence(surface)
    before = deepcopy(surface)

    projected = project_actual_identity(evidence)
    projected["branding"]["name"] = "mutated"
    projected["manifest"]["manifest_id"] = "mutated"

    assert surface == before
    assert evidence.value.branding.value == {"name": "actual"}


def test_projection_does_not_copy_expected_identity() -> None:
    expected = _expected_from_surface(
        _surface(branding=IdentityField.present({"name": "expected"}))
    )
    actual = _surface(branding=IdentityField.present({"name": "actual"}))
    projected = project_actual_identity(_evidence(actual))

    assert projected["branding"] == {"name": "actual"}
    assert "instance_digest" not in projected
    assert projected["branding"] != expected["branding"]


def test_missing_expected_digest_is_unavailable_not_match() -> None:
    assert (
        establish_identity_correspondence({}, _evidence(_surface()))
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_contradictory_configuration_is_unavailable() -> None:
    surface = _surface(
        configuration=IdentityField.present(
            {"authorization": {"platform_id": "other-platform"}}
        )
    )

    with pytest.raises(ActualIdentityUnavailable, match="contradicts"):
        validate_actual_evidence(_evidence(surface))


def _sealed_artifact(digest: str) -> dict[str, object]:
    return {
        "artifact_type": "source_package",
        "digest": digest,
        "pinned": True,
        "canonical_form": "source_package/v1",
    }


def test_proved_artifact_absence_is_the_canonical_tuple() -> None:
    projected = project_actual_identity(_evidence(_surface()))

    assert projected["components"][0]["artifact"] == {
        "artifact_type": "none",
        "canonical_form": None,
        "digest": None,
        "pinned": False,
    }


def test_incomplete_artifact_absence_is_unavailable() -> None:
    surface = _surface(
        components=(_component(artifact={"artifact_type": "none", "pinned": False}),)
    )

    with pytest.raises(ActualIdentityUnavailable, match="artifact"):
        validate_actual_evidence(_evidence(surface))
    assert (
        establish_identity_correspondence(
            _expected_from_surface(_surface()), _evidence(surface)
        )
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_contradictory_artifact_absence_is_unavailable() -> None:
    surface = _surface(
        components=(
            _component(
                artifact={
                    "artifact_type": "none",
                    "digest": "sha256:" + "ab" * 32,
                    "pinned": False,
                    "canonical_form": None,
                }
            ),
        )
    )

    with pytest.raises(ActualIdentityUnavailable, match="artifact"):
        compute_actual_digest(_evidence(surface))


def test_sealed_artifact_without_measured_digest_is_unavailable() -> None:
    surface = _surface(
        components=(
            _component(
                artifact={
                    "artifact_type": "source_package",
                    "digest": None,
                    "pinned": True,
                    "canonical_form": "source_package/v1",
                }
            ),
        )
    )

    with pytest.raises(ActualIdentityUnavailable, match="digest"):
        compute_actual_digest(_evidence(surface))


def test_measured_sealed_artifact_changes_actual_digest() -> None:
    sealed = compute_actual_digest(
        _evidence(
            _surface(
                components=(
                    _component(artifact=_sealed_artifact("sha256:" + "cd" * 32)),
                )
            )
        )
    )
    absent = compute_actual_digest(_evidence(_surface()))

    assert sealed != absent


def test_expected_artifact_identity_is_not_substituted() -> None:
    expected = _expected_from_surface(_surface())
    actual = _surface(
        components=(_component(artifact={"artifact_type": "source_package"}),)
    )

    assert (
        establish_identity_correspondence(expected, _evidence(actual))
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_non_json_identity_value_is_unavailable_at_correspondence_boundary() -> None:
    surface = _surface(
        branding=IdentityField.present(object()),
    )
    expected = _expected_from_surface(_surface())

    with pytest.raises(TypeError):
        compute_actual_digest(_evidence(surface))

    with pytest.raises(TypeError):
        establish_identity_correspondence(expected, _evidence(surface))


def test_incomplete_configuration_does_not_reach_canonicalizer(monkeypatch) -> None:
    def fail(document: object) -> str:
        raise AssertionError(document)

    monkeypatch.setattr("platform_instance.instance.compute_instance_digest", fail)
    surface = _surface(configuration=IdentityField.present("not-a-mapping"))

    with pytest.raises(ActualIdentityUnavailable, match="configuration"):
        compute_actual_digest(_evidence(surface))
    assert (
        establish_identity_correspondence({}, _evidence(surface))
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_current_platform_id_contradiction_is_unavailable() -> None:
    surface = _surface(
        configuration=IdentityField.present(
            {"identity": {"current_platform_id": "other-platform"}}
        )
    )

    with pytest.raises(ActualIdentityUnavailable, match="contradicts"):
        validate_actual_evidence(_evidence(surface))
    assert (
        establish_identity_correspondence(
            _expected_from_surface(_surface()),
            _evidence(surface),
        )
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_deployment_lifecycle_is_not_manifest_state() -> None:
    from platform_manifest.lifecycle import LIFECYCLE_STATES

    for state in (
        *STAGES,
        LIFECYCLE_IN_PROGRESS,
        LIFECYCLE_REALIZED,
        LIFECYCLE_FAILED,
    ):
        if state in LIFECYCLE_STATES:
            continue
        with pytest.raises(ActualIdentityUnavailable, match="manifest_state"):
            validate_actual_evidence(_evidence(_surface(manifest_state=state)))

    assert compute_actual_digest(
        _evidence(_surface(manifest_state="deployed"))
    ) != compute_actual_digest(_evidence(_surface(manifest_state="validated")))


def test_mixed_component_correlation_fails_without_envelope() -> None:
    surface = _surface(
        components=(
            _component(token="one"),
            _component("booking", token="two"),
        )
    )

    with pytest.raises(ActualIdentityUnavailable, match="correlation"):
        validate_actual_surface(surface)
    assert (
        establish_identity_correspondence(
            _expected_from_surface(_surface()),
            _evidence(surface),
        )
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_absent_golden_bundle_requires_complete_inventory() -> None:
    unproved = _surface(golden_bundle_inventory_established=False)

    with pytest.raises(ActualIdentityUnavailable, match="inventory"):
        validate_actual_evidence(_evidence(unproved))
    assert project_actual_identity(_evidence(_surface()))["golden_bundle"] is None


def test_incomplete_golden_bundle_inventory_is_unavailable() -> None:
    surface = _surface(
        golden_bundle=IdentityField.present({"bundle_id": "certified-a"}),
        golden_bundle_inventory_established=False,
    )

    with pytest.raises(ActualIdentityUnavailable, match="inventory"):
        compute_actual_digest(_evidence(surface))


def test_lifecycle_vocabulary_is_unchanged() -> None:
    assert STAGES == (
        "requested",
        "validated",
        "provisioning",
        "deploying",
        "starting",
        "health_check",
        "ready",
    )
    assert LIFECYCLE_IN_PROGRESS == "in_progress"
    assert LIFECYCLE_REALIZED == "realized"
    assert LIFECYCLE_FAILED == "failed"


def test_no_second_digest_type_is_defined() -> None:
    import deployment_operations.platform_identity as identity

    for forbidden in (
        "ActualIdentityDigest",
        "RunningPlatformDigest",
        "RunningPlatformIdentityDigest",
        "ActualPlatformDigest",
    ):
        assert not hasattr(identity, forbidden)
