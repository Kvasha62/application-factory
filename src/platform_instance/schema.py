"""Deterministic structural validation of the Platform Instance document.

The normative schema lives in
``factory/platform_instance/schema/platform_instance.schema.json``
and is the declarative contract of the instance assembly format.

This module reuses the small, dependency-free JSON Schema subset evaluator
shipped by Slice C (``platform_manifest.schema.validate_structure``): the
factory has one structural evaluator, not one per slice. It supports
``$ref`` (local ``#/$defs/...`` only), ``type``, ``enum``, ``const``,
``pattern``, ``minLength``, ``minItems``, ``minimum``, ``required``,
``properties``, ``additionalProperties``, ``items`` and ``oneOf`` — exactly
the subset used by the instance schema.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from platform_manifest.schema import validate_structure

#: Canonical instance schema path, relative to repository root.
SCHEMA_PATH = "factory/platform_instance/schema/platform_instance.schema.json"

SchemaDocument = Mapping[str, Any]


def load_schema(root: Path) -> SchemaDocument:
    """Return the normative instance schema read from ``root``."""
    path = root / SCHEMA_PATH
    if not path.is_file():
        message = f"platform instance schema not found: {SCHEMA_PATH}"
        raise FileNotFoundError(message)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        message = f"platform instance schema is not a JSON object: {SCHEMA_PATH}"
        raise TypeError(message)
    return document


def validate_structure_against_schema(
    instance: object, schema: SchemaDocument
) -> list[str]:
    """Validate ``instance`` against the normative instance schema."""
    return validate_structure(instance, schema)


__all__ = ["SCHEMA_PATH", "load_schema", "validate_structure_against_schema"]
