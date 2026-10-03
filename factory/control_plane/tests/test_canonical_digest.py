"""Canonicalization and digest determinism (Slice 1 proofs).

Proves: byte-stable canonical serialization, construction-order
independence, digest exclusion of the declared digest field, and digest
change on any technical payload change.
"""

from __future__ import annotations

import copy
import json

from conftest import EXAMPLES
from factory_control_plane import documents
from factory_control_plane.canonical import canonical_bytes
from factory_control_plane.digests import digest_of, verify_digest


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_construction_order_produces_identical_bytes_and_digest():
    first = {
        "b": 1,
        "a": {"y": 2, "x": [3, {"n": 1, "m": 2}]},
        "note": "Näkymä reruns",
    }
    second = {
        "note": "Näkymä reruns",
        "a": {"x": [3, {"m": 2, "n": 1}], "y": 2},
        "b": 1,
    }
    assert canonical_bytes(first) == canonical_bytes(second)
    assert digest_of(first) == digest_of(second)


def test_canonical_bytes_are_stable_across_json_roundtrips():
    document = _load(EXAMPLES["version"])
    roundtripped = json.loads(json.dumps(document))
    assert canonical_bytes(document) == canonical_bytes(roundtripped)
    assert canonical_bytes(document) == canonical_bytes(document)
    assert digest_of(document) == digest_of(roundtripped)


def test_canonical_form_is_readable_utf8_and_compact():
    document = {"summary": "Näkymä", "count": 1}
    text = canonical_bytes(document).decode("utf-8")
    assert text == '{"count":1,"summary":"Näkymä"}'


def test_declared_digest_is_excluded_from_the_hashed_payload():
    document = _load(EXAMPLES["requirements"])
    without_digest = {key: value for key, value in document.items() if key != "digest"}
    assert digest_of(document) == digest_of(without_digest)
    assert verify_digest(document)


def test_digest_changes_when_the_technical_payload_changes():
    original = _load(EXAMPLES["version"])
    baseline = digest_of(original)

    changed_value = copy.deepcopy(original)
    changed_value["configuration"]["identity"]["session_ttl_seconds"] = 7200
    assert digest_of(changed_value) != baseline

    changed_version = copy.deepcopy(original)
    changed_version["components"][0]["component_version"] = "0.3.1"
    assert digest_of(changed_version) != baseline

    changed_schema = copy.deepcopy(original)
    changed_schema["schema_version"] = "control-plane/configuration/v2"
    assert digest_of(changed_schema) != baseline

    requirements = _load(EXAMPLES["requirements"])
    requirements_baseline = digest_of(requirements)
    edited = copy.deepcopy(requirements)
    edited["entries"][0]["statement"]["summary"] = "A different intent."
    assert digest_of(edited) != requirements_baseline


def test_tampered_document_fails_digest_verification():
    tampered = copy.deepcopy(_load(EXAMPLES["version"]))
    tampered["attachments"]["approval"] = {"approver": "not-yet"}
    assert verify_digest(tampered) is False


def test_builder_settles_a_matching_digest():
    requirements = _load(EXAMPLES["requirements"])
    successor = documents.new_requirements_version(
        project_ref=requirements["project_ref"],
        entries=requirements["entries"],
        predecessor=requirements,
    )
    assert verify_digest(successor)
    assert successor["requirements_id"].endswith("/requirements/2")
    assert digest_of(successor) != digest_of(requirements)
