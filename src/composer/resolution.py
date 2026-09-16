"""Deterministic dependency resolution and version selection (Slice E).

ARCHITECTURE.md §17 gives the Composer its duties before a manifest is written:
resolve dependencies, verify compatibility, select versions, apply
configuration, check extensions, produce a reproducible manifest. This module
performs dependency resolution and version selection strictly from
authoritative metadata:

* the candidate set is the authoritative Component Registry — the canonical
  inventory publishes exactly one concrete public version per component
  (ARCHITECTURE.md §11), and the Composer invents no other source of versions;
* a requested explicit version must be that registered version: the Composer
  never substitutes another one;
* a requested explicit range is evaluated against that registered version;
* declared dependencies are added to the composition as a deterministic
  closure, and every addition is recorded as an explicit decision, so a
  composition change is always visible (ARCHITECTURE.md §17: nothing may be
  replaced silently).

Step order is fixed — explicitly requested components are processed in
``component_id`` order, then their declared dependencies — so the same request
against the same registry always yields the same closure, the same decisions
and the same Platform Manifest. No version is ever discovered, guessed or
looked up outside the registry: a component the registry does not publish
cannot enter a platform.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from component_registry import (
    is_floating_selector,
    parse_version_range,
)

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: How a component entered the composition.
SELECTED_AS_EXPLICIT_VERSION = "component_version"
SELECTED_AS_EXPLICIT_RANGE = "version_range"
SELECTED_AS_DEPENDENCY = "dependency"


@dataclass(frozen=True)
class SelectedComponent:
    """One component of the resolved composition.

    ``artifact`` is repeated from the authoritative registry entry — the
    Composer never authors artifact identity (ARCHITECTURE.md §1.3, §11).
    ``required_by`` records the dependents whose declared dependency pulled
    this component in; it is empty for explicitly requested components, so an
    addition is never silent.
    """

    component_id: str
    component_version: str
    artifact: Mapping[str, Any]
    selected_as: str
    constraint: str | None
    required_by: tuple[str, ...] = ()

    def as_manifest_entry(self) -> dict[str, Any]:
        """Return the Platform Manifest component entry for this selection."""
        return {
            "component_id": self.component_id,
            "component_version": self.component_version,
            "artifact": dict(self.artifact),
        }

    def as_decision(self) -> dict[str, Any]:
        """Return the deterministic selection decision for this component."""
        return {
            "component_id": self.component_id,
            "component_version": self.component_version,
            "selected_as": self.selected_as,
            "constraint": self.constraint,
            "required_by": list(self.required_by),
        }


@dataclass(frozen=True)
class Resolution:
    """The resolved composition: concrete selections plus their decisions."""

    selected: tuple[SelectedComponent, ...]

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(component.component_id for component in self.selected)

    @property
    def decisions(self) -> tuple[dict[str, Any], ...]:
        return tuple(component.as_decision() for component in self.selected)

    def manifest_entries(self) -> list[dict[str, Any]]:
        return [component.as_manifest_entry() for component in self.selected]

    def explicit_ids(self) -> tuple[str, ...]:
        return tuple(
            component.component_id
            for component in self.selected
            if component.selected_as != SELECTED_AS_DEPENDENCY
        )

    def resolved_ids(self) -> tuple[str, ...]:
        return tuple(
            component.component_id
            for component in self.selected
            if component.selected_as == SELECTED_AS_DEPENDENCY
        )


def _dependency_errors(component_id: str, dependency: Entry, index: int) -> list[str]:
    """Validate one declared dependency before it can enter the closure."""
    errors: list[str] = []
    path = f"$.components.{component_id}.dependencies[{index}]"
    dependency_id = dependency.get("component_id")
    if not isinstance(dependency_id, str) or not dependency_id:
        errors.append(
            f"{path}.component_id: a declared dependency needs a component identity"
        )
    elif is_floating_selector(dependency_id):
        errors.append(
            f"{path}.component_id: {dependency_id!r} is a floating selector; a declared "
            f"dependency names a concrete component"
        )

    version_range = dependency.get("version_range")
    if parse_version_range(version_range) is None:
        errors.append(
            f"{path}.version_range: {version_range!r} is not an explicit version range; a "
            f"declared dependency states its range explicitly"
        )

    kind = dependency.get("kind")
    if not isinstance(kind, str) or not kind:
        errors.append(f"{path}.kind: a declared dependency needs an explicit kind")

    return errors


def resolve_closure(
    registry: Any, components: Sequence[Entry]
) -> tuple[Resolution, list[str]]:
    """Resolve the requested set and its declared dependency closure.

    Returns the resolution and every deterministic violation. A violation means
    the composition cannot be produced: nothing is dropped, substituted or
    re-pointed to make it succeed.
    """
    errors: list[str] = []
    selections: dict[str, SelectedComponent] = {}
    required_by: dict[str, set[str]] = {}

    registered = set(registry.component_ids)

    def select(
        component_id: str, selected_as: str, constraint: str | None
    ) -> SelectedComponent | None:
        if component_id in selections:
            return selections[component_id]
        if component_id not in registered:
            return None
        entry = registry.entry(component_id)
        version = registry.version(component_id)
        artifact = entry.get("artifact")
        selections[component_id] = SelectedComponent(
            component_id=component_id,
            component_version=version,
            artifact=copy.deepcopy(artifact) if isinstance(artifact, Mapping) else {},
            selected_as=selected_as,
            constraint=constraint,
            required_by=(),
        )
        return selections[component_id]

    # Explicitly requested components first, in deterministic order.
    requested: list[tuple[str, str, str | None]] = []
    for entry in components:
        component_id = entry.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            continue
        if "component_version" in entry:
            requested_as = SELECTED_AS_EXPLICIT_VERSION
            constraint = entry.get("component_version")
        else:
            requested_as = SELECTED_AS_EXPLICIT_RANGE
            constraint = entry.get("version_range")
        requested.append(
            (
                component_id,
                requested_as,
                constraint if isinstance(constraint, str) else None,
            )
        )
    requested.sort(key=lambda item: item[0])

    queue: list[tuple[str, str, str | None, str | None]] = [
        (component_id, selected_as, constraint, None)
        for component_id, selected_as, constraint in requested
    ]
    visited: set[str] = set()

    while queue:
        component_id, selected_as, constraint, parent = queue.pop(0)
        if parent is not None:
            required_by.setdefault(component_id, set()).add(parent)
        if component_id in visited:
            continue
        visited.add(component_id)

        if component_id not in registered:
            if parent is None:
                errors.append(
                    f"$.components: {component_id!r} is not registered in the authoritative "
                    f"Component Registry"
                )
            else:
                errors.append(
                    f"$.components.{parent}.dependencies: {component_id!r} is not registered in "
                    f"the authoritative Component Registry; a composition cannot include a "
                    f"component the registry does not publish"
                )
            continue

        select(component_id, selected_as, constraint)

        for index, dependency in enumerate(registry.dependencies(component_id)):
            if not isinstance(dependency, Mapping):
                continue
            errors.extend(_dependency_errors(component_id, dependency, index))
            dependency_id = dependency.get("component_id")
            if not isinstance(dependency_id, str) or not dependency_id:
                continue
            queue.append(
                (
                    dependency_id,
                    SELECTED_AS_DEPENDENCY,
                    None,
                    component_id,
                )
            )

    ordered: list[SelectedComponent] = []
    for component_id in sorted(selections):
        selection = selections[component_id]
        ordered.append(
            SelectedComponent(
                component_id=selection.component_id,
                component_version=selection.component_version,
                artifact=selection.artifact,
                selected_as=selection.selected_as,
                constraint=selection.constraint,
                required_by=tuple(sorted(required_by.get(component_id, ()))),
            )
        )

    return Resolution(selected=tuple(ordered)), sorted(set(errors))


def selected_versions(resolution: Resolution) -> dict[str, str]:
    """Return ``component_id → selected version`` for the resolved composition."""
    return {
        component.component_id: component.component_version
        for component in resolution.selected
    }


__all__ = [
    "SELECTED_AS_DEPENDENCY",
    "SELECTED_AS_EXPLICIT_RANGE",
    "SELECTED_AS_EXPLICIT_VERSION",
    "Resolution",
    "SelectedComponent",
    "resolve_closure",
    "selected_versions",
]
