"""Semantic validation of the Component Registry.

Structural shape is validated against the shipped JSON Schema
(:mod:`component_registry.schema`). Everything a schema cannot express is
enforced here:

* identity is stable, unique and never a floating selector;
* every public version is an explicit SemVer, never ``latest``;
* contract references resolve to real published contracts that agree with
  the entry that references them;
* declared dependencies are explicit, resolve inside the registry and are
  satisfied by the registered version of their target;
* compatibility, ownership, artifact identity and lifecycle metadata are
  explicit and internally consistent.

The registry is validated purely as metadata: this module reads published
contract descriptors and never imports a component's internals and never
opens a component database (ARCHITECTURE.md §1.1, §18; ADR-0015 §8, §11).
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from component_registry.schema import load_schema, validate_structure

# --------------------------------------------------------------------------
# Vocabulary — every value below is grounded in the ratified architecture or
# in the published component contracts, not invented by Slice A.
# --------------------------------------------------------------------------

#: Strict public SemVer MAJOR.MINOR.PATCH (ARCHITECTURE.md §11).
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

DATA_SCOPES = frozenset({"tenant-scoped", "platform-scoped", "system-scoped"})
COMPONENT_CLASSES = frozenset(
    {
        "business_system",
        "platform_service",
        "computational_service",
        "streaming_pipeline",
    }
)
DEPENDENCY_KINDS = frozenset(
    {"api", "events", "data_export_cdc", "internal-consumer-surface"}
)
FORBIDDEN_DEPENDENCY_KINDS = frozenset(
    {"database", "internal_code", "private_schema", "internal_queue"}
)
ARTIFACT_TYPES = frozenset({"none", "source_package", "container_image"})
LIFECYCLE_STATES = frozenset({"registered", "deprecated", "retired"})
BREAKING_CHANGE_REQUIREMENTS = frozenset({"major_version"})
API_POLICIES = frozenset({"additive-minor-breaking-major"})
EVENT_POLICIES = frozenset({"tolerant-reader"})

#: Canonical repository layout of a published component contract.
#: A contract is identified by where the architecture says it lives, not by
#: whether some file somewhere happens to declare a matching ``component_id``.
CONTRACT_ROOT = "components"
CONTRACT_DIRNAME = "contract"
CONTRACT_FILENAME = "component_contract.json"
OPENAPI_FILENAME = "openapi.yaml"

#: JSON Pointer (RFC 6901) that each ``source`` reference must address inside
#: the canonical component contract.
OWNERSHIP_POINTER = "/data_ownership"
COMPATIBILITY_POINTER = "/compatibility_policy"

_CLAUSE_RE = re.compile(r"^(==|>=|<=|>|<)(?P<version>.+)$")
_DIGEST_RE = re.compile(r"^(sha256:)?[0-9a-f]{64}$")

#: A URL-ish scheme prefix: ``https:``, ``file:``, ``C:`` — never a repository
#: relative path.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

#: A JSON Pointer token: an escaped ``~0``/``~1`` run or any non-tilde run.
_POINTER_TOKEN_RE = re.compile(r"(?:~[01]|[^~])*")

_OPERATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


# --------------------------------------------------------------------------
# Small deterministic predicates
# --------------------------------------------------------------------------


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


def parse_version_range(value: object) -> tuple[tuple[str, str], ...] | None:
    """Split an explicit range such as ``>=0.3.0,<0.4.0`` into its clauses."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    clauses: list[tuple[str, str]] = []
    for raw in text.split(","):
        clause = raw.strip()
        if not clause:
            return None
        match = _CLAUSE_RE.fullmatch(clause)
        if match is None:
            return None
        clauses.append((match.group(1), match.group("version").strip()))
    return tuple(clauses)


def range_admits(version_range: object, version: object) -> bool:
    """True when every clause of ``version_range`` admits ``version``."""
    clauses = parse_version_range(version_range)
    target = parse_semver(version)
    if clauses is None or target is None:
        return False
    for operator_name, bound in clauses:
        candidate = parse_semver(bound)
        if candidate is None:
            return False
        if not _OPERATORS[operator_name](target, candidate):
            return False
    return True


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


# --------------------------------------------------------------------------
# Canonical reference semantics
#
# A registry reference is not an arbitrary filesystem path: it is a
# repository-relative canonical reference whose identity is derived from the
# authoritative component_id. A file that merely declares a matching
# component_id at some other path is a decoy and must be rejected.
# --------------------------------------------------------------------------


def canonical_contract_path(component_id: object) -> str | None:
    """Return the canonical contract path of ``component_id``, or None."""
    if not isinstance(component_id, str) or not component_id:
        return None
    return f"{CONTRACT_ROOT}/{component_id}/{CONTRACT_DIRNAME}/{CONTRACT_FILENAME}"


def canonical_openapi_path(component_id: object) -> str | None:
    """Return the canonical OpenAPI path of ``component_id``, or None."""
    if not isinstance(component_id, str) or not component_id:
        return None
    return f"{CONTRACT_ROOT}/{component_id}/{CONTRACT_DIRNAME}/{OPENAPI_FILENAME}"


def _path_violation(value: object) -> str | None:
    """Return a reason when ``value`` is not a canonical repository-relative path.

    Rejects, in order: non-strings, URL-like references, non-POSIX separators,
    absolute paths, traversal/normalization segments, and any path that is not
    already normalized — so ``a//b``, ``./a`` and ``a/b/`` cannot be used to
    smuggle a second spelling of the canonical path.
    """
    if not isinstance(value, str) or not value:
        return "a canonical repository-relative path is required"
    if _SCHEME_RE.match(value) or value.startswith("//"):
        return f"{value!r} is a URL-like reference, not a repository-relative path"
    if "\\" in value:
        return f"{value!r} uses a non-canonical path separator"
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        return f"{value!r} is an absolute filesystem path"
    if any(part in {"..", "."} for part in candidate.parts):
        return f"{value!r} contains a path traversal or normalization segment"
    if str(candidate) != value:
        return f"{value!r} is not a normalized repository-relative path"
    return None


def _resolve_json_pointer(document: object, pointer: str) -> tuple[bool, object]:
    """Resolve an RFC 6901 JSON Pointer; return ``(resolved, node)``."""
    if not pointer.startswith("/"):
        return False, None
    if pointer == "/":
        return True, document
    node: object = document
    for raw_token in pointer[1:].split("/"):
        if _POINTER_TOKEN_RE.fullmatch(raw_token) is None:
            return False, None
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if _is_mapping(node) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return False, None
    return True, node


def _canonical_path_errors(
    field: str,
    path: str,
    reference: object,
    component_id: object,
    expected: str | None,
) -> list[str]:
    """Validate that ``reference`` *is* the canonical path, without reading it.

    Used for every reference whose identity must be canonical, including the
    OpenAPI document, which is YAML and therefore not parsed as JSON here.
    """
    if not isinstance(reference, str) or not reference:
        return [f"{path}.{field}: a canonical contract reference is required"]
    if "#" in reference:
        return [
            f"{path}.{field}: {reference!r} must not carry a fragment; it addresses the whole contract"
        ]
    violation = _path_violation(reference)
    if violation is not None:
        return [f"{path}.{field}: {violation}"]
    if expected is None:
        return [
            f"{path}.{field}: the entry has no component identity from which to derive a canonical contract"
        ]
    if reference != expected:
        suffix = (
            f" of component {component_id!r}" if isinstance(component_id, str) else ""
        )
        return [
            f"{path}.{field}: {reference!r} is not the canonical contract {expected!r}{suffix}"
        ]
    return []


def _canonical_contract_errors(
    field: str,
    path: str,
    reference: object,
    component_id: object,
    expected: str | None,
    root: Path,
) -> tuple[list[str], Mapping | None]:
    """Validate a whole-contract reference (no fragment) and load the contract."""
    errors = _canonical_path_errors(field, path, reference, component_id, expected)
    if errors or not isinstance(reference, str):
        return errors, None
    document, error = _read_published_contract(reference, root)
    if error is not None:
        errors.append(f"{path}.{field}: {error}")
        return errors, None
    return errors, document


def _canonical_source_errors(
    field: str,
    path: str,
    source: object,
    component_id: object,
    expected_pointer: str,
    root: Path,
) -> tuple[list[str], Mapping | None]:
    """Validate a ``canonical contract path # JSON Pointer`` source reference.

    Every part of the reference is checked: exactly one fragment, a canonical
    document path for *this* component, the expected JSON Pointer, and that
    the pointer really resolves to an object inside that document.
    """
    errors: list[str] = []
    if not isinstance(source, str) or not source:
        errors.append(f"{path}.{field}: a canonical source reference is required")
        return errors, None
    if source.count("#") != 1:
        errors.append(
            f"{path}.{field}: {source!r} must be the canonical component contract plus exactly one JSON Pointer fragment"
        )
        return errors, None

    document_path, _, fragment = source.partition("#")
    violation = _path_violation(document_path)
    if violation is not None:
        errors.append(f"{path}.{field}: {violation}")
        return errors, None

    expected_path = canonical_contract_path(component_id)
    if expected_path is None:
        errors.append(
            f"{path}.{field}: the entry has no component identity from which to derive a canonical contract"
        )
        return errors, None
    if document_path != expected_path:
        errors.append(
            f"{path}.{field}: {document_path!r} is not the canonical contract {expected_path!r} of component {component_id!r}"
        )
        return errors, None
    if fragment != expected_pointer:
        errors.append(
            f"{path}.{field}: fragment {fragment!r} does not address {expected_pointer!r} of the canonical contract"
        )
        return errors, None

    document, error = _read_published_contract(document_path, root)
    if error is not None:
        errors.append(f"{path}.{field}: {error}")
        return errors, None
    resolved, node = _resolve_json_pointer(document, fragment)
    if not resolved:
        errors.append(
            f"{path}.{field}: JSON Pointer {fragment!r} does not resolve inside {document_path}"
        )
        return errors, None
    if not _is_mapping(node):
        errors.append(
            f"{path}.{field}: JSON Pointer {fragment!r} does not address an object inside {document_path}"
        )
        return errors, None
    return errors, document


def _read_published_contract(
    relative: object, root: Path
) -> tuple[Mapping | None, str | None]:
    """Load a published contract descriptor; return ``(document, error)``."""
    if not isinstance(relative, str) or not relative:
        return None, "contract reference is not a non-empty string"
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if not candidate.is_relative_to(root_resolved):
        return None, f"contract reference escapes the repository: {relative!r}"
    if not candidate.is_file():
        return None, f"referenced contract does not exist: {relative}"
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return (
            None,
            f"referenced contract is unreadable: {relative} ({error.__class__.__name__})",
        )
    if not _is_mapping(document):
        return None, f"referenced contract is not a JSON object: {relative}"
    return document, None


# --------------------------------------------------------------------------
# Per-entry semantic rules
# --------------------------------------------------------------------------


def _identity_errors(entry: Mapping, path: str) -> list[str]:
    errors: list[str] = []
    component_id = entry.get("component_id")
    if is_floating_selector(component_id):
        errors.append(
            f"{path}.component_id: {component_id!r} is a floating selector, not a stable component identity"
        )
    version = entry.get("component_version")
    if is_floating_selector(version):
        errors.append(
            f"{path}.component_version: {version!r} is a floating selector; an explicit SemVer version is required"
        )
    elif parse_semver(version) is None and version is not None:
        errors.append(
            f"{path}.component_version: {version!r} is not SemVer MAJOR.MINOR.PATCH"
        )
    if entry.get("class") not in COMPONENT_CLASSES:
        errors.append(f"{path}.class: {entry.get('class')!r} is not a component class")
    owner = entry.get("owner")
    if isinstance(owner, str) and not owner.strip():
        errors.append(f"{path}.owner: an explicit owner is required")
    return errors


def _contract_errors(entry: Mapping, path: str, root: Path) -> list[str]:
    errors: list[str] = []
    contracts = entry.get("contracts")
    if not _is_mapping(contracts):
        return [f"{path}.contracts: published contract references are required"]

    component_id = entry.get("component_id")
    contract_errors, document = _canonical_contract_errors(
        "contracts.component_contract",
        path,
        contracts.get("component_contract"),
        component_id,
        canonical_contract_path(component_id),
        root,
    )
    errors.extend(contract_errors)

    if document is not None:
        declared_id = document.get("component_id")
        if declared_id != component_id:
            errors.append(
                f"{path}.contracts.component_contract: contract declares component_id {declared_id!r}, registry declares {component_id!r}"
            )
        declared_version = document.get("component_version")
        if declared_version != entry.get("component_version"):
            errors.append(
                f"{path}.contracts.component_contract: contract declares component_version {declared_version!r}, registry declares {entry.get('component_version')!r}"
            )

    openapi = contracts.get("openapi")
    if openapi is not None:
        openapi_errors = _canonical_path_errors(
            "contracts.openapi",
            path,
            openapi,
            component_id,
            canonical_openapi_path(component_id),
        )
        errors.extend(openapi_errors)
        if not openapi_errors and not (root / openapi).is_file():
            errors.append(
                f"{path}.contracts.openapi: referenced contract does not exist: {openapi}"
            )
    return errors


def _ownership_errors(entry: Mapping, path: str, root: Path) -> list[str]:
    errors: list[str] = []
    ownership = entry.get("data_ownership")
    if not _is_mapping(ownership):
        return [f"{path}.data_ownership: explicit data ownership metadata is required"]

    component_id = entry.get("component_id")
    if ownership.get("owner") != component_id:
        errors.append(
            f"{path}.data_ownership.owner: {ownership.get('owner')!r} must be the component that owns the data ({component_id!r})"
        )

    datasets = ownership.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        errors.append(
            f"{path}.data_ownership.datasets: at least one dataset is required"
        )
        return errors

    scopes: set[str] = set()
    seen: set[str] = set()
    for index, dataset in enumerate(datasets):
        item = f"{path}.data_ownership.datasets[{index}]"
        if not _is_mapping(dataset):
            errors.append(f"{item}: dataset must be an object")
            continue
        name = dataset.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{item}.name: dataset name is required")
            continue
        if name in seen:
            errors.append(f"{item}.name: duplicate dataset {name!r}")
        seen.add(name)
        scope = dataset.get("scope")
        if scope not in DATA_SCOPES:
            errors.append(f"{item}.scope: {scope!r} is not a data scope (LAW-16a)")
        else:
            scopes.add(scope)

    declared = ownership.get("data_scopes")
    if isinstance(declared, list) and set(declared) != scopes:
        errors.append(
            f"{path}.data_ownership.data_scopes: {sorted(declared)!r} disagrees with the scopes of the declared datasets {sorted(scopes)!r}"
        )

    source = ownership.get("source")
    if isinstance(source, str) and source:
        source_errors, document = _canonical_source_errors(
            "data_ownership.source",
            path,
            source,
            component_id,
            OWNERSHIP_POINTER,
            root,
        )
        errors.extend(source_errors)
        if document is not None:
            published = document.get("data_ownership", {})
            if not _is_mapping(published):
                errors.append(
                    f"{path}.data_ownership.source: {OWNERSHIP_POINTER!r} does not address an object in the canonical contract"
                )
            elif not isinstance(published.get("datasets"), list):
                errors.append(
                    f"{path}.data_ownership.source: the canonical contract declares no dataset list at {OWNERSHIP_POINTER}/datasets"
                )
            else:
                published_pairs = {
                    (item.get("name"), item.get("scope"))
                    for item in published["datasets"]
                    if _is_mapping(item)
                }
                registry_pairs = {
                    (item.get("name"), item.get("scope"))
                    for item in datasets
                    if _is_mapping(item)
                }
                if published_pairs and published_pairs != registry_pairs:
                    errors.append(
                        f"{path}.data_ownership.datasets: registry metadata diverges from the published contract {canonical_contract_path(component_id)}"
                    )
    return errors


def _dependency_errors(
    entry: Mapping,
    path: str,
    root: Path,
    registered: Mapping[str, Mapping],
) -> list[str]:
    errors: list[str] = []
    dependencies = entry.get("dependencies")
    if not isinstance(dependencies, list):
        return [f"{path}.dependencies: a list of declared dependencies is required"]

    component_id = entry.get("component_id")
    seen: set[str] = set()
    for index, dependency in enumerate(dependencies):
        item = f"{path}.dependencies[{index}]"
        if not _is_mapping(dependency):
            errors.append(f"{item}: dependency must be an object")
            continue
        target = dependency.get("component_id")
        if is_floating_selector(target):
            errors.append(f"{item}.component_id: {target!r} is a floating selector")
        if target == component_id:
            errors.append(f"{item}.component_id: a component may not depend on itself")
        if target not in registered:
            errors.append(
                f"{item}.component_id: dependency target {target!r} is not registered"
            )
        if isinstance(target, str) and target in seen:
            errors.append(f"{item}.component_id: duplicate dependency on {target!r}")
        if isinstance(target, str):
            seen.add(target)

        version_range = dependency.get("version_range")
        clauses = parse_version_range(version_range)
        if clauses is None:
            errors.append(
                f"{item}.version_range: {version_range!r} is not an explicit version constraint"
            )
        else:
            for operator_name, bound in clauses:
                if is_floating_selector(bound):
                    errors.append(
                        f"{item}.version_range: {bound!r} is a floating selector, not a version"
                    )
                elif parse_semver(bound) is None:
                    errors.append(
                        f"{item}.version_range: {bound!r} is not SemVer MAJOR.MINOR.PATCH"
                    )
            target_entry = registered.get(target) if isinstance(target, str) else None
            if target_entry is not None and not range_admits(
                version_range, target_entry.get("component_version")
            ):
                errors.append(
                    f"{item}.version_range: {version_range!r} does not admit the registered version {target_entry.get('component_version')!r} of {target!r}"
                )

        kind = dependency.get("kind")
        if kind in FORBIDDEN_DEPENDENCY_KINDS:
            errors.append(
                f"{item}.kind: {kind!r} crosses a component boundary (ARCHITECTURE.md §18)"
            )
        elif kind not in DEPENDENCY_KINDS:
            errors.append(
                f"{item}.kind: {kind!r} is not a declared dependency mechanism"
            )

        dependency_contract = dependency.get("contract")
        if dependency_contract is not None:
            contract_errors, document = _canonical_contract_errors(
                f"dependencies[{index}].contract",
                path,
                dependency_contract,
                target,
                canonical_contract_path(target),
                root,
            )
            errors.extend(contract_errors)
            if document is not None:
                if document.get("component_id") != target:
                    errors.append(
                        f"{item}.contract: contract declares component_id {document.get('component_id')!r}, dependency targets {target!r}"
                    )
                target_entry = (
                    registered.get(target) if isinstance(target, str) else None
                )
                if target_entry is not None:
                    declared = document.get("component_version")
                    registered_version = target_entry.get("component_version")
                    if declared != registered_version:
                        errors.append(
                            f"{item}.contract: contract declares component_version {declared!r}, which is not the registered version {registered_version!r} of {target!r}"
                        )
    return errors


def _compatibility_errors(entry: Mapping, path: str, root: Path) -> list[str]:
    errors: list[str] = []
    compatibility = entry.get("compatibility")
    if not _is_mapping(compatibility):
        return [f"{path}.compatibility: explicit compatibility metadata is required"]

    api_policy = compatibility.get("api_policy")
    if api_policy not in API_POLICIES:
        errors.append(
            f"{path}.compatibility.api_policy: {api_policy!r} is not a declared API policy"
        )
    if is_floating_selector(api_policy):
        errors.append(
            f"{path}.compatibility.api_policy: {api_policy!r} is a floating selector"
        )

    event_policy = compatibility.get("event_policy")
    if event_policy is not None:
        if event_policy not in EVENT_POLICIES:
            errors.append(
                f"{path}.compatibility.event_policy: {event_policy!r} is not a declared event policy"
            )
        if is_floating_selector(event_policy):
            errors.append(
                f"{path}.compatibility.event_policy: {event_policy!r} is a floating selector"
            )

    if (
        compatibility.get("breaking_change_requires")
        not in BREAKING_CHANGE_REQUIREMENTS
    ):
        errors.append(
            f"{path}.compatibility.breaking_change_requires: a breaking change requires a new major version (ARCHITECTURE.md §1.4)"
        )

    source = compatibility.get("source")
    if isinstance(source, str) and source:
        source_errors, document = _canonical_source_errors(
            "compatibility.source",
            path,
            source,
            entry.get("component_id"),
            COMPATIBILITY_POINTER,
            root,
        )
        errors.extend(source_errors)
        if document is not None:
            published = document.get("compatibility_policy", {})
            if not _is_mapping(published):
                errors.append(
                    f"{path}.compatibility.source: {COMPATIBILITY_POINTER!r} does not address an object in the canonical contract"
                )
            else:
                if published.get("api") != api_policy:
                    errors.append(
                        f"{path}.compatibility.api_policy: registry declares {api_policy!r}, published contract declares {published.get('api')!r}"
                    )
                if published.get("events") != event_policy:
                    errors.append(
                        f"{path}.compatibility.event_policy: registry declares {event_policy!r}, published contract declares {published.get('events')!r}"
                    )
    return errors


def _artifact_errors(entry: Mapping, path: str) -> list[str]:
    errors: list[str] = []
    artifact = entry.get("artifact")
    if not _is_mapping(artifact):
        return [f"{path}.artifact: explicit artifact identity metadata is required"]

    artifact_type = artifact.get("artifact_type")
    if artifact_type not in ARTIFACT_TYPES:
        errors.append(
            f"{path}.artifact.artifact_type: {artifact_type!r} is not an artifact type"
        )
    if is_floating_selector(artifact_type):
        errors.append(
            f"{path}.artifact.artifact_type: {artifact_type!r} is a floating selector"
        )

    digest = artifact.get("digest")
    if is_floating_selector(digest):
        errors.append(
            f"{path}.artifact.digest: {digest!r} is a floating selector; an artifact is selected by immutable digest (ARCHITECTURE.md §1.3)"
        )
    pinned = artifact.get("pinned")
    canonical_form = artifact.get("canonical_form")

    if artifact_type == "none":
        if digest is not None:
            errors.append(
                f"{path}.artifact.digest: artifact_type 'none' must not declare a digest"
            )
        if pinned is not False:
            errors.append(
                f"{path}.artifact.pinned: artifact_type 'none' must not be pinned"
            )
        if canonical_form is not None:
            errors.append(
                f"{path}.artifact.canonical_form: artifact_type 'none' must not declare a canonical form"
            )
    elif artifact_type == "source_package":
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            errors.append(
                f"{path}.artifact.digest: a published artifact requires an immutable digest (ARCHITECTURE.md §11)"
            )
        if pinned is not True:
            errors.append(
                f"{path}.artifact.pinned: a published artifact must be pinned by digest"
            )
        if canonical_form != "source_package/v1":
            errors.append(
                f"{path}.artifact.canonical_form: source_package requires 'source_package/v1'"
            )
    elif artifact_type == "container_image":
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            errors.append(
                f"{path}.artifact.digest: a published artifact requires an immutable digest (ARCHITECTURE.md §11)"
            )
        if pinned is not True:
            errors.append(
                f"{path}.artifact.pinned: a published artifact must be pinned by digest"
            )
        if canonical_form != "container_image/v1":
            errors.append(
                f"{path}.artifact.canonical_form: container_image requires 'container_image/v1'"
            )
    return errors


def _lifecycle_errors(entry: Mapping, path: str) -> list[str]:
    errors: list[str] = []
    lifecycle = entry.get("lifecycle")
    if not _is_mapping(lifecycle):
        return [f"{path}.lifecycle: an explicit lifecycle/release state is required"]

    state = lifecycle.get("registry_state")
    if state not in LIFECYCLE_STATES:
        errors.append(
            f"{path}.lifecycle.registry_state: {state!r} is not a registry lifecycle state"
        )
    if is_floating_selector(state):
        errors.append(
            f"{path}.lifecycle.registry_state: {state!r} is a floating selector"
        )

    deployable = lifecycle.get("deployable")
    publishable = lifecycle.get("publishable")
    blockers = lifecycle.get("publishability_blockers")

    if not isinstance(publishable, bool):
        errors.append(f"{path}.lifecycle.publishable: must be a boolean")
    elif publishable is False:
        if not isinstance(blockers, list) or not blockers:
            errors.append(
                f"{path}.lifecycle.publishability_blockers: a non-publishable entry must state why (ARCHITECTURE.md §30)"
            )
    elif isinstance(blockers, list) and blockers:
        errors.append(
            f"{path}.lifecycle.publishability_blockers: a publishable entry must not state blockers"
        )

    if not isinstance(deployable, bool):
        errors.append(f"{path}.lifecycle.deployable: must be a boolean")
    else:
        artifact = entry.get("artifact")
        pinned = artifact.get("pinned") if _is_mapping(artifact) else None
        if deployable and pinned is not True:
            errors.append(
                f"{path}.lifecycle.deployable: a deployable entry requires a pinned artifact (ARCHITECTURE.md §1.3, §31)"
            )
    return errors


# --------------------------------------------------------------------------
# Document-level validation
# --------------------------------------------------------------------------


def _discover_inventory(root: Path) -> set[str]:
    """Return component ids that publish a contract descriptor under components/."""
    inventory: set[str] = set()
    for path in sorted(
        (root / "components").glob("*/contract/component_contract.json")
    ):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if _is_mapping(document) and isinstance(document.get("component_id"), str):
            inventory.add(document["component_id"])
    return inventory


def validate_document(
    document: object,
    *,
    root: Path | None = None,
    check_inventory: bool = True,
) -> list[str]:
    """Validate a registry document and return every violation, sorted.

    The same document always yields the same list. ``check_inventory``
    additionally requires the registry to describe exactly the component
    inventory that publishes contracts under ``components/`` — no invented
    components, none silently missing.
    """
    from component_registry.registry import discover_root

    base = root if root is not None else discover_root()
    errors: list[str] = list(validate_structure(document, load_schema(base)))

    if not _is_mapping(document):
        return sorted(set(errors))

    components = document.get("components")
    if not isinstance(components, list):
        errors.append("$.components: expected a list of component entries")
        return sorted(set(errors))

    registered: dict[str, Mapping] = {}
    for index, entry in enumerate(components):
        path = f"$.components[{index}]"
        if not _is_mapping(entry):
            errors.append(f"{path}: component entry must be an object")
            continue
        component_id = entry.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            errors.append(
                f"{path}.component_id: a stable component identity is required"
            )
            continue
        if component_id in registered:
            errors.append(
                f"{path}.component_id: duplicate component identity {component_id!r}"
            )
            continue
        registered[component_id] = entry

    for index, entry in enumerate(components):
        path = f"$.components[{index}]"
        if not _is_mapping(entry):
            continue
        errors.extend(_identity_errors(entry, path))
        errors.extend(_contract_errors(entry, path, base))
        errors.extend(_ownership_errors(entry, path, base))
        errors.extend(_dependency_errors(entry, path, base, registered))
        errors.extend(_compatibility_errors(entry, path, base))
        errors.extend(_artifact_errors(entry, path))
        errors.extend(_lifecycle_errors(entry, path))

    if check_inventory:
        published = _discover_inventory(base)
        declared = set(registered)
        for component_id in sorted(declared - published):
            errors.append(
                f"$.components: {component_id!r} is registered but publishes no component contract"
            )
        for component_id in sorted(published - declared):
            errors.append(
                f"$.components: {component_id!r} publishes a component contract but is not registered"
            )

    return sorted(set(errors))
