"""Semantic validation of the Golden Bundle (Slice D).

Structural shape is validated against the shipped JSON Schema
(:mod:`golden_bundle.schema`). Everything a schema cannot express is enforced
here:

* bundle identity is stable, unique and never a floating selector;
* the bundle version is an explicit SemVer, never ``latest``;
* ``bundle_digest`` matches the canonical content digest;
* lifecycle states are explicit and transitions are allowed (draft →
  candidate → certified → deprecated → revoked); certified requires evidence;
* published (certified or later) bundles are immutable — checked via
  ``check_immutability``;
* the pinned component set is concrete, unique, deterministically ordered and
  resolves against the authoritative Component Registry;
* artifact identity is explicit and pinned where required;
* declared dependencies are validated against the pinned, authoritative
  registry metadata — never discovered, resolved or substituted;
* the compatibility matrix records an explicit, deterministic verdict for
  every declared dependency edge; no automatic compatibility repair;
* reproducibility: same document always yields the same validation result,
  no file-order dependency, no time dependency, no network.

The bundle is validated purely as metadata: this module reads the canonical
Component Registry for reference checks, but never imports a component's
internals and never opens a component database (ARCHITECTURE.md §1.1, §18;
ADR-0015 §7, §8, §11).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from component_registry.validation import range_admits
from golden_bundle.bundle import compute_bundle_digest
from golden_bundle.lifecycle import (
    BUNDLE_STATES,
    is_allowed_transition,
    requires_certification,
)
from golden_bundle.schema import load_schema, validate_structure

# --------------------------------------------------------------------------
# Vocabulary — grounded in ratified architecture, not invented.
# --------------------------------------------------------------------------

SEMVER_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
_SEMVER_RE = re.compile(SEMVER_PATTERN)

#: Tokens that select "whatever happens to be newest" instead of a version.
#: Forbidden as a version, release, artifact, dependency or production
#: selector (ARCHITECTURE.md §1.3; ADR-0015 §13). They remain legitimate in
#: prose, fixtures, assertions and error messages — this check applies only
#: to fields that actually perform selection.
FLOATING_SELECTOR_TOKENS = frozenset(
    {"latest", "current", "default", "stable", "edge", "main", "master", "head", "tip"}
)

BUNDLE_ID_PATTERN = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"
_BUNDLE_ID_RE = re.compile(BUNDLE_ID_PATTERN)

COMPONENT_ID_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
_COMPONENT_ID_RE = re.compile(COMPONENT_ID_PATTERN)

SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SHA256_RE = re.compile(SHA256_PATTERN)

SHA256_NULLABLE_PATTERN = r"^(sha256:)?[0-9a-f]{64}$"
_SHA256_NULLABLE_RE = re.compile(SHA256_NULLABLE_PATTERN)

ISO8601_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
_ISO8601_RE = re.compile(ISO8601_PATTERN)

ARTIFACT_TYPES = frozenset({"none", "container_image", "source_package"})

#: Dependency mechanisms permitted by ARCHITECTURE.md §18. The registry
#: vocabulary is the single authority; this slice reuses it.
DEPENDENCY_KINDS = frozenset(
    {"api", "events", "data_export_cdc", "internal-consumer-surface"}
)

#: States that make a delivered bundle immutable (ARCHITECTURE.md §14:
#: the supplied bundle is immutable, identified by bundle_id + version +
#: digest). ``certified`` and later are the delivered states.
_IMMUTABLE_STATES = frozenset({"certified", "deprecated", "revoked"})


def parse_semver(value: object) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` for a strict SemVer string, else None."""
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_floating_selector(value: object) -> bool:
    """True when ``value`` is a floating selector rather than a concrete identity."""
    if not isinstance(value, str):
        return False
    token = value.strip().lower()
    if not token:
        return False
    return token in FLOATING_SELECTOR_TOKENS or "*" in token


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


# --------------------------------------------------------------------------
# Authoritative Component Registry loading (fail-closed)
# --------------------------------------------------------------------------


def _registry_unavailable_error(root: Path, error: BaseException) -> str:
    """Render an unloadable authoritative registry as a validation error."""
    reason = str(error).strip() or error.__class__.__name__
    return (
        "$.components: authoritative Component Registry cannot be loaded: "
        f"{reason}; Golden Bundle validation fails closed — the pinned "
        "composition cannot be checked against missing or invalid "
        "authoritative metadata (ADR-0015 §4, §7)"
    )


def _load_registry(root: Path) -> tuple[Any, list[str]]:
    """Load the authoritative Component Registry — **fail-closed**.

    Returns ``(registry, errors)``. ``registry`` is ``None`` only when the
    authoritative registry cannot be loaded, and in that case ``errors`` is
    non-empty. The Component Registry is the authoritative factory metadata
    source, so an unavailable registry is a validation failure of the bundle
    (ADR-0015 §4, §7).
    """
    try:
        from component_registry import load_registry
        from component_registry.errors import RegistryError
    except ImportError as error:
        return None, [_registry_unavailable_error(root, error)]

    try:
        registry = load_registry(root)
    except RegistryError as error:
        return None, [_registry_unavailable_error(root, error)]
    except Exception as error:  # noqa: BLE001 — fail closed, never fail open
        return None, [_registry_unavailable_error(root, error)]

    return registry, []


def _registry_component_ids(registry: Any) -> set[str]:
    if registry is None:
        return set()
    try:
        return set(registry.component_ids)
    except (AttributeError, KeyError, TypeError, ValueError):
        return set()


def _registry_entry(registry: Any, component_id: object) -> Mapping | None:
    """Return the authoritative entry for ``component_id``, or None."""
    if registry is None or not isinstance(component_id, str) or not component_id:
        return None
    if component_id not in _registry_component_ids(registry):
        return None
    return registry.entry(component_id)


def _canonical_digest(value: object) -> object:
    """Normalise a digest value for comparison.

    The optional ``sha256:`` prefix is a notation, not part of the digest
    value, so ``sha256:<hex>`` and ``<hex>`` denote the same artifact content.
    """
    if not isinstance(value, str):
        return value
    return value.strip().removeprefix("sha256:")


# --------------------------------------------------------------------------
# Per-field semantic checks
# --------------------------------------------------------------------------


def _identity_digest_errors(document: Mapping, path: str = "$") -> list[str]:
    errors: list[str] = []
    bundle_id = document.get("bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id:
        errors.append(f"{path}.bundle_id: a stable bundle identity is required")
    else:
        if is_floating_selector(bundle_id):
            errors.append(
                f"{path}.bundle_id: {bundle_id!r} is a floating selector, not a stable bundle identity"
            )
        if _BUNDLE_ID_RE.fullmatch(bundle_id) is None:
            errors.append(
                f"{path}.bundle_id: {bundle_id!r} does not match pattern {BUNDLE_ID_PATTERN!r}"
            )

    version = document.get("bundle_version")
    if is_floating_selector(version):
        errors.append(
            f"{path}.bundle_version: {version!r} is a floating selector; an explicit SemVer version is required"
        )
    elif parse_semver(version) is None and version is not None:
        errors.append(
            f"{path}.bundle_version: {version!r} is not SemVer MAJOR.MINOR.PATCH"
        )

    digest = document.get("bundle_digest")
    if is_floating_selector(digest):
        errors.append(
            f"{path}.bundle_digest: {digest!r} is a floating selector; an immutable digest is required"
        )
    elif isinstance(digest, str) and _SHA256_RE.fullmatch(digest) is None:
        errors.append(f"{path}.bundle_digest: {digest!r} is not a valid sha256 digest")

    if (
        isinstance(digest, str)
        and _SHA256_RE.fullmatch(digest) is not None
        and isinstance(bundle_id, str)
        and parse_semver(version) is not None
    ):
        try:
            computed = compute_bundle_digest(document)
            if digest != computed:
                errors.append(
                    f"{path}.bundle_digest: declared {digest!r} does not match computed digest {computed!r} of canonical content"
                )
        except (TypeError, ValueError, KeyError, AttributeError):
            pass

    return errors


def _lifecycle_errors(document: Mapping, path: str = "$") -> list[str]:
    errors: list[str] = []
    lifecycle = document.get("lifecycle")
    if not _is_mapping(lifecycle):
        errors.append(f"{path}.lifecycle: explicit bundle lifecycle state is required")
        return errors

    state = lifecycle.get("state")
    if state not in BUNDLE_STATES:
        errors.append(
            f"{path}.lifecycle.state: {state!r} is not a bundle lifecycle state"
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

    certification = document.get("certification")

    if isinstance(state, str):
        if requires_certification(state):
            if not _is_mapping(certification):
                errors.append(
                    f"{path}.certification: certification evidence is required when lifecycle is {state!r} (ARCHITECTURE.md §14)"
                )
            else:
                certified_by = certification.get("certified_by")
                certified_at = certification.get("certified_at")
                certification_id = certification.get("certification_id")
                checks = certification.get("checks")
                if is_floating_selector(certified_by) or is_floating_selector(
                    certification_id
                ):
                    errors.append(f"{path}.certification: contains floating selector")
                if (
                    isinstance(certified_at, str)
                    and _ISO8601_RE.fullmatch(certified_at) is None
                ):
                    errors.append(
                        f"{path}.certification.certified_at: {certified_at!r} is not ISO8601 UTC"
                    )
                if is_floating_selector(certified_at):
                    errors.append(
                        f"{path}.certification.certified_at: {certified_at!r} is a floating selector"
                    )
                if not isinstance(checks, list) or not checks:
                    errors.append(
                        f"{path}.certification.checks: at least one approved validation check is required"
                    )
        elif _is_mapping(certification):
            errors.append(
                f"{path}.certification: certification evidence must be null when lifecycle is {state!r} (certification exists only from certified onward)"
            )

    return errors


def _component_errors(document: Mapping, path: str, registry: Any) -> list[str]:
    """Pin checks: concrete, unique, ordered, registry-consistent, explicit.

    * identity/version/artifact must be explicit and selector-free;
    * every component must be registered with exactly the pinned version;
    * artifact identity must restate the authoritative registry (it may only
      pin, never invent or redefine);
    * components must be deterministically ordered;
    * printed here is a dual of ``platform_manifest.validation``'s
      component/artifact contract, so the bundle speaks the same metadata
      language as its authoritative sources.
    """
    errors: list[str] = []
    components = document.get("components")
    if not isinstance(components, list):
        errors.append(f"{path}.components: expected a list of component entries")
        return errors

    seen_ids: set[str] = set()

    for index, entry in enumerate(components):
        entry_path = f"{path}.components[{index}]"
        if not _is_mapping(entry):
            errors.append(f"{entry_path}: component entry must be an object")
            continue

        comp_id = entry.get("component_id")
        comp_version = entry.get("component_version")
        artifact = entry.get("artifact")

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

        if is_floating_selector(comp_version):
            errors.append(
                f"{entry_path}.component_version: {comp_version!r} is a floating selector; an explicit SemVer version is required"
            )
        elif parse_semver(comp_version) is None:
            errors.append(
                f"{entry_path}.component_version: {comp_version!r} is not SemVer MAJOR.MINOR.PATCH"
            )

        reg_entry = _registry_entry(registry, comp_id)
        if registry is not None and reg_entry is None:
            errors.append(
                f"{entry_path}.component_id: component {comp_id!r} is not registered in the canonical Component Registry"
            )
        elif reg_entry is not None:
            reg_version = reg_entry.get("component_version")
            if (
                isinstance(comp_version, str)
                and isinstance(reg_version, str)
                and comp_version != reg_version
            ):
                errors.append(
                    f"{entry_path}.component_version: bundle pins {comp_version!r}, canonical registry declares {reg_version!r} for {comp_id!r}"
                )

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
            if (
                not isinstance(digest, str)
                or _SHA256_NULLABLE_RE.fullmatch(digest) is None
            ):
                errors.append(
                    f"{entry_path}.artifact.digest: a published artifact requires an immutable digest"
                )
            if pinned is not True:
                errors.append(
                    f"{entry_path}.artifact.pinned: a published artifact must be pinned by digest"
                )

        if reg_entry is not None:
            registry_artifact = reg_entry.get("artifact")
            if not _is_mapping(registry_artifact):
                errors.append(
                    f"{entry_path}.artifact: canonical Component Registry declares no authoritative artifact metadata for this component"
                )
            else:
                if artifact_type != registry_artifact.get("artifact_type"):
                    errors.append(
                        f"{entry_path}.artifact.artifact_type: bundle declares {artifact_type!r}, canonical registry declares {registry_artifact.get('artifact_type')!r}"
                    )
                if _canonical_digest(digest) != _canonical_digest(
                    registry_artifact.get("digest")
                ):
                    errors.append(
                        f"{entry_path}.artifact.digest: bundle declares {digest!r}, canonical registry declares {registry_artifact.get('digest')!r}"
                    )
                if pinned != registry_artifact.get("pinned"):
                    errors.append(
                        f"{entry_path}.artifact.pinned: bundle declares {pinned!r}, canonical registry declares {registry_artifact.get('pinned')!r}"
                    )

    # Deterministic order: pinned set must be sorted by component_id.
    if isinstance(components, list) and len(components) > 1:
        ids = [entry.get("component_id") for entry in components if _is_mapping(entry)]
        sorted_ids = sorted(ids)
        if ids != sorted_ids:
            errors.append(
                f"{path}.components: components must be sorted by component_id for deterministic reproducibility (expected {sorted_ids!r}, got {ids!r})"
            )

    return errors


def _compatibility_errors(document: Mapping, path: str, registry: Any) -> list[str]:
    """Validate the explicit compatibility matrix against pinned dependencies.

    The matrix must be explicit and deterministic, and it must cover exactly
    the validated relationship set with no invented rows:

    * every pinned component's declared dependency (authoritative registry
      ``dependencies``) must have an explicit ``compatible: true`` verdict —
      a Golden Bundle packages *validated compatible* relationships (ADR-0015
      §7);
    * no matrix row may claim a dependency that the authoritative registry
      does not declare (the bundle does not add dependencies);
    * rows must be deterministic (sorted) and non-duplicated.
    """
    errors: list[str] = []
    compatibility = document.get("compatibility")
    if not _is_mapping(compatibility):
        errors.append(
            f"{path}.compatibility: explicit compatibility metadata is required"
        )
        return errors

    pairs = compatibility.get("pairs")
    if not isinstance(pairs, list):
        errors.append(
            f"{path}.compatibility.pairs: expected a list of compatibility pairs"
        )
        return errors

    components = document.get("components")
    pinned_ids = {
        entry.get("component_id")
        for entry in components
        if _is_mapping(entry) and isinstance(entry.get("component_id"), str)
    }
    pinned_versions = {
        entry.get("component_id"): entry.get("component_version")
        for entry in components
        if _is_mapping(entry)
        and isinstance(entry.get("component_id"), str)
        and parse_semver(entry.get("component_version")) is not None
    }

    seen_pairs: set[tuple[str, str, str]] = set()

    for index, pair in enumerate(pairs):
        pair_path = f"{path}.compatibility.pairs[{index}]"
        if not _is_mapping(pair):
            errors.append(f"{pair_path}: compatibility pair must be an object")
            continue

        from_id = pair.get("from")
        to_id = pair.get("to")
        mechanism = pair.get("mechanism")
        compatible = pair.get("compatible")

        if not isinstance(from_id, str) or not from_id:
            errors.append(f"{pair_path}.from: a stable component identity is required")
            continue
        if is_floating_selector(from_id):
            errors.append(f"{pair_path}.from: {from_id!r} is a floating selector")
        if from_id not in pinned_ids:
            errors.append(
                f"{pair_path}.from: component {from_id!r} is not pinned in the bundle"
            )

        if not isinstance(to_id, str) or not to_id:
            errors.append(f"{pair_path}.to: a stable component identity is required")
            continue
        if is_floating_selector(to_id):
            errors.append(f"{pair_path}.to: {to_id!r} is a floating selector")
        if to_id not in pinned_ids:
            errors.append(
                f"{pair_path}.to: component {to_id!r} is not pinned in the bundle"
            )

        if mechanism not in DEPENDENCY_KINDS:
            errors.append(
                f"{pair_path}.mechanism: {mechanism!r} is not a declared dependency mechanism"
            )
        if is_floating_selector(mechanism):
            errors.append(
                f"{pair_path}.mechanism: {mechanism!r} is a floating selector"
            )

        if not isinstance(compatible, bool):
            errors.append(
                f"{pair_path}.compatible: explicit boolean compatibility verdict is required"
            )

        if isinstance(from_id, str) and isinstance(to_id, str):
            edge = (
                (from_id, to_id, mechanism)
                if isinstance(mechanism, str)
                else (from_id, to_id, "")
            )
            if edge in seen_pairs:
                errors.append(
                    f"{pair_path}: duplicate compatibility pair ({from_id!r} → {to_id!r} via {mechanism!r})"
                )
            seen_pairs.add(edge)

    # Matrix must cover exactly the validated set: every declared dependency
    # of a pinned component over a pinned target must be recorded compatible.
    if registry is not None:
        for comp_id in sorted(pinned_ids):
            reg_entry = _registry_entry(registry, comp_id)
            if reg_entry is None:
                continue
            dependencies = reg_entry.get("dependencies", [])
            if not isinstance(dependencies, list):
                continue
            for dependency in dependencies:
                if not _is_mapping(dependency):
                    continue
                dep_id = dependency.get("component_id")
                kind = dependency.get("kind")
                version_range = dependency.get("version_range")
                if not isinstance(dep_id, str) or not dep_id:
                    continue
                if dep_id not in pinned_ids:
                    # The pinned set is incomplete relative to the declared
                    # dependencies (checked below in _dependency_consistency).
                    continue
                edge = (
                    (comp_id, dep_id, kind)
                    if isinstance(kind, str)
                    else (comp_id, dep_id, "")
                )
                if edge not in seen_pairs:
                    errors.append(
                        f"{path}.components: pinned component {comp_id!r} declares dependency {dep_id!r} ({version_range!r}), but no compatibility pair records it"
                    )
                else:
                    # A recorded edge must carry a validated compatible verdict
                    # for a pinned, in-range dependency.
                    verdict = None
                    for pair in pairs:
                        if not _is_mapping(pair):
                            continue
                        if (
                            pair.get("from") == comp_id
                            and pair.get("to") == dep_id
                            and pair.get("mechanism") == kind
                        ):
                            verdict = pair.get("compatible")
                            break
                    if verdict is not True:
                        errors.append(
                            f"{path}.compatibility.pairs: pinned dependency {comp_id!r} → {dep_id!r} is recorded but not validated compatible"
                        )
                    selected_version = pinned_versions.get(dep_id)
                    if selected_version is not None and not range_admits(
                        version_range, selected_version
                    ):
                        errors.append(
                            f"{path}.components: bundle pins {dep_id!r} {selected_version!r}, which does not satisfy {comp_id!r}'s declared range {version_range!r}"
                        )

    # No invented rows: a recorded pair must correspond to a declared
    # dependency of the authoritative registry, never a bundle-invented edge.
    if registry is not None:
        declared_edges: set[tuple[str, str, str]] = set()
        for comp_id in sorted(pinned_ids):
            reg_entry = _registry_entry(registry, comp_id)
            if reg_entry is None:
                continue
            dependencies = reg_entry.get("dependencies", [])
            if not isinstance(dependencies, list):
                continue
            for dependency in dependencies:
                if not _is_mapping(dependency):
                    continue
                dep_id = dependency.get("component_id")
                kind = dependency.get("kind")
                if isinstance(dep_id, str) and isinstance(kind, str):
                    declared_edges.add((comp_id, dep_id, kind))
        for pair in pairs:
            if not _is_mapping(pair):
                continue
            from_id = pair.get("from")
            to_id = pair.get("to")
            mechanism = pair.get("mechanism")
            if (
                isinstance(from_id, str)
                and isinstance(to_id, str)
                and isinstance(mechanism, str)
                and (from_id, to_id, mechanism) not in declared_edges
            ):
                errors.append(
                    f"{path}.compatibility.pairs: pair {from_id!r} → {to_id!r} via {mechanism!r} is not a dependency declared by the canonical Component Registry (the bundle does not invent dependencies)"
                )

    # Deterministic order of pairs.
    if len(pairs) > 1:
        keys = [
            (
                pair.get("from", ""),
                pair.get("to", ""),
                pair.get("mechanism", ""),
            )
            for pair in pairs
            if _is_mapping(pair)
        ]
        if keys != sorted(keys):
            errors.append(
                f"{path}.compatibility.pairs: compatibility pairs must be sorted deterministically by (from, to, mechanism)"
            )

    return errors


def _dependency_consistency_errors(
    document: Mapping, path: str, registry: Any
) -> list[str]:
    """Verify pin consistency: the bundle never resolves, adds or substitutes.

    For every pinned component, every dependency declared by the
    authoritative registry over the pinned set must be satisfiable by the
    pinned version. The bundle makes no choices and substitutes nothing: a
    missing or out-of-range pin is a deterministic error, never an implicit
    fix.
    """
    errors: list[str] = []
    components = document.get("components")
    if not isinstance(components, list) or registry is None:
        return errors

    pinned_versions = {
        entry.get("component_id"): entry.get("component_version")
        for entry in components
        if _is_mapping(entry)
        and isinstance(entry.get("component_id"), str)
        and parse_semver(entry.get("component_version")) is not None
    }

    for entry in components:
        if not _is_mapping(entry):
            continue
        comp_id = entry.get("component_id")
        if not isinstance(comp_id, str):
            continue
        reg_entry = _registry_entry(registry, comp_id)
        if reg_entry is None:
            continue
        dependencies = reg_entry.get("dependencies", [])
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if not _is_mapping(dependency):
                continue
            dep_id = dependency.get("component_id")
            version_range = dependency.get("version_range")
            if not isinstance(dep_id, str) or not dep_id:
                continue
            selected_version = pinned_versions.get(dep_id)
            if selected_version is None:
                errors.append(
                    f"{path}.components: pinned component {comp_id!r} requires dependency {dep_id!r}, but it is absent from the bundle"
                )
                continue
            if not range_admits(version_range, selected_version):
                errors.append(
                    f"{path}.components: component {comp_id!r} requires {dep_id!r} in range {version_range!r}, but the bundle pins {selected_version!r}"
                )
    return errors


# --------------------------------------------------------------------------
# Document-level validation
# --------------------------------------------------------------------------


def validate_document(document: object, *, root: Path | None = None) -> list[str]:
    """Validate a bundle document and return every violation, sorted.

    The same document always yields the same list. Validation is total and
    deterministic.
    """
    from golden_bundle.bundle import discover_root

    base = root if root is not None else discover_root()
    errors: list[str] = list(validate_structure(document, load_schema(base)))

    if not _is_mapping(document):
        return sorted(set(errors))

    errors.extend(_identity_digest_errors(document, "$"))
    errors.extend(_lifecycle_errors(document, "$"))

    registry, registry_errors = _load_registry(base)
    errors.extend(registry_errors)

    errors.extend(_component_errors(document, "$", registry))
    errors.extend(_compatibility_errors(document, "$", registry))
    errors.extend(_dependency_consistency_errors(document, "$", registry))

    return sorted(set(errors))


def check_immutability(
    existing_bundles: list[Mapping[str, Any]], new_bundle: Mapping[str, Any]
) -> list[str]:
    """Check that a new bundle does not silently rewrite a delivered bundle.

    ``existing_bundles`` is a list of already recorded bundles. If
    ``new_bundle`` carries the same ``bundle_id`` and ``bundle_version`` as an
    existing delivered (certified or later) bundle but different digest or
    content, it is rejected (ARCHITECTURE.md §14: the supplied bundle is
    immutable, identified by bundle_id + version + digest).

    Returns a list of errors (empty when immutable).
    """
    errors: list[str] = []
    if not _is_mapping(new_bundle):
        return ["new bundle is not an object"]

    new_id = new_bundle.get("bundle_id")
    new_version = new_bundle.get("bundle_version")
    new_digest = new_bundle.get("bundle_digest")

    if not isinstance(new_id, str) or not isinstance(new_version, str):
        return []

    for existing in existing_bundles:
        if not _is_mapping(existing):
            continue
        ex_id = existing.get("bundle_id")
        ex_version = existing.get("bundle_version")
        ex_digest = existing.get("bundle_digest")
        ex_state = _is_mapping(existing.get("lifecycle")) and existing.get(
            "lifecycle"
        ).get("state")

        if (
            ex_id == new_id
            and ex_version == new_version
            and ex_state in _IMMUTABLE_STATES
        ):
            if ex_digest != new_digest:
                errors.append(
                    f"$.bundle_digest: delivered bundle {new_id!r} version {new_version!r} is immutable; existing digest {ex_digest!r} != new digest {new_digest!r}"
                )
            try:
                from golden_bundle.bundle import (
                    bundle_content_for_digest,
                    canonical_json,
                )

                existing_content = bundle_content_for_digest(existing)
                new_content = bundle_content_for_digest(new_bundle)
                if (
                    canonical_json(existing_content) != canonical_json(new_content)
                    and ex_digest == new_digest
                ):
                    errors.append(
                        f"$.bundle_id: delivered bundle {new_id!r} version {new_version!r} content differs but digest is the same — tampering"
                    )
            except (TypeError, ValueError, KeyError, AttributeError):
                pass

    return sorted(set(errors))


def validate_transition(previous_state: str, new_state: str) -> list[str]:
    """Validate a lifecycle transition for the same bundle identity/version.

    Returns a list of errors (empty when allowed).
    """
    if previous_state not in BUNDLE_STATES:
        return [f"previous state {previous_state!r} is not a bundle lifecycle state"]
    if new_state not in BUNDLE_STATES:
        return [f"new state {new_state!r} is not a bundle lifecycle state"]
    if previous_state == new_state:
        return []
    if is_allowed_transition(previous_state, new_state):
        return []
    from golden_bundle.lifecycle import _ALLOWED_NEXT

    allowed = _ALLOWED_NEXT.get(previous_state, frozenset())
    if allowed:
        return [
            f"lifecycle transition {previous_state!r} → {new_state!r} is not allowed; allowed next: {sorted(allowed)!r}"
        ]
    return [
        f"lifecycle transition {previous_state!r} → {new_state!r} is not allowed; {previous_state!r} is terminal"
    ]
