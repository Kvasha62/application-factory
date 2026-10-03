"""Structural validation for control-plane documents (Slice 1).

Handwritten, dependency-free validators mirroring the JSON Schemas under
``factory/control_plane/schema/`` — the same style as the execution core's
validators. Every function returns a list of error strings; an empty list
means the document is valid.

Enforced invariants:
- selector-free, exact-version component references only;
- identifier and digest formats;
- explicit attachment points present and null (Proposal/Approval/Build/
  Release are future domains — attachment slots only, no subsystem);
- no parallel Registry/Catalog authority: configuration documents carry
  references, never component facts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

#: Project/component identifier shape (same rule as the composer's
#: component id pattern; see ``src/composer/request.py``).
ID_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"

#: Manifest identifier shape (same rule as the composer/manifest law).
MANIFEST_ID_PATTERN = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"

#: Declared digest shape for control-plane documents.
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

#: An exact, selector-free semantic version: MAJOR.MINOR.PATCH with an
#: optional prerelease/build suffix and no range operators anywhere.
EXACT_VERSION_PATTERN = r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$"
_RANGE_CHARACTERS = frozenset("^~<>=*,")

_ID_RE = re.compile(ID_PATTERN)
_MANIFEST_ID_RE = re.compile(MANIFEST_ID_PATTERN)
_DIGEST_RE = re.compile(DIGEST_PATTERN)
_EXACT_VERSION_RE = re.compile(EXACT_VERSION_PATTERN)

_ENTRY_KINDS = frozenset({"capability", "constraint", "input"})
_SOURCE_CHANNELS = frozenset({"form", "nl", "import", "api"})
_ENTRY_STATUSES = frozenset({"open", "resolved"})
_PROJECT_STATUSES = frozenset({"active", "archived"})
_ATTACHMENT_KEYS = ("proposal", "approval", "build", "release")
_SCALAR_TYPES = (str, int, float, bool, type(None))


class ControlPlaneError(ValueError):
    """A control-plane document or reference is invalid (fail closed)."""


def is_exact_version(value: object) -> bool:
    """True when ``value`` is an exact version, never a range selector."""
    if not isinstance(value, str):
        return False
    if any(character in value for character in _RANGE_CHARACTERS):
        return False
    if value != value.strip() or not value:
        return False
    return _EXACT_VERSION_RE.fullmatch(value) is not None


def _is_scalar(value: object) -> bool:
    return isinstance(value, _SCALAR_TYPES)


def _check_keys(
    document: Mapping[str, Any], required: Sequence[str], where: str, errors: list[str]
) -> None:
    keys = set(document)
    for key in required:
        if key not in keys:
            errors.append(f"{where}: missing required field {key!r}")
    for key in sorted(keys - set(required)):
        errors.append(f"{where}: unknown field {key!r}")


def _check_string(
    document: Mapping[str, Any], field: str, where: str, errors: list[str]
) -> None:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        errors.append(f"{where}: {field} must be a non-empty string")


def _check_id(value: object, field: str, where: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        errors.append(f"{where}: {field} must match {ID_PATTERN}")


def _check_digest(value: object, field: str, where: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        errors.append(f"{where}: {field} must be a sha256: digest")


def _check_digest_ref(
    value: object, id_field: str, where: str, errors: list[str]
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{where}: must be an object with {id_field} and digest")
        return
    _check_keys(value, (id_field, "digest"), where, errors)
    if id_field in value:
        _check_string(value, id_field, where, errors)
    if "digest" in value:
        _check_digest(value["digest"], "digest", where, errors)


def _check_predecessor(
    value: object, id_field: str, where: str, errors: list[str]
) -> None:
    if value is None:
        return
    _check_digest_ref(value, id_field, where, errors)


def _check_versioned_id(
    value: object, suffix: str, where: str, errors: list[str]
) -> None:
    """A versioned id looks like ``<project_id>/<suffix>/<seq>``."""
    if not isinstance(value, str):
        errors.append(f"{where} must be a string")
        return
    parts = value.split("/")
    if len(parts) != 3 or parts[1] != suffix:
        errors.append(f"{where} must be <project_id>/{suffix}/<seq>")
        return
    _check_id(parts[0], "project id", where, errors)
    if not parts[2].isdigit() or parts[2] == "0":
        errors.append(f"{where}: sequence must be a positive integer")


def project_errors(document: object) -> list[str]:
    """Validate a Project document."""
    if not isinstance(document, dict):
        return ["project: must be an object"]
    errors: list[str] = []
    _check_keys(
        document,
        (
            "schema_version",
            "project_id",
            "title",
            "status",
            "created_at",
            "pointers",
        ),
        "project",
        errors,
    )
    _check_string(document, "schema_version", "project", errors)
    if "project_id" in document:
        _check_id(document["project_id"], "project_id", "project", errors)
    _check_string(document, "title", "project", errors)
    if document.get("status") not in _PROJECT_STATUSES:
        errors.append("project: status must be 'active' or 'archived'")
    created = document.get("created_at")
    if not isinstance(created, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", created):
        errors.append("project: created_at must be YYYY-MM-DD")
    pointers = document.get("pointers")
    if not isinstance(pointers, dict):
        errors.append("project: pointers must be an object")
    else:
        _check_keys(pointers, ("requirements", "configuration"), "pointers", errors)
        for key in ("requirements", "configuration"):
            if key not in pointers:
                continue
            value = pointers[key]
            if value is None:
                continue
            if key == "requirements":
                _check_digest_ref(
                    value, "requirements_id", "pointers.requirements", errors
                )
            else:
                _check_digest_ref(
                    value, "configuration_id", "pointers.configuration", errors
                )
    return errors


def requirements_errors(document: object) -> list[str]:
    """Validate an immutable RequirementsVersion document."""
    if not isinstance(document, dict):
        return ["requirements: must be an object"]
    errors: list[str] = []
    _check_keys(
        document,
        (
            "schema_version",
            "requirements_id",
            "project_ref",
            "entries",
            "predecessor",
            "digest",
        ),
        "requirements",
        errors,
    )
    _check_string(document, "schema_version", "requirements", errors)
    if "requirements_id" in document:
        _check_versioned_id(
            document["requirements_id"], "requirements", "requirements_id", errors
        )
    if "project_ref" in document:
        _check_id(document["project_ref"], "project_ref", "requirements", errors)
    if (
        isinstance(document.get("requirements_id"), str)
        and isinstance(document.get("project_ref"), str)
        and document["requirements_id"].split("/")[0] != document["project_ref"]
    ):
        errors.append("requirements: requirements_id must live under project_ref")
    _check_predecessor(
        document.get("predecessor"), "requirements_id", "predecessor", errors
    )
    if "digest" in document:
        _check_digest(document["digest"], "digest", "requirements", errors)
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("requirements: entries must be a non-empty list")
        return errors
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            continue
        _check_keys(
            entry,
            ("req_id", "kind", "statement", "source_channels", "refs", "status"),
            where,
            errors,
        )
        req_id = entry.get("req_id")
        if not isinstance(req_id, str) or not req_id:
            errors.append(f"{where}: req_id must be a non-empty string")
        elif req_id in seen:
            errors.append(f"{where}: req_id {req_id!r} is duplicated")
        else:
            seen.add(req_id)
        if entry.get("kind") not in _ENTRY_KINDS:
            errors.append(f"{where}: kind must be one of {sorted(_ENTRY_KINDS)}")
        statement = entry.get("statement")
        if not isinstance(statement, dict):
            errors.append(f"{where}: statement must be an object")
        else:
            if "summary" not in statement:
                errors.append(f"{where}.statement: missing required field 'summary'")
            for key in sorted(set(statement) - {"summary", "constraints"}):
                errors.append(f"{where}.statement: unknown field {key!r}")
            summary = statement.get("summary")
            if not isinstance(summary, str) or not summary:
                errors.append(f"{where}: statement.summary must be a non-empty string")
            if "constraints" in statement:
                errors.extend(
                    scalar_constraints(statement["constraints"], f"{where}.statement")
                )
        channels = entry.get("source_channels")
        if not isinstance(channels, list) or not channels:
            errors.append(f"{where}: source_channels must be a non-empty list")
        else:
            for channel in channels:
                if channel not in _SOURCE_CHANNELS:
                    errors.append(
                        f"{where}: source channel {channel!r} must be one of "
                        f"{sorted(_SOURCE_CHANNELS)}"
                    )
        refs = entry.get("refs")
        if not isinstance(refs, list):
            errors.append(f"{where}: refs must be a list")
        else:
            for ref in refs:
                if not isinstance(ref, str) or not ref:
                    errors.append(f"{where}: refs must contain non-empty strings")
        if entry.get("status") not in _ENTRY_STATUSES:
            errors.append(f"{where}: status must be one of {sorted(_ENTRY_STATUSES)}")
    return errors


def configuration_index_errors(document: object) -> list[str]:
    """Validate the mutable Configuration aggregate (current pointer)."""
    if not isinstance(document, dict):
        return ["configuration index: must be an object"]
    errors: list[str] = []
    _check_keys(
        document,
        ("schema_version", "configuration_id", "project_ref", "current"),
        "configuration index",
        errors,
    )
    _check_string(document, "schema_version", "configuration index", errors)
    if "configuration_id" in document:
        expected_id = (
            f"{document['project_ref']}/configurations"
            if isinstance(document.get("project_ref"), str)
            else None
        )
        if document["configuration_id"] != expected_id:
            errors.append(
                "configuration index: configuration_id must be "
                "<project_ref>/configurations"
            )
    if "project_ref" in document:
        _check_id(document["project_ref"], "project_ref", "configuration index", errors)
    current = document.get("current")
    if not isinstance(current, dict):
        errors.append("configuration index: current must be an object")
    else:
        _check_digest_ref(
            current, "configuration_id", "configuration index current", errors
        )
    return errors


def configuration_version_errors(document: object) -> list[str]:
    """Validate an immutable ConfigurationVersion document."""
    if not isinstance(document, dict):
        return ["configuration version: must be an object"]
    errors: list[str] = []
    _check_keys(
        document,
        (
            "schema_version",
            "configuration_id",
            "project_ref",
            "requirements_ref",
            "template_ref",
            "variant_ref",
            "manifest",
            "components",
            "configuration",
            "golden_bundle",
            "extensions",
            "branding",
            "attachments",
            "predecessor",
            "digest",
        ),
        "configuration version",
        errors,
    )
    _check_string(document, "schema_version", "configuration version", errors)
    if "configuration_id" in document:
        _check_versioned_id(
            document["configuration_id"],
            "configurations",
            "configuration_id",
            errors,
        )
    if "project_ref" in document:
        _check_id(
            document["project_ref"], "project_ref", "configuration version", errors
        )
    if (
        isinstance(document.get("configuration_id"), str)
        and isinstance(document.get("project_ref"), str)
        and document["configuration_id"].split("/")[0] != document["project_ref"]
    ):
        errors.append(
            "configuration version: configuration_id must live under project_ref"
        )
    if "requirements_ref" in document:
        _check_digest_ref(
            document["requirements_ref"],
            "requirements_id",
            "configuration version requirements_ref",
            errors,
        )
    for field in ("template_ref", "variant_ref"):
        value = document.get(field)
        if value is not None and (
            not isinstance(value, str) or _ID_RE.fullmatch(value) is None
        ):
            errors.append(
                f"configuration version: {field} must be null or match {ID_PATTERN}"
            )
    manifest = document.get("manifest")
    if not isinstance(manifest, dict):
        errors.append("configuration version: manifest must be an object")
    else:
        _check_keys(
            manifest, ("manifest_id",), "configuration version manifest", errors
        )
        manifest_id = manifest.get("manifest_id")
        if (
            not isinstance(manifest_id, str)
            or _MANIFEST_ID_RE.fullmatch(manifest_id) is None
        ):
            errors.append(
                "configuration version: manifest_id must match "
                f"{MANIFEST_ID_PATTERN}"
            )
    components = document.get("components")
    if not isinstance(components, list) or not components:
        errors.append("configuration version: components must be a non-empty list")
    else:
        seen: set[str] = set()
        for index, component in enumerate(components):
            where = f"configuration version components[{index}]"
            if not isinstance(component, dict):
                errors.append(f"{where}: must be an object")
                continue
            _check_keys(component, ("component_id", "component_version"), where, errors)
            component_id = component.get("component_id")
            if isinstance(component_id, str) and _ID_RE.fullmatch(component_id) is None:
                errors.append(f"{where}: component_id must match {ID_PATTERN}")
            elif isinstance(component_id, str):
                if component_id in seen:
                    errors.append(
                        f"{where}: component_id {component_id!r} is duplicated"
                    )
                seen.add(component_id)
            if "component_version" in component and not is_exact_version(
                component["component_version"]
            ):
                errors.append(
                    f"{where}: component_version {component.get('component_version')!r} "
                    "must be an exact selector-free version"
                )
    configuration = document.get("configuration")
    if not isinstance(configuration, dict):
        errors.append("configuration version: configuration must be an object")
    for field in ("golden_bundle", "extensions", "branding"):
        value = document.get(field)
        if value is not None and not isinstance(value, dict):
            errors.append(f"configuration version: {field} must be null or an object")
    attachments = document.get("attachments")
    if not isinstance(attachments, dict):
        errors.append("configuration version: attachments must be an object")
    else:
        _check_keys(
            attachments, _ATTACHMENT_KEYS, "configuration version attachments", errors
        )
        for key in _ATTACHMENT_KEYS:
            if key in attachments and attachments[key] is not None:
                errors.append(
                    "configuration version: attachment "
                    f"{key!r} must be null in Slice 1 (future domain, not implemented)"
                )
    _check_predecessor(
        document.get("predecessor"),
        "configuration_id",
        "configuration version predecessor",
        errors,
    )
    if "digest" in document:
        _check_digest(document["digest"], "digest", "configuration version", errors)
    return errors


def scalar_constraints(constraints: object, where: str) -> list[str]:
    """Requirement constraints must be a flat mapping of scalars."""
    errors: list[str] = []
    if not isinstance(constraints, dict):
        return [f"{where}: constraints must be an object"]
    for key, value in constraints.items():
        if not isinstance(key, str) or not key:
            errors.append(f"{where}: constraint keys must be non-empty strings")
        if not _is_scalar(value):
            errors.append(f"{where}: constraint {key!r} must be a scalar value")
    return errors
