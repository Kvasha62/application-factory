"""Deterministic component artifact build, OCI packaging, GHCR publication seam,
dependency closure validation, and fail-closed registry reconciliation for the
nine registered components (Issue #127).

Sources of truth:
- ``factory/registry/component_registry.json``
- ``factory/registry/README.md`` §9.1
- ``src/factory_artifact/source_package.py``
- ``src/factory_artifact/container_image.py``
- ``src/factory_artifact/descriptor.py``
- ``src/component_registry/validation.py``

This module does NOT create a second canonicalization or descriptor authority:
``factory_artifact.source_package``, ``factory_artifact.container_image``, and
``factory_artifact.descriptor`` are the sole authorities for canonical artifact
representation, SHA-256 digest calculation, descriptor construction, and
physical-to-canonical verification.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from component_registry import (
    REGISTRY_PATH,
    is_floating_selector,
    load_registry,
    load_registry_document,
    parse_semver,
    parse_version_range,
    range_admits,
    validate_document,
)
from factory_artifact import (
    CANONICAL_FORMS,
    DIGEST_PATTERN,
    DIGEST_PREFIX,
    ArtifactDescriptor,
    ArtifactError,
    artifact_violations,
    describe_container_image,
    describe_source_package,
    descriptor_from_registry_metadata,
    registry_metadata,
    registry_metadata_violations,
    verify_artifact,
)
from factory_artifact.container_image import build_container_image_canonical
from factory_artifact.source_package import build_source_package_canonical

#: Explicitly approved nine-component production set (Issue #127).
COMPONENT_ROOTS: dict[str, Path] = {
    "authorization": Path("src/authorization_service"),
    "identity": Path("src/identity_service"),
    "tenant_authority": Path("src/tenant_authority"),
    "records": Path("src/records_service"),
    "learning": Path("src/learning_service"),
    "saga": Path("src/saga"),
    "idempotency": Path("src/idempotency"),
    "commerce": Path("src/commerce_service"),
    "booking": Path("src/booking_service"),
}

APPROVED_COMPONENT_IDS: frozenset[str] = frozenset(COMPONENT_ROOTS)

WORKFLOW_PATH = Path(".github/workflows/publish-component-artifacts.yml")

#: Immutable content-addressed digest lock for each registered
#: ``(component_id, component_version)`` source-package artifact. A given
#: ``(component_id, component_version)`` cannot silently point to different
#: artifact content: changing component source requires an explicit
#: ``component_version`` bump and corresponding digest lock update.
COMPONENT_VERSION_DIGESTS: dict[tuple[str, str], str] = {
    ("authorization", "0.1.0"): (
        "sha256:98463d8d1e7eadbd843a457f8fc11818b425bc6237802ff0aa031b49f07bf211"
    ),
    ("booking", "0.1.0"): (
        "sha256:03ac4b88cacb311c5447385c039abc41ee11ac19db482b6b3711c2d482384d6e"
    ),
    ("commerce", "0.2.0"): (
        "sha256:bf2d23fabcce4a755b465f597d9f35ee437724331347ea6a11b8c730e0e8ff35"
    ),
    ("idempotency", "0.1.0"): (
        "sha256:7980398597cd4cbab1052c09c8c1efc05424c637ac1c3fb819aae5c6bd308988"
    ),
    ("identity", "0.3.0"): (
        "sha256:e0d77dbeefff0a07dd0605555334dfed81714b7b6b5154d5d29c667f3817c40c"
    ),
    ("learning", "0.3.0"): (
        "sha256:e7c28a27d50ef5a99110aa9e02a68126e2316cc1a3687ae44b7fd3c8502bc82c"
    ),
    ("records", "0.1.0"): (
        "sha256:0912156cf1b124b0b49feccc37a047427ca5b62a8877f59c47714d2a45e721f1"
    ),
    ("saga", "0.1.0"): (
        "sha256:cdd548b073ec8e1db790624ab036cde003cb79f424491290dc21430f2221d2ed"
    ),
    ("tenant_authority", "0.1.0"): (
        "sha256:60d817d9febf648aee54e43c6f6c2ea8d040039a06ad9d601efd4b0de7e7f80c"
    ),
}

_SHARED_INFRA_SRC_PATTERNS = frozenset(
    {
        "src/factory_artifact/**",
        "src/component_registry/**",
    }
)
_REQUIRED_TRIGGER_EVENTS = ("pull_request", "push")

DEFAULT_OCI_REGISTRY = "ghcr.io"
DEFAULT_OCI_NAMESPACE = "kvasha62/application-factory"

OCI_LAYOUT_VERSION = "1.0.0"
OCI_IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.application-factory.component.config.v1+json"
CANONICAL_MANIFEST_MEDIA_TYPE = (
    "application/vnd.application-factory.canonical-manifest.v1+json"
)
SOURCE_PACKAGE_OCI_ARTIFACT_TYPE = (
    "application/vnd.application-factory.component.source-package.v1"
)
SOURCE_PACKAGE_LAYER_MEDIA_TYPE = (
    "application/vnd.application-factory.source-package.v1+tar"
)
CONTAINER_IMAGE_OCI_ARTIFACT_TYPE = (
    "application/vnd.application-factory.component.container-image.v1"
)
CONTAINER_IMAGE_LAYER_MEDIA_TYPE = (
    "application/vnd.application-factory.container-image.layer.v1+tar"
)

_DIGEST_RE = re.compile(DIGEST_PATTERN)
_DIGEST_REFERENCE_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\.[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{64}$"
)
_REPOSITORY_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\.[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s\"',;]+"),
    re.compile(r"(?i)(basic\s+)[^\s\"',;]+"),
    re.compile(r"(?i)(token[=:\s]+)[^\s\"',;]+"),
    re.compile(r"(?i)(password[=:\s]+)[^\s\"',;]+"),
    re.compile(r"(https?://[^/\s:@]+:)[^@\s]+(@)"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b"),
)

_EXECUTABLE_SUFFIXES = {
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
}
_MACH_O_MAGICS = {
    bytes.fromhex("feedfacf"),
    bytes.fromhex("feedface"),
    bytes.fromhex("cafebabe"),
}
_BYTECODE_CACHE_SUFFIXES = {".pyc", ".pyo"}


class PublicationError(ValueError):
    """Base fail-closed error for the component artifact publication pipeline."""

    def __init__(self, violations: Sequence[str], subject: str) -> None:
        self.violations: tuple[str, ...] = tuple(violations)
        joined = "\n".join(f"  - {item}" for item in self.violations)
        super().__init__(f"{subject} ({len(self.violations)} errors):\n{joined}")


class DependencyClosureError(PublicationError):
    """Raised when component selection or dependency closure validation fails."""

    def __init__(self, violations: Sequence[str]) -> None:
        super().__init__(
            violations, "component set / dependency closure validation failed"
        )


class PublicationVerificationError(PublicationError):
    """Raised when artifact publication or digest verification fails."""

    def __init__(self, violations: Sequence[str]) -> None:
        super().__init__(violations, "artifact publication verification failed")


class PublicationBlockedError(PublicationError):
    """Raised when external OCI/GHCR publication cannot proceed."""

    def __init__(self, violations: Sequence[str]) -> None:
        super().__init__(violations, "external OCI/GHCR publication blocked")


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Scrub credentials and tokens from error messages, logs, and reports."""
    redacted = str(text)
    env_secrets = [
        os.environ.get("GHCR_TOKEN", ""),
        os.environ.get("GITHUB_TOKEN", ""),
        os.environ.get("GHCR_PASSWORD", ""),
    ]
    for secret in (*secrets, *env_secrets):
        if isinstance(secret, str) and secret.strip():
            redacted = redacted.replace(secret, "<REDACTED>")
            redacted = redacted.replace(secret.strip(), "<REDACTED>")

    redacted = _SECRET_PATTERNS[0].sub(r"\1<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[1].sub(r"\1<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[2].sub(r"\1<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[3].sub(r"\1<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[4].sub(r"\1<REDACTED>\2", redacted)
    redacted = _SECRET_PATTERNS[5].sub("<REDACTED>", redacted)
    return redacted


def dependency_closure_violations(
    registry_document: Mapping[str, Any],
    *,
    repository_root: Path,
    selected_components: Iterable[str] | None = None,
    component_roots: Mapping[str, Path] | None = None,
) -> list[str]:
    """Return all component selection and dependency closure violations."""
    repository_root = Path(repository_root)
    roots_map = COMPONENT_ROOTS if component_roots is None else component_roots
    expected_set = set(roots_map)
    selected_set = (
        set(selected_components)
        if selected_components is not None
        else set(expected_set)
    )

    errors: list[str] = []
    if not isinstance(registry_document, Mapping):
        return ["registry: document must be a JSON object"]

    raw_components = registry_document.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        return ["registry.components: a non-empty list of components is required"]

    registered: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(raw_components):
        where = f"components[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{where}: entry must be an object")
            continue
        cid = entry.get("component_id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"{where}.component_id: component_id must be a string")
            continue
        if is_floating_selector(cid):
            errors.append(f"{where}.component_id: {cid!r} is a floating selector")
        if cid in registered:
            errors.append(f"{where}.component_id: duplicate component {cid!r}")
            continue
        registered[cid] = entry

    registered_ids = set(registered)
    if registered_ids != expected_set:
        missing = sorted(expected_set - registered_ids)
        extra = sorted(registered_ids - expected_set)
        errors.append(f"component set mismatch: missing={missing!r}, extra={extra!r}")

    if selected_set != expected_set:
        missing_sel = sorted(expected_set - selected_set)
        extra_sel = sorted(selected_set - expected_set)
        errors.append(
            f"component set mismatch: selected set differs from approved 9 "
            f"(missing={missing_sel!r}, extra={extra_sel!r})"
        )

    for cid in sorted(selected_set):
        if is_floating_selector(cid):
            errors.append(f"selected_components: {cid!r} is a floating selector")
        if cid not in registered:
            errors.append(
                f"selected_components: unknown component {cid!r} is not registered"
            )
        if cid not in roots_map:
            errors.append(
                f"selected_components: component {cid!r} has no declared source root"
            )
        else:
            source_dir = repository_root / roots_map[cid]
            if not source_dir.is_dir():
                errors.append(
                    f"selected_components: component source root is missing for "
                    f"{cid!r}: {source_dir}"
                )

    for cid in sorted(selected_set & registered_ids):
        entry = registered[cid]
        version = entry.get("component_version")
        if is_floating_selector(version):
            errors.append(
                f"{cid}.component_version: {version!r} is a floating selector"
            )
        elif not isinstance(version, str) or parse_semver(version) is None:
            errors.append(
                f"{cid}.component_version: {version!r} is not explicit SemVer"
            )

        contract_path = (
            repository_root
            / "components"
            / cid
            / "contract"
            / "component_contract.json"
        )
        contract_doc: Mapping[str, Any] | None = None
        if not contract_path.is_file():
            errors.append(
                f"{cid}.contract: published contract missing at "
                f"components/{cid}/contract/component_contract.json"
            )
        else:
            try:
                loaded = json.loads(contract_path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    contract_doc = loaded
                else:
                    errors.append(f"{cid}.contract: contract is not a JSON object")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"{cid}.contract: unreadable contract ({exc})")

        if contract_doc is not None:
            if contract_doc.get("component_id") != cid:
                errors.append(
                    f"{cid}.contract: contract component_id "
                    f"{contract_doc.get('component_id')!r} != {cid!r}"
                )
            if contract_doc.get("component_version") != version:
                errors.append(
                    f"{cid}.contract: contract component_version "
                    f"{contract_doc.get('component_version')!r} != {version!r}"
                )

        dependencies = entry.get("dependencies")
        if not isinstance(dependencies, list):
            errors.append(f"{cid}.dependencies: must be a list")
            continue

        seen_targets: set[str] = set()
        for dep_index, dep in enumerate(dependencies):
            dep_where = f"{cid}.dependencies[{dep_index}]"
            if not isinstance(dep, Mapping):
                errors.append(f"{dep_where}: dependency entry must be an object")
                continue

            target = dep.get("component_id")
            if not isinstance(target, str) or not target:
                errors.append(
                    f"{dep_where}.component_id: dependency target must be a string"
                )
                continue
            if is_floating_selector(target):
                errors.append(
                    f"{dep_where}.component_id: {target!r} is a floating selector"
                )
            if target == cid:
                errors.append(
                    f"{dep_where}.component_id: component {cid!r} cannot depend on itself"
                )
            if target in seen_targets:
                errors.append(
                    f"{dep_where}.component_id: duplicate dependency on {target!r}"
                )
            seen_targets.add(target)

            if target not in registered:
                errors.append(
                    f"{dep_where}.component_id: unknown dependency target {target!r} "
                    "is not registered"
                )
            elif target not in selected_set:
                errors.append(
                    f"{dep_where}.component_id: dependency closure violation: "
                    f"{cid!r} depends on {target!r}, which is missing from the "
                    "selected publication set"
                )

            version_range = dep.get("version_range")
            if is_floating_selector(version_range):
                errors.append(
                    f"{dep_where}.version_range: {version_range!r} is a floating selector"
                )
            clauses = parse_version_range(version_range)
            if clauses is None:
                errors.append(
                    f"{dep_where}.version_range: {version_range!r} is not a valid "
                    "explicit version range"
                )
            else:
                for _, bound in clauses:
                    if is_floating_selector(bound):
                        errors.append(
                            f"{dep_where}.version_range: bound {bound!r} is a "
                            "floating selector"
                        )
                    elif parse_semver(bound) is None:
                        errors.append(
                            f"{dep_where}.version_range: bound {bound!r} is not SemVer"
                        )
                if target in registered:
                    target_version = registered[target].get("component_version")
                    if not isinstance(target_version, str) or not range_admits(
                        version_range, target_version
                    ):
                        errors.append(
                            f"{dep_where}.version_range: incompatible dependency "
                            f"version: {cid!r} requires {target!r} matching "
                            f"{version_range!r}, but registered version is "
                            f"{target_version!r}"
                        )

    return sorted(set(errors))


def validate_dependency_closure(
    registry_document: Mapping[str, Any],
    *,
    repository_root: Path,
    selected_components: Iterable[str] | None = None,
    component_roots: Mapping[str, Path] | None = None,
) -> None:
    """Validate component set and dependency closure or raise fail-closed."""
    violations = dependency_closure_violations(
        registry_document,
        repository_root=repository_root,
        selected_components=selected_components,
        component_roots=component_roots,
    )
    if violations:
        raise DependencyClosureError(violations)


def extract_workflow_trigger_paths(workflow_text: str) -> dict[str, list[str]]:
    """Extract ``on.<event>.paths`` lists from the publication workflow YAML."""
    triggers: dict[str, list[str]] = {}
    in_on = False
    current_event: str | None = None
    in_paths = False

    for raw_line in workflow_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if indent == 0:
            in_on = stripped == "on:"
            current_event = None
            in_paths = False
            continue

        if not in_on:
            continue

        if indent == 2 and stripped.endswith(":"):
            current_event = stripped[:-1].strip()
            triggers.setdefault(current_event, [])
            in_paths = False
            continue

        if current_event is None:
            continue

        if indent == 4 and stripped.endswith(":"):
            in_paths = stripped == "paths:"
            continue

        if in_paths and indent >= 6 and stripped.startswith("- "):
            item = stripped[2:].strip()
            if len(item) >= 2 and item[0] == item[-1] and item[0] in {'"', "'"}:
                item = item[1:-1]
            triggers[current_event].append(item)

    return triggers


def _pattern_covers_component_root(pattern: str, root: Path) -> bool:
    """Return True when ``pattern`` is a recursive glob covering ``root``."""
    if not pattern.endswith("/**"):
        return False
    prefix = pattern[:-3]
    root_posix = root.as_posix()
    prefix_parts = PurePosixPath(prefix).parts
    root_parts = PurePosixPath(root_posix).parts
    if len(prefix_parts) != len(root_parts):
        return False
    return all(
        fnmatchcase(root_part, pat_part)
        for root_part, pat_part in zip(root_parts, prefix_parts, strict=True)
    )


def validate_workflow_trigger_coverage(
    workflow_path: Path,
    component_roots: Mapping[str, Path] = COMPONENT_ROOTS,
    *,
    events: Sequence[str] = _REQUIRED_TRIGGER_EVENTS,
) -> None:
    """Fail closed when any component source root is missing from workflow triggers."""
    workflow_path = Path(workflow_path)
    if not workflow_path.is_file():
        raise ValueError(f"publication workflow is missing: {workflow_path}")

    triggers = extract_workflow_trigger_paths(workflow_path.read_text(encoding="utf-8"))
    violations: list[str] = []

    for event in events:
        paths = triggers.get(event)
        if not paths:
            violations.append(f"on.{event}.paths is missing or empty")
            continue

        for component_id, root in sorted(component_roots.items()):
            if not any(_pattern_covers_component_root(p, root) for p in paths):
                violations.append(
                    f"on.{event}.paths does not cover component {component_id!r} "
                    f"source root {root.as_posix()!r}"
                )

        for pattern in paths:
            if not pattern.startswith("src/") or pattern in _SHARED_INFRA_SRC_PATTERNS:
                continue
            if not any(
                _pattern_covers_component_root(pattern, root)
                for root in component_roots.values()
            ):
                violations.append(
                    f"on.{event}.paths contains unmatched component pattern {pattern!r}"
                )

    if "pull_request" in events and "push" in events:
        pr_src = {
            p
            for p in triggers.get("pull_request", [])
            if p.startswith("src/") and p not in _SHARED_INFRA_SRC_PATTERNS
        }
        push_src = {
            p
            for p in triggers.get("push", [])
            if p.startswith("src/") and p not in _SHARED_INFRA_SRC_PATTERNS
        }
        if pr_src != push_src:
            violations.append(
                "component source-root path patterns differ between "
                f"pull_request ({sorted(pr_src)!r}) and push ({sorted(push_src)!r})"
            )

    if violations:
        joined = "; ".join(violations)
        raise ValueError(f"workflow trigger coverage violation: {joined}")


def verify_immutable_version_digest(
    component_id: str,
    component_version: str,
    digest: str,
    *,
    expected_version_digests: Mapping[tuple[str, str], str] = COMPONENT_VERSION_DIGESTS,
) -> None:
    """Fail closed if ``(component_id, component_version)`` points to unexpected content."""
    key = (component_id, component_version)
    expected = expected_version_digests.get(key)
    if expected is None:
        raise ValueError(
            f"immutable publication violation: {component_id}@{component_version} "
            "has no locked canonical digest in COMPONENT_VERSION_DIGESTS"
        )
    if digest != expected:
        raise ValueError(
            f"immutable publication violation: {component_id}@{component_version} "
            f"resolved to digest {digest!r}, expected locked digest {expected!r}; "
            "same (component_id, component_version) cannot point to different "
            "artifact content"
        )


def _check_existing_manifest_immutability(
    manifest_path: Path,
    component_id: str,
    component_version: str,
    digest: str,
) -> None:
    """Refuse to overwrite an existing publication-manifest entry with different content."""
    if not manifest_path.is_file():
        return
    try:
        existing_items = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"existing publication manifest is unreadable: {manifest_path}"
        ) from exc

    if not isinstance(existing_items, list):
        raise TypeError(
            f"existing publication manifest must be a JSON list: {manifest_path}"
        )

    for item in existing_items:
        if not isinstance(item, Mapping):
            continue
        if (
            item.get("component_id") == component_id
            and item.get("component_version") == component_version
        ):
            artifact = item.get("artifact")
            existing_digest = (
                artifact.get("digest") if isinstance(artifact, Mapping) else None
            )
            if existing_digest != digest:
                raise ValueError(
                    f"immutable publication violation: existing manifest binds "
                    f"{component_id}@{component_version} to {existing_digest!r}, "
                    f"refusing to reassign to {digest!r}"
                )


def _physical_executable(path: Path) -> bool:
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        return True
    prefix = path.read_bytes()[:4]
    if prefix.startswith((b"\x7fELF", b"MZ", b"#!")):
        return True
    if prefix in _MACH_O_MAGICS:
        return True
    return path.suffix in _EXECUTABLE_SUFFIXES


def _is_bytecode_cache(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix in _BYTECODE_CACHE_SUFFIXES


def stage_sealed_source_root(source_root: Path, staged_root: Path) -> Path:
    """Materialize a clean sealed component root without ``__pycache__`` / ``.pyc``."""
    source_root = Path(source_root)
    staged_root = Path(staged_root)
    if not source_root.is_dir():
        raise ValueError(f"component source root is missing: {source_root}")

    if staged_root.exists():
        shutil.rmtree(staged_root)
    staged_root.mkdir(parents=True, exist_ok=True)

    for directory, dirnames, filenames in os.walk(source_root, followlinks=False):
        directory_path = Path(directory)
        rel_dir = directory_path.relative_to(source_root)
        if _is_bytecode_cache(rel_dir):
            dirnames[:] = []
            continue

        dirnames[:] = sorted(
            name for name in dirnames if not _is_bytecode_cache(rel_dir / name)
        )

        for name in dirnames:
            src_dir = directory_path / name
            dst_dir = staged_root / rel_dir / name
            if src_dir.is_symlink():
                os.symlink(os.readlink(src_dir), dst_dir)
            else:
                dst_dir.mkdir(parents=True, exist_ok=True)

        for name in sorted(filenames):
            rel_file = rel_dir / name
            if _is_bytecode_cache(rel_file):
                continue
            src_file = directory_path / name
            dst_file = staged_root / rel_file
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if src_file.is_symlink():
                os.symlink(os.readlink(src_file), dst_file)
            else:
                dst_file.write_bytes(src_file.read_bytes())
                mode = 0o755 if _physical_executable(src_file) else 0o644
                dst_file.chmod(mode)

    return staged_root


def declaration_for(root: Path) -> dict[str, dict[str, bool]]:
    """Derive the complete Factory declaration for one sealed source root."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"component source root is missing: {root}")

    declaration: dict[str, dict[str, bool]] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            declaration[relative] = {"owned": True, "executable": False}
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            is_exec = False if path.is_symlink() else _physical_executable(path)
            declaration[relative] = {
                "owned": True,
                "executable": is_exec,
            }
    return declaration


def stage_sealed_container_image_root(
    source_root: Path,
    staged_root: Path,
    *,
    package_name: str,
) -> tuple[Path, dict[str, Any]]:
    """Materialize a sealed ``container_image/v1`` root and Factory declaration."""
    staged_root = Path(staged_root)
    if staged_root.exists():
        shutil.rmtree(staged_root)
    layer_0 = staged_root / "layers" / "0"
    stage_sealed_source_root(source_root, layer_0)
    layer_entries = declaration_for(layer_0)
    image_declaration: dict[str, Any] = {
        "layers": [
            {
                "ownership": "component",
                "entries": layer_entries,
            }
        ],
        "config": {
            "entrypoint": ["python", "-m", package_name],
            "cmd": [],
            "working_dir": package_name,
        },
    }
    return staged_root, image_declaration


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_digest(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def build_deterministic_tar_bytes(
    sealed_root: Path,
    declaration: Mapping[str, Mapping[str, bool]],
) -> tuple[bytes, str]:
    """Build a deterministic POSIX USTAR tar archive from ``sealed_root``."""
    sealed_root = Path(sealed_root)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for rel_path in sorted(declaration, key=lambda item: item.encode("utf-8")):
            full_path = sealed_root / rel_path
            info = tarfile.TarInfo(name=rel_path)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            is_exec = bool(declaration[rel_path]["executable"])

            if full_path.is_symlink():
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(full_path)
                info.mode = 0o777
                info.size = 0
                archive.addfile(info)
            elif full_path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.size = 0
                archive.addfile(info)
            else:
                payload = full_path.read_bytes()
                info.type = tarfile.REGTYPE
                info.mode = 0o755 if is_exec else 0o644
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

    tar_bytes = buffer.getvalue()
    return tar_bytes, _sha256_digest(tar_bytes)


def _write_oci_blob(blobs_dir: Path, digest: str, data: bytes) -> Path:
    hex_digest = digest.removeprefix(DIGEST_PREFIX)
    blob_path = blobs_dir / hex_digest
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(data)
    return blob_path


def build_oci_artifact_layout(
    *,
    component_id: str,
    component_version: str,
    descriptor: ArtifactDescriptor,
    canonical_bytes: bytes,
    package_tar_bytes: bytes,
    package_tar_digest: str,
    output_root: Path,
    oci_registry: str = DEFAULT_OCI_REGISTRY,
    oci_namespace: str = DEFAULT_OCI_NAMESPACE,
) -> dict[str, Any]:
    """Write a deterministic OCI Image Layout v1.0.0 for one component artifact."""
    layout_rel = f"oci/{component_id}"
    layout_root = Path(output_root) / "oci" / component_id
    blobs_dir = layout_root / "blobs" / "sha256"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    oci_artifact_type = (
        SOURCE_PACKAGE_OCI_ARTIFACT_TYPE
        if descriptor.artifact_type == "source_package"
        else CONTAINER_IMAGE_OCI_ARTIFACT_TYPE
    )
    layer_media_type = (
        SOURCE_PACKAGE_LAYER_MEDIA_TYPE
        if descriptor.artifact_type == "source_package"
        else CONTAINER_IMAGE_LAYER_MEDIA_TYPE
    )

    config_obj = {
        "component_id": component_id,
        "component_version": component_version,
        "artifact_type": descriptor.artifact_type,
        "canonical_form": descriptor.canonical_form,
        "artifact_digest": descriptor.digest,
        "canonical_manifest": descriptor.canonical_manifest,
        "layer_digests": list(descriptor.layer_digests),
    }
    config_bytes = _canonical_json_bytes(config_obj)
    config_digest = _sha256_digest(config_bytes)

    _write_oci_blob(blobs_dir, descriptor.digest, canonical_bytes)
    _write_oci_blob(blobs_dir, package_tar_digest, package_tar_bytes)
    _write_oci_blob(blobs_dir, config_digest, config_bytes)

    oci_manifest_obj = {
        "schemaVersion": 2,
        "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
        "artifactType": oci_artifact_type,
        "config": {
            "mediaType": OCI_CONFIG_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": CANONICAL_MANIFEST_MEDIA_TYPE,
                "digest": descriptor.digest,
                "size": len(canonical_bytes),
            },
            {
                "mediaType": layer_media_type,
                "digest": package_tar_digest,
                "size": len(package_tar_bytes),
            },
        ],
        "annotations": {
            "io.application-factory.artifact.canonical_form": descriptor.canonical_form,
            "io.application-factory.artifact.canonical_manifest": (
                descriptor.canonical_manifest
            ),
            "io.application-factory.artifact.digest": descriptor.digest,
            "io.application-factory.artifact.type": descriptor.artifact_type,
            "io.application-factory.component.id": component_id,
            "io.application-factory.component.version": component_version,
        },
    }
    oci_manifest_bytes = _canonical_json_bytes(oci_manifest_obj)
    oci_manifest_digest = _sha256_digest(oci_manifest_bytes)
    _write_oci_blob(blobs_dir, oci_manifest_digest, oci_manifest_bytes)

    oci_layout_bytes = _canonical_json_bytes({"imageLayoutVersion": OCI_LAYOUT_VERSION})
    (layout_root / "oci-layout").write_bytes(oci_layout_bytes)

    index_obj = {
        "schemaVersion": 2,
        "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
        "manifests": [
            {
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "artifactType": oci_artifact_type,
                "digest": oci_manifest_digest,
                "size": len(oci_manifest_bytes),
                "annotations": {
                    "io.application-factory.artifact.digest": descriptor.digest,
                    "io.application-factory.component.id": component_id,
                    "io.application-factory.component.version": component_version,
                },
            }
        ],
    }
    (layout_root / "index.json").write_bytes(_canonical_json_bytes(index_obj))

    repository = f"{oci_registry}/{oci_namespace}/{component_id}"
    return {
        "registry_repository": repository,
        "digest_reference": f"{repository}@{descriptor.digest}",
        "oci_manifest_digest": oci_manifest_digest,
        "oci_digest_reference": f"{repository}@{oci_manifest_digest}",
        "config_digest": config_digest,
        "package_layer_digest": package_tar_digest,
        "layout_path": layout_rel,
    }


def build_all(
    repository_root: Path,
    output_root: Path,
    *,
    artifact_type: str = "source_package",
    oci_registry: str = DEFAULT_OCI_REGISTRY,
    oci_namespace: str = DEFAULT_OCI_NAMESPACE,
    expected_version_digests: (
        Mapping[tuple[str, str], str] | None
    ) = COMPONENT_VERSION_DIGESTS,
) -> list[dict[str, Any]]:
    """Build canonical manifests and deterministic OCI layouts for all 9 components."""
    repository_root = Path(repository_root)
    output_root = Path(output_root)

    if artifact_type not in CANONICAL_FORMS:
        raise ValueError(
            f"unsupported artifact_type {artifact_type!r}; "
            f"expected one of {sorted(CANONICAL_FORMS)!r}"
        )

    registry = load_registry(root=repository_root)
    validate_dependency_closure(
        registry.document,
        repository_root=repository_root,
        selected_components=set(COMPONENT_ROOTS),
        component_roots=COMPONENT_ROOTS,
    )

    manifest_path = output_root / "publication-manifest.json"
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as staging_base:
        staging_root = Path(staging_base)
        for component_id in sorted(COMPONENT_ROOTS):
            source_root = repository_root / COMPONENT_ROOTS[component_id]
            package_name = COMPONENT_ROOTS[component_id].name

            if artifact_type == "source_package":
                sealed_root = stage_sealed_source_root(
                    source_root, staging_root / component_id
                )
                declaration: Mapping[str, Any] = declaration_for(sealed_root)
                descriptor = describe_source_package(sealed_root, declaration)
                canonical, canonical_bytes, recomputed = build_source_package_canonical(
                    sealed_root, declaration
                )
                tar_bytes, tar_digest = build_deterministic_tar_bytes(
                    sealed_root, declaration
                )
            else:
                sealed_root, declaration = stage_sealed_container_image_root(
                    source_root,
                    staging_root / component_id,
                    package_name=package_name,
                )
                descriptor = describe_container_image(sealed_root, declaration)
                canonical, canonical_bytes, recomputed, _ = (
                    build_container_image_canonical(sealed_root, declaration)
                )
                layer_0 = sealed_root / "layers" / "0"
                tar_bytes, tar_digest = build_deterministic_tar_bytes(
                    layer_0, declaration["layers"][0]["entries"]
                )

            if recomputed != descriptor.digest:
                raise ValueError(
                    f"{component_id}: descriptor digest differs from canonical digest"
                )

            verify_artifact(
                sealed_root,
                descriptor,
                declaration,
                canonical_manifest_bytes=canonical_bytes,
            )

            version = registry.version(component_id)
            if (
                artifact_type == "source_package"
                and expected_version_digests is not None
            ):
                verify_immutable_version_digest(
                    component_id,
                    version,
                    descriptor.digest,
                    expected_version_digests=expected_version_digests,
                )
            _check_existing_manifest_immutability(
                manifest_path,
                component_id,
                version,
                descriptor.digest,
            )

            metadata = registry_metadata(descriptor)
            canonical_file = output_root / descriptor.digest / "canonical.json"
            canonical_file.parent.mkdir(parents=True, exist_ok=True)
            canonical_file.write_bytes(canonical_bytes)

            oci_info = build_oci_artifact_layout(
                component_id=component_id,
                component_version=version,
                descriptor=descriptor,
                canonical_bytes=canonical_bytes,
                package_tar_bytes=tar_bytes,
                package_tar_digest=tar_digest,
                output_root=output_root,
                oci_registry=oci_registry,
                oci_namespace=oci_namespace,
            )

            results.append(
                {
                    "component_id": component_id,
                    "component_version": version,
                    "artifact": metadata,
                    "source_root": COMPONENT_ROOTS[component_id].as_posix(),
                    "canonical": canonical,
                    "oci": oci_info,
                }
            )

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(results, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return results


@dataclass(frozen=True)
class PublicationRecord:
    """Immutable record of a component artifact's OCI publication state."""

    component_id: str
    component_version: str
    registry_repository: str
    artifact: Mapping[str, Any]
    digest_reference: str
    oci_manifest_digest: str
    oci_digest_reference: str
    published: bool
    verified: bool
    authority_tag: str | None = None


class OCIRegistryTransport:
    """Abstract OCI Registry V2 transport seam (digest-addressed only)."""

    def push_blob(self, repository: str, digest: str, data: bytes) -> None:
        raise NotImplementedError

    def push_manifest(
        self, repository: str, digest: str, media_type: str, data: bytes
    ) -> None:
        raise NotImplementedError

    def fetch_blob(self, repository: str, digest: str) -> bytes:
        raise NotImplementedError

    def fetch_manifest(self, repository: str, digest: str) -> bytes:
        raise NotImplementedError


class InMemoryOCIRegistry(OCIRegistryTransport):
    """Deterministic in-memory OCI registry for local and adversarial testing."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.manifests: dict[tuple[str, str], bytes] = {}

    def push_blob(self, repository: str, digest: str, data: bytes) -> None:
        if _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError(f"blob reference {digest!r} is not an immutable digest")
        actual = _sha256_digest(data)
        if actual != digest:
            raise ValueError(
                f"blob digest mismatch on upload: declared {digest!r}, actual {actual!r}"
            )
        self.blobs[(repository, digest)] = bytes(data)

    def push_manifest(
        self, repository: str, digest: str, media_type: str, data: bytes
    ) -> None:
        if _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError(
                f"manifest reference {digest!r} is not an immutable digest; "
                "mutable tag push is forbidden as authority"
            )
        actual = _sha256_digest(data)
        if actual != digest:
            raise ValueError(
                f"manifest digest mismatch on upload: declared {digest!r}, "
                f"actual {actual!r}"
            )
        self.manifests[(repository, digest)] = bytes(data)

    def fetch_blob(self, repository: str, digest: str) -> bytes:
        key = (repository, digest)
        if key not in self.blobs:
            raise KeyError(f"blob not found in OCI registry: {repository}@{digest}")
        return self.blobs[key]

    def fetch_manifest(self, repository: str, digest: str) -> bytes:
        key = (repository, digest)
        if key not in self.manifests:
            raise KeyError(f"manifest not found in OCI registry: {repository}@{digest}")
        return self.manifests[key]


class _Unauthorized401(Exception):
    """Internal marker: the registry answered HTTP 401 with an auth challenge."""

    def __init__(self, challenge: str | None) -> None:
        super().__init__("unauthorized")
        self.challenge = challenge


class GHCRHTTPTransport(OCIRegistryTransport):
    """OCI Distribution Spec v1.1 HTTP transport for GHCR (digest-addressed).

    Authentication follows the documented OCI token-authentication protocol:
    the workflow token is presented as a bearer credential; if (and only if)
    the registry answers HTTP 401 with a ``WWW-Authenticate: Bearer realm=...``
    challenge, the transport performs exactly one token exchange against the
    challenge realm (Basic credentials, scope ``repository:<path>:pull,push``)
    and retries the original request once with the obtained token. Mutable tag
    references are never requested or written.
    """

    _REALM_RE = re.compile(r'realm="([^"]+)"')
    _SERVICE_RE = re.compile(r'service="([^"]+)"')

    def __init__(
        self,
        *,
        token: str,
        username: str = "github-actions",
        base_url: str = "https://ghcr.io",
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            msg = (
                "ghcr_credentials_unavailable: GHCR_TOKEN / GITHUB_TOKEN "
                "is required for external GHCR publication"
            )
            raise PublicationBlockedError([msg])
        self._token = token.strip()
        self._username = username
        self._base_url = base_url.rstrip("/")
        self._open = opener if opener is not None else urllib.request.urlopen

    def _repo_path(self, repository: str) -> str:
        prefix = "ghcr.io/"
        return (
            repository.removeprefix(prefix)
            if repository.startswith(prefix)
            else repository
        )

    def _send(
        self,
        method: str,
        url: str,
        *,
        token: str,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        req_headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "application-factory-artifact-publisher/1.0",
        }
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with self._open(req, timeout=30) as response:
                status = getattr(response, "status", 200)
                resp_headers = dict(getattr(response, "headers", {}))
                body = response.read()
                return status, resp_headers, body
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise _Unauthorized401(exc.headers.get("WWW-Authenticate")) from None
            safe_msg = redact_secrets(str(exc), [self._token, token])
            raise PublicationBlockedError(
                [f"ghcr_http_error: {method} {url} failed ({exc.code}): {safe_msg}"]
            ) from exc
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            safe_msg = redact_secrets(str(exc))
            raise PublicationBlockedError(
                [f"ghcr_unreachable: {method} {url} failed: {safe_msg}"]
            ) from exc

    def _exchange_token(self, challenge: str, repository: str) -> str:
        realm_match = self._REALM_RE.search(challenge)
        if realm_match is None:
            msg = (
                "ghcr_auth_error: HTTP 401 challenge carries no realm; "
                "cannot exchange a registry token"
            )
            raise PublicationBlockedError([msg])
        params: dict[str, str] = {"scope": f"repository:{repository}:pull,push"}
        service_match = self._SERVICE_RE.search(challenge)
        if service_match is not None:
            params["service"] = service_match.group(1)
        exchange_url = f"{realm_match.group(1)}?{urllib.parse.urlencode(params)}"
        credentials = base64.b64encode(
            f"{self._username}:{self._token}".encode()
        ).decode("ascii")
        req = urllib.request.Request(
            exchange_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "User-Agent": "application-factory-artifact-publisher/1.0",
            },
        )
        try:
            with self._open(req, timeout=30) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            safe_msg = redact_secrets(str(exc), [self._token, credentials])
            msg = f"ghcr_auth_error: token exchange for {repository} failed: {safe_msg}"
            raise PublicationBlockedError([msg]) from exc
        exchanged: str | None = None
        if isinstance(payload, Mapping):
            candidate = payload.get("token") or payload.get("access_token")
            if isinstance(candidate, str) and candidate:
                exchanged = candidate
        if exchanged is None:
            msg = (
                "ghcr_auth_error: token exchange response for "
                f"{repository} carries no usable token"
            )
            raise PublicationBlockedError([msg])
        return exchanged

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        scope_repository: str | None = None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        try:
            return self._send(
                method, url, token=self._token, data=data, headers=headers
            )
        except _Unauthorized401 as exc:
            challenge = exc.challenge
            if (
                scope_repository is None
                or not isinstance(challenge, str)
                or not challenge
            ):
                msg = f"ghcr_http_error: {method} {url} failed (401): unauthorized"
                raise PublicationBlockedError([msg]) from None
            exchanged = self._exchange_token(challenge, scope_repository)
            try:
                return self._send(
                    method, url, token=exchanged, data=data, headers=headers
                )
            except _Unauthorized401 as retry_exc:
                msg = (
                    "ghcr_auth_error: "
                    f"{method} {url} was rejected (401) even after token exchange"
                )
                raise PublicationBlockedError([msg]) from retry_exc

    def push_blob(self, repository: str, digest: str, data: bytes) -> None:
        repo_path = self._repo_path(repository)
        start_url = f"{self._base_url}/v2/{repo_path}/blobs/uploads/"
        _, headers, _ = self._request(
            "POST", start_url, data=b"", scope_repository=repo_path
        )
        location = headers.get("Location") or headers.get("location")
        if not location:
            raise PublicationBlockedError(
                [f"ghcr_protocol_error: missing upload Location header for {repo_path}"]
            )
        if location.startswith("/"):
            location = f"{self._base_url}{location}"
        sep = "&" if "?" in location else "?"
        put_url = f"{location}{sep}digest={digest}"
        self._request(
            "PUT",
            put_url,
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            scope_repository=repo_path,
        )

    def push_manifest(
        self, repository: str, digest: str, media_type: str, data: bytes
    ) -> None:
        repo_path = self._repo_path(repository)
        url = f"{self._base_url}/v2/{repo_path}/manifests/{digest}"
        self._request(
            "PUT",
            url,
            data=data,
            headers={"Content-Type": media_type},
            scope_repository=repo_path,
        )

    def fetch_blob(self, repository: str, digest: str) -> bytes:
        repo_path = self._repo_path(repository)
        url = f"{self._base_url}/v2/{repo_path}/blobs/{digest}"
        _, _, body = self._request("GET", url, scope_repository=repo_path)
        return body

    def fetch_manifest(self, repository: str, digest: str) -> bytes:
        repo_path = self._repo_path(repository)
        url = f"{self._base_url}/v2/{repo_path}/manifests/{digest}"
        _, _, body = self._request(
            "GET",
            url,
            headers={"Accept": OCI_IMAGE_MANIFEST_MEDIA_TYPE},
            scope_repository=repo_path,
        )
        return body


def publish_artifacts_to_oci(
    build_results: Sequence[Mapping[str, Any]],
    *,
    artifacts_root: Path,
    transport: OCIRegistryTransport | None = None,
) -> list[PublicationRecord]:
    """Publish built component OCI artifacts by immutable digest and verify them."""
    artifacts_root = Path(artifacts_root)
    if transport is None:
        token = os.environ.get("GHCR_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        username = (
            os.environ.get("GHCR_USER")
            or os.environ.get("GITHUB_ACTOR")
            or "github-actions"
        )
        transport = GHCRHTTPTransport(token=token, username=username)

    if not build_results:
        raise PublicationVerificationError(
            ["publication: build_results is empty; all 9 components are required"]
        )

    records: list[PublicationRecord] = []
    for item in build_results:
        component_id = str(item["component_id"])
        component_version = str(item["component_version"])
        artifact = dict(item["artifact"])
        oci = item["oci"]
        repository = str(oci["registry_repository"])
        oci_manifest_digest = str(oci["oci_manifest_digest"])
        config_digest = str(oci["config_digest"])
        package_layer_digest = str(oci["package_layer_digest"])
        artifact_digest = str(artifact["digest"])

        layout_root = artifacts_root / oci["layout_path"]
        blobs_dir = layout_root / "blobs" / "sha256"

        canonical_blob = (
            blobs_dir / artifact_digest.removeprefix(DIGEST_PREFIX)
        ).read_bytes()
        package_blob = (
            blobs_dir / package_layer_digest.removeprefix(DIGEST_PREFIX)
        ).read_bytes()
        config_blob = (
            blobs_dir / config_digest.removeprefix(DIGEST_PREFIX)
        ).read_bytes()
        manifest_blob = (
            blobs_dir / oci_manifest_digest.removeprefix(DIGEST_PREFIX)
        ).read_bytes()

        try:
            transport.push_blob(repository, config_digest, config_blob)
            transport.push_blob(repository, artifact_digest, canonical_blob)
            transport.push_blob(repository, package_layer_digest, package_blob)
            transport.push_manifest(
                repository,
                oci_manifest_digest,
                OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                manifest_blob,
            )

            remote_manifest_bytes = transport.fetch_manifest(
                repository, oci_manifest_digest
            )
            remote_manifest_digest = _sha256_digest(remote_manifest_bytes)
            if remote_manifest_digest != oci_manifest_digest:
                msg = (
                    f"{component_id}: remote OCI manifest digest "
                    f"{remote_manifest_digest!r} != expected {oci_manifest_digest!r}"
                )
                raise PublicationVerificationError([msg])

            remote_canonical_bytes = transport.fetch_blob(repository, artifact_digest)
            remote_canonical_digest = _sha256_digest(remote_canonical_bytes)
            if remote_canonical_digest != artifact_digest:
                msg = (
                    f"{component_id}: remote canonical manifest digest "
                    f"{remote_canonical_digest!r} != expected {artifact_digest!r}"
                )
                raise PublicationVerificationError([msg])
        except (PublicationBlockedError, PublicationVerificationError):
            raise
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            safe_msg = redact_secrets(str(exc))
            raise PublicationVerificationError(
                [
                    f"{component_id}: OCI publication or digest verification failed: {safe_msg}"
                ]
            ) from exc

        records.append(
            PublicationRecord(
                component_id=component_id,
                component_version=component_version,
                registry_repository=repository,
                artifact=artifact,
                digest_reference=f"{repository}@{artifact_digest}",
                oci_manifest_digest=oci_manifest_digest,
                oci_digest_reference=f"{repository}@{oci_manifest_digest}",
                published=True,
                verified=True,
                authority_tag=None,
            )
        )

    return records


def _component_has_migrations(repository_root: Path, component_id: str) -> bool:
    """Return True only when a component declares and ships real migrations."""
    migrations_dir = repository_root / "components" / component_id / "migrations"
    contract_path = (
        repository_root
        / "components"
        / component_id
        / "contract"
        / "component_contract.json"
    )
    if not contract_path.is_file():
        return False
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(contract, Mapping):
        return False
    migrations_field = contract.get("migrations")
    return bool(migrations_field) and migrations_dir.is_dir()


def _validate_digest_reference(
    reference: object,
    *,
    expected_repository: str,
    expected_digest: str | None,
    where: str,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(reference, str) or not reference:
        return [f"{where}: digest-pinned OCI reference is required"]

    if is_floating_selector(reference):
        errors.append(f"{where}: {reference!r} is a floating selector")

    if "@" not in reference:
        errors.append(
            f"{where}: {reference!r} is not pinned by '@sha256:<64 lowercase hex>'; "
            "mutable tag references are forbidden as authority"
        )
        return errors

    repo_part, digest_part = reference.rsplit("@", 1)
    if ":" in repo_part:
        errors.append(
            f"{where}: {reference!r} embeds a tag in the repository path; "
            "mutable tags are forbidden as authority"
        )
    if is_floating_selector(digest_part):
        errors.append(f"{where}: {digest_part!r} is a floating selector")
    if _DIGEST_RE.fullmatch(digest_part) is None:
        errors.append(
            f"{where}: {digest_part!r} is not an immutable 'sha256:<64 lowercase hex>' digest"
        )
    if _DIGEST_REFERENCE_RE.fullmatch(reference) is None:
        errors.append(
            f"{where}: {reference!r} is not a valid immutable OCI digest reference"
        )
    if expected_repository and repo_part != expected_repository:
        errors.append(
            f"{where}: repository {repo_part!r} does not match expected {expected_repository!r}"
        )
    if (
        expected_digest is not None
        and _DIGEST_RE.fullmatch(expected_digest) is not None
        and digest_part != expected_digest
    ):
        errors.append(
            f"{where}: reference digest {digest_part!r} does not match "
            f"expected digest {expected_digest!r}"
        )
    return errors


def publication_violations(
    registry_document: Mapping[str, Any],
    publication_records: Sequence[PublicationRecord | Mapping[str, Any]],
    *,
    repository_root: Path,
    artifacts_root: Path,
    component_roots: Mapping[str, Path] | None = None,
) -> list[str]:
    """Return all violations preventing registry reconciliation."""
    repository_root = Path(repository_root)
    artifacts_root = Path(artifacts_root)
    roots_map = COMPONENT_ROOTS if component_roots is None else component_roots
    expected_ids = set(roots_map)

    errors = dependency_closure_violations(
        registry_document,
        repository_root=repository_root,
        selected_components=expected_ids,
        component_roots=roots_map,
    )

    if not isinstance(publication_records, Sequence) or isinstance(
        publication_records, (str, bytes)
    ):
        errors.append(
            "publication_records: a sequence of publication records is required"
        )
        return sorted(set(errors))

    if not publication_records:
        errors.append(
            "publication_records: missing publication records for all 9 components"
        )
        return sorted(set(errors))

    registered_versions: dict[str, str] = {}
    if isinstance(registry_document, Mapping) and isinstance(
        registry_document.get("components"), list
    ):
        for entry in registry_document["components"]:
            if isinstance(entry, Mapping) and isinstance(
                entry.get("component_id"), str
            ):
                ver = entry.get("component_version")
                if isinstance(ver, str):
                    registered_versions[entry["component_id"]] = ver

    by_component: dict[str, Mapping[str, Any]] = {}
    for index, raw_record in enumerate(publication_records):
        where = f"publication_records[{index}]"
        if isinstance(raw_record, PublicationRecord):
            record: Mapping[str, Any] = {
                "component_id": raw_record.component_id,
                "component_version": raw_record.component_version,
                "registry_repository": raw_record.registry_repository,
                "artifact": raw_record.artifact,
                "digest_reference": raw_record.digest_reference,
                "oci_manifest_digest": raw_record.oci_manifest_digest,
                "oci_digest_reference": raw_record.oci_digest_reference,
                "published": raw_record.published,
                "verified": raw_record.verified,
                "authority_tag": raw_record.authority_tag,
            }
        elif isinstance(raw_record, Mapping):
            record = raw_record
        else:
            errors.append(
                f"{where}: publication record must be a PublicationRecord or mapping"
            )
            continue

        cid = record.get("component_id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"{where}.component_id: component_id is required")
            continue
        if is_floating_selector(cid):
            errors.append(f"{where}.component_id: {cid!r} is a floating selector")
        if cid not in expected_ids:
            errors.append(f"{where}.component_id: unexpected/unknown component {cid!r}")
            continue
        if cid in by_component:
            errors.append(
                f"{where}.component_id: duplicate publication record for {cid!r}"
            )
            continue
        by_component[cid] = record

    recorded_ids = set(by_component)
    missing_ids = sorted(expected_ids - recorded_ids)
    if missing_ids:
        errors.append(
            f"publication_records: partial or missing publication; missing "
            f"components: {missing_ids!r}"
        )

    with tempfile.TemporaryDirectory() as staging_base:
        staging_root = Path(staging_base)
        for cid in sorted(recorded_ids):
            record = by_component[cid]
            where = f"publication[{cid}]"

            if record.get("published") is not True:
                errors.append(
                    f"{where}.published: artifact for {cid!r} has not been published "
                    f"(published={record.get('published')!r})"
                )
            if record.get("verified") is not True:
                errors.append(
                    f"{where}.verified: artifact for {cid!r} has not been digest-verified "
                    f"(verified={record.get('verified')!r})"
                )

            authority_tag = record.get("authority_tag")
            if authority_tag is not None:
                errors.append(
                    f"{where}.authority_tag: mutable tag {authority_tag!r} cannot be "
                    "used as publication authority"
                )

            rec_version = record.get("component_version")
            if is_floating_selector(rec_version):
                errors.append(
                    f"{where}.component_version: {rec_version!r} is a floating selector"
                )
            elif cid in registered_versions and rec_version != registered_versions[cid]:
                errors.append(
                    f"{where}.component_version: record declares {rec_version!r}, "
                    f"registry declares {registered_versions[cid]!r}"
                )

            repository = record.get("registry_repository")
            if not isinstance(repository, str) or not repository:
                errors.append(
                    f"{where}.registry_repository: OCI registry repository is required"
                )
                repository_str = ""
            else:
                repository_str = repository
                if is_floating_selector(repository_str):
                    errors.append(
                        f"{where}.registry_repository: {repository_str!r} is a floating selector"
                    )
                if ":" in repository_str or "@" in repository_str:
                    errors.append(
                        f"{where}.registry_repository: {repository_str!r} must not "
                        "contain a tag or digest suffix"
                    )
                elif _REPOSITORY_RE.fullmatch(repository_str) is None:
                    errors.append(
                        f"{where}.registry_repository: {repository_str!r} is not a "
                        "valid lowercase OCI repository path"
                    )
                elif not repository_str.endswith(f"/{cid}"):
                    errors.append(
                        f"{where}.registry_repository: {repository_str!r} does not "
                        f"target component {cid!r}"
                    )

            artifact_block = record.get("artifact")
            meta_errors = registry_metadata_violations(artifact_block)
            for item in meta_errors:
                errors.append(f"{where}.{item}")

            declared_digest = (
                artifact_block.get("digest")
                if isinstance(artifact_block, Mapping)
                else None
            )
            declared_digest_str = (
                declared_digest if isinstance(declared_digest, str) else None
            )

            errors.extend(
                _validate_digest_reference(
                    record.get("digest_reference"),
                    expected_repository=repository_str,
                    expected_digest=declared_digest_str,
                    where=f"{where}.digest_reference",
                )
            )

            oci_manifest_digest = record.get("oci_manifest_digest")
            if not isinstance(oci_manifest_digest, str) or (
                _DIGEST_RE.fullmatch(oci_manifest_digest) is None
            ):
                errors.append(
                    f"{where}.oci_manifest_digest: {oci_manifest_digest!r} is not an "
                    "immutable 'sha256:<64 lowercase hex>' digest"
                )
                oci_manifest_digest_str = None
            else:
                oci_manifest_digest_str = oci_manifest_digest

            errors.extend(
                _validate_digest_reference(
                    record.get("oci_digest_reference"),
                    expected_repository=repository_str,
                    expected_digest=oci_manifest_digest_str,
                    where=f"{where}.oci_digest_reference",
                )
            )

            if meta_errors:
                continue

            try:
                descriptor = descriptor_from_registry_metadata(artifact_block)
            except ArtifactError as exc:
                errors.append(f"{where}.artifact: {exc}")
                continue

            canonical_path = artifacts_root / descriptor.digest / "canonical.json"
            if not canonical_path.is_file():
                errors.append(
                    f"{where}.canonical_manifest: stored canonical manifest is "
                    f"missing at {canonical_path}"
                )
                canonical_bytes: bytes | None = None
            else:
                canonical_bytes = canonical_path.read_bytes()

            source_root = repository_root / roots_map[cid]
            if not source_root.is_dir():
                errors.append(
                    f"{where}.source_root: missing component source directory {source_root}"
                )
                continue

            if descriptor.artifact_type == "source_package":
                sealed_root = stage_sealed_source_root(source_root, staging_root / cid)
                declaration: Mapping[str, Any] = declaration_for(sealed_root)
            else:
                sealed_root, declaration = stage_sealed_container_image_root(
                    source_root,
                    staging_root / cid,
                    package_name=roots_map[cid].name,
                )
                try:
                    _, _, _, layer_digests = build_container_image_canonical(
                        sealed_root, declaration
                    )
                    descriptor = ArtifactDescriptor(
                        artifact_type=descriptor.artifact_type,
                        canonical_form=descriptor.canonical_form,
                        digest=descriptor.digest,
                        canonical_manifest=descriptor.canonical_manifest,
                        layer_digests=tuple(layer_digests),
                    )
                except (ArtifactError, ValueError, OSError) as exc:
                    errors.append(f"{where}.artifact: {exc}")
                    continue

            phys_violations = artifact_violations(
                sealed_root,
                descriptor,
                declaration,
                canonical_manifest_bytes=canonical_bytes,
            )
            for violation in phys_violations:
                errors.append(f"{where}.{violation}")

            if oci_manifest_digest_str is not None:
                oci_blobs_dir = artifacts_root / "oci" / cid / "blobs" / "sha256"
                oci_manifest_file = (
                    oci_blobs_dir / oci_manifest_digest_str.removeprefix(DIGEST_PREFIX)
                )
                if not oci_manifest_file.is_file():
                    errors.append(
                        f"{where}.oci_manifest_digest: OCI manifest blob missing at "
                        f"{oci_manifest_file}"
                    )
                else:
                    raw_oci_manifest = oci_manifest_file.read_bytes()
                    actual_oci_digest = _sha256_digest(raw_oci_manifest)
                    if actual_oci_digest != oci_manifest_digest_str:
                        errors.append(
                            f"{where}.oci_manifest_digest: stored OCI manifest hashes "
                            f"to {actual_oci_digest!r}, expected "
                            f"{oci_manifest_digest_str!r}"
                        )
                    else:
                        try:
                            oci_doc = json.loads(raw_oci_manifest.decode("utf-8"))
                            annotations = oci_doc.get("annotations", {})
                            if (
                                annotations.get("io.application-factory.component.id")
                                != cid
                            ):
                                errors.append(
                                    f"{where}.oci_manifest: annotation component.id "
                                    "does not match component_id"
                                )
                            if (
                                annotations.get(
                                    "io.application-factory.artifact.digest"
                                )
                                != descriptor.digest
                            ):
                                errors.append(
                                    f"{where}.oci_manifest: annotation artifact.digest "
                                    "does not match descriptor.digest"
                                )
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            errors.append(f"{where}.oci_manifest: invalid JSON ({exc})")

    return sorted(set(errors))


def reconcile_registry(
    registry_document: Mapping[str, Any],
    publication_records: Sequence[PublicationRecord | Mapping[str, Any]],
    *,
    repository_root: Path,
    artifacts_root: Path,
    component_roots: Mapping[str, Path] | None = None,
    allow_fabricated_migrations: bool = False,
) -> dict[str, Any]:
    """Reconcile the Component Registry after verified publication of all 9 artifacts."""
    repository_root = Path(repository_root)
    artifacts_root = Path(artifacts_root)
    roots_map = COMPONENT_ROOTS if component_roots is None else component_roots

    if allow_fabricated_migrations:
        msg = (
            "lifecycle.publishability_blockers: fabricating migration contracts "
            "or suppressing 'migrations_absent' is forbidden (Issue #127 Decision #5)"
        )
        raise PublicationVerificationError([msg])

    violations = publication_violations(
        registry_document,
        publication_records,
        repository_root=repository_root,
        artifacts_root=artifacts_root,
        component_roots=roots_map,
    )
    if violations:
        raise PublicationVerificationError(violations)

    by_component: dict[str, Mapping[str, Any]] = {}
    for item in publication_records:
        if isinstance(item, PublicationRecord):
            by_component[item.component_id] = item.artifact
        elif isinstance(item, Mapping):
            by_component[str(item["component_id"])] = dict(item["artifact"])

    reconciled: dict[str, Any] = copy.deepcopy(dict(registry_document))
    lifecycle_violations: list[str] = []

    for entry in reconciled.get("components", []):
        if not isinstance(entry, Mapping):
            continue
        cid = str(entry["component_id"])
        descriptor = descriptor_from_registry_metadata(by_component[cid])
        entry["artifact"] = registry_metadata(descriptor)

        lifecycle = entry.get("lifecycle")
        if not isinstance(lifecycle, dict):
            lifecycle_violations.append(f"{cid}.lifecycle: lifecycle block is missing")
            continue

        existing_blockers = list(lifecycle.get("publishability_blockers") or [])
        remaining_blockers = [
            blocker
            for blocker in existing_blockers
            if blocker != "deployment_artifact_absent"
        ]

        has_migrations = _component_has_migrations(repository_root, cid)
        if not has_migrations and "migrations_absent" not in remaining_blockers:
            lifecycle_violations.append(
                f"{cid}.lifecycle.publishability_blockers: 'migrations_absent' "
                "cannot be removed when no component migration contract exists"
            )
            continue

        lifecycle["deployable"] = True
        if remaining_blockers:
            lifecycle["publishable"] = False
            lifecycle["publishability_blockers"] = remaining_blockers
        else:
            lifecycle["publishable"] = True
            lifecycle.pop("publishability_blockers", None)

    if lifecycle_violations:
        raise PublicationVerificationError(lifecycle_violations)

    doc_errors = validate_document(reconciled, root=repository_root)
    if doc_errors:
        raise PublicationVerificationError(doc_errors)

    return reconciled


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, verify, and optionally publish the 9 component artifacts."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("factory/artifacts"))
    parser.add_argument(
        "--artifact-type",
        choices=sorted(CANONICAL_FORMS),
        default="source_package",
    )
    parser.add_argument(
        "--publish-ghcr",
        action="store_true",
        help="Publish built OCI artifacts to GHCR and verify digests remotely.",
    )
    parser.add_argument(
        "--reconcile-registry",
        action="store_true",
        help="Reconcile factory/registry/component_registry.json after verified publication.",
    )
    parser.add_argument(
        "--write-records",
        type=Path,
        default=None,
        help=(
            "After publication, serialize the PublicationRecords as canonical "
            "JSON to this path (publication evidence; records carry no "
            "lifecycle claims and are re-verified before any reconciliation)."
        ),
    )
    args = parser.parse_args(argv)

    if args.write_records is not None and not (
        args.publish_ghcr or args.reconcile_registry
    ):
        parser.error("--write-records requires --publish-ghcr or --reconcile-registry")

    repository_root = args.root.resolve()
    output_root = (
        args.output if args.output.is_absolute() else repository_root / args.output
    )

    results = build_all(
        repository_root,
        output_root,
        artifact_type=args.artifact_type,
    )

    if args.publish_ghcr or args.reconcile_registry:
        records = publish_artifacts_to_oci(results, artifacts_root=output_root)
        if args.write_records is not None:
            records_path = (
                args.write_records
                if args.write_records.is_absolute()
                else repository_root / args.write_records
            )
            records_path.parent.mkdir(parents=True, exist_ok=True)
            records_path.write_text(
                json.dumps(
                    [asdict(record) for record in records],
                    sort_keys=True,
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        if args.reconcile_registry:
            registry_path = repository_root / REGISTRY_PATH
            registry_doc = load_registry_document(registry_path)
            reconciled = reconcile_registry(
                registry_doc,
                records,
                repository_root=repository_root,
                artifacts_root=output_root,
            )
            registry_path.write_text(
                json.dumps(reconciled, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
