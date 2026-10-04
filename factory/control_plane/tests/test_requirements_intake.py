"""Adversarial tests for the Slice 3 intake normalizers (requirements_intake)."""

from __future__ import annotations

import copy

import pytest
from factory_control_plane import requirements_intake as ri
from factory_control_plane.digests import verify_digest
from s3_fixtures import PROJECT_REF

VALID = {"kind": "capability", "statement": {"summary": "Shoppers can check out."}}


def test_four_channels_normalize_to_v1_entries():
    for channel in ("form", "import", "api", "nl"):
        entry = ri.normalize_intake(channel, copy.deepcopy(VALID), seq=1)
        assert entry["source_channels"] == [channel]
        assert entry["kind"] == "capability"
        assert entry["statement"]["summary"] == "Shoppers can check out."
        assert entry["refs"] == []
        assert entry["status"] == "open"


def test_source_channel_is_adapter_assigned_not_caller_asserted():
    # A caller-supplied source_channels is an unknown field and fails closed.
    with pytest.raises(ri.RequirementsIntakeError) as error:
        ri.normalize_intake("form", {**VALID, "source_channels": ["api"]}, seq=1)
    assert error.value.code == "S3-FLOW-001"


def test_status_defaults_open_and_explicit_values_preserved():
    assert ri.normalize_intake("form", copy.deepcopy(VALID), seq=1)["status"] == "open"
    resolved = ri.normalize_intake("form", {**VALID, "status": "resolved"}, seq=1)
    assert resolved["status"] == "resolved"
    # An unrepresentable status is refused; resolved is never inferred.
    with pytest.raises(ri.RequirementsIntakeError) as error:
        ri.normalize_intake("form", {**VALID, "status": "in_progress"}, seq=1)
    assert error.value.code == "S3-FLOW-001"


def test_constraints_are_flat_scalars_folded_into_statement():
    entry = ri.normalize_intake(
        "form",
        {
            "kind": "constraint",
            "statement": {"summary": "PCI scoped."},
            "constraints": {"scope": "pci", "limit": 10},
        },
        seq=1,
    )
    assert entry["statement"]["constraints"] == {"scope": "pci", "limit": 10}

    # Top-level constraints are folded the same way.
    entry = ri.normalize_intake(
        "import",
        {
            "kind": "constraint",
            "statement": {"summary": "PCI scoped."},
            "constraints": {"scope": "pci"},
        },
        seq=1,
    )
    assert entry["statement"]["constraints"] == {"scope": "pci"}


def test_constraints_given_twice_is_contradictory():
    with pytest.raises(ri.RequirementsIntakeError) as error:
        ri.normalize_intake(
            "form",
            {
                "kind": "constraint",
                "statement": {"summary": "x", "constraints": {"a": 1}},
                "constraints": {"a": 2},
            },
            seq=1,
        )
    assert error.value.code == "S3-FLOW-001"


def test_nested_constraints_fail_closed():
    with pytest.raises(ri.RequirementsIntakeError) as error:
        ri.normalize_intake(
            "form",
            {
                "kind": "constraint",
                "statement": {"summary": "x", "constraints": {"a": {"b": 1}}},
            },
            seq=1,
        )
    assert error.value.code == "S3-FLOW-001"


def test_unknown_candidate_field_fails_closed():
    with pytest.raises(ri.RequirementsIntakeError) as error:
        ri.normalize_intake("form", {**VALID, "priority": "high"}, seq=1)
    assert error.value.code == "S3-FLOW-001"


def test_malformed_inputs_fail_closed():
    malformed = [
        None,
        "raw text",
        123,
        [],
        {},
        {"statement": {"summary": "x"}},  # missing kind
        {"kind": "bogus", "statement": {"summary": "x"}},  # invalid kind
        {"kind": "capability"},  # missing statement
        {"kind": "capability", "statement": {}},  # missing summary
        {"kind": "capability", "statement": {"summary": ""}},  # empty summary
        {"kind": "capability", "statement": {"summary": "x"}, "refs": "not-a-list"},
        {"kind": "capability", "statement": {"summary": "x"}, "refs": [1]},
        {"kind": "capability", "statement": {"summary": "x"}, "req_id": ""},
    ]
    for candidate in malformed:
        with pytest.raises(ri.RequirementsIntakeError) as error:
            ri.normalize_intake("form", candidate, seq=1)
        assert error.value.code == "S3-FLOW-001"


def test_nl_channel_refuses_raw_text_and_stores_no_text():
    # Raw natural-language text is never accepted or retained.
    with pytest.raises(ri.RequirementsIntakeError) as error:
        ri.normalize_nl("Shoppers should be able to check out easily.")
    assert error.value.code == "S3-FLOW-001"

    # A structured nl candidate normalizes without embedding the free text.
    entry = ri.normalize_nl(
        {"kind": "capability", "statement": {"summary": "Structured intent only."}},
        seq=1,
    )
    assert entry["source_channels"] == ["nl"]
    assert "Shoppers should be able" not in repr(entry)


def test_duplicate_req_id_fails_closed():
    with pytest.raises(ri.RequirementsIntakeError) as error:
        ri.normalize_channel(
            "form",
            [
                {"kind": "capability", "statement": {"summary": "a"}, "req_id": "r1"},
                {"kind": "capability", "statement": {"summary": "b"}, "req_id": "r1"},
            ],
        )
    assert error.value.code == "S3-FLOW-001"


def test_missing_req_id_is_allocated_deterministically():
    entries = ri.normalize_channel(
        "api",
        [
            {"kind": "capability", "statement": {"summary": "a"}},
            {"kind": "capability", "statement": {"summary": "b"}},
        ],
    )
    assert [entry["req_id"] for entry in entries] == ["s3-1", "s3-2"]


def test_non_empty_candidate_list_required():
    with pytest.raises(ri.RequirementsIntakeError):
        ri.normalize_channel("form", [])
    with pytest.raises(ri.RequirementsIntakeError):
        ri.normalize_channel("form", "not-a-list")


def test_intake_builds_digest_verifying_requirements_document():
    document = ri.intake_requirements_version(
        project_ref=PROJECT_REF,
        channel="import",
        candidates=[
            {"kind": "capability", "statement": {"summary": "a"}},
            {
                "kind": "constraint",
                "statement": {"summary": "b", "constraints": {"k": "v"}},
                "refs": ["req-other"],
                "status": "resolved",
                "req_id": "r2",
            },
        ],
    )
    assert document["schema_version"] == "control-plane/requirements/v1"
    assert document["project_ref"] == PROJECT_REF
    assert document["requirements_id"].startswith(f"{PROJECT_REF}/requirements/")
    assert verify_digest(document)
    assert document["entries"][1]["status"] == "resolved"
    assert document["entries"][1]["refs"] == ["req-other"]


def test_intake_build_supports_a_different_project():
    document = ri.intake_requirements_version(
        project_ref="other_shop",
        channel="form",
        candidates=[{**VALID}],
    )
    assert document["project_ref"] == "other_shop"
    assert document["requirements_id"].startswith("other_shop/requirements/")
    assert verify_digest(document)
