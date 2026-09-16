"""Composition request — the Composer input document (Slice E).

A composition request is factory-level composition metadata (ADR-0015 §11): it
names the Platform Manifest identity to produce, the components the platform
asks for with explicit versions or explicit version constraints, and the
optional Golden Bundle reference, configuration, extensions and branding.

It owns no business data, restates no component metadata and carries no
artifact identity of its own: artifact identity is repeated from the
authoritative Component Registry during composition, never authored here.

Public surface:

* :func:`load_request` — load and strictly validate a request document;
* :class:`CompositionRequest` — read access and validation of a request;
* :func:`build_request_document` — deterministic request construction;
* :func:`validate_request_document` — deterministic validation of any request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from component_registry import (
    RegistryError,
    is_floating_selector,
    load_registry,
    parse_semver,
    parse_version_range,
    range_admits,
)
from composer.errors import ComposerNotFoundError, ComposerValidationError
from composer.schema import SCHEMA_PATH, load_schema, validate_structure

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Canonical composition request schema path, relative to repository root.
REQUEST_SCHEMA_PATH = SCHEMA_PATH

#: Composer directory: the request format and its deterministic example live
#: here. The request itself is a per-platform artifact; this directory holds
#: the normative schema and the example, never a second source of truth.
REQUEST_DIR = "factory/composer"

#: Deterministic example request produced by ``python -m composer build-example``.
EXAMPLE_REQUEST_PATH = "factory/composer/example_request.json"

MANIFEST_ID_PATTERN = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"
COMPONENT_ID_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"

_MANIFEST_ID_RE = re.compile(MANIFEST_ID_PATTERN)
_COMPONENT_ID_RE = re.compile(COMPONENT_ID_PATTERN)

#: Permitted extension mechanisms (ARCHITECTURE.md §21). A mechanism outside
#: this set would have to reach a component by something other than its
#: published contract, which the law forbids.
EXTENSION_MECHANISMS = frozenset(
    {
        "webhook",
        "custom_fields",
        "ui_widget",
        "theme",
        "plugin",
        "official_extension_api",
    }
)

#: Dependency kinds the factory may carry (ARCHITECTURE.md §18). ``database``,
#: ``internal_code``, ``private_schema`` and ``internal_queue`` are forbidden
#: and must never appear in a composition.
FORBIDDEN_DEPENDENCY_KINDS = frozenset(
    {"database", "internal_code", "private_schema", "internal_queue"}
)


def discover_root(start: Path | None = None) -> Path:
    """Return the repository root, found by walking up from ``start``."""
    origin = Path(start if start is not None else __file__).resolve()
    candidates = [origin, *origin.parents]
    for candidate in candidates:
        base = candidate if candidate.is_dir() else candidate.parent
        if (base / "pyproject.toml").is_file() and (base / "components").is_dir():
            return base
    message = f"repository root not found above {origin}"
    raise ComposerNotFoundError(message)


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _contains_floating_selector_recursive(value: object, path: str) -> list[str]:
    """Recursively report floating selectors inside arbitrary configuration."""
    errors: list[str] = []
    if isinstance(value, str):
        if is_floating_selector(value):
            errors.append(
                f"{path}: {value!r} is a floating selector; concrete values are required"
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if is_floating_selector(key):
                errors.append(f"{path}: key {key!r} is a floating selector")
            errors.extend(_contains_floating_selector_recursive(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(
                _contains_floating_selector_recursive(item, f"{path}[{index}]")
            )
    return errors


def load_request_document(path: Path) -> Document:
    """Read and JSON-decode a composition request document."""
    if not path.is_file():
        message = f"composition request not found: {path}"
        raise ComposerNotFoundError(message)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = (
            f"composition request is unreadable: {path} ({error.__class__.__name__})"
        )
        raise ComposerNotFoundError(message) from error
    if not isinstance(document, Mapping):
        message = f"composition request is not a JSON object: {path}"
        raise ComposerNotFoundError(message)
    return document


def _registry_failure(root: Path, error: BaseException) -> str:
    """One deterministic message for an unusable authoritative registry."""
    detail = getattr(error, "errors", None)
    if detail:
        first = str(detail[0])
    else:
        first = str(error).splitlines()[0] if str(error) else error.__class__.__name__
    return (
        f"$: the authoritative Component Registry is unavailable or invalid "
        f"({first}); composition is fail-closed"
    )


def _load_registry(root: Path) -> tuple[Any | None, list[str]]:
    """Load the authoritative registry, fail-closed on any failure."""
    try:
        return load_registry(root), []
    except (RegistryError, OSError, ValueError, KeyError) as error:
        return None, [_registry_failure(root, error)]


# ---------------------------------------------------------------------------
# Semantic validation of the request document
# ---------------------------------------------------------------------------


def _manifest_errors(document: Document) -> list[str]:
    errors: list[str] = []
    manifest = document.get("manifest")
    if not _is_mapping(manifest):
        return errors

    manifest_id = manifest.get("manifest_id")
    if is_floating_selector(manifest_id):
        errors.append(f"$.manifest.manifest_id: {manifest_id!r} is a floating selector")
    elif (
        isinstance(manifest_id, str) and _MANIFEST_ID_RE.fullmatch(manifest_id) is None
    ):
        errors.append(
            f"$.manifest.manifest_id: {manifest_id!r} is not a stable manifest identity"
        )

    manifest_version = manifest.get("manifest_version")
    if is_floating_selector(manifest_version):
        errors.append(
            f"$.manifest.manifest_version: {manifest_version!r} is a floating selector"
        )
    elif parse_semver(manifest_version) is None:
        errors.append(
            f"$.manifest.manifest_version: {manifest_version!r} is not an explicit SemVer version"
        )

    return errors


def _component_errors(document: Document, registry: Any | None) -> list[str]:
    """Validate the requested component set and its registry availability."""
    errors: list[str] = []
    components = document.get("components")
    if not isinstance(components, list):
        return errors

    identifiers: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(components):
        path = f"$.components[{index}]"
        if not _is_mapping(entry):
            continue
        component_id = entry.get("component_id")
        if isinstance(component_id, str):
            identifiers.append(component_id)
            if is_floating_selector(component_id):
                errors.append(
                    f"{path}.component_id: {component_id!r} is a floating selector"
                )
            elif _COMPONENT_ID_RE.fullmatch(component_id) is None:
                errors.append(
                    f"{path}.component_id: {component_id!r} is not a stable component identity"
                )
            if component_id in seen:
                errors.append(
                    f"{path}.component_id: duplicate requested component {component_id!r}"
                )
            seen.add(component_id)

        has_version = "component_version" in entry
        has_range = "version_range" in entry
        if has_version == has_range:
            errors.append(
                f"{path}: exactly one of component_version (explicit pin) or version_range "
                f"(explicit constraint) is required"
            )

        if has_version:
            version = entry.get("component_version")
            if is_floating_selector(version):
                errors.append(
                    f"{path}.component_version: {version!r} is a floating selector"
                )
            elif parse_semver(version) is None:
                errors.append(
                    f"{path}.component_version: {version!r} is not an explicit SemVer version"
                )

        if has_range:
            version_range = entry.get("version_range")
            if is_floating_selector(version_range):
                errors.append(
                    f"{path}.version_range: {version_range!r} is a floating selector"
                )
            elif parse_version_range(version_range) is None:
                errors.append(
                    f"{path}.version_range: {version_range!r} is not an explicit version range "
                    f"(operator and exact SemVer per clause are required)"
                )

    if identifiers and identifiers != sorted(identifiers):
        errors.append(
            f"$.components: components must be sorted by component_id for deterministic "
            f"reproducibility (expected {sorted(identifiers)!r}, got {identifiers!r})"
        )

    if registry is None:
        return errors

    registered = set(registry.component_ids)
    for index, entry in enumerate(components):
        if not _is_mapping(entry):
            continue
        component_id = entry.get("component_id")
        if not isinstance(component_id, str):
            # Structural validation already reported the malformed identity.
            continue
        if component_id not in registered:
            errors.append(
                f"$.components[{index}].component_id: {component_id!r} is not registered in the "
                f"authoritative Component Registry"
            )
            continue
        registered_version = registry.version(component_id)
        if "component_version" in entry:
            requested = entry.get("component_version")
            if requested != registered_version:
                errors.append(
                    f"$.components[{index}].component_version: {component_id} {requested!r} is not "
                    f"the version the authoritative Component Registry publishes "
                    f"({registered_version}); the Composer never substitutes or invents versions"
                )
        elif "version_range" in entry:
            requested_range = entry.get("version_range")
            if parse_version_range(requested_range) is not None and not range_admits(
                requested_range, registered_version
            ):
                errors.append(
                    f"$.components[{index}].version_range: {component_id} "
                    f"{registered_version} is not admitted by {requested_range!r}; the Composer "
                    f"never substitutes or invents versions"
                )

    return errors


def _configuration_errors(document: Document) -> list[str]:
    errors: list[str] = []
    configuration = document.get("configuration")
    if not _is_mapping(configuration):
        return errors
    for key, value in configuration.items():
        path = f"$.configuration.{key}"
        if is_floating_selector(key):
            errors.append(f"{path}: key {key!r} is a floating selector")
        elif _COMPONENT_ID_RE.fullmatch(key) is None:
            errors.append(f"{path}: {key!r} is not a stable component identity")
        errors.extend(_contains_floating_selector_recursive(value, path))
    return errors


def _extension_errors(document: Document) -> list[str]:
    errors: list[str] = []
    extensions = document.get("extensions")
    if not isinstance(extensions, list):
        return errors

    seen: set[str] = set()
    for index, entry in enumerate(extensions):
        path = f"$.extensions[{index}]"
        if not _is_mapping(entry):
            continue
        extension_id = entry.get("extension_id")
        if isinstance(extension_id, str):
            if is_floating_selector(extension_id):
                errors.append(
                    f"{path}.extension_id: {extension_id!r} is a floating selector"
                )
            elif _MANIFEST_ID_RE.fullmatch(extension_id) is None:
                errors.append(
                    f"{path}.extension_id: {extension_id!r} is not a stable extension identity"
                )
            if extension_id in seen:
                errors.append(
                    f"{path}.extension_id: duplicate extension identity {extension_id!r}"
                )
            seen.add(extension_id)

        component_id = entry.get("component_id")
        if isinstance(component_id, str) and (
            is_floating_selector(component_id)
            or _COMPONENT_ID_RE.fullmatch(component_id) is None
        ):
            errors.append(
                f"{path}.component_id: {component_id!r} is not a stable component identity"
            )

        mechanism = entry.get("mechanism")
        if isinstance(mechanism, str) and mechanism not in EXTENSION_MECHANISMS:
            errors.append(
                f"{path}.mechanism: {mechanism!r} is not a permitted extension mechanism "
                f"(ARCHITECTURE.md §21); an extension works through a published contract only"
            )

        errors.extend(_contains_floating_selector_recursive(entry, path))

    return errors


def validate_request_document(
    document: object, *, root: Path | None = None
) -> list[str]:
    """Validate a composition request and return every violation, sorted.

    The check is total, deterministic and fail-closed: an unavailable or
    invalid authoritative Component Registry rejects the request instead of
    being silently skipped. The same document always yields the same list.
    """
    base = root if root is not None else discover_root()
    errors: list[str] = []

    try:
        schema = load_schema(base)
    except (FileNotFoundError, TypeError, ValueError, KeyError) as error:
        return [
            f"$: composition request schema is unavailable ({error}); validation is fail-closed"
        ]

    if not _is_mapping(document):
        return ["$: composition request must be a JSON object"]

    errors.extend(validate_structure(document, schema))
    errors.extend(_manifest_errors(document))

    registry, registry_errors = _load_registry(base)
    errors.extend(registry_errors)
    errors.extend(_component_errors(document, registry))
    errors.extend(_configuration_errors(document))
    errors.extend(_extension_errors(document))

    return sorted(set(errors))


@dataclass(frozen=True)
class CompositionRequest:
    """A loaded composition request document."""

    document: Document
    root: Path
    path: Path | None = None

    @property
    def manifest_request(self) -> Entry:
        manifest = self.document.get("manifest")
        return manifest if _is_mapping(manifest) else {}

    @property
    def manifest_id(self) -> str | None:
        value = self.manifest_request.get("manifest_id")
        return value if isinstance(value, str) else None

    @property
    def manifest_version(self) -> str | None:
        value = self.manifest_request.get("manifest_version")
        return value if isinstance(value, str) else None

    @property
    def predecessor(self) -> Entry | None:
        value = self.manifest_request.get("predecessor")
        return value if _is_mapping(value) else None

    @property
    def components(self) -> tuple[Entry, ...]:
        components = self.document.get("components", [])
        if not isinstance(components, list):
            return ()
        return tuple(entry for entry in components if _is_mapping(entry))

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(
            entry["component_id"]
            for entry in self.components
            if isinstance(entry.get("component_id"), str)
        )

    @property
    def golden_bundle(self) -> Entry | None:
        value = self.document.get("golden_bundle")
        return value if _is_mapping(value) else None

    @property
    def configuration(self) -> Entry:
        value = self.document.get("configuration")
        return value if _is_mapping(value) else {}

    @property
    def extensions(self) -> tuple[Entry, ...]:
        value = self.document.get("extensions", [])
        if not isinstance(value, list):
            return ()
        return tuple(entry for entry in value if _is_mapping(entry))

    @property
    def branding(self) -> Entry | None:
        value = self.document.get("branding")
        return value if _is_mapping(value) else None

    def validate(self) -> list[str]:
        """Return every validation violation of this request (empty when valid)."""
        return validate_request_document(self.document, root=self.root)

    def require_valid(self) -> list[str]:
        """Raise :class:`ComposerValidationError` when the request is invalid."""
        errors = self.validate()
        if errors:
            raise ComposerValidationError(errors)
        return errors


def load_request(
    path: Path, root: Path | None = None, *, strict: bool = True
) -> CompositionRequest:
    """Load and optionally strictly validate a composition request from ``path``."""
    base = root if root is not None else discover_root()
    document = load_request_document(path)
    request = CompositionRequest(document=document, root=base, path=path)
    if strict:
        request.require_valid()
    return request


def build_request_document(
    *,
    manifest_id: str,
    manifest_version: str,
    components: list[dict[str, Any]],
    predecessor: dict[str, Any] | None = None,
    golden_bundle: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
    extensions: list[dict[str, Any]] | None = None,
    branding: dict[str, Any] | None = None,
    schema_path: str = REQUEST_SCHEMA_PATH,
) -> dict[str, Any]:
    """Build a composition request document deterministically.

    The caller provides the semantic content; this function only orders it:
    components are sorted by ``component_id`` and extensions by
    ``extension_id``, so the same semantic request always renders the same
    bytes. Nothing is filled in, defaulted or guessed — an absent optional
    field stays absent.
    """
    base: dict[str, Any] = {
        "$schema": schema_path,
        "manifest": {
            "manifest_id": manifest_id,
            "manifest_version": manifest_version,
        },
        "components": sorted(
            (dict(entry) for entry in components),
            key=lambda entry: str(entry.get("component_id", "")),
        ),
    }
    if predecessor is not None:
        base["manifest"]["predecessor"] = predecessor
    if golden_bundle is not None:
        base["golden_bundle"] = dict(golden_bundle)
    if configuration is not None:
        base["configuration"] = {
            key: dict(value) for key, value in configuration.items()
        }
    if extensions is not None:
        base["extensions"] = sorted(
            (dict(entry) for entry in extensions),
            key=lambda entry: str(entry.get("extension_id", "")),
        )
    if branding is not None:
        base["branding"] = dict(branding)
    return base


def render_request_document(document: Document) -> str:
    """Serialize a composition request deterministically for storage."""
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
