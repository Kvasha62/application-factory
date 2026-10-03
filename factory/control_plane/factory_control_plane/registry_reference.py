"""Read-only cross-check against the canonical Component Registry.

The Component Registry remains the single authority for component
identity, version and dependency facts (ratified source-of-truth rule).
This module reads the canonical registry document; it never writes it,
never mirrors it, and never re-derives dependency or availability facts
into control-plane documents — configurations hold references only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Canonical registry location, relative to the repository root.
REGISTRY_RELATIVE_PATH = "factory/registry/component_registry.json"


class RegistryReferenceError(ValueError):
    """The canonical registry document cannot be read (fail closed)."""


def repository_root(start: Path | None = None) -> Path:
    """Walk upward until the canonical registry document is found."""
    origin = start if start is not None else Path(__file__).resolve()
    if origin.is_file():
        origin = origin.parent
    for candidate in (origin, *origin.parents):
        if (candidate / REGISTRY_RELATIVE_PATH).is_file():
            return candidate
    message = f"repository root holding {REGISTRY_RELATIVE_PATH} not found"
    raise RegistryReferenceError(message)


def load_registry_components(start: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return ``component_id -> registry entry`` from the canonical file."""
    root = repository_root(start)
    try:
        document = json.loads((root / REGISTRY_RELATIVE_PATH).read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        message = f"{REGISTRY_RELATIVE_PATH} is unavailable"
        raise RegistryReferenceError(message) from error
    if not isinstance(document, dict) or not isinstance(
        document.get("components"), list
    ):
        message = f"{REGISTRY_RELATIVE_PATH} has no component list"
        raise RegistryReferenceError(message)
    entries: dict[str, dict[str, Any]] = {}
    for entry in document["components"]:
        if isinstance(entry, dict) and isinstance(entry.get("component_id"), str):
            entries[entry["component_id"]] = entry
    return entries


def component_reference_errors(components: object) -> list[str]:
    """Validate exact references against the live registry, read-only.

    A reference is valid only when the registry publishes the component,
    publishes exactly that version, and its lifecycle state is
    ``registered``. Any disagreement fails closed in the registry's favor.
    """
    if not isinstance(components, list):
        return ["components must be a list"]
    try:
        entries = load_registry_components()
    except RegistryReferenceError as error:
        return [f"registry reference unavailable: {error}"]
    errors: list[str] = []
    for index, component in enumerate(components):
        where = f"components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{where}: must be an object")
            continue
        component_id = component.get("component_id")
        version = component.get("component_version")
        if not isinstance(component_id, str) or not isinstance(version, str):
            errors.append(f"{where}: component_id and component_version required")
            continue
        entry = entries.get(component_id)
        if entry is None:
            errors.append(f"{where}: {component_id!r} is not in the canonical registry")
            continue
        if entry.get("component_version") != version:
            errors.append(
                f"{where}: version {version!r} differs from the registry's "
                f"{entry.get('component_version')!r} for {component_id!r}"
            )
        lifecycle = entry.get("lifecycle")
        state = lifecycle.get("registry_state") if isinstance(lifecycle, dict) else None
        if state != "registered":
            errors.append(
                f"{where}: {component_id!r} registry state is {state!r}, "
                "not 'registered'"
            )
    return errors
