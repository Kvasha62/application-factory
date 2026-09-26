from __future__ import annotations

from dataclasses import replace

from deployment_operations.identity import (
    ActualIdentityEvidence,
    Evidence,
    EvidenceProvenance,
    EvidenceState,
    ProofStatus,
    project_actual_identity,
    prove_correspondence,
)
from platform_instance.instance import compute_instance_digest, instance_content_for_digest


SOURCE = "running-platform-identity-surface"
CORRELATION = "deployment-123"


def present(value):
    return Evidence(
        state=EvidenceState.PRESENT,
        value=value,
        provenance=EvidenceProvenance.MEASURED,
        source=SOURCE,
        correlation_id=CORRELATION,
    )


def absent():
    return Evidence(
        state=EvidenceState.ABSENT,
        provenance=EvidenceProvenance.MEASURED,
        source=SOURCE,
        correlation_id=CORRELATION,
    )


def identity(**overrides):
    values = {
        "platform_id": present("platform-a"),
        "manifest": present(
            {
                "manifest_id": "manifest-a",
                "manifest_version": "1.2.3",
                "manifest_digest": "sha256:manifest",
            }
        ),
        "manifest_state": present("published"),
        "components": present(
            [
                {
                    "component_id": "billing",
                    "component_version": "2.0.0",
                    "artifact": {
                        "artifact_type": "source_package",
                        "digest": "sha256:artifact",
                        "pinned": True,
                        "canonical_form": "source_package/v1",
                    },
                }
            ]
        ),
        "golden_bundle": absent(),
        "configuration": present({"region": "eu"}),
        "extensions": present({"feature": "enabled"}),
        "branding": present({"name": "Example"}),
    }
    values.update(overrides)
    return ActualIdentityEvidence(**values)


def expected(evidence=None):
    evidence = evidence or identity()
    return compute_instance_digest(project_actual_identity(evidence))


def test_actual_identity_uses_existing_instance_canonicalization():
    evidence = identity()
    projection = project_actual_identity(evidence)
    assert compute_instance_digest(projection) == prove_correspondence(
        evidence, expected_digest=expected(evidence)
    ).actual_digest


def test_actual_identity_excludes_only_instance_digest_and_schema():
    document = {
        "$schema": "ignored",
        "instance_digest": "sha256:old",
        "platform_id": "platform-a",
        "manifest": {"manifest_id": "manifest-a"},
    }
    assert instance_content_for_digest(document) == {
        "platform_id": "platform-a",
        "manifest": {"manifest_id": "manifest-a"},
    }


def test_same_actual_content_produces_same_d_actual():
    left = identity()
    right = identity()
    assert prove_correspondence(
        left, expected_digest=expected(left)
    ).actual_digest == prove_correspondence(
        right, expected_digest=expected(right)
    ).actual_digest


def test_key_order_does_not_change_d_actual():
    left = identity(
        configuration=present({"a": 1, "b": 2}),
        branding=present({"x": 1, "y": 2}),
    )
    right = identity(
        configuration=present({"b": 2, "a": 1}),
        branding=present({"y": 2, "x": 1}),
    )
    assert expected(left) == expected(right)


def test_expected_digest_is_not_used_as_actual_digest():
    evidence = identity()
    result = prove_correspondence(evidence, expected_digest="sha256:expected-only")
    assert result.status is ProofStatus.MISMATCH
    assert result.actual_digest == expected(evidence)
    assert result.actual_digest != "sha256:expected-only"


def test_expected_manifest_is_not_used_as_actual_manifest():
    evidence = identity()
    projection = project_actual_identity(evidence)
    assert projection["manifest"]["manifest_id"] == "manifest-a"
    assert "expected_manifest" not in projection


def test_expected_components_are_not_used_as_actual_membership():
    evidence = identity()
    projection = project_actual_identity(evidence)
    assert [entry["component_id"] for entry in projection["components"]] == ["billing"]


def test_expected_artifact_none_is_not_actual_artifact_none():
    evidence = identity()
    artifact = evidence.components.value[0]["artifact"]
    assert artifact["artifact_type"] == "source_package"
    assert (
        project_actual_identity(evidence)["components"][0]["artifact"]["artifact_type"]
        != "none"
    )


def test_missing_actual_configuration_is_unavailable():
    evidence = identity(
        configuration=Evidence(
            state=EvidenceState.MISSING,
            provenance=None,
            source=None,
            correlation_id=None,
        )
    )
    result = prove_correspondence(evidence, expected_digest="sha256:any")
    assert result.status is ProofStatus.UNAVAILABLE
    assert any("configuration" in error for error in result.errors)


def test_missing_actual_component_is_unavailable():
    evidence = identity(components=present([]))
    result = prove_correspondence(evidence, expected_digest="sha256:any")
    assert result.status is ProofStatus.UNAVAILABLE
    assert any("components" in error for error in result.errors)


def test_extra_actual_component_is_not_ignored():
    components = list(identity().components.value)
    components.append(
        {
            "component_id": "extra",
            "component_version": "1.0.0",
            "artifact": {
                "artifact_type": "source_package",
                "digest": "sha256:extra",
                "pinned": True,
                "canonical_form": "source_package/v1",
            },
        }
    )
    actual = identity(components=present(components))
    result = prove_correspondence(actual, expected_digest=expected())
    assert result.status is ProofStatus.MISMATCH
    assert result.actual_digest is not None


def test_duplicate_actual_component_is_unavailable():
    component = identity().components.value[0]
    result = prove_correspondence(
        identity(components=present([component, dict(component)])),
        expected_digest="sha256:any",
    )
    assert result.status is ProofStatus.UNAVAILABLE
    assert any("duplicate" in error for error in result.errors)


def test_contradictory_component_evidence_is_unavailable():
    evidence = identity(
        components=Evidence(
            state=EvidenceState.CONFLICTING,
            value=None,
            provenance=EvidenceProvenance.ATTESTED,
            source=SOURCE,
            correlation_id=CORRELATION,
        )
    )
    result = prove_correspondence(evidence, expected_digest="sha256:any")
    assert result.status is ProofStatus.UNAVAILABLE


def test_equal_actual_and_expected_digest_is_valid():
    evidence = identity()
    result = prove_correspondence(evidence, expected_digest=expected(evidence))
    assert result.status is ProofStatus.VALID
    assert result.verified


def test_different_actual_and_expected_digest_is_mismatch():
    evidence = identity()
    result = prove_correspondence(evidence, expected_digest="sha256:different")
    assert result.status is ProofStatus.MISMATCH
    assert not result.verified


def test_unavailable_actual_identity_is_unavailable():
    evidence = identity(platform_id=Evidence(state=EvidenceState.STALE))
    result = prove_correspondence(evidence, expected_digest="sha256:any")
    assert result.status is ProofStatus.UNAVAILABLE
    assert not result.verified


def test_mismatch_is_not_collapsed_into_unavailable():
    evidence = identity()
    result = prove_correspondence(evidence, expected_digest="sha256:different")
    assert result.status is ProofStatus.MISMATCH
    assert result.actual_digest is not None


def test_unavailable_is_not_collapsed_into_mismatch():
    evidence = identity(platform_id=Evidence(state=EvidenceState.MISSING))
    result = prove_correspondence(evidence, expected_digest="sha256:different")
    assert result.status is ProofStatus.UNAVAILABLE
    assert result.actual_digest is None


def test_correct_execution_digest_does_not_establish_platform_identity():
    evidence = identity()
    execution_digest = "sha256:execution"
    result = prove_correspondence(evidence, expected_digest=execution_digest)
    assert result.status is ProofStatus.MISMATCH
    assert result.actual_digest != execution_digest


def test_correct_execution_content_with_wrong_platform_identity_is_mismatch():
    evidence = identity(platform_id=present("platform-b"))
    result = prove_correspondence(evidence, expected_digest=expected())
    assert result.status is ProofStatus.MISMATCH


def test_execution_identity_is_not_used_as_instance_digest():
    evidence = identity()
    result = prove_correspondence(evidence, expected_digest="sha256:execution-content")
    assert result.actual_digest == expected(evidence)
    assert result.actual_digest != "sha256:execution-content"


def test_explicit_actual_absence_of_golden_bundle_is_preserved():
    projection = project_actual_identity(identity(golden_bundle=absent()))
    assert projection["golden_bundle"] is None


def test_stale_evidence_is_unavailable_even_when_value_is_present():
    stale = replace(present("platform-a"), fresh=False)
    result = prove_correspondence(
        identity(platform_id=stale), expected_digest="sha256:any"
    )
    assert result.status is ProofStatus.UNAVAILABLE
