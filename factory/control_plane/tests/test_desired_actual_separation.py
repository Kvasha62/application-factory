"""Desired-vs-Actual document separation (Slice 1 proof).

Proves: every Slice 1 document and schema is Desired State only — no
actual/runtime/observed keys — and the README states the separation
explicitly. Actual-state domains (Proposal/Approval/Build/Release,
intake, runtime reading) remain future work outside this package.
"""

from __future__ import annotations

import json

from conftest import _SUBTREE_ROOT, EXAMPLES, SCHEMA_DIR

ACTUAL_STATE_KEYS = frozenset(
    {
        "actual",
        "deployed",
        "deployment",
        "drift",
        "health",
        "observed",
        "observed_identity",
        "runtime",
        "runtime_health",
        "runtime_root",
        "runtime_state",
        "reconciliation",
        "running_platform",
        "actual_state",
    }
)

README = _SUBTREE_ROOT / "README.md"


def _walk_keys(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _walk_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_keys(item, found)
    return found


def test_documents_contain_no_actual_state_keys():
    for name, path in EXAMPLES.items():
        document = json.loads(path.read_text(encoding="utf-8"))
        overlap = _walk_keys(document, set()) & ACTUAL_STATE_KEYS
        assert overlap == set(), f"{name} leaks actual-state keys: {overlap}"


def test_schemas_contain_no_actual_state_keys():
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        overlap = _walk_keys(schema, set()) & ACTUAL_STATE_KEYS
        assert overlap == set(), f"{path.name} leaks actual-state keys: {overlap}"


def test_readme_states_the_desired_actual_separation():
    text = README.read_text(encoding="utf-8")
    for marker in ("Desired", "Actual", "selector-free", "sha256:"):
        assert marker in text, f"README must state {marker!r}"


def test_slice1_implements_no_actual_state_domains():
    """Proposal/Approval/Build/Release exist only as null attachment slots."""
    document = json.loads(EXAMPLES["version"].read_text(encoding="utf-8"))
    attachments = document["attachments"]
    assert sorted(attachments) == ["approval", "build", "proposal", "release"]
    assert all(value is None for value in attachments.values())
