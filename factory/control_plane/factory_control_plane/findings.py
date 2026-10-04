"""Deterministic, content-addressed findings — the Slice 2 result form.

Every Slice 2 validation outcome is reported as a *finding*: a small
immutable object with a frozen code, a severity, a target, a message and a
content-addressed id. Findings are the only place where a decision is
attached to a validation result, and the severity model is the ratified
S2-3 contract:

* ``error`` — blocking. Cannot be acknowledged and cannot be bypassed: while
  one exists, the proposal is ``blocked`` and no approval may be granted.
* ``warning`` — must be explicitly acknowledged. Every warning of a proposal
  must appear in the acknowledging approval, or the approval is ineffective.
* ``info`` — informational, no acknowledgement.

Determinism. Findings are emitted in a frozen order
(``severity, target, code, message, finding_id``), de-duplicated by their
content-addressed id, and every id is ``fnd-`` + the first 12 hex characters
of the SHA-256 of the finding content. The same inputs therefore always
produce the same list, the same ids and the same document digest, in any
process and in any dictionary order.

Authority. This module never re-encodes another authority's rules. Registry
references are checked by the canonical Component Registry through the
unchanged Slice 1 helper (``component_reference_errors``) and composition
outcomes by the Composer's public diagnostics; their messages are reported
verbatim, only the reporting wrapper is Slice 2's.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from factory_control_plane.canonical import canonical_bytes
from factory_control_plane.digests import digest_of
from factory_control_plane.registry_reference import (
    REGISTRY_RELATIVE_PATH,
    RegistryReferenceError,
    component_reference_errors,
    repository_root,
)
from factory_control_plane.validation import (
    ACTUAL_STATE_KEYS,
    SCHEMA_CONFIGURATION_V2,
    SECRET_KEY_NAMES,
    SECRET_VALUE_PREFIXES,
    SEVERITIES,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_ORDER,
    SEVERITY_WARNING,
    configuration_version_errors,
    configuration_version_v2_errors,
    finding_sort_key,
)

# ---------------------------------------------------------------------------
# Frozen code registry. A code keeps its meaning forever: a new meaning needs
# a new code, never a reinterpretation of an old one.
# ---------------------------------------------------------------------------

#: Control-plane structural validator violation (message verbatim).
CODE_STRUCTURAL = "CP-STRUCT-000"
#: The referenced RequirementsVersion is unavailable (Slice 1 had no such check).
CODE_REQUIREMENTS_UNKNOWN = "CP-SEM-REQ-UNKNOWN"
#: The referenced RequirementsVersion does not match its declared digest.
CODE_REQUIREMENTS_DIGEST = "CP-SEM-REQ-DIGEST"
#: Chain/provenance inconsistency between the documents.
CODE_CHAIN = "CP-SEM-CHAIN"
#: Delegated: the canonical Component Registry refuses the references.
CODE_REGISTRY_REFUSAL = "CP-REG-000"
#: Delegated: the Composer rejects the projected request.
CODE_COMPOSER_REJECTION = "CP-DEP-000"
#: The composition pins no Golden Bundle and will be uncertified (informational).
CODE_COMPOSER_UNCERTIFIED = "CP-DEP-001"
#: Secret-like material in a control-plane document.
CODE_SECRET_MATERIAL = "CP-POL-001"
#: Actual-state key in a Desired control-plane document.
CODE_ACTUAL_STATE_KEY = "CP-POL-002"
#: The referenced project is not active.
CODE_PROJECT_NOT_ACTIVE = "CP-POL-004"
#: A v1 ConfigurationVersion cannot be projected into a Composition Request.
CODE_V1_NOT_PROJECTABLE = "CP-CONF-V1-NOT-PROJECTABLE"
#: WARNING: requirements remain open while the configuration is resolved.
CODE_OPEN_REQUIREMENTS = "CP-WARN-OPEN-REQUIREMENTS"
#: WARNING: a manifest predecessor is recorded but cannot be verified offline.
CODE_PREDECESSOR_UNVERIFIED = "CP-WARN-PREDECESSOR-UNVERIFIED"
#: An approval's acknowledgement set differs from the proposal's warning set.
CODE_ACKNOWLEDGEMENT_MISMATCH = "CP-APR-001"
#: An approval is not effective for the exact referenced documents.
CODE_APPROVAL_NOT_EFFECTIVE = "CP-APR-002"
#: An ``error`` finding was listed as acknowledged.
CODE_ERROR_ACKNOWLEDGED = "CP-APR-003"

#: Every code this slice may emit, with its frozen severity.
CODE_SEVERITIES: dict[str, str] = {
    CODE_STRUCTURAL: SEVERITY_ERROR,
    CODE_REQUIREMENTS_UNKNOWN: SEVERITY_ERROR,
    CODE_REQUIREMENTS_DIGEST: SEVERITY_ERROR,
    CODE_CHAIN: SEVERITY_ERROR,
    CODE_REGISTRY_REFUSAL: SEVERITY_ERROR,
    CODE_COMPOSER_REJECTION: SEVERITY_ERROR,
    CODE_COMPOSER_UNCERTIFIED: SEVERITY_INFO,
    CODE_SECRET_MATERIAL: SEVERITY_ERROR,
    CODE_ACTUAL_STATE_KEY: SEVERITY_ERROR,
    CODE_PROJECT_NOT_ACTIVE: SEVERITY_ERROR,
    CODE_V1_NOT_PROJECTABLE: SEVERITY_ERROR,
    CODE_OPEN_REQUIREMENTS: SEVERITY_WARNING,
    CODE_PREDECESSOR_UNVERIFIED: SEVERITY_WARNING,
    CODE_ACKNOWLEDGEMENT_MISMATCH: SEVERITY_ERROR,
    CODE_APPROVAL_NOT_EFFECTIVE: SEVERITY_ERROR,
    CODE_ERROR_ACKNOWLEDGED: SEVERITY_ERROR,
}


# ---------------------------------------------------------------------------
# Construction and inspection
# ---------------------------------------------------------------------------


def finding(
    code: str,
    severity: str,
    target: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one finding with its content-addressed id."""
    if severity not in SEVERITIES:
        message_ = (
            f"unknown severity {severity!r}; expected one of {sorted(SEVERITIES)}"
        )
        raise ValueError(message_)
    body: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "target": target,
        "message": message,
        "details": dict(details) if details else {},
    }
    body["finding_id"] = _content_id(body)
    return body


def _finding_payload(finding_: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": finding_.get("code"),
        "severity": finding_.get("severity"),
        "target": finding_.get("target"),
        "message": finding_.get("message"),
        "details": finding_.get("details") or {},
    }


def _content_id(finding_: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_bytes(_finding_payload(finding_))).hexdigest()
    return f"fnd-{digest[:12]}"


def finding_id(finding_: Mapping[str, Any]) -> str:
    """Return the content-addressed id of a finding (recomputed, not trusted)."""
    return _content_id(finding_)


def sort_findings(
    findings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return findings in the frozen order, de-duplicated by finding id."""
    ordered = sorted((dict(item) for item in findings), key=finding_sort_key)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ordered:
        identifier = item.get("finding_id")
        if identifier in seen:
            continue
        seen.add(identifier)
        unique.append(item)
    return unique


def errors_of(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the blocking (``error``) findings, in frozen order."""
    return [dict(item) for item in findings if item.get("severity") == SEVERITY_ERROR]


def warnings_of(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the acknowledgement-gated (``warning``) findings, frozen order."""
    return [dict(item) for item in findings if item.get("severity") == SEVERITY_WARNING]


def infos_of(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the informational (``info``) findings, in frozen order."""
    return [dict(item) for item in findings if item.get("severity") == SEVERITY_INFO]


def has_blocking_findings(findings: Iterable[Mapping[str, Any]]) -> bool:
    """True when at least one finding is an ``error``."""
    return any(item.get("severity") == SEVERITY_ERROR for item in findings)


def warning_ids(findings: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return the sorted finding ids of every warning (the ack set contract)."""
    return sorted(
        str(item["finding_id"])
        for item in findings
        if item.get("severity") == SEVERITY_WARNING
    )


def severity_counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Return a deterministic count per severity (all severities present)."""
    counts = dict.fromkeys(SEVERITY_ORDER, 0)
    for item in findings:
        severity = item.get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _target_of(document: object) -> str:
    if isinstance(document, Mapping):
        identifier = document.get("configuration_id")
        if isinstance(identifier, str) and identifier:
            return identifier
    return "$"


def _walk(document: object, path: str = "$") -> Iterable[tuple[str, object]]:
    """Yield ``(path, value)`` for every node of a JSON-like document."""
    yield path, document
    if isinstance(document, Mapping):
        for key in document:
            yield from _walk(document[key], f"{path}.{key}")
    elif isinstance(document, Sequence) and not isinstance(document, str | bytes):
        for index, item in enumerate(document):
            yield from _walk(item, f"{path}[{index}]")


def structural_findings(document: object) -> list[dict[str, Any]]:
    """Control-plane structural findings (registry refusals excluded).

    Registry messages are carried by :func:`registry_findings` instead, so a
    refusal is reported once, under its delegated code, exactly as the
    canonical registry phrased it.
    """
    schema = document.get("schema_version") if isinstance(document, Mapping) else None
    if schema == SCHEMA_CONFIGURATION_V2:
        messages = configuration_version_v2_errors(document)
    else:
        messages = configuration_version_errors(document)
    components = document.get("components") if isinstance(document, Mapping) else None
    delegated = list(component_reference_errors(components))
    remaining = list(messages)
    for message in delegated:
        if message in remaining:
            remaining.remove(message)
    target = _target_of(document)
    return [
        finding(CODE_STRUCTURAL, SEVERITY_ERROR, target, text) for text in remaining
    ]


def registry_findings(document: object) -> list[dict[str, Any]]:
    """Delegated registry refusal findings (messages verbatim)."""
    components = document.get("components") if isinstance(document, Mapping) else None
    target = _target_of(document)
    return [
        finding(CODE_REGISTRY_REFUSAL, SEVERITY_ERROR, target, message)
        for message in component_reference_errors(components)
    ]


def policy_findings(
    document: object, *, project: object = None
) -> list[dict[str, Any]]:
    """Policy findings: secrets, Actual-state keys, project status."""
    target = _target_of(document)
    findings: list[dict[str, Any]] = []
    for path, value in _walk(document):
        if not isinstance(value, Mapping):
            continue
        for key in sorted(value):
            if not isinstance(key, str):
                continue
            if key.lower() in SECRET_KEY_NAMES:
                findings.append(
                    finding(
                        CODE_SECRET_MATERIAL,
                        SEVERITY_ERROR,
                        target,
                        "secret-like key in a Desired control-plane document: "
                        "secrets have no representation here",
                        {"path": f"{path}.{key}", "key": key},
                    )
                )
    for path, value in _walk(document):
        if isinstance(value, str) and value.startswith(SECRET_VALUE_PREFIXES):
            findings.append(
                finding(
                    CODE_SECRET_MATERIAL,
                    SEVERITY_ERROR,
                    target,
                    "secret-like value (key material block) in a Desired "
                    "control-plane document",
                    {"path": path},
                )
            )
    for path, value in _walk(document):
        if not isinstance(value, Mapping):
            continue
        for key in sorted(value):
            if isinstance(key, str) and key in ACTUAL_STATE_KEYS:
                findings.append(
                    finding(
                        CODE_ACTUAL_STATE_KEY,
                        SEVERITY_ERROR,
                        target,
                        "Actual-state key in a Desired control-plane document "
                        "(Desired and Actual stay strictly separated)",
                        {"path": f"{path}.{key}", "key": key},
                    )
                )
    if isinstance(project, Mapping) and project.get("status") not in (None, "active"):
        findings.append(
            finding(
                CODE_PROJECT_NOT_ACTIVE,
                SEVERITY_ERROR,
                target,
                "the referenced project is not active",
                {
                    "project_id": project.get("project_id"),
                    "status": project.get("status"),
                },
            )
        )
    return findings


def requirements_findings(
    document: object, *, requirements: object = None
) -> list[dict[str, Any]]:
    """Semantic findings about the referenced RequirementsVersion.

    Slice 1 validated the *shape* of ``requirements_ref`` only; Slice 2 checks
    that the referenced document exists (is supplied), carries the referenced
    identity and still matches the declared digest, and reports requirements
    that remain open as an acknowledgement-gated warning.
    """
    target = _target_of(document)
    reference = (
        document.get("requirements_ref") if isinstance(document, Mapping) else None
    )
    reference = reference if isinstance(reference, Mapping) else {}
    requested_id = reference.get("requirements_id")
    requested_digest = reference.get("digest")

    if not isinstance(requirements, Mapping):
        return [
            finding(
                CODE_REQUIREMENTS_UNKNOWN,
                SEVERITY_ERROR,
                target,
                "the referenced RequirementsVersion is not available: the "
                "configuration cannot be traced to its intent",
                {"requirements_id": requested_id, "digest": requested_digest},
            )
        ]

    findings: list[dict[str, Any]] = []
    actual_id = requirements.get("requirements_id")
    if actual_id != requested_id:
        findings.append(
            finding(
                CODE_REQUIREMENTS_UNKNOWN,
                SEVERITY_ERROR,
                target,
                f"the referenced RequirementsVersion is {actual_id!r}, not "
                f"{requested_id!r}",
                {"requirements_id": requested_id, "found": actual_id},
            )
        )
    declared = requirements.get("digest")
    recomputed = digest_of(requirements)
    if declared != requested_digest or recomputed != requested_digest:
        findings.append(
            finding(
                CODE_REQUIREMENTS_DIGEST,
                SEVERITY_ERROR,
                target,
                "the referenced RequirementsVersion does not match its declared "
                "digest",
                {
                    "requirements_id": requested_id,
                    "declared_digest": declared,
                    "computed_digest": recomputed,
                    "referenced_digest": requested_digest,
                },
            )
        )
    project_ref = document.get("project_ref") if isinstance(document, Mapping) else None
    if (
        isinstance(project_ref, str)
        and isinstance(requirements.get("project_ref"), str)
        and requirements["project_ref"] != project_ref
    ):
        findings.append(
            finding(
                CODE_CHAIN,
                SEVERITY_ERROR,
                target,
                "the RequirementsVersion belongs to a different project",
                {
                    "requirements_project_ref": requirements.get("project_ref"),
                    "project_ref": project_ref,
                },
            )
        )
    entries = requirements.get("entries")
    if isinstance(entries, list):
        open_ids = sorted(
            str(entry.get("req_id"))
            for entry in entries
            if isinstance(entry, Mapping) and entry.get("status") == "open"
        )
        if open_ids:
            findings.append(
                finding(
                    CODE_OPEN_REQUIREMENTS,
                    SEVERITY_WARNING,
                    target,
                    f"{len(open_ids)} requirement(s) remain open while the "
                    "configuration is resolved; accepting this configuration "
                    "requires acknowledging the residual openness",
                    {"count": len(open_ids), "req_ids": open_ids},
                )
            )
    return findings


def notice_findings(document: object) -> list[dict[str, Any]]:
    """Informational notices and non-blocking warnings about a configuration."""
    if not isinstance(document, Mapping):
        return []
    if document.get("schema_version") != SCHEMA_CONFIGURATION_V2:
        return []
    target = _target_of(document)
    findings: list[dict[str, Any]] = []
    if document.get("golden_bundle") is None:
        findings.append(
            finding(
                CODE_COMPOSER_UNCERTIFIED,
                SEVERITY_INFO,
                target,
                "the configuration pins no Golden Bundle: the composition will "
                "be explicitly uncertified (golden_bundle: null in the manifest)",
            )
        )
    manifest = document.get("manifest")
    predecessor = manifest.get("predecessor") if isinstance(manifest, Mapping) else None
    if predecessor is not None:
        findings.append(
            finding(
                CODE_PREDECESSOR_UNVERIFIED,
                SEVERITY_WARNING,
                target,
                "the configuration records a manifest predecessor that cannot be "
                "verified offline: the repository stores no manifest documents, "
                "so publication-state verification remains with the Platform "
                "Manifest lifecycle",
                {"manifest_predecessor": dict(predecessor)},
            )
        )
    return findings


def collect_findings(
    document: object,
    *,
    requirements: object = None,
    project: object = None,
    composer_errors: Iterable[str] = (),
    not_projectable: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Collect every Slice 2 finding for one configuration document.

    ``composer_errors`` are the Composer's own diagnostics for the projected
    request (reported verbatim under the delegated code) and
    ``not_projectable`` are the fail-closed projection refusals (a v1
    configuration, or a document that cannot be projected at all).
    """
    target = _target_of(document)
    findings: list[dict[str, Any]] = []
    findings.extend(structural_findings(document))
    findings.extend(registry_findings(document))
    findings.extend(policy_findings(document, project=project))
    findings.extend(requirements_findings(document, requirements=requirements))
    findings.extend(
        finding(CODE_COMPOSER_REJECTION, SEVERITY_ERROR, target, message)
        for message in composer_errors
    )
    findings.extend(
        finding(CODE_V1_NOT_PROJECTABLE, SEVERITY_ERROR, target, message)
        for message in not_projectable
    )
    findings.extend(notice_findings(document))
    return sort_findings(findings)


# ---------------------------------------------------------------------------
# Registry fingerprint (an input fingerprint, never an identity)
# ---------------------------------------------------------------------------


def registry_fingerprint(start: Path | None = None) -> str | None:
    """Return ``sha256:`` of the registry document bytes as read, or ``None``.

    The fingerprint records *which registry state* a proposal was built
    against. It is an input fingerprint, not an identity: it never substitutes
    for the registry's own facts and is never written back to the registry.
    """
    try:
        root = repository_root(start)
        content = (root / REGISTRY_RELATIVE_PATH).read_bytes()
    except (OSError, RegistryReferenceError):
        return None
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
