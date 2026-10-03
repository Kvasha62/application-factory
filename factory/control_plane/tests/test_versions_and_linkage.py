"""Immutable version semantics, aggregate pointers, Project linkage, and
explicit attachment points (Slice 1 proofs)."""

from __future__ import annotations

import copy
import json

import pytest
from conftest import EXAMPLES
from factory_control_plane import documents, validation
from factory_control_plane.digests import digest_of, verify_digest
from factory_control_plane.validation import ControlPlaneError


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_configuration_versions_are_immutable_successors():
    first = _load(EXAMPLES["version"])
    before = json.dumps(first, sort_keys=True).encode("utf-8")
    second = documents.new_configuration_version(
        project_ref=first["project_ref"],
        requirements_ref=first["requirements_ref"],
        manifest_id=first["manifest"]["manifest_id"],
        components=first["components"],
        configuration={
            **first["configuration"],
            "identity": {
                **first["configuration"]["identity"],
                "session_ttl_seconds": 7200,
            },
        },
        predecessor=first,
        template_ref=first["template_ref"],
        variant_ref=first["variant_ref"],
    )
    after = json.dumps(first, sort_keys=True).encode("utf-8")
    assert before == after, "the predecessor document was mutated"
    assert second["predecessor"] == {
        "configuration_id": first["configuration_id"],
        "digest": first["digest"],
    }
    assert second["configuration_id"] != first["configuration_id"]
    assert verify_digest(second)
    assert digest_of(second) != digest_of(first)


def test_tampering_invalidates_the_declared_digest():
    tampered = copy.deepcopy(_load(EXAMPLES["version"]))
    tampered["components"][1]["component_version"] = "0.2.0"
    assert verify_digest(tampered) is False


def test_moving_the_current_pointer_never_touches_the_version():
    index = _load(EXAMPLES["index"])
    version = _load(EXAMPLES["version"])
    index_before = copy.deepcopy(index)
    successor = documents.new_configuration_version(
        project_ref=version["project_ref"],
        requirements_ref=version["requirements_ref"],
        manifest_id=version["manifest"]["manifest_id"],
        components=version["components"],
        configuration=version["configuration"],
        predecessor=version,
    )
    moved = documents.set_current(index, successor)
    assert index == index_before, "set_current mutated its input index"
    assert moved["current"]["digest"] == successor["digest"]
    assert index["current"] == {
        "configuration_id": version["configuration_id"],
        "digest": version["digest"],
    }
    assert verify_digest(version), "pointer move altered the version digest"


def test_set_current_refuses_a_foreign_project():
    index = _load(EXAMPLES["index"])
    assert validation.configuration_index_errors(index) == []
    foreign_version = _load(EXAMPLES["version"])
    foreign_version["project_ref"] = "other_shop"
    with pytest.raises(ControlPlaneError):
        documents.set_current(index, foreign_version)


def test_project_links_requirements_and_configuration():
    project = _load(EXAMPLES["project"])
    requirements = _load(EXAMPLES["requirements"])
    version = _load(EXAMPLES["version"])
    index = _load(EXAMPLES["index"])
    assert requirements["project_ref"] == project["project_id"]
    assert version["project_ref"] == project["project_id"]
    assert index["project_ref"] == project["project_id"]
    assert version["requirements_ref"] == {
        "requirements_id": requirements["requirements_id"],
        "digest": requirements["digest"],
    }
    assert project["pointers"]["requirements"] == {
        "requirements_id": requirements["requirements_id"],
        "digest": requirements["digest"],
    }
    assert project["pointers"]["configuration"] == {
        "configuration_id": version["configuration_id"],
        "digest": version["digest"],
    }
    assert index["current"] == {
        "configuration_id": version["configuration_id"],
        "digest": version["digest"],
    }


def test_broken_project_linkage_is_detectable():
    requirements = copy.deepcopy(_load(EXAMPLES["requirements"]))
    requirements["project_ref"] = "other_shop"
    errors = validation.requirements_errors(requirements)
    assert any("live under project_ref" in error for error in errors)


def test_attachments_are_explicit_and_null_in_slice_1():
    version = _load(EXAMPLES["version"])
    assert set(version["attachments"]) == {"proposal", "approval", "build", "release"}
    assert all(value is None for value in version["attachments"].values())
    successor = documents.new_configuration_version(
        project_ref=version["project_ref"],
        requirements_ref=version["requirements_ref"],
        manifest_id=version["manifest"]["manifest_id"],
        components=version["components"],
        configuration=version["configuration"],
        predecessor=version,
    )
    assert successor["attachments"] == version["attachments"]
