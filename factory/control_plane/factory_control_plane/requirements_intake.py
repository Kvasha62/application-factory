"""Pure in-process intake adapters/normalizers for Slice 3 (S3).

Slice 3 adds the four ratified intake channels -- ``form`` / ``import`` /
``api`` / structured ``nl`` -- that normalize heterogeneous requirement input
into the single converged ``control-plane/requirements/v1`` entry shape. Every
channel is a *pure* value boundary:

* no network access,
* no persistence,
* no external model or automatic synthesis, and
* no raw natural-language text is ever retained.

The ``nl`` channel accepts only a *structured* parser candidate (the NL step
that produced it is out of scope); free text is refused closed.

The adapter -- never the caller -- assigns ``source_channels`` from the channel
the input arrived on, so a caller cannot assert its own provenance.
Normalization fails closed (``RequirementsIntakeError`` / ``S3-FLOW-001``) on
any malformed, ambiguous, contradictory, unsupported or unrepresentable
candidate; it never silently coerces an unresolved requirement into
``resolved``. The converged RequirementsVersion is built only through the
existing ``documents.new_requirements_version`` builder, which validates shape,
project ownership and the recomputed digest before settling.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from factory_control_plane.documents import new_requirements_version
from factory_control_plane.validation import ControlPlaneError, scalar_constraints

#: The four intake channels; each is also a valid v1 ``source_channel`` value.
CHANNELS = ("form", "import", "api", "nl")

#: Requirement entry ``kind`` values (frozen copy of
#: ``factory_control_plane.validation._ENTRY_KINDS`` -- part of the ratified
#: ``control-plane/requirements/v1`` schema; Slice 3 does not edit Slice 1/2).
KINDS = frozenset({"capability", "constraint", "input"})

#: Requirement entry ``status`` values (frozen copy of
#: ``factory_control_plane.validation._ENTRY_STATUSES``).
STATUSES = ("open", "resolved")

#: Candidate fields Slice 3 accepts. ``source_channels`` is deliberately absent:
#: the adapter assigns it, so a caller-supplied value fails closed.
_CANDIDATE_FIELDS = ("kind", "statement", "constraints", "refs", "status", "req_id")


class RequirementsIntakeError(ControlPlaneError):
    """Intake normalization or Requirements build failed closed.

    ``code`` is one of the S3 codes (``S3-FLOW-001`` for intake refusal,
    ``S3-FLOW-002`` for an invalid Requirements document). The S3 failure codes
    are local to Slice 3 and are never added to the S2 finding registry.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_constraints(value: Any) -> dict[str, Any]:
    """Return a flat scalar constraint mapping, or fail closed (S3-FLOW-001)."""
    problems = scalar_constraints(value, "constraints")
    if problems:
        raise RequirementsIntakeError(
            "S3-FLOW-001", "constraints: " + "; ".join(problems)
        )
    return dict(value)


def normalize_intake(
    channel: str, candidate: Any, *, seq: int | None = None
) -> dict[str, Any]:
    """Normalize one candidate from ``channel`` into a v1 entry (fail closed).

    The adapter assigns ``source_channels = [channel]``. ``refs`` defaults to
    ``[]``, ``status`` defaults to ``open``, and a missing ``req_id`` is
    allocated deterministically as ``s3-<seq>``. Any unknown field, invalid
    value, ambiguity or contradiction raises ``RequirementsIntakeError``
    (``S3-FLOW-001``).
    """
    if channel not in CHANNELS:
        raise RequirementsIntakeError(
            "S3-FLOW-001", f"unknown intake channel {channel!r}"
        )
    if not isinstance(candidate, Mapping):
        raise RequirementsIntakeError("S3-FLOW-001", "candidate must be a JSON object")

    unknown = set(candidate) - set(_CANDIDATE_FIELDS)
    if unknown:
        raise RequirementsIntakeError(
            "S3-FLOW-001", "unknown candidate fields: " + ", ".join(sorted(unknown))
        )

    kind = candidate.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise RequirementsIntakeError(
            "S3-FLOW-001", f"kind must be one of {sorted(KINDS)}"
        )

    statement = candidate.get("statement")
    if not isinstance(statement, Mapping):
        raise RequirementsIntakeError("S3-FLOW-001", "statement must be an object")
    if set(statement) - {"summary", "constraints"}:
        raise RequirementsIntakeError(
            "S3-FLOW-001",
            "unknown statement fields: "
            + ", ".join(sorted(set(statement) - {"summary", "constraints"})),
        )
    summary = statement.get("summary")
    if not isinstance(summary, str) or not summary:
        raise RequirementsIntakeError(
            "S3-FLOW-001", "statement.summary must be a non-empty string"
        )

    # Constraints may appear at most once: inside ``statement`` or at the
    # candidate top level, never both (that would be contradictory).
    top_constraints = candidate.get("constraints")
    statement_constraints = statement.get("constraints")
    if "constraints" in candidate and "constraints" in statement:
        raise RequirementsIntakeError(
            "S3-FLOW-001",
            "constraints given both at the candidate top level and in statement",
        )
    source_constraints = (
        top_constraints if top_constraints is not None else statement_constraints
    )
    constraints = (
        _validate_constraints(source_constraints)
        if source_constraints is not None
        else None
    )

    refs = candidate.get("refs", [])
    if not isinstance(refs, list) or not all(
        isinstance(item, str) and bool(item) for item in refs
    ):
        raise RequirementsIntakeError(
            "S3-FLOW-001", "refs must be a list of non-empty strings"
        )

    status = candidate.get("status", "open")
    if status not in STATUSES:
        raise RequirementsIntakeError(
            "S3-FLOW-001", f"status must be one of {sorted(STATUSES)}"
        )

    req_id = candidate.get("req_id")
    if req_id is None:
        if seq is None:
            raise RequirementsIntakeError(
                "S3-FLOW-001",
                "req_id is required when no sequence is supplied for allocation",
            )
        req_id = f"s3-{seq}"
    elif not isinstance(req_id, str) or not req_id:
        raise RequirementsIntakeError(
            "S3-FLOW-001", "req_id must be a non-empty string"
        )

    entry: dict[str, Any] = {
        "req_id": req_id,
        "kind": kind,
        "statement": {"summary": summary},
        "source_channels": [channel],
        "refs": list(refs),
        "status": status,
    }
    if constraints is not None:
        entry["statement"]["constraints"] = constraints
    return entry


def normalize_channel(channel: str, candidates: Any) -> list[dict[str, Any]]:
    """Normalize a list of candidates from ``channel`` (fail closed)."""
    if not isinstance(candidates, list) or not candidates:
        raise RequirementsIntakeError(
            "S3-FLOW-001", "candidates must be a non-empty list"
        )
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        entry = normalize_intake(channel, candidate, seq=index + 1)
        if entry["req_id"] in seen:
            raise RequirementsIntakeError(
                "S3-FLOW-001", f"duplicate req_id {entry['req_id']!r}"
            )
        seen.add(entry["req_id"])
        entries.append(entry)
    return entries


def normalize_form(candidate: Any, *, seq: int | None = None) -> dict[str, Any]:
    """Normalize one ``form`` candidate into a v1 entry (fail closed)."""
    return normalize_intake("form", candidate, seq=seq)


def normalize_import(candidate: Any, *, seq: int | None = None) -> dict[str, Any]:
    """Normalize one ``import`` candidate into a v1 entry (fail closed)."""
    return normalize_intake("import", candidate, seq=seq)


def normalize_api(candidate: Any, *, seq: int | None = None) -> dict[str, Any]:
    """Normalize one ``api`` candidate into a v1 entry (fail closed)."""
    return normalize_intake("api", candidate, seq=seq)


def normalize_nl(candidate: Any, *, seq: int | None = None) -> dict[str, Any]:
    """Normalize one *structured* ``nl`` parser candidate (never raw text)."""
    return normalize_intake("nl", candidate, seq=seq)


def intake_requirements_version(
    *,
    project_ref: str,
    channel: str,
    candidates: Any,
    predecessor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a converged RequirementsVersion from intake (fail closed).

    Normalizes ``candidates`` through ``channel`` and builds the immutable
    version only through ``documents.new_requirements_version`` -- which settles
    the digest and validates shape, project ownership and the recomputed digest.
    An invalid document raises ``RequirementsIntakeError`` (``S3-FLOW-002``).
    No persistence or current-pointer mutation is performed.
    """
    entries = normalize_channel(channel, candidates)
    try:
        return new_requirements_version(
            project_ref=project_ref, entries=entries, predecessor=predecessor
        )
    except ControlPlaneError as error:
        raise RequirementsIntakeError(
            "S3-FLOW-002", f"requirements document rejected: {error}"
        ) from error
