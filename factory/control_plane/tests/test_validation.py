"""Schema and document validation proofs (Slice 1).

Proves: the shipped examples validate against both the handwritten
validators and their JSON Schemas; malformed documents are refused;
selector-free exact-version enforcement holds; builders fail closed.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from conftest import EXAMPLES, SCHEMA_DIR
from factory_control_plane import documents, validation

SCHEMAS = {
    "project": "project.schema.json",
    "requirements": "requirements_version.schema.json",
    "index": "configuration_index.schema.json",
    "version": "configuration_version.schema.json",
}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_schema(name):
    return _load(SCHEMA_DIR / SCHEMAS[name])


def schema_accepts(schema, value) -> bool:
    """Minimal stdlib JSON-Schema check for the shapes this subtree uses."""
    if "oneOf" in schema:
        return any(schema_accepts(branch, value) for branch in schema["oneOf"])
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    declared = schema.get("type")
    if declared is not None:
        allowed = declared if isinstance(declared, list) else [declared]
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
        }
        if not any(checks[kind](value) for kind in allowed):
            return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        for key in schema.get("required", []):
            if key not in value:
                return False
        for key, sub in properties.items():
            if key in value and not schema_accepts(sub, value[key]):
                return False
    if isinstance(value, list):
        items = schema.get("items")
        if items is not None and not all(schema_accepts(items, item) for item in value):
            return False
        if "minItems" in schema and len(value) < schema["minItems"]:
            return False
    if isinstance(value, str):
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            return False
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
    return True


VALIDATORS = {
    "project": validation.project_errors,
    "requirements": validation.requirements_errors,
    "index": validation.configuration_index_errors,
    "version": validation.configuration_version_errors,
}


def test_examples_pass_handwritten_validators():
    for name, path in EXAMPLES.items():
        document = _load(path)
        assert VALIDATORS[name](document) == [], name


def test_examples_conform_to_their_json_schemas():
    for name, path in EXAMPLES.items():
        schema = _load_schema(name)
        assert schema_accepts(schema, _load(path)), name


def test_malformed_project_is_refused():
    broken = copy.deepcopy(_load(EXAMPLES["project"]))
    broken["status"] = "frozen"
    broken["extra"] = True
    errors = validation.project_errors(broken)
    assert any("status" in error for error in errors)
    assert any("unknown field" in error for error in errors)


def test_malformed_requirements_are_refused():
    broken = copy.deepcopy(_load(EXAMPLES["requirements"]))
    broken["entries"][0]["kind"] = "wish"
    broken["entries"].append(dict(broken["entries"][0]))
    errors = validation.requirements_errors(broken)
    assert any("kind" in error for error in errors)
    assert any("duplicated" in error for error in errors)


def test_malformed_configuration_version_is_refused():
    broken = copy.deepcopy(_load(EXAMPLES["version"]))
    broken["attachments"]["proposal"] = {"proposal_id": "p1"}
    broken["manifest"]["manifest_id"] = "Demo Shop Platform"
    errors = validation.configuration_version_errors(broken)
    assert any("attachment" in error for error in errors)
    assert any("manifest_id" in error for error in errors)


def test_selector_free_exact_version_enforcement():
    accepted = ["0.3.0", "1.2.3", "1.2.3-rc.1", "1.0.0+build.5"]
    refused = ["^1.0.0", "~1.2", "1.0.*", "*", ">=1,<2", "1.0", "latest", "1.0.0 ", ""]
    for version in accepted:
        assert validation.is_exact_version(version) is True, version
    for version in refused:
        assert validation.is_exact_version(version) is False, version
        broken = copy.deepcopy(_load(EXAMPLES["version"]))
        broken["components"][0]["component_version"] = version
        errors = validation.configuration_version_errors(broken)
        assert any("selector-free" in error for error in errors), version


def test_builders_fail_closed_on_invalid_documents():
    with pytest.raises(validation.ControlPlaneError):
        documents.new_project(
            project_id="Demo Shop", title="x", created_at="2026-10-03"
        )
    with pytest.raises(validation.ControlPlaneError):
        documents.new_configuration_version(
            project_ref="demo_shop",
            requirements_ref=_load(EXAMPLES["requirements"]),
            manifest_id="demo_shop_platform",
            components=[{"component_id": "identity", "component_version": "^0.3.0"}],
            configuration={},
        )
    with pytest.raises(validation.ControlPlaneError):
        documents.new_requirements_version(project_ref="demo_shop", entries=[])
