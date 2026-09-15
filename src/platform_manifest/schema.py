"""Deterministic structural validation of the Platform Manifest document.

The normative schema lives in
``factory/platform_manifest/schema/platform_manifest.schema.json``
and is the declarative contract of the manifest format.

This module reuses the small, dependency-free evaluator from Slice A:
it supports ``$ref`` (local ``#/$defs/...`` only), ``type``, ``enum``,
``const``, ``pattern``, ``minLength``, ``minItems``, ``minimum``,
``required``, ``properties``, ``additionalProperties``, ``items`` and
``oneOf`` — exactly the subset used by the manifest schema.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_PATH = "factory/platform_manifest/schema/platform_manifest.schema.json"

SchemaDocument = Mapping[str, Any]


def load_schema(root: Path) -> SchemaDocument:
    """Return the normative manifest schema read from ``root``."""
    path = root / SCHEMA_PATH
    if not path.is_file():
        message = f"manifest schema not found: {SCHEMA_PATH}"
        raise FileNotFoundError(message)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        message = f"manifest schema is not a JSON object: {SCHEMA_PATH}"
        raise TypeError(message)
    return document


def _resolve_ref(ref: str, root: SchemaDocument) -> SchemaDocument:
    if not ref.startswith("#/"):
        message = f"unsupported JSON Schema reference: {ref}"
        raise ValueError(message)
    node: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[token]
    return node


def _matches_type(instance: Any, expected: Any) -> bool:
    names = (expected,) if isinstance(expected, str) else tuple(expected)
    for name in names:
        if name == "object" and isinstance(instance, Mapping):
            return True
        if name == "array" and isinstance(instance, list):
            return True
        if name == "string" and isinstance(instance, str):
            return True
        if name == "boolean" and isinstance(instance, bool):
            return True
        if (
            name == "integer"
            and isinstance(instance, int)
            and not isinstance(instance, bool)
        ):
            return True
        if (
            name == "number"
            and isinstance(instance, (int, float))
            and not isinstance(instance, bool)
        ):
            return True
        if name == "null" and instance is None:
            return True
    return False


def validate_structure(
    instance: Any,
    schema: SchemaDocument,
    path: str = "$",
    *,
    root_schema: SchemaDocument | None = None,
) -> list[str]:
    """Validate ``instance`` against ``schema`` and return every violation."""
    document = schema if root_schema is None else root_schema
    errors: list[str] = []

    if "$ref" in schema:
        return validate_structure(
            instance,
            _resolve_ref(schema["$ref"], document),
            path,
            root_schema=document,
        )

    if "type" in schema and not _matches_type(instance, schema["type"]):
        errors.append(
            f"{path}: expected type {schema['type']!r}, got {type(instance).__name__}"
        )
        return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(
            f"{path}: expected constant {schema['const']!r}, got {instance!r}"
        )

    if "enum" in schema and instance not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        errors.append(f"{path}: {instance!r} is not one of [{allowed}]")

    if isinstance(instance, str):
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, instance) is None:
            errors.append(f"{path}: {instance!r} does not match pattern {pattern!r}")
        min_length = schema.get("minLength")
        if min_length is not None and len(instance) < min_length:
            errors.append(
                f"{path}: {instance!r} is shorter than minLength {min_length}"
            )

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if minimum is not None and instance < minimum:
            errors.append(f"{path}: {instance!r} is below minimum {minimum}")

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(instance) < min_items:
            errors.append(
                f"{path}: expected at least {min_items} item(s), got {len(instance)}"
            )
        items_schema = schema.get("items")
        if items_schema is not None:
            for index, item in enumerate(instance):
                errors.extend(
                    validate_structure(
                        item,
                        items_schema,
                        f"{path}[{index}]",
                        root_schema=document,
                    )
                )

    if isinstance(instance, Mapping):
        for key in schema.get("required", ()):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties: Mapping[str, Any] = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(
                    validate_structure(
                        value,
                        properties[key],
                        f"{path}.{key}",
                        root_schema=document,
                    )
                )
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}: unexpected property {key!r}")
        elif isinstance(additional, Mapping):
            for key, value in instance.items():
                if key not in properties:
                    errors.extend(
                        validate_structure(
                            value,
                            additional,
                            f"{path}.{key}",
                            root_schema=document,
                        )
                    )

    branches = schema.get("oneOf", ())
    if branches:
        matches = [
            branch
            for branch in branches
            if not validate_structure(instance, branch, path, root_schema=document)
        ]
        if len(matches) != 1:
            errors.append(
                f"{path}: {instance!r} matches {len(matches)} of {len(branches)} oneOf branches, expected exactly 1"
            )

    return errors
