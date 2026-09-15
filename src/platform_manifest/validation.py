"""Semantic validation of the Platform Manifest.

Structural shape is validated against the shipped JSON Schema
(:mod:`platform_manifest.schema`). Everything a schema cannot express is
enforced here:

* identity is stable, unique and never a floating selector;
* every public version is an explicit SemVer, never ``latest``;
* manifest_digest matches the canonical content digest;
* predecessor is valid and not self-referential;
* lifecycle states are valid and transitions are allowed;
* approved/published states require approval/publication metadata;
* published is immutable — checked via ``check_immutability``;
* components are concrete, unique, and resolve against authoritative
  Component Registry/Catalog metadata;
* artifact identity is explicit and pinned where required;
* golden_bundle reference, if present, is explicit and not floating;
* configuration/extensions/branding contain no hidden floating selectors;
* reproducibility: same document always yields same validation result,
  no file-order dependency, no time dependency, no network.

The manifest is validated purely as metadata: this module reads the
canonical Component Registry (and optionally Catalog) for reference checks,
but never imports a component's internals and never opens a component
database (ARCHITECTURE.md §1.1, §18; ADR-0015 §8, §11).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from platform_manifest.lifecycle import (
    LIFECYCLE_STATES,
    is_allowed_transition,
    requires_approval,
    requires_publication,
)
from platform_manifest.manifest import compute_manifest_digest
from platform_manifest.schema import load_schema, validate_structure

# --------------------------------------------------------------------------
# Vocabulary — grounded in ratified architecture, not invented.
# --------------------------------------------------------------------------

SEMVER_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
_SEMVER_RE = re.compile(SEMVER_PATTERN)

FLOATING_SELECTOR_TOKENS = frozenset(
    {"latest", "current", "default", "stable", "edge", "main", "master", "head", "tip"}
)

MANIFEST_ID_PATTERN = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"
_MANIFEST_ID_RE = re.compile(MANIFEST_ID_PATTERN)

COMPONENT_ID_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
_COMPONENT_ID_RE = re.compile(COMPONENT_ID_PATTERN)

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SHA256_RE = re.compile(SHA256_PATTERN)

SHA256_NULLABLE_PATTERN = r"^(sha256:)?[0-9a-f]{64}$"
_SHA256_NULLABLE_RE = re.compile(SHA256_NULLABLE_PATTERN)

ISO8601_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
_ISO8601_RE = re.compile(ISO8601_PATTERN)

ARTIFACT_TYPES = frozenset({"none", "container_image", "source_package"})


def parse_semver(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    m = _SEMVER_RE.fullmatch(value)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def compare_semver(a: str, b: str) -> int:
    """Compare two SemVer strings: -1 if a<b, 0 if equal, 1 if a>b. Assumes valid."""
    pa = parse_semver(a)
    pb = parse_semver(b)
    if pa is None or pb is None:
        return 0
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def is_floating_selector(value: object) -> bool:
    if not isinstance(value, str):
        return False
    token = value.strip().lower()
    if not token:
        return False
    return token in FLOATING_SELECTOR_TOKENS or "*" in token


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _contains_floating_selector_recursive(value: object, path: str) -> list[str]:
    """Recursively search for floating selectors inside arbitrary config/branding."""
    errors: list[str] = []
    if isinstance(value, str):
        if is_floating_selector(value):
            errors.append(
                f"{path}: {value!r} is a floating selector; concrete values are required"
            )
    elif isinstance(value, Mapping):
        for k, v in value.items():
            # Key itself could be floating? Check.
            if is_floating_selector(k):
                errors.append(f"{path}.{k}: key {k!r} is a floating selector")
            # Value recursive.
            errors.extend(_contains_floating_selector_recursive(v, f"{path}.{k}"))
            # Also check nested object where value is dict containing version-like fields?
            # We already recurse, but also check if any string value that looks like
            # version field contains floating token.
            if isinstance(v, str) and is_floating_selector(v):
                # Already covered above, but keep.
                pass
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            errors.extend(_contains_floating_selector_recursive(item, f"{path}[{idx}]"))
    return errors


# --------------------------------------------------------------------------
# Registry / Catalog loading helpers (authoritative sources)
# --------------------------------------------------------------------------


def _load_registry(root: Path):
    """Load canonical registry strictly, or None if not loadable."""
    try:
        from component_registry import load_registry
        from component_registry.errors import RegistryError

        return load_registry(root)
    except (
        RegistryError,
        FileNotFoundError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ):
        return None


def _registry_component_ids(registry) -> set[str]:
    if registry is None:
        return set()
    try:
        return set(registry.component_ids)
    except (AttributeError, KeyError, TypeError, ValueError):
        return set()


# --------------------------------------------------------------------------
# Per-field semantic checks
# --------------------------------------------------------------------------


def _identity_errors(document: Mapping, path: str = "$") -> list[str]:
    errors: list[str] = []
    manifest_id = document.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id:
        errors.append(f"{path}.manifest_id: a stable manifest identity is required")
    else:
        if is_floating_selector(manifest_id):
            errors.append(
                f"{path}.manifest_id: {manifest_id!r} is a floating selector, not a stable manifest identity"
            )
        if _MANIFEST_ID_RE.fullmatch(manifest_id) is None:
            errors.append(
                f"{path}.manifest_id: {manifest_id!r} does not match pattern {MANIFEST_ID_PATTERN!r}"
            )

    version = document.get("manifest_version")
    if is_floating_selector(version):
        errors.append(
            f"{path}.manifest_version: {version!r} is a floating selector; an explicit SemVer version is required"
        )
    elif parse_semver(version) is None and version is not None:
        errors.append(
            f"{path}.manifest_version: {version!r} is not SemVer MAJOR.MINOR.PATCH"
        )

    digest = document.get("manifest_digest")
    if is_floating_selector(digest):
        errors.append(
            f"{path}.manifest_digest: {digest!r} is a floating selector; an immutable digest is required"
        )
    elif isinstance(digest, str) and _SHA256_RE.fullmatch(digest) is None:
        errors.append(
            f"{path}.manifest_digest: {digest!r} is not a valid sha256 digest"
        )

    # Check computed digest matches declared, if both present and structurally valid.
    if (
        isinstance(digest, str)
        and _SHA256_RE.fullmatch(digest) is not None
        and isinstance(manifest_id, str)
        and parse_semver(version) is not None
    ):
        try:
            computed = compute_manifest_digest(document)
            if digest != computed:
                errors.append(
                    f"{path}.manifest_digest: declared {digest!r} does not match computed digest {computed!r} of canonical content"
                )
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            errors.append(f"{path}.manifest_digest: cannot compute digest: {exc}")

    return errors


def _predecessor_errors(document: Mapping, path: str = "$") -> list[str]:
    errors: list[str] = []
    predecessor = document.get("predecessor")
    if predecessor is None:
        return errors
    if not _is_mapping(predecessor):
        errors.append(f"{path}.predecessor: must be null or an object")
        return errors

    pred_id = predecessor.get("manifest_id")
    pred_version = predecessor.get("manifest_version")
    pred_digest = predecessor.get("manifest_digest")

    if not isinstance(pred_id, str) or not pred_id:
        errors.append(
            f"{path}.predecessor.manifest_id: a stable manifest identity is required"
        )
    else:
        if is_floating_selector(pred_id):
            errors.append(
                f"{path}.predecessor.manifest_id: {pred_id!r} is a floating selector"
            )
        if _MANIFEST_ID_RE.fullmatch(pred_id) is None:
            errors.append(
                f"{path}.predecessor.manifest_id: {pred_id!r} does not match pattern"
            )

    if is_floating_selector(pred_version):
        errors.append(
            f"{path}.predecessor.manifest_version: {pred_version!r} is a floating selector"
        )
    elif parse_semver(pred_version) is None:
        errors.append(
            f"{path}.predecessor.manifest_version: {pred_version!r} is not SemVer"
        )

    if pred_digest is not None:
        if is_floating_selector(pred_digest):
            errors.append(
                f"{path}.predecessor.manifest_digest: {pred_digest!r} is a floating selector"
            )
        elif isinstance(pred_digest, str) and _SHA256_RE.fullmatch(pred_digest) is None:
            errors.append(
                f"{path}.predecessor.manifest_digest: {pred_digest!r} is not a valid sha256 digest"
            )

    # Self-reference check.
    manifest_id = document.get("manifest_id")
    manifest_version = document.get("manifest_version")
    if (
        isinstance(manifest_id, str)
        and isinstance(pred_id, str)
        and isinstance(manifest_version, str)
        and isinstance(pred_version, str)
    ):
        if manifest_id == pred_id and manifest_version == pred_version:
            errors.append(
                f"{path}.predecessor: predecessor must not be self (same manifest_id and manifest_version)"
            )
        # Version must be less than current if same lineage.
        if (
            manifest_id == pred_id
            and parse_semver(manifest_version)
            and parse_semver(pred_version)
            and compare_semver(pred_version, manifest_version) >= 0
        ):
            errors.append(
                f"{path}.predecessor.manifest_version: predecessor version {pred_version!r} must be less than current version {manifest_version!r} for same manifest_id"
            )

    return errors


def _lifecycle_errors(document: Mapping, path: str = "$") -> list[str]:
    errors: list[str] = []
    lifecycle = document.get("lifecycle")
    if not _is_mapping(lifecycle):
        errors.append(f"{path}.lifecycle: explicit lifecycle state is required")
        return errors

    state = lifecycle.get("state")
    if state not in LIFECYCLE_STATES:
        errors.append(
            f"{path}.lifecycle.state: {state!r} is not a valid lifecycle state"
        )
    if is_floating_selector(state):
        errors.append(f"{path}.lifecycle.state: {state!r} is a floating selector")

    created_at = lifecycle.get("created_at")
    if created_at is not None:
        if not isinstance(created_at, str) or _ISO8601_RE.fullmatch(created_at) is None:
            errors.append(
                f"{path}.lifecycle.created_at: {created_at!r} is not ISO8601 UTC"
            )
        if is_floating_selector(created_at):
            errors.append(
                f"{path}.lifecycle.created_at: {created_at!r} is a floating selector"
            )

    # Approval / publication requirements based on state.
    if isinstance(state, str):
        approval = document.get("approval")
        publication = document.get("publication")

        if requires_approval(state):
            if not _is_mapping(approval):
                errors.append(
                    f"{path}.approval: approval metadata is required when lifecycle is {state!r}"
                )
            else:
                # Validate approval fields not floating.
                approved_by = approval.get("approved_by")
                approved_at = approval.get("approved_at")
                approval_id = approval.get("approval_id")
                if is_floating_selector(approved_by) or is_floating_selector(
                    approval_id
                ):
                    errors.append(f"{path}.approval: contains floating selector")
                if (
                    isinstance(approved_at, str)
                    and _ISO8601_RE.fullmatch(approved_at) is None
                ):
                    errors.append(
                        f"{path}.approval.approved_at: {approved_at!r} is not ISO8601 UTC"
                    )
                if is_floating_selector(approved_at):
                    errors.append(
                        f"{path}.approval.approved_at: {approved_at!r} is a floating selector"
                    )

        if requires_publication(state):
            if not _is_mapping(publication):
                errors.append(
                    f"{path}.publication: publication metadata is required when lifecycle is {state!r}"
                )
            else:
                published_by = publication.get("published_by")
                published_at = publication.get("published_at")
                publication_id = publication.get("publication_id")
                if is_floating_selector(published_by) or is_floating_selector(
                    publication_id
                ):
                    errors.append(f"{path}.publication: contains floating selector")
                if (
                    isinstance(published_at, str)
                    and _ISO8601_RE.fullmatch(published_at) is None
                ):
                    errors.append(
                        f"{path}.publication.published_at: {published_at!r} is not ISO8601 UTC"
                    )
                if is_floating_selector(published_at):
                    errors.append(
                        f"{path}.publication.published_at: {published_at!r} is a floating selector"
                    )

        # For draft, approval/publication should be null? Not strictly required,
        # but if present, they should be valid. We allow null for all states
        # except where required. For draft/validated, approval should be null
        # or absent? Let's enforce: if state is draft or validated, approval
        # must be null, publication must be null.
        if state in ("draft", "validated"):
            if _is_mapping(approval):
                errors.append(
                    f"{path}.approval: must be null when lifecycle is {state!r} (approval only from approved onward)"
                )
            if _is_mapping(publication):
                errors.append(
                    f"{path}.publication: must be null when lifecycle is {state!r} (publication only from published onward)"
                )
        if state == "approved" and _is_mapping(publication):
            errors.append(
                f"{path}.publication: must be null when lifecycle is approved (publication only from published onward)"
            )

    return errors


_VERSION_CONSTRAINT_RE = re.compile(
    r"^(<=|>=|==|<|>)(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


def _version_satisfies_range(version: object, version_range: object) -> bool:
    """Check an explicit registry dependency range without resolving anything."""
    if parse_semver(version) is None or not isinstance(version_range, str):
        return False

    constraints = [part.strip() for part in version_range.split(",")]
    if not constraints or any(not part for part in constraints):
        return False

    for constraint in constraints:
        match = _VERSION_CONSTRAINT_RE.fullmatch(constraint)
        if match is None:
            return False

        operator = match.group(1)
        bound = ".".join(match.group(i) for i in range(2, 5))
        comparison = compare_semver(version, bound)

        if operator == ">=" and comparison < 0:
            return False
        if operator == ">" and comparison <= 0:
            return False
        if operator == "<=" and comparison > 0:
            return False
        if operator == "<" and comparison >= 0:
            return False
        if operator == "==" and comparison != 0:
            return False

    return True


def _component_errors(document: Mapping, path: str, root: Path, registry) -> list[str]:
    errors: list[str] = []
    components = document.get("components")
    if not isinstance(components, list):
        errors.append(f"{path}.components: expected a list of component entries")
        return errors

    seen_ids: set[str] = set()
    seen_artifacts: set[str] = set()

    registered_ids = _registry_component_ids(registry)

    for index, entry in enumerate(components):
        entry_path = f"{path}.components[{index}]"
        if not _is_mapping(entry):
            errors.append(f"{entry_path}: component entry must be an object")
            continue

        comp_id = entry.get("component_id")
        comp_version = entry.get("component_version")
        artifact = entry.get("artifact")

        # component_id checks
        if not isinstance(comp_id, str) or not comp_id:
            errors.append(
                f"{entry_path}.component_id: a stable component identity is required"
            )
            continue
        if is_floating_selector(comp_id):
            errors.append(
                f"{entry_path}.component_id: {comp_id!r} is a floating selector, not a stable component identity"
            )
        if _COMPONENT_ID_RE.fullmatch(comp_id) is None:
            errors.append(
                f"{entry_path}.component_id: {comp_id!r} does not match pattern {COMPONENT_ID_PATTERN!r}"
            )
        if comp_id in seen_ids:
            errors.append(
                f"{entry_path}.component_id: duplicate component identity {comp_id!r}"
            )
        else:
            seen_ids.add(comp_id)

        # version checks
        if is_floating_selector(comp_version):
            errors.append(
                f"{entry_path}.component_version: {comp_version!r} is a floating selector; an explicit SemVer version is required"
            )
        elif parse_semver(comp_version) is None:
            errors.append(
                f"{entry_path}.component_version: {comp_version!r} is not SemVer MAJOR.MINOR.PATCH"
            )

        # registry consistency
        if registry is not None and comp_id not in registered_ids:
            errors.append(
                f"{entry_path}.component_id: component {comp_id!r} is not registered in the canonical Component Registry"
            )
        elif registry is not None:
            try:
                reg_entry = registry.entry(comp_id)
                reg_version = reg_entry.get("component_version")
                if (
                    isinstance(comp_version, str)
                    and isinstance(reg_version, str)
                    and comp_version != reg_version
                ):
                    errors.append(
                        f"{entry_path}.component_version: manifest declares {comp_version!r}, canonical registry declares {reg_version!r} for {comp_id!r}"
                    )
            except KeyError:
                # Already reported as not registered
                pass

        # artifact checks
        if not _is_mapping(artifact):
            errors.append(
                f"{entry_path}.artifact: explicit artifact identity is required"
            )
            continue

        artifact_type = artifact.get("artifact_type")
        digest = artifact.get("digest")
        pinned = artifact.get("pinned")

        if artifact_type not in ARTIFACT_TYPES:
            errors.append(
                f"{entry_path}.artifact.artifact_type: {artifact_type!r} is not a valid artifact type"
            )
        if is_floating_selector(artifact_type):
            errors.append(
                f"{entry_path}.artifact.artifact_type: {artifact_type!r} is a floating selector"
            )
        if is_floating_selector(digest):
            errors.append(
                f"{entry_path}.artifact.digest: {digest!r} is a floating selector; an artifact is selected by immutable digest"
            )

        if artifact_type == "none":
            if digest is not None:
                errors.append(
                    f"{entry_path}.artifact.digest: artifact_type 'none' must not declare a digest"
                )
            if pinned is not False:
                errors.append(
                    f"{entry_path}.artifact.pinned: artifact_type 'none' must not be pinned"
                )
        elif artifact_type in ARTIFACT_TYPES:
            # For container_image / source_package, digest required and pinned true.
            if (
                not isinstance(digest, str)
                or _SHA256_NULLABLE_RE.fullmatch(digest) is None
            ):
                errors.append(
                    f"{entry_path}.artifact.digest: a published artifact requires an immutable digest"
                )
            else:
                # Normalize digest for duplicate check (strip prefix)
                normalized = digest.lower()
                normalized = normalized.removeprefix("sha256:")
                # Check duplicate artifact identities across components
                if normalized in seen_artifacts:
                    # Duplicate digest across different components could be suspicious,
                    # but not necessarily invalid — same artifact reused? For safety,
                    # we only check duplicate component entries, not digest reuse.
                    # So we don't error on duplicate digest, but we track for
                    # duplicate component entries already.
                    pass
                else:
                    seen_artifacts.add(normalized)
            if pinned is not True:
                errors.append(
                    f"{entry_path}.artifact.pinned: a published artifact must be pinned by digest"
                )

        # Check artifact digest duplication across components? Not necessarily error,
        # but we can warn if same component_id appears twice (already checked).
        # For adversarial: duplicate artifact identities with same digest but different component_id
        # is allowed (shared base image), so no error.

    # Validate the explicit composition against dependencies declared by the
    # authoritative Component Registry. Missing or incompatible dependencies
    # are errors; this validator never adds, substitutes, or resolves them.
    if registry is not None:
        selected_versions = {
            entry.get("component_id"): entry.get("component_version")
            for entry in components
            if _is_mapping(entry)
            and isinstance(entry.get("component_id"), str)
            and parse_semver(entry.get("component_version")) is not None
        }

        for index, entry in enumerate(components):
            if not _is_mapping(entry):
                continue

            comp_id = entry.get("component_id")
            if not isinstance(comp_id, str) or comp_id not in registered_ids:
                continue

            reg_entry = registry.entry(comp_id)
            dependencies = reg_entry.get("dependencies", [])
            entry_path = f"{path}.components[{index}]"

            if not isinstance(dependencies, list):
                errors.append(
                    f"{entry_path}: canonical registry dependencies for "
                    f"{comp_id!r} must be a list"
                )
                continue

            for dep_index, dependency in enumerate(dependencies):
                dep_path = f"{entry_path}.dependencies[{dep_index}]"

                if not _is_mapping(dependency):
                    errors.append(f"{dep_path}: canonical dependency must be an object")
                    continue

                dep_id = dependency.get("component_id")
                version_range = dependency.get("version_range")

                if not isinstance(dep_id, str) or not dep_id:
                    errors.append(
                        f"{dep_path}.component_id: dependency identity is required"
                    )
                    continue

                if is_floating_selector(dep_id):
                    errors.append(
                        f"{dep_path}.component_id: {dep_id!r} is a floating selector"
                    )
                    continue

                if not isinstance(version_range, str) or not version_range.strip():
                    errors.append(
                        f"{dep_path}.version_range: explicit SemVer constraints are required"
                    )
                    continue

                if is_floating_selector(version_range):
                    errors.append(
                        f"{dep_path}.version_range: {version_range!r} is a floating selector"
                    )
                    continue

                selected_version = selected_versions.get(dep_id)

                if selected_version is None:
                    errors.append(
                        f"{entry_path}.component_id: component {comp_id!r} "
                        f"requires dependency {dep_id!r}, but it is absent from the manifest"
                    )
                    continue

                if not _version_satisfies_range(selected_version, version_range):
                    errors.append(
                        f"{entry_path}.component_version: component {comp_id!r} "
                        f"requires {dep_id!r} in range {version_range!r}, "
                        f"but manifest selects {selected_version!r}"
                    )

    # Ensure deterministic order: components should be sorted by component_id for reproducibility.
    # If not sorted, we report error to enforce determinism.
    if isinstance(components, list) and len(components) > 1:
        ids = [entry.get("component_id") for entry in components if _is_mapping(entry)]
        sorted_ids = sorted(ids)
        if ids != sorted_ids:
            errors.append(
                f"{path}.components: components must be sorted by component_id for deterministic reproducibility (expected {sorted_ids!r}, got {ids!r})"
            )

    return errors


def _golden_bundle_errors(document: Mapping, path: str) -> list[str]:
    errors: list[str] = []
    bundle = document.get("golden_bundle")
    if bundle is None:
        return errors
    if not _is_mapping(bundle):
        errors.append(f"{path}.golden_bundle: must be null or an object")
        return errors

    bundle_id = bundle.get("bundle_id")
    bundle_version = bundle.get("bundle_version")
    bundle_digest = bundle.get("bundle_digest")

    if not isinstance(bundle_id, str) or not bundle_id:
        errors.append(
            f"{path}.golden_bundle.bundle_id: a stable bundle identity is required"
        )
    else:
        if is_floating_selector(bundle_id):
            errors.append(
                f"{path}.golden_bundle.bundle_id: {bundle_id!r} is a floating selector"
            )
        if _MANIFEST_ID_RE.fullmatch(bundle_id) is None:
            errors.append(
                f"{path}.golden_bundle.bundle_id: {bundle_id!r} does not match pattern"
            )

    if is_floating_selector(bundle_version):
        errors.append(
            f"{path}.golden_bundle.bundle_version: {bundle_version!r} is a floating selector"
        )
    elif parse_semver(bundle_version) is None:
        errors.append(
            f"{path}.golden_bundle.bundle_version: {bundle_version!r} is not SemVer"
        )

    if is_floating_selector(bundle_digest):
        errors.append(
            f"{path}.golden_bundle.bundle_digest: {bundle_digest!r} is a floating selector"
        )
    elif isinstance(bundle_digest, str) and _SHA256_RE.fullmatch(bundle_digest) is None:
        errors.append(
            f"{path}.golden_bundle.bundle_digest: {bundle_digest!r} is not a valid sha256 digest"
        )

    return errors


def _extensions_errors(document: Mapping, path: str) -> list[str]:
    errors: list[str] = []
    extensions = document.get("extensions")
    if extensions is None:
        return errors
    if not isinstance(extensions, list):
        errors.append(f"{path}.extensions: must be a list")
        return errors

    seen: set[str] = set()
    for idx, ext in enumerate(extensions):
        ext_path = f"{path}.extensions[{idx}]"
        if not _is_mapping(ext):
            errors.append(f"{ext_path}: extension must be an object")
            continue
        ext_id = ext.get("extension_id")
        if not isinstance(ext_id, str) or not ext_id:
            errors.append(
                f"{ext_path}.extension_id: a stable extension identity is required"
            )
            continue
        if is_floating_selector(ext_id):
            errors.append(f"{ext_path}.extension_id: {ext_id!r} is a floating selector")
        if _MANIFEST_ID_RE.fullmatch(ext_id) is None:
            errors.append(f"{ext_path}.extension_id: {ext_id!r} does not match pattern")
        if ext_id in seen:
            errors.append(
                f"{ext_path}.extension_id: duplicate extension identity {ext_id!r}"
            )
        seen.add(ext_id)

        ext_version = ext.get("extension_version")
        if ext_version is not None:
            if is_floating_selector(ext_version):
                errors.append(
                    f"{ext_path}.extension_version: {ext_version!r} is a floating selector"
                )
            elif parse_semver(ext_version) is None:
                errors.append(
                    f"{ext_path}.extension_version: {ext_version!r} is not SemVer"
                )

        # Check for floating selectors inside extension object recursively
        errors.extend(_contains_floating_selector_recursive(ext, ext_path))

    return errors


def _config_branding_errors(document: Mapping, path: str) -> list[str]:
    errors: list[str] = []
    for field in ("configuration", "branding"):
        value = document.get(field)
        if value is None:
            continue
        if not _is_mapping(value):
            errors.append(f"{path}.{field}: must be an object")
            continue
        errors.extend(_contains_floating_selector_recursive(value, f"{path}.{field}"))

    # validation_attestation
    attestation = document.get("validation_attestation")
    if attestation is not None and _is_mapping(attestation):
        validated_by = attestation.get("validated_by")
        validated_at = attestation.get("validated_at")
        if is_floating_selector(validated_by):
            errors.append(
                f"{path}.validation_attestation.validated_by: {validated_by!r} is a floating selector"
            )
        if (
            isinstance(validated_at, str)
            and _ISO8601_RE.fullmatch(validated_at) is None
        ):
            errors.append(
                f"{path}.validation_attestation.validated_at: {validated_at!r} is not ISO8601 UTC"
            )
        if is_floating_selector(validated_at):
            errors.append(
                f"{path}.validation_attestation.validated_at: {validated_at!r} is a floating selector"
            )
        errors.extend(
            _contains_floating_selector_recursive(
                attestation, f"{path}.validation_attestation"
            )
        )

    return errors


# --------------------------------------------------------------------------
# Document-level validation
# --------------------------------------------------------------------------


def validate_document(document: object, *, root: Path | None = None) -> list[str]:
    """Validate a manifest document and return every violation, sorted.

    The same document always yields the same list. Validation is total and
    deterministic.
    """
    from platform_manifest.manifest import discover_root

    base = root if root is not None else discover_root()
    errors: list[str] = list(validate_structure(document, load_schema(base)))

    if not isinstance(document, Mapping):
        return sorted(set(errors))

    # Semantic checks
    errors.extend(_identity_errors(document, "$"))
    errors.extend(_predecessor_errors(document, "$"))
    errors.extend(_lifecycle_errors(document, "$"))

    registry = _load_registry(base)

    errors.extend(_component_errors(document, "$", base, registry))
    errors.extend(_golden_bundle_errors(document, "$"))
    errors.extend(_extensions_errors(document, "$"))
    errors.extend(_config_branding_errors(document, "$"))

    # Additional floating selector checks for any unexpected field that might
    # contain floating selector (defense in depth) — already covered by
    # config/branding recursive check, but also check top-level string fields
    # that are not part of allowed vocabulary? Schema already rejects unknown
    # top-level fields, so this is just extra.

    # Ensure no duplicate components already checked.

    return sorted(set(errors))


def check_immutability(
    existing_manifests: list[Mapping[str, Any]], new_manifest: Mapping[str, Any]
) -> list[str]:
    """Check that a new manifest does not silently rewrite a published manifest.

    ``existing_manifests`` is a list of already published manifests (e.g., from
    a store). If ``new_manifest`` has same manifest_id and manifest_version as
    an existing published manifest but different digest/content, it is rejected.

    Returns list of errors (empty when immutable).
    """
    errors: list[str] = []
    if not _is_mapping(new_manifest):
        return ["new manifest is not an object"]

    new_id = new_manifest.get("manifest_id")
    new_version = new_manifest.get("manifest_version")
    new_digest = new_manifest.get("manifest_digest")

    if not isinstance(new_id, str) or not isinstance(new_version, str):
        return []  # Identity errors will be reported by validate_document

    for existing in existing_manifests:
        if not _is_mapping(existing):
            continue
        ex_id = existing.get("manifest_id")
        ex_version = existing.get("manifest_version")
        ex_digest = existing.get("manifest_digest")
        ex_lifecycle = existing.get("lifecycle")
        ex_state = ex_lifecycle.get("state") if _is_mapping(ex_lifecycle) else None

        if (
            ex_id == new_id
            and ex_version == new_version
            and ex_state in ("published", "deployed", "superseded", "retired")
        ):
            # Same identity/version and published — immutable
            if ex_digest != new_digest:
                errors.append(
                    f"$.manifest_digest: published manifest {new_id!r} version {new_version!r} is immutable; existing digest {ex_digest!r} != new digest {new_digest!r}"
                )
            # Also check content differs (even if digest same but content differs? digest should catch)
            # For safety, compare canonical content excluding digest
            try:
                from platform_manifest.manifest import (
                    canonical_json,
                    manifest_content_for_digest,
                )

                existing_content = manifest_content_for_digest(existing)
                new_content = manifest_content_for_digest(new_manifest)
                if (
                    canonical_json(existing_content) != canonical_json(new_content)
                    and ex_digest == new_digest
                ):
                    errors.append(
                        f"$.manifest_id: published manifest {new_id!r} version {new_version!r} content differs but digest is same — possible tampering"
                    )
            except (TypeError, ValueError, KeyError, AttributeError):
                pass

    return sorted(set(errors))


def validate_transition(previous_state: str, new_state: str) -> list[str]:
    """Validate a lifecycle transition for the same manifest identity/version.

    Returns list of errors (empty when allowed).
    """
    if previous_state not in LIFECYCLE_STATES:
        return [f"previous state {previous_state!r} is not a valid lifecycle state"]
    if new_state not in LIFECYCLE_STATES:
        return [f"new state {new_state!r} is not a valid lifecycle state"]
    if previous_state == new_state:
        return []
    if is_allowed_transition(previous_state, new_state):
        return []
    # Provide helpful message about allowed next
    from platform_manifest.lifecycle import _ALLOWED_NEXT

    allowed = _ALLOWED_NEXT.get(previous_state, frozenset())
    if allowed:
        return [
            f"lifecycle transition {previous_state!r} → {new_state!r} is not allowed; allowed next: {sorted(allowed)!r}"
        ]
    return [
        f"lifecycle transition {previous_state!r} → {new_state!r} is not allowed; {previous_state!r} is terminal"
    ]
