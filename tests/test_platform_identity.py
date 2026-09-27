"""Focused tests for the D&O-side Platform Identity contract."""

from __future__ import annotations

from copy import deepcopy

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
    PlatformIdentitySurface,
    PresenceState,
    establish_identity_correspondence,
    project_actual_identity,
    validate_actual_evidence,
)
from platform_instance.instance import compute_instance_digest


def _surface(
    *,
    platform_id: str = "example-platform",
    branding: IdentityField[object] | None = None,
    extensions: IdentityField[object] | None = None,
    golden_bundle: IdentityField[object] | None = None,
    configuration: IdentityField[object] | None = None,
    components: tuple[ActualComponentIdentity, ...] | None = None,
) -> PlatformIdentitySurface:
    return PlatformIdentitySurface(
        platform_id=platform_id,
        manifest={
            "manifest_id": "example-platform",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:manifest",
        },
        manifest_state="validated",
        components=components
        if components is not None
        else (
            ActualComponentIdentity(
                "authorization",
                "0.1.0",
                {
                    "artifact_type": "none",
                    "canonical_form": None,
                    "digest": None,
                    "pinned": False,
                },
            ),
        ),
        configuration=configuration
        if configuration is not None
        else IdentityField.present({"authorization": {"platform_id": platform_id}}),
        golden_bundle=golden_bundle
        if golden_bundle is not None
        else IdentityField.absent(),
        extensions=extensions
        if extensions is not None
        else IdentityField.absent(),
        branding=branding
        if branding is not None
        else IdentityField.absent(),
    )


def _evidence(
    surface: PlatformIdentitySurface,
    *,
    provenance: EvidenceProvenance = EvidenceProvenance.MEASURED,
    current: bool = True,
    token: object = "evaluation-1",
) -> ActualEvidence[PlatformIdentitySurface]:
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


def test_platform_identity_binding_is_opaque_to_d_and_o() -> None:
    binding = PlatformIdentityBinding(token=object())

    assert binding.token is not None
    assert not hasattr(binding, "instance_digest")
    assert not hasattr(binding, "platform_id")


@pytest.mark.parametrize(
    "provenance",
    [
        EvidenceProvenance.MEASURED,
        EvidenceProvenance.TRANSITIVE,
        EvidenceProvenance.ATTESTED,
    ],
)
def test_all_normative_provenance_classes_are_accepted(
    provenance: EvidenceProvenance,
) -> None:
    evidence = _evidence(_surface(), provenance=provenance)

    assert validate_actual_evidence(evidence) is evidence


def test_stale_evidence_fails_closed() -> None:
    with pytest.raises(ActualIdentityUnavailable, match="stale"):
        validate_actual_evidence(_evidence(_surface(), current=False))


def test_unknown_branding_never_becomes_absent() -> None:
    surface = _surface(branding=IdentityField.unknown())

    with pytest.raises(ActualIdentityUnavailable, match="UNKNOWN"):
        project_actual_identity(_evidence(surface))


def test_duplicate_actual_components_fail_closed() -> None:
    component = ActualComponentIdentity(
        "authorization",
        "0.1.0",
        {"artifact_type": "none"},
    )
    surface = _surface(components=(component, component))

    with pytest.raises(ActualIdentityUnavailable, match="duplicate"):
        validate_actual_evidence(_evidence(surface))


@pytest.mark.parametrize(
    ("field_name", "first", "second"),
    [
        ("branding", {"name": "A"}, {"name": "B"}),
        ("extensions", [{"id": "A"}], [{"id": "B"}]),
        ("golden_bundle", {"bundle_id": "A"}, {"bundle_id": "B"}),
        (
            "configuration",
            {"authorization": {"platform_id": "A"}},
            {"authorization": {"platform_id": "B"}},
        ),
    ],
)
def test_identity_bearing_difference_changes_digest(
    field_name: str,
    first: object,
    second: object,
) -> None:
    kwargs_a = {field_name: IdentityField.present(first)}
    kwargs_b = {field_name: IdentityField.present(second)}

    digest_a = compute_instance_digest(
        project_actual_identity(_evidence(_surface(**kwargs_a)))
    )
    digest_b = compute_instance_digest(
        project_actual_identity(_evidence(_surface(**kwargs_b)))
    )

    assert digest_a != digest_b


def test_manifest_state_difference_changes_digest() -> None:
    first = _surface()
    second = PlatformIdentitySurface(
        platform_id=first.platform_id,
        manifest=first.manifest,
        manifest_state="published",
        components=first.components,
        configuration=first.configuration,
        golden_bundle=first.golden_bundle,
        extensions=first.extensions,
        branding=first.branding,
    )

    assert (
        compute_instance_digest(project_actual_identity(_evidence(first)))
        != compute_instance_digest(project_actual_identity(_evidence(second)))
    )


def test_extra_actual_component_cannot_match_expected_instance() -> None:
    expected_surface = _surface()
    expected = _expected_from_surface(expected_surface)

    extra = ActualComponentIdentity(
        "booking",
        "0.1.0",
        {"artifact_type": "none"},
    )
    actual_surface = _surface(components=expected_surface.components + (extra,))

    assert (
        establish_identity_correspondence(expected, _evidence(actual_surface))
        is IdentityCorrespondenceResult.MISMATCH
    )


def test_missing_expected_component_cannot_match_expected_instance() -> None:
    expected_surface = _surface(
        components=(
            _surface().components[0],
            ActualComponentIdentity(
                "booking",
                "0.1.0",
                {"artifact_type": "none"},
            ),
        )
    )
    expected = _expected_from_surface(expected_surface)

    actual_surface = _surface(
        components=(expected_surface.components[0],),
    )

    assert (
        establish_identity_correspondence(expected, _evidence(actual_surface))
        is IdentityCorrespondenceResult.MISMATCH
    )


def test_expected_digest_echo_is_not_used_as_actual_identity() -> None:
    expected = _expected_from_surface(_surface())
    different = _surface(platform_id="another-platform")

    assert expected["instance_digest"] == compute_instance_digest(
        project_actual_identity(_evidence(_surface()))
    )
    assert (
        establish_identity_correspondence(expected, _evidence(different))
        is IdentityCorrespondenceResult.MISMATCH
    )


def test_valid_execution_identity_is_not_platform_identity() -> None:
    expected = _expected_from_surface(_surface())
    actual = _surface(
        branding=IdentityField.present({"name": "different"}),
    )

    # The contract contains no execution digest field and therefore cannot
    # substitute F3B execution evidence for Platform Instance identity.
    assert (
        establish_identity_correspondence(expected, _evidence(actual))
        is IdentityCorrespondenceResult.MISMATCH
    )


def test_missing_expected_digest_is_unavailable() -> None:
    assert (
        establish_identity_correspondence(
            {},
            _evidence(_surface()),
        )
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_presence_states_are_distinct() -> None:
    assert IdentityField.absent().state is PresenceState.ABSENT
    assert IdentityField.unknown().state is PresenceState.UNKNOWN
    assert IdentityField.present({"name": "brand"}).state is PresenceState.PRESENT


def test_actual_projection_does_not_copy_expected_instance() -> None:
    surface = _surface(branding=IdentityField.present({"name": "actual"}))
    evidence = _evidence(surface)

    projected = project_actual_identity(evidence)

    assert projected["branding"] == {"name": "actual"}
    assert "instance_digest" not in projected


def test_projection_does_not_mutate_surface_documents() -> None:
    surface = _surface(
        branding=IdentityField.present({"name": "actual"}),
    )
    before = deepcopy(surface)

    project_actual_identity(_evidence(surface))

    assert surface == before
