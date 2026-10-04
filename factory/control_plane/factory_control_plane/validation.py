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
  references, never component facts;
- Registry authority: ConfigurationVersion references are cross-checked
  against the canonical Component Registry on the authoritative
  validation/build path and fail closed on any disagreement.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from factory_control_plane.registry_reference import component_reference_errors

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
    # Registry authority is enforced on the authoritative validation/build
    # path (new_configuration_version -> configuration_version_errors): any
    # disagreement with the canonical Component Registry fails closed here —
    # unknown id, version mismatch, or lifecycle state != registered.
    if isinstance(components, list):
        errors.extend(component_reference_errors(components))
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


# ---------------------------------------------------------------------------
# Slice 2 — additive validators (S2-1 configuration/v2, S2-2 approval ledger,
# S2-3 acknowledgement-gated findings).
#
# Every rule below is ADDITIVE: the Slice 1 functions above are unchanged, so
# the v1 schema and its validation semantics remain exactly as merged. The v2
# family deliberately restates its own structural rules instead of delegating
# to the v1 functions: the two schema generations are frozen independently, so
# a later v1 change can never silently redefine v2 semantics.
# ---------------------------------------------------------------------------

#: New document schema ids introduced by Slice 2.
SCHEMA_CONFIGURATION_V2 = "control-plane/configuration/v2"
SCHEMA_PROPOSAL = "control-plane/proposal/v1"
SCHEMA_APPROVAL = "control-plane/approval/v1"
SCHEMA_COMPOSITION_REQUEST = "control-plane/composition-request/v1"

#: Strict public SemVer MAJOR.MINOR.PATCH — the version form the existing
#: Composition Request requires for ``manifest.manifest_version``.
SEMVER_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"

#: ISO-8601 UTC instant, same rule the Platform Manifest approval metadata
#: uses; a floating or local timestamp is never accepted.
ISO8601_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"

#: Finding identity: ``fnd-`` + 12 lowercase hex characters (content address).
FINDING_ID_PATTERN = r"^fnd-[0-9a-f]{12}$"

#: Finding code shape (frozen registry lives in ``findings.py``).
CODE_PATTERN = r"^CP-[A-Z0-9-]+$"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
SEVERITY_ORDER = (SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)
SEVERITIES = frozenset(SEVERITY_ORDER)
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITY_ORDER)}

PROPOSAL_STATES = frozenset({"ready", "blocked"})
APPROVAL_DECISIONS = frozenset({"granted", "rejected", "revoked"})
APPROVER_KINDS = frozenset({"owner", "agent", "system"})

FINDING_KEYS = ("code", "severity", "target", "message", "details", "finding_id")

#: Desired/Actual vocabulary. A Desired control-plane document must never
#: carry Actual-state keys; the Slice 1 separation proof is extended to every
#: Slice 2 document and schema by the same rule.
ACTUAL_STATE_KEYS = frozenset(
    {
        "actual",
        "actual_state",
        "deployed",
        "deployment",
        "drift",
        "health",
        "observed",
        "observed_identity",
        "reconciliation",
        "running_platform",
        "runtime",
        "runtime_health",
        "runtime_root",
        "runtime_state",
    }
)

#: Leaf key names that denote secret material. Matching is exact on the key
#: name (case-insensitive), so descriptive keys such as ``token_prefix`` or
#: ``service_token_prefix`` are not candidates.
SECRET_KEY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
    }
)

#: Value prefixes that are secret material by construction (PEM blocks).
SECRET_VALUE_PREFIXES = ("-----BEGIN ",)

#: Control-plane provenance keys that must never appear inside the composer
#: payload nested in a Composition Request record.
REQUEST_PAYLOAD_FORBIDDEN_KEYS = frozenset(
    {
        "approval_ref",
        "configuration_ref",
        "digest",
        "generator_version",
        "proposal_ref",
        "request_id",
        "schema_version",
    }
)

_CODE_RE = re.compile(CODE_PATTERN)
_SEMVER_RE = re.compile(SEMVER_PATTERN)
_ISO8601_RE = re.compile(ISO8601_PATTERN)
_FINDING_ID_RE = re.compile(FINDING_ID_PATTERN)


def finding_sort_key(finding: Mapping[str, Any]) -> tuple[int, str, str, str, str]:
    """Return the frozen ordering key of a finding.

    Order is ``(severity, target, code, message, finding_id)`` with
    ``error < warning < info``. The finding id is the final tie-break, so the
    order of an arbitrary input list never affects the sorted result.
    """
    severity = finding.get("severity")
    rank = _SEVERITY_RANK.get(severity, len(SEVERITY_ORDER))
    if not isinstance(severity, str):
        rank = len(SEVERITY_ORDER)
    return (
        rank,
        str(finding.get("target", "")),
        str(finding.get("code", "")),
        str(finding.get("message", "")),
        str(finding.get("finding_id", "")),
    )


def _check_finding(value: object, where: str, errors: list[str]) -> None:
    """Validate one finding object (structural rules only)."""
    if not isinstance(value, dict):
        errors.append(f"{where}: must be an object")
        return
    _check_keys(value, FINDING_KEYS, where, errors)
    code = value.get("code")
    if not isinstance(code, str) or _CODE_RE.fullmatch(code) is None:
        errors.append(f"{where}: code must match {CODE_PATTERN}")
    if value.get("severity") not in SEVERITIES:
        errors.append(f"{where}: severity must be one of {sorted(SEVERITIES)}")
    _check_string(value, "target", where, errors)
    _check_string(value, "message", where, errors)
    details = value.get("details")
    if not isinstance(details, dict):
        errors.append(f"{where}: details must be an object")
    finding_identifier = value.get("finding_id")
    if (
        not isinstance(finding_identifier, str)
        or _FINDING_ID_RE.fullmatch(finding_identifier) is None
    ):
        errors.append(f"{where}: finding_id must match {FINDING_ID_PATTERN}")


def _check_manifest_request(
    manifest: object, where: str, *, version_pattern: str, errors: list[str]
) -> None:
    """Validate the request-shaped manifest header of a v2 configuration."""
    if not isinstance(manifest, dict):
        errors.append(f"{where}: must be an object")
        return
    _check_keys(
        manifest, ("manifest_id", "manifest_version", "predecessor"), where, errors
    )
    manifest_id = manifest.get("manifest_id")
    if (
        not isinstance(manifest_id, str)
        or _MANIFEST_ID_RE.fullmatch(manifest_id) is None
    ):
        errors.append(f"{where}: manifest_id must match {MANIFEST_ID_PATTERN}")
    version = manifest.get("manifest_version")
    if not isinstance(version, str) or re.fullmatch(version_pattern, version) is None:
        errors.append(
            f"{where}: manifest_version must be an explicit SemVer "
            "MAJOR.MINOR.PATCH (the Composition Request form)"
        )
    predecessor = manifest.get("predecessor")
    if predecessor is not None:
        if not isinstance(predecessor, dict):
            errors.append(f"{where}.predecessor: must be null or an object")
        else:
            _check_keys(
                predecessor,
                ("manifest_id", "manifest_version", "manifest_digest"),
                f"{where}.predecessor",
                errors,
            )
            _check_manifest_request(
                {
                    "manifest_id": predecessor.get("manifest_id"),
                    "manifest_version": predecessor.get("manifest_version"),
                    "predecessor": None,
                },
                f"{where}.predecessor",
                version_pattern=version_pattern,
                errors=errors,
            )
            _check_digest(
                predecessor.get("manifest_digest"),
                "manifest_digest",
                f"{where}.predecessor",
                errors,
            )


def configuration_version_v2_errors(document: object) -> list[str]:
    """Validate an immutable ``control-plane/configuration/v2`` document.

    v2 is the Slice 2 generation of the ConfigurationVersion: identical to v1
    except that the payload carries the fields the existing Composition
    Request requires — ``manifest{manifest_id, manifest_version, predecessor}``,
    a request-shaped nullable ``golden_bundle`` reference and a request-shaped
    nullable ``extensions`` list. Both are validated for shape here; their
    vocabulary (bundle identity, extension mechanisms, repository paths) stays
    the Composer's published schema, enforced when the request is generated.
    """
    if not isinstance(document, dict):
        return ["configuration version v2: must be an object"]
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
        "configuration version v2",
        errors,
    )
    if document.get("schema_version") != SCHEMA_CONFIGURATION_V2:
        errors.append(
            f"configuration version v2: schema_version must be "
            f"{SCHEMA_CONFIGURATION_V2!r}"
        )
    if "configuration_id" in document:
        _check_versioned_id(
            document["configuration_id"],
            "configurations",
            "configuration_id",
            errors,
        )
    if "project_ref" in document:
        _check_id(
            document["project_ref"], "project_ref", "configuration version v2", errors
        )
    if (
        isinstance(document.get("configuration_id"), str)
        and isinstance(document.get("project_ref"), str)
        and document["configuration_id"].split("/")[0] != document["project_ref"]
    ):
        errors.append(
            "configuration version v2: configuration_id must live under project_ref"
        )
    if "requirements_ref" in document:
        _check_digest_ref(
            document["requirements_ref"],
            "requirements_id",
            "configuration version v2 requirements_ref",
            errors,
        )
    for field in ("template_ref", "variant_ref"):
        value = document.get(field)
        if value is not None and (
            not isinstance(value, str) or _ID_RE.fullmatch(value) is None
        ):
            errors.append(
                f"configuration version v2: {field} must be null or match {ID_PATTERN}"
            )
    _check_manifest_request(
        document.get("manifest"),
        "configuration version v2 manifest",
        version_pattern=SEMVER_PATTERN,
        errors=errors,
    )
    components = document.get("components")
    if not isinstance(components, list) or not components:
        errors.append("configuration version v2: components must be a non-empty list")
    else:
        seen: set[str] = set()
        for index, component in enumerate(components):
            where = f"configuration version v2 components[{index}]"
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
                    f"{where}: component_version "
                    f"{component.get('component_version')!r} must be an exact "
                    "selector-free version"
                )
        # Registry authority is enforced on the authoritative build/validation
        # path exactly as in v1: any disagreement with the canonical Component
        # Registry fails closed here (unknown id, version mismatch, lifecycle
        # state other than ``registered``, or an unreadable registry).
        errors.extend(component_reference_errors(components))
    configuration = document.get("configuration")
    if not isinstance(configuration, dict):
        errors.append("configuration version v2: configuration must be an object")
    golden_bundle = document.get("golden_bundle")
    if golden_bundle is not None and not isinstance(golden_bundle, dict):
        errors.append(
            "configuration version v2: golden_bundle must be null or the "
            "Composition Request bundle reference object"
        )
    extensions = document.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, list):
            errors.append(
                "configuration version v2: extensions must be null or the "
                "Composition Request extension list"
            )
        elif not all(isinstance(entry, dict) for entry in extensions):
            errors.append(
                "configuration version v2: extensions entries must be objects"
            )
    branding = document.get("branding")
    if branding is not None and not isinstance(branding, dict):
        errors.append("configuration version v2: branding must be null or an object")
    attachments = document.get("attachments")
    if not isinstance(attachments, dict):
        errors.append("configuration version v2: attachments must be an object")
    else:
        _check_keys(
            attachments,
            _ATTACHMENT_KEYS,
            "configuration version v2 attachments",
            errors,
        )
        for key in _ATTACHMENT_KEYS:
            if key in attachments and attachments[key] is not None:
                errors.append(
                    "configuration version v2: attachment "
                    f"{key!r} must be null (approval is an immutable ledger "
                    "record in Slice 2, never a field of the configuration)"
                )
    _check_predecessor(
        document.get("predecessor"),
        "configuration_id",
        "configuration version v2 predecessor",
        errors,
    )
    if "digest" in document:
        _check_digest(document["digest"], "digest", "configuration version v2", errors)
    return errors


def proposal_version_errors(document: object) -> list[str]:
    """Validate an immutable ``control-plane/proposal/v1`` document.

    A ProposalVersion is the Slice 2 validation report for one exact
    ConfigurationVersion: it binds the configuration digest to the findings
    that justify accepting, acknowledging or rejecting it.
    """
    if not isinstance(document, dict):
        return ["proposal version: must be an object"]
    errors: list[str] = []
    _check_keys(
        document,
        (
            "schema_version",
            "proposal_id",
            "project_ref",
            "requirements_ref",
            "configuration_ref",
            "inputs",
            "findings",
            "state",
            "predecessor",
            "digest",
        ),
        "proposal version",
        errors,
    )
    if document.get("schema_version") != SCHEMA_PROPOSAL:
        errors.append(f"proposal version: schema_version must be {SCHEMA_PROPOSAL!r}")
    if "proposal_id" in document:
        _check_versioned_id(document["proposal_id"], "proposals", "proposal_id", errors)
    if "project_ref" in document:
        _check_id(document["project_ref"], "project_ref", "proposal version", errors)
    if (
        isinstance(document.get("proposal_id"), str)
        and isinstance(document.get("project_ref"), str)
        and document["proposal_id"].split("/")[0] != document["project_ref"]
    ):
        errors.append("proposal version: proposal_id must live under project_ref")
    if "requirements_ref" in document:
        _check_digest_ref(
            document["requirements_ref"],
            "requirements_id",
            "proposal version requirements_ref",
            errors,
        )
    if "configuration_ref" in document:
        _check_digest_ref(
            document["configuration_ref"],
            "configuration_id",
            "proposal version configuration_ref",
            errors,
        )
    inputs = document.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("proposal version: inputs must be an object")
    else:
        _check_keys(
            inputs,
            ("validator_set", "registry_fingerprint"),
            "proposal version inputs",
            errors,
        )
        _check_string(inputs, "validator_set", "proposal version inputs", errors)
        fingerprint = inputs.get("registry_fingerprint")
        if fingerprint is not None:
            _check_digest(
                fingerprint, "registry_fingerprint", "proposal version inputs", errors
            )
    findings_list = document.get("findings")
    if not isinstance(findings_list, list):
        errors.append("proposal version: findings must be a list")
    else:
        seen_ids: set[str] = set()
        for index, item in enumerate(findings_list):
            where = f"proposal version findings[{index}]"
            _check_finding(item, where, errors)
            if isinstance(item, dict) and isinstance(item.get("finding_id"), str):
                identifier = item["finding_id"]
                if identifier in seen_ids:
                    errors.append(f"{where}: finding_id {identifier!r} is duplicated")
                seen_ids.add(identifier)
        if all(isinstance(item, dict) for item in findings_list):
            keys = [finding_sort_key(item) for item in findings_list]
            if keys != sorted(keys):
                errors.append(
                    "proposal version: findings must be sorted by "
                    "(severity, target, code, message, finding_id)"
                )
    state = document.get("state")
    if state not in PROPOSAL_STATES:
        errors.append("proposal version: state must be 'ready' or 'blocked'")
    elif isinstance(findings_list, list) and all(
        isinstance(item, dict) and item.get("severity") in SEVERITIES
        for item in findings_list
    ):
        blocking = any(item["severity"] == SEVERITY_ERROR for item in findings_list)
        expected_state = "blocked" if blocking else "ready"
        if state != expected_state:
            errors.append(
                "proposal version: state must be "
                f"{expected_state!r} for these findings (a blocking error "
                "makes a proposal blocked; warnings never do)"
            )
    _check_predecessor(
        document.get("predecessor"),
        "proposal_id",
        "proposal version predecessor",
        errors,
    )
    if "digest" in document:
        _check_digest(document["digest"], "digest", "proposal version", errors)
    return errors


def _check_approver(value: object, where: str, errors: list[str]) -> None:
    """Validate the approver reference (RBAC attachment point included)."""
    if not isinstance(value, dict):
        errors.append(f"{where}: must be an object")
        return
    _check_keys(value, ("kind", "reference", "authority_ref"), where, errors)
    if value.get("kind") not in APPROVER_KINDS:
        errors.append(f"{where}: kind must be one of {sorted(APPROVER_KINDS)}")
    _check_string(value, "reference", where, errors)
    if value.get("authority_ref") is not None:
        errors.append(
            f"{where}: authority_ref is the RBAC attachment point and must be "
            "null — no authorization policy is implemented in this slice"
        )


def approval_record_errors(document: object) -> list[str]:
    """Validate one immutable ``control-plane/approval/v1`` ledger record.

    Records are append-only: a written record has no update or delete form, an
    undo is a new ``revoked`` record, and effectiveness is always derived
    (never stored).
    """
    if not isinstance(document, dict):
        return ["approval record: must be an object"]
    errors: list[str] = []
    _check_keys(
        document,
        (
            "schema_version",
            "approval_id",
            "project_ref",
            "decision",
            "configuration_ref",
            "proposal_ref",
            "requirements_ref",
            "acknowledged_findings",
            "approver",
            "decided_at",
            "reason",
            "revokes",
            "predecessor",
            "digest",
        ),
        "approval record",
        errors,
    )
    if document.get("schema_version") != SCHEMA_APPROVAL:
        errors.append(f"approval record: schema_version must be {SCHEMA_APPROVAL!r}")
    if "approval_id" in document:
        _check_versioned_id(document["approval_id"], "approvals", "approval_id", errors)
    if "project_ref" in document:
        _check_id(document["project_ref"], "project_ref", "approval record", errors)
    if (
        isinstance(document.get("approval_id"), str)
        and isinstance(document.get("project_ref"), str)
        and document["approval_id"].split("/")[0] != document["project_ref"]
    ):
        errors.append("approval record: approval_id must live under project_ref")
    decision = document.get("decision")
    if decision not in APPROVAL_DECISIONS:
        errors.append(
            f"approval record: decision must be one of {sorted(APPROVAL_DECISIONS)}"
        )
    if "configuration_ref" in document:
        _check_digest_ref(
            document["configuration_ref"],
            "configuration_id",
            "approval record configuration_ref",
            errors,
        )
    if "proposal_ref" in document:
        _check_digest_ref(
            document["proposal_ref"],
            "proposal_id",
            "approval record proposal_ref",
            errors,
        )
    if "requirements_ref" in document:
        _check_digest_ref(
            document["requirements_ref"],
            "requirements_id",
            "approval record requirements_ref",
            errors,
        )
    acknowledged = document.get("acknowledged_findings")
    if not isinstance(acknowledged, list):
        errors.append("approval record: acknowledged_findings must be a list")
    else:
        for index, identifier in enumerate(acknowledged):
            where = f"approval record acknowledged_findings[{index}]"
            if (
                not isinstance(identifier, str)
                or _FINDING_ID_RE.fullmatch(identifier) is None
            ):
                errors.append(f"{where}: must match {FINDING_ID_PATTERN}")
        if acknowledged != sorted(set(acknowledged)):
            errors.append(
                "approval record: acknowledged_findings must be sorted and "
                "free of duplicates"
            )
        if decision == "revoked" and acknowledged:
            errors.append(
                "approval record: a revocation acknowledges nothing "
                "(acknowledged_findings must be empty)"
            )
    _check_approver(document.get("approver"), "approval record approver", errors)
    decided_at = document.get("decided_at")
    if not isinstance(decided_at, str) or _ISO8601_RE.fullmatch(decided_at) is None:
        errors.append(
            "approval record: decided_at must be an ISO-8601 UTC instant "
            "(YYYY-MM-DDTHH:MM:SSZ) — a floating selector is not a time"
        )
    _check_string(document, "reason", "approval record", errors)
    revokes = document.get("revokes")
    if decision == "revoked":
        if not isinstance(revokes, dict):
            errors.append(
                "approval record: revokes is required when decision is 'revoked'"
            )
        else:
            _check_digest_ref(revokes, "approval_id", "approval record revokes", errors)
    elif revokes is not None:
        errors.append(
            "approval record: revokes is only meaningful for a revocation "
            "(decision 'revoked'); it must be null otherwise"
        )
    _check_predecessor(
        document.get("predecessor"),
        "approval_id",
        "approval record predecessor",
        errors,
    )
    if "digest" in document:
        _check_digest(document["digest"], "digest", "approval record", errors)
    return errors


def composition_request_record_errors(document: object) -> list[str]:
    """Validate an immutable ``control-plane/composition-request/v1`` record.

    The record wraps the exact Composition Request payload that the Composer
    consumes; provenance (configuration digest, approval digest, generator
    identity) lives in the wrapper, never inside the payload.
    """
    if not isinstance(document, dict):
        return ["composition request record: must be an object"]
    errors: list[str] = []
    _check_keys(
        document,
        (
            "schema_version",
            "request_id",
            "project_ref",
            "configuration_ref",
            "approval_ref",
            "generator_version",
            "request",
            "digest",
        ),
        "composition request record",
        errors,
    )
    if document.get("schema_version") != SCHEMA_COMPOSITION_REQUEST:
        errors.append(
            "composition request record: schema_version must be "
            f"{SCHEMA_COMPOSITION_REQUEST!r}"
        )
    if "request_id" in document:
        _check_versioned_id(document["request_id"], "requests", "request_id", errors)
    if "project_ref" in document:
        _check_id(
            document["project_ref"], "project_ref", "composition request record", errors
        )
    if (
        isinstance(document.get("request_id"), str)
        and isinstance(document.get("project_ref"), str)
        and document["request_id"].split("/")[0] != document["project_ref"]
    ):
        errors.append(
            "composition request record: request_id must live under project_ref"
        )
    if "configuration_ref" in document:
        _check_digest_ref(
            document["configuration_ref"],
            "configuration_id",
            "composition request record configuration_ref",
            errors,
        )
    if "approval_ref" in document:
        _check_digest_ref(
            document["approval_ref"],
            "approval_id",
            "composition request record approval_ref",
            errors,
        )
    _check_string(document, "generator_version", "composition request record", errors)
    request = document.get("request")
    if not isinstance(request, dict):
        errors.append(
            "composition request record: request must be the Composition "
            "Request object"
        )
    else:
        for key in ("manifest", "components"):
            if key not in request:
                errors.append(
                    f"composition request record: request is missing required "
                    f"property {key!r}"
                )
        carried = sorted(set(request) & REQUEST_PAYLOAD_FORBIDDEN_KEYS)
        if carried:
            errors.append(
                "composition request record: the nested request must carry no "
                f"control-plane provenance fields, found {carried}"
            )
    if "digest" in document:
        _check_digest(
            document["digest"], "digest", "composition request record", errors
        )
    return errors
