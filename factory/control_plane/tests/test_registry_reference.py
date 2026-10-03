"""Registry authority proofs (Slice 1).

Proves: the Component Registry stays canonical — control-plane code only
reads it to cross-check references; configuration documents never carry
parallel component facts (no dependencies, lifecycle, or availability
data); unknown/different-version/non-registered references fail closed.
"""

from __future__ import annotations

import json

from conftest import EXAMPLES, REPOSITORY_ROOT
from factory_control_plane import registry_reference

PARALLEL_AUTHORITY_KEYS = frozenset(
    {
        "artifact",
        "available_versions",
        "class",
        "compatibility",
        "contracts",
        "data_ownership",
        "dependencies",
        "dependency",
        "deployable",
        "latest_version",
        "lifecycle",
        "maturity_level",
        "owner",
        "owner_source",
        "publishability_blockers",
        "publishable",
        "registry_state",
        "scs_id",
        "versions",
    }
)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _walk_keys(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _walk_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_keys(item, found)
    return found


def test_registry_is_canonical_and_referenced_read_only():
    entries = registry_reference.load_registry_components()
    assert entries, "canonical registry must be readable"
    assert entries["identity"]["component_version"] == "0.3.0"
    version = _load(EXAMPLES["version"])
    assert registry_reference.component_reference_errors(version["components"]) == []
    # the registry file itself is never written by this package: only reads
    root = registry_reference.repository_root()
    assert root == REPOSITORY_ROOT


def test_unknown_component_reference_fails_closed():
    errors = registry_reference.component_reference_errors(
        [{"component_id": "not_a_component", "component_version": "1.0.0"}]
    )
    assert errors and "not in the canonical registry" in errors[0]


def test_different_version_reference_fails_closed():
    errors = registry_reference.component_reference_errors(
        [{"component_id": "identity", "component_version": "9.9.9"}]
    )
    assert errors and "differs from the registry" in errors[0]


def test_non_registered_component_fails_closed(monkeypatch):
    entries = registry_reference.load_registry_components()
    entries["identity"] = dict(entries["identity"])
    entries["identity"]["lifecycle"] = {
        **entries["identity"]["lifecycle"],
        "registry_state": "deprecated",
    }
    monkeypatch.setattr(registry_reference, "load_registry_components", lambda: entries)
    errors = registry_reference.component_reference_errors(
        [{"component_id": "identity", "component_version": "0.3.0"}]
    )
    assert errors and "registry state" in errors[0]


def test_configuration_documents_carry_no_parallel_authority_data():
    version = _load(EXAMPLES["version"])
    keys = _walk_keys(version, set())
    overlap = keys & PARALLEL_AUTHORITY_KEYS
    assert overlap == set(), f"config document re-declares registry facts: {overlap}"
    schema_dir = EXAMPLES["version"].parent.parent / "schema"
    for path in sorted(schema_dir.glob("*.json")):
        schema_keys = _walk_keys(_load(path), set())
        overlap = schema_keys & PARALLEL_AUTHORITY_KEYS
        assert overlap == set(), f"{path.name} re-declares registry facts: {overlap}"


def test_registry_document_itself_is_untouched_by_tests():
    registry_path = REPOSITORY_ROOT / "factory" / "registry" / "component_registry.json"
    before = registry_path.stat().st_mtime_ns
    registry_reference.load_registry_components()
    after = registry_path.stat().st_mtime_ns
    assert before == after, "reading the registry must not modify it"
