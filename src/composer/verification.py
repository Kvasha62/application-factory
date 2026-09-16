"""Composition verification — compatibility, contracts, configuration, extensions.

A composition is produced only from published factory metadata and only when it
is compatible and contract-valid. This module performs the checks that decide
that question and always reports *every* violation, sorted:

* **Dependency compatibility** (ARCHITECTURE.md §18; ADR-0015 §8, §10): every
  declared dependency of every composed component is present in the
  composition and the selected version satisfies the declared range; a
  forbidden dependency mechanism is never a way to satisfy a dependency.
* **Contract validity** (LAW-05; ADR-0015 §15): the published contract of every
  composed component exists, names the same identity and version, and declares
  the same dependencies as the authoritative registry. A registry that
  diverges from its published contract cannot be composed.
* **Data ownership** (LAW-03; ADR-0015 §11): component data ownership metadata
  is preserved as published — the Composer never re-points ownership.
* **Configuration** (ARCHITECTURE.md §20): every configuration key must be
  declared by the component's published configuration schema; an unknown key is
  a BUILD ERROR, and an unsupported schema keyword fails closed instead of
  being silently skipped.
* **Extensions** (ARCHITECTURE.md §21): an extension works only through a
  contract the authoritative registry publishes for a component that is part of
  the composed platform.
* **Golden Bundle evidence** (ARCHITECTURE.md §14, §31; ADR-0015 §8): a
  referenced bundle must be certified, must match its declared identity, and
  the composed platform must be exactly the bundle's pinned set — a bundle's
  certification is never inherited by a different composition.

Nothing here reads business data or a component's internals: the checks read
the request, the authoritative registry, published contracts and the bundle
document only (ADR-0015 §8, §11). No check depends on time, randomness, the
network or file iteration order, so the same inputs always produce the same
report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from component_registry import parse_version_range, range_admits
from composer.request import FORBIDDEN_DEPENDENCY_KINDS
from composer.resolution import Resolution
from golden_bundle import validate_document as validate_bundle_document
from golden_bundle.bundle import compute_bundle_digest, load_bundle_document

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Keywords this verification understands in a published component configuration
#: schema. An unsupported keyword fails closed: silently skipping it would let a
#: configuration key pass unchecked, which ARCHITECTURE.md §20 forbids.
#:
#: ``additionalProperties`` is accepted but not decisive — §20 makes *any*
#: undeclared configuration key a BUILD ERROR, whether or not a published schema
#: happens to be permissive, so the declared ``properties`` are what count.
#: ``default`` is accepted and deliberately not applied: the Composer never
#: injects a value the request did not state.
SUPPORTED_CONFIGURATION_KEYWORDS = frozenset(
    {
        "$schema",
        "additionalProperties",
        "const",
        "default",
        "description",
        "enum",
        "items",
        "maxLength",
        "maximum",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)

#: Golden Bundle states a new platform may be composed from (ARCHITECTURE.md
#: §14): only a certified bundle is a validated known-good composition.
#: draft/candidate bundles carry no certification evidence yet, and
#: deprecated/revoked bundles must not serve new deliveries.
COMPOSABLE_BUNDLE_STATES = frozenset({"certified"})

_JSON_TYPES = frozenset(
    {"object", "array", "string", "boolean", "integer", "number", "null"}
)


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _type_matches(value: object, expected: object) -> bool:
    names = (expected,) if isinstance(expected, str) else tuple(expected or ())
    for name in names:
        if name == "object" and isinstance(value, Mapping):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if (
            name == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return True
        if name == "null" and value is None:
            return True
    return False


# ---------------------------------------------------------------------------
# Dependency compatibility
# ---------------------------------------------------------------------------


def verify_dependency_compatibility(registry: Any, resolution: Resolution) -> list[str]:
    """Verify every declared dependency of every composed component."""
    errors: list[str] = []
    versions = {
        component.component_id: component.component_version
        for component in resolution.selected
    }

    for component in resolution.selected:
        component_id = component.component_id
        for index, dependency in enumerate(registry.dependencies(component_id)):
            if not _is_mapping(dependency):
                continue
            dependency_id = dependency.get("component_id")
            path = f"$.compositions.{component_id}.dependencies[{index}]"
            if not isinstance(dependency_id, str) or not dependency_id:
                continue

            kind = dependency.get("kind")
            if isinstance(kind, str) and kind in FORBIDDEN_DEPENDENCY_KINDS:
                errors.append(
                    f"{path}.kind: {kind!r} is a forbidden dependency mechanism "
                    f"(ARCHITECTURE.md §1.1, §18); a composition is built from published "
                    f"contracts only"
                )

            if dependency_id not in versions:
                errors.append(
                    f"{path}: {component_id} declares a dependency on {dependency_id}, which is "
                    f"absent from the composition; the Composer never drops a declared "
                    f"dependency to make a composition succeed"
                )
                continue

            version_range = dependency.get("version_range")
            selected = versions[dependency_id]
            if parse_version_range(version_range) is None:
                errors.append(
                    f"{path}.version_range: {version_range!r} is not an explicit version range"
                )
            elif not range_admits(version_range, selected):
                errors.append(
                    f"{path}: incompatible dependency — {component_id} requires {dependency_id} "
                    f"{version_range!r}, but {dependency_id} {selected} was selected; the "
                    f"Composer never substitutes or silently replaces a version "
                    f"(ARCHITECTURE.md §1.4, §17)"
                )

    return errors


# ---------------------------------------------------------------------------
# Published contracts and data ownership
# ---------------------------------------------------------------------------


def contract_path(entry: Entry) -> object:
    """Return the canonical contract path the registry publishes for ``entry``."""
    contracts = entry.get("contracts")
    if not _is_mapping(contracts):
        return None
    return contracts.get("component_contract")


def read_published_contract(root: Path, path: object) -> tuple[Entry | None, str]:
    """Read one published contract; return ``(document, reason)`` on failure."""
    if not isinstance(path, str) or not path:
        return None, "the registry publishes no component contract path"
    contract_path = root / path
    if not contract_path.is_file():
        return None, f"the published contract is missing: {path}"
    try:
        document = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, (
            f"the published contract is unreadable: {path} ({error.__class__.__name__})"
        )
    if not _is_mapping(document):
        return None, f"the published contract is not a JSON object: {path}"
    return document, ""


def _dependency_triples(dependencies: object) -> set[tuple[object, object, object]]:
    if not isinstance(dependencies, list):
        return set()
    triples: set[tuple[object, object, object]] = set()
    for dependency in dependencies:
        if not _is_mapping(dependency):
            continue
        triples.add(
            (
                dependency.get("component_id"),
                dependency.get("kind"),
                dependency.get("version_range"),
            )
        )
    return triples


def read_composed_contracts(
    root: Path, registry: Any, resolution: Resolution
) -> tuple[dict[str, Entry], list[str]]:
    """Read the published contract of every composed component.

    Returns ``component_id → contract`` for every contract that could be read,
    plus one violation per contract that could not. This is the only place a
    contract read failure is reported, so a composition reports it once.
    """
    contracts: dict[str, Entry] = {}
    errors: list[str] = []
    for component in resolution.selected:
        component_id = component.component_id
        entry = registry.entry(component_id)
        contract, failure = read_published_contract(root, contract_path(entry))
        if contract is None:
            errors.append(
                f"$.compositions.{component_id}.contract: {component_id} cannot be composed as a "
                f"contract-valid component — {failure}"
            )
            continue
        contracts[component_id] = contract
    return contracts, errors


def verify_contracts(
    registry: Any, resolution: Resolution, contracts: Mapping[str, Entry]
) -> list[str]:
    """Verify identity, version, dependency and ownership agreement per contract.

    A contract that could not be read is not reported here: it has already been
    reported by :func:`read_composed_contracts`, and no manifest is produced in
    that case.
    """
    errors: list[str] = []

    for component in resolution.selected:
        component_id = component.component_id
        path = f"$.compositions.{component_id}"
        contract = contracts.get(component_id)
        if contract is None:
            continue

        if contract.get("component_id") != component_id:
            errors.append(
                f"{path}.contract.component_id: {contract.get('component_id')!r} does not match "
                f"the composed component {component_id!r}"
            )
        if contract.get("component_version") != component.component_version:
            errors.append(
                f"{path}.contract.component_version: {contract.get('component_version')!r} does "
                f"not match the selected version {component.component_version!r}; a composition "
                f"is never built on a contract of a different version"
            )

        registry_triples = _dependency_triples(
            registry.entry(component_id).get("dependencies")
        )
        contract_triples = _dependency_triples(contract.get("dependencies"))
        if registry_triples != contract_triples:
            errors.append(
                f"{path}.contract.dependencies: the published contract and the authoritative "
                f"Component Registry declare different dependencies (registry-only "
                f"{sorted(registry_triples - contract_triples, key=repr)!r}, contract-only "
                f"{sorted(contract_triples - registry_triples, key=repr)!r})"
            )

        ownership = contract.get("data_ownership")
        if _is_mapping(ownership) and ownership.get("owner") not in (
            None,
            component_id,
        ):
            errors.append(
                f"{path}.contract.data_ownership.owner: {ownership.get('owner')!r} is not the "
                f"component that owns these data (LAW-03); a composition never re-points "
                f"ownership"
            )

    return errors


# ---------------------------------------------------------------------------
# Configuration (ARCHITECTURE.md §20)
# ---------------------------------------------------------------------------


def _configuration_errors(
    configuration: object, schema: object, path: str
) -> list[str]:
    """Validate one component configuration against its published schema."""
    errors: list[str] = []
    if not _is_mapping(schema):
        return [
            (
                f"{path}: this component publishes no configuration schema; no configuration "
                f"key is declared for it, and an unknown configuration key is a BUILD ERROR "
                f"(ARCHITECTURE.md §20)"
            )
        ]

    unsupported = sorted(set(schema) - SUPPORTED_CONFIGURATION_KEYWORDS)
    if unsupported:
        return [
            (
                f"{path}: the published configuration schema uses unsupported keywords "
                f"{unsupported!r}; composition is fail-closed (ARCHITECTURE.md §20)"
            )
        ]

    if not _is_mapping(configuration):
        return [f"{path}: a component configuration must be an object"]

    properties = schema.get("properties")
    declared = properties if _is_mapping(properties) else {}

    for key in configuration:
        if key not in declared:
            errors.append(
                f"{path}.{key}: unknown configuration key {key!r} for this component "
                f"(ARCHITECTURE.md §20: an unknown configuration key is a BUILD ERROR)"
            )

    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in configuration:
                errors.append(
                    f"{path}: required configuration key {key!r} is absent; component "
                    f"configuration is declarative and explicit (ARCHITECTURE.md §20)"
                )

    for key, value in configuration.items():
        value_schema = declared.get(key)
        if isinstance(value_schema, Mapping):
            errors.extend(
                _configuration_value_errors(value, value_schema, f"{path}.{key}")
            )

    return errors


def _configuration_value_errors(value: object, schema: Entry, path: str) -> list[str]:
    """Validate one configuration value against its declared schema."""
    errors: list[str] = []

    unsupported = sorted(set(schema) - SUPPORTED_CONFIGURATION_KEYWORDS)
    if unsupported:
        return [
            (
                f"{path}: the published configuration schema uses unsupported keywords "
                f"{unsupported!r}; composition is fail-closed (ARCHITECTURE.md §20)"
            )
        ]

    expected = schema.get("type")
    if expected is not None:
        if isinstance(expected, list) and not set(expected) <= _JSON_TYPES:
            return [
                (
                    f"{path}: the published configuration schema declares an unknown type "
                    f"{expected!r}; composition is fail-closed (ARCHITECTURE.md §20)"
                )
            ]
        if not _type_matches(value, expected):
            return [f"{path}: expected type {expected!r}, got {type(value).__name__}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}, got {value!r}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        allowed = ", ".join(repr(item) for item in enum)
        errors.append(f"{path}: {value!r} is not one of [{allowed}]")

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path}: {value!r} does not match pattern {pattern!r}")
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path}: {value!r} is shorter than minLength {min_length}")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path}: {value!r} is longer than maxLength {max_length}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: {value!r} is below minimum {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: {value!r} is above maximum {maximum}")

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                errors.extend(
                    _configuration_value_errors(item, items, f"{path}[{index}]")
                )

    if isinstance(value, Mapping):
        errors.extend(_configuration_errors(value, schema, path))

    return errors


def verify_configuration(
    registry: Any,
    resolution: Resolution,
    configuration: Entry,
    contracts: Mapping[str, Entry],
) -> list[str]:
    """Verify the requested component configuration (ARCHITECTURE.md §20).

    A component whose contract could not be read is not reported here: contract
    reading failure has already been reported once, and no manifest is produced
    in that case.
    """
    errors: list[str] = []
    composed = {component.component_id for component in resolution.selected}

    for component_id in sorted(configuration):
        path = f"$.configuration.{component_id}"
        if component_id not in composed:
            errors.append(
                f"{path}: configuration was requested for {component_id!r}, which is not part "
                f"of the composed platform; a composition configures exactly the components it "
                f"contains"
            )
            continue

        contract = contracts.get(component_id)
        if contract is None:
            continue

        errors.extend(
            _configuration_errors(
                configuration[component_id], contract.get("configuration_schema"), path
            )
        )

    return errors


# ---------------------------------------------------------------------------
# Extensions (ARCHITECTURE.md §21)
# ---------------------------------------------------------------------------


def verify_extensions(
    registry: Any, resolution: Resolution, extensions: tuple[Entry, ...]
) -> list[str]:
    """Verify that every extension works through a published component contract."""
    errors: list[str] = []
    composed = {component.component_id for component in resolution.selected}

    for index, extension in enumerate(extensions):
        path = f"$.extensions[{index}]"
        component_id = extension.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            continue
        if component_id not in composed:
            errors.append(
                f"{path}.component_id: the extension extends {component_id!r}, which is not part "
                f"of the composed platform; an extension never introduces a component"
            )
            continue

        contracts = registry.entry(component_id).get("contracts")
        published = (
            {value for value in contracts.values() if isinstance(value, str) and value}
            if _is_mapping(contracts)
            else set()
        )
        contract = extension.get("contract")
        if not isinstance(contract, str) or contract not in published:
            errors.append(
                f"{path}.contract: {contract!r} is not a contract the authoritative Component "
                f"Registry publishes for {component_id} (published: {sorted(published)!r}); an "
                f"extension works through a published contract only (ARCHITECTURE.md §21)"
            )

    return errors


# ---------------------------------------------------------------------------
# Golden Bundle evidence (ARCHITECTURE.md §14, §31; ADR-0015 §8)
# ---------------------------------------------------------------------------


def _bundle_pairs(document: Entry) -> list[Entry]:
    compatibility = document.get("compatibility")
    if not _is_mapping(compatibility):
        return []
    pairs = compatibility.get("pairs")
    if not isinstance(pairs, list):
        return []
    return [pair for pair in pairs if _is_mapping(pair)]


def verify_golden_bundle(
    root: Path, registry: Any, resolution: Resolution, reference: Entry
) -> tuple[dict[str, Any] | None, list[str]]:
    """Verify a referenced certified bundle against the composed platform.

    Returns the manifest-level bundle reference (``bundle_id`` +
    ``bundle_version`` + ``bundle_digest``) and every violation. A composition
    without a bundle is explicitly uncertified (ARCHITECTURE.md §31): that
    status is expressed by ``golden_bundle: null`` in the produced manifest,
    never by an implied certification.
    """
    path = reference.get("path")
    if not isinstance(path, str) or not path:
        return None, [
            (
                "$.golden_bundle.path: a repository-relative bundle path is required to consume "
                "a Golden Bundle"
            )
        ]

    bundle_path = root / path
    if not bundle_path.is_file():
        return None, [
            f"$.golden_bundle.path: the referenced Golden Bundle is missing: {path}"
        ]

    try:
        document = load_bundle_document(bundle_path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, [
            (
                f"$.golden_bundle.path: the referenced Golden Bundle is unreadable: {path} "
                f"({error.__class__.__name__})"
            )
        ]

    errors: list[str] = [
        f"$.golden_bundle: the referenced Golden Bundle is invalid — {error}"
        for error in validate_bundle_document(document, root=root)
    ]
    if errors:
        return None, errors

    declared_id = reference.get("bundle_id")
    declared_version = reference.get("bundle_version")
    declared_digest = reference.get("bundle_digest")
    if document.get("bundle_id") != declared_id:
        errors.append(
            f"$.golden_bundle.bundle_id: {declared_id!r} is not the identity of the bundle at "
            f"{path} ({document.get('bundle_id')!r})"
        )
    if document.get("bundle_version") != declared_version:
        errors.append(
            f"$.golden_bundle.bundle_version: {declared_version!r} is not the version of the "
            f"bundle at {path} ({document.get('bundle_version')!r})"
        )
    if (
        document.get("bundle_digest") != declared_digest
        or compute_bundle_digest(document) != declared_digest
    ):
        errors.append(
            f"$.golden_bundle.bundle_digest: {declared_digest!r} does not match the content "
            f"digest of the bundle at {path}; a bundle identity is pinned by its digest"
        )

    lifecycle = document.get("lifecycle")
    state = lifecycle.get("state") if _is_mapping(lifecycle) else None
    if state not in COMPOSABLE_BUNDLE_STATES:
        errors.append(
            f"$.golden_bundle.lifecycle.state: the bundle is {state!r}; only a certified Golden "
            f"Bundle is a validated known-good composition a new platform may be composed from "
            f"(ARCHITECTURE.md §14)"
        )

    pinned = {
        entry["component_id"]: entry["component_version"]
        for entry in document.get("components", [])
        if _is_mapping(entry) and isinstance(entry.get("component_id"), str)
    }
    selected = {
        component.component_id: component.component_version
        for component in resolution.selected
    }
    missing = sorted(set(pinned) - set(selected))
    extra = sorted(set(selected) - set(pinned))
    if missing:
        errors.append(
            f"$.golden_bundle.components: the certified bundle pins {missing!r}, which the "
            f"composition does not contain; a bundle's certification is never inherited by a "
            f"different composition"
        )
    if extra:
        errors.append(
            f"$.golden_bundle.components: the composition contains {extra!r}, which the "
            f"certified bundle does not pin; adding a component produces an uncertified "
            f"composition that must not claim the bundle's certification"
        )
    for component_id in sorted(set(pinned) & set(selected)):
        if pinned[component_id] != selected[component_id]:
            errors.append(
                f"$.golden_bundle.components: {component_id} is pinned by the bundle at "
                f"{pinned[component_id]!r} but the composition selects "
                f"{selected[component_id]!r}; the Composer never substitutes a version"
            )

    declared_pairs = {
        (pair.get("from"), pair.get("to"), pair.get("mechanism")): pair.get(
            "compatible"
        )
        for pair in _bundle_pairs(document)
    }
    for owner in sorted(selected):
        for dependency in registry.dependencies(owner):
            if not _is_mapping(dependency):
                continue
            dependency_id = dependency.get("component_id")
            if dependency_id not in selected:
                continue
            mechanism = dependency.get("kind")
            if declared_pairs.get((owner, dependency_id, mechanism)) is not True:
                errors.append(
                    f"$.golden_bundle.compatibility.pairs: the certified bundle records no "
                    f"compatible {mechanism!r} relationship for {owner} → {dependency_id}; a "
                    f"composition over a bundle uses only its validated relationships "
                    f"(ADR-0015 §7)"
                )

    if errors:
        return None, sorted(set(errors))

    manifest_reference = {
        "bundle_id": document.get("bundle_id"),
        "bundle_version": document.get("bundle_version"),
        "bundle_digest": document.get("bundle_digest"),
    }
    return manifest_reference, []


# ---------------------------------------------------------------------------
# Composition verification entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of verifying one resolved composition."""

    errors: tuple[str, ...]
    golden_bundle: dict[str, Any] | None = None

    @property
    def rejected(self) -> bool:
        return bool(self.errors)


def verify_composition(
    root: Path,
    registry: Any,
    resolution: Resolution,
    *,
    configuration: Entry,
    extensions: tuple[Entry, ...],
    golden_bundle: Entry | None = None,
) -> VerificationResult:
    """Verify one resolved composition and return every violation.

    Reading, compatibility, contract validity, configuration, extensions and
    bundle evidence are checked exactly once each, so the report carries no
    duplicate lines. The caller decides what to do with the result; the
    Composer rejects a composition with any violation (ADR-0015 §8).
    """
    contracts, errors = read_composed_contracts(root, registry, resolution)
    errors = [
        *errors,
        *verify_dependency_compatibility(registry, resolution),
        *verify_contracts(registry, resolution, contracts),
        *verify_configuration(registry, resolution, configuration, contracts),
        *verify_extensions(registry, resolution, extensions),
    ]

    reference: dict[str, Any] | None = None
    if golden_bundle is not None:
        reference, bundle_errors = verify_golden_bundle(
            root, registry, resolution, golden_bundle
        )
        errors.extend(bundle_errors)

    return VerificationResult(
        errors=tuple(sorted(set(errors))), golden_bundle=reference
    )
