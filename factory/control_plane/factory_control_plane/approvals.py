"""Immutable, append-only approval ledger (S2-2) with gated warnings (S2-3).

An approval is an **act**, not a state: one immutable record bound to the exact
``(configuration_id, digest)`` it authorizes, to the proposal it was taken
against and to the warnings it knowingly accepted. Records are append-only —
there is no update or delete path anywhere in this module, an undo is a new
``revoked`` record, and the ledger is a frozen object whose record mappings
cannot be mutated in place (attempting to does raise ``TypeError``).

Effectiveness is always **derived**, never stored: an approval is effective
only while its configuration digest still matches, its proposal still carries
no blocking error, its acknowledgement set still equals the proposal's warning
set, the canonical Component Registry still admits the approved references, and
no later record revoked it. This is what makes "any technical change requires a
new approval" true by construction — and what makes registry drift a derived
invalidation with no writes anywhere.

RBAC attachment point: :class:`AuthorityPolicy` is the single seam where a
future authorization subsystem attaches. This slice implements no policy — the
default policy records the attachment and allows — and
``approver.authority_ref`` is present but must be null.

PR #138 is the communication channel for this work; it is never the approval
state store. The store is this immutable ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol

from factory_control_plane.digests import verify_digest
from factory_control_plane.findings import (
    CODE_ACKNOWLEDGEMENT_MISMATCH,
    CODE_APPROVAL_NOT_EFFECTIVE,
    CODE_ERROR_ACKNOWLEDGED,
    errors_of,
    warning_ids,
)
from factory_control_plane.registry_reference import component_reference_errors
from factory_control_plane.support import settle, successor_sequence
from factory_control_plane.validation import (
    SCHEMA_APPROVAL,
    ControlPlaneError,
    approval_record_errors,
)

#: Decisions a *builder* may take directly. ``revoked`` is appended through
#: :func:`new_approval_revocation`, so it can never be fabricated as a grant.
GRANT = "granted"
REJECT = "rejected"
REVOKE = "revoked"

_BUILDER_DECISIONS = (GRANT, REJECT)


class ApprovalError(ControlPlaneError):
    """An approval action violates the ledger contract (fail closed)."""


class ApprovalNotAuthorizedError(ApprovalError):
    """The attached authority policy refused the action (RBAC seam)."""


@dataclass(frozen=True)
class AuthorityDecision:
    """The answer of an authority policy: allowed, or refused with a reason."""

    allowed: bool
    reason: str | None = None


class AuthorityPolicy(Protocol):
    """The RBAC attachment point: the only place authorization is decided.

    A future authorization subsystem implements this protocol (roles, sessions,
    delegation); this slice only calls it. The policy receives the approver
    reference, the action, the approval id being created and the exact
    configuration reference — never a mutable session or a credential.
    """

    def authorize(
        self,
        *,
        approver: Mapping[str, Any],
        action: str,
        approval_id: str,
        configuration_ref: Mapping[str, Any],
    ) -> AuthorityDecision: ...


class AllowAllAuthority:
    """Default Slice 2 policy: records the attachment point, decides nothing.

    It implements the interface only so the seam is real and testable; no roles,
    no policy engine and no identity provider exist in this slice.
    """

    policy_id = "control-plane/authority/allow-all/v1"

    def authorize(
        self,
        *,
        approver: Mapping[str, Any],
        action: str,
        approval_id: str,
        configuration_ref: Mapping[str, Any],
    ) -> AuthorityDecision:
        return AuthorityDecision(allowed=True)


def _freeze(value: Any) -> Any:
    """Recursively freeze a JSON-like value (mappings become read-only)."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def plain(value: Any) -> Any:
    """Return a JSON-serializable plain copy of a (possibly frozen) value."""
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def approval_reference(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``(approval_id, digest)`` reference of a record."""
    return {"approval_id": record["approval_id"], "digest": record["digest"]}


def _sequence_of(record: Mapping[str, Any]) -> int:
    tail = str(record["approval_id"]).rsplit("/", 1)[-1]
    if not tail.isdigit():
        message = f"approval id {record['approval_id']!r} has no numeric sequence"
        raise ApprovalError(message)
    return int(tail)


def _authorize(
    authority: AuthorityPolicy | None,
    *,
    approver: Mapping[str, Any],
    action: str,
    approval_id: str,
    configuration_ref: Mapping[str, Any],
) -> None:
    policy = authority if authority is not None else AllowAllAuthority()
    decision = policy.authorize(
        approver=dict(approver),
        action=action,
        approval_id=approval_id,
        configuration_ref=dict(configuration_ref),
    )
    if not decision.allowed:
        message = f"authority refused the {action}: {decision.reason}"
        raise ApprovalNotAuthorizedError(message)


def new_approval_record(
    *,
    project_ref: str,
    configuration: Mapping[str, Any],
    proposal: Mapping[str, Any],
    decision: str,
    approver: Mapping[str, Any],
    decided_at: str,
    reason: str,
    acknowledged_findings: Iterable[str] = (),
    predecessor: Mapping[str, Any] | None = None,
    authority: AuthorityPolicy | None = None,
) -> dict[str, Any]:
    """Create an immutable ``granted``/``rejected`` ledger record (fail closed).

    ``granted`` requires a ``ready`` proposal **and** an acknowledgement set
    equal to the proposal's warning set: a warning cannot proceed
    unacknowledged, and an ``error`` cannot be acknowledged at all.
    """
    if decision not in _BUILDER_DECISIONS:
        message = (
            f"decision must be one of {list(_BUILDER_DECISIONS)}; a revocation "
            "is a separate appended record"
        )
        raise ApprovalError(message)
    if configuration.get("project_ref") != project_ref:
        raise ApprovalError("the configuration version does not belong to this project")
    if proposal.get("project_ref") != project_ref:
        raise ApprovalError("the proposal does not belong to this project")
    configuration_ref = {
        "configuration_id": configuration.get("configuration_id"),
        "digest": configuration.get("digest"),
    }
    if proposal.get("configuration_ref") != configuration_ref:
        message = (
            "the proposal analyses a different configuration version than the "
            "one being approved"
        )
        raise ApprovalError(message)

    findings = [item for item in (proposal.get("findings") or [])]
    blocking = errors_of(findings)
    warning_set = warning_ids(findings)
    known_ids = {
        item["finding_id"]
        for item in findings
        if isinstance(item, Mapping) and isinstance(item.get("finding_id"), str)
    }
    acknowledged = sorted({str(item) for item in acknowledged_findings})
    unknown = [item for item in acknowledged if item not in known_ids]
    if unknown:
        message = (
            f"acknowledged_findings contains ids the proposal does not carry: {unknown}"
        )
        raise ApprovalError(message)
    acknowledged_errors = sorted(
        set(acknowledged) & {item["finding_id"] for item in blocking}
    )
    if acknowledged_errors:
        message = (
            f"{CODE_ERROR_ACKNOWLEDGED}: an 'error' finding cannot be "
            f"acknowledged and cannot be bypassed: {acknowledged_errors}"
        )
        raise ApprovalError(message)
    if decision == GRANT:
        if blocking:
            messages = "; ".join(str(item["message"]) for item in blocking)
            raise ApprovalError(
                f"cannot grant approval for a blocked proposal: {messages}"
            )
        if acknowledged != warning_set:
            message = (
                f"{CODE_ACKNOWLEDGEMENT_MISMATCH}: acknowledged findings "
                f"{acknowledged} differ from the proposal's warning set "
                f"{warning_set}; every warning needs an explicit acknowledgement"
            )
            raise ApprovalError(message)
    elif acknowledged:
        message = (
            "a rejection acknowledges nothing: acknowledged_findings must be empty"
        )
        raise ApprovalError(message)

    approval_id = (
        f"{project_ref}/approvals/"
        f"{successor_sequence(predecessor['approval_id'] if predecessor else None)}"
    )
    _authorize(
        authority,
        approver=approver,
        action="grant" if decision == GRANT else "reject",
        approval_id=approval_id,
        configuration_ref=configuration_ref,
    )
    document: dict[str, Any] = {
        "schema_version": SCHEMA_APPROVAL,
        "approval_id": approval_id,
        "project_ref": project_ref,
        "decision": decision,
        "configuration_ref": dict(configuration_ref),
        "proposal_ref": {
            "proposal_id": proposal["proposal_id"],
            "digest": proposal["digest"],
        },
        "requirements_ref": dict(proposal["requirements_ref"]),
        "acknowledged_findings": acknowledged,
        "approver": dict(approver),
        "decided_at": decided_at,
        "reason": reason,
        "revokes": None,
        "predecessor": (
            None
            if predecessor is None
            else {
                "approval_id": predecessor["approval_id"],
                "digest": predecessor["digest"],
            }
        ),
    }
    return settle(document, approval_record_errors, subject="approval record")


def new_approval_revocation(
    *,
    project_ref: str,
    revoked: Mapping[str, Any],
    approver: Mapping[str, Any],
    decided_at: str,
    reason: str,
    predecessor: Mapping[str, Any] | None = None,
    authority: AuthorityPolicy | None = None,
) -> dict[str, Any]:
    """Append a ``revoked`` record for ``revoked``; the grant stays untouched."""
    record = plain(revoked)
    problems = approval_record_errors(record)
    if problems:
        joined = "; ".join(problems)
        raise ApprovalError(f"cannot revoke an invalid approval record: {joined}")
    if not verify_digest(record):
        message = "the record to revoke does not match its declared digest"
        raise ApprovalError(message)
    if record.get("decision") == REVOKE:
        raise ApprovalError("a revocation cannot be revoked")
    if record.get("project_ref") != project_ref:
        raise ApprovalError("the record to revoke belongs to a different project")
    approval_id = (
        f"{project_ref}/approvals/"
        f"{successor_sequence(predecessor['approval_id'] if predecessor else None)}"
    )
    _authorize(
        authority,
        approver=approver,
        action="revoke",
        approval_id=approval_id,
        configuration_ref=record["configuration_ref"],
    )
    document: dict[str, Any] = {
        "schema_version": SCHEMA_APPROVAL,
        "approval_id": approval_id,
        "project_ref": project_ref,
        "decision": REVOKE,
        "configuration_ref": dict(record["configuration_ref"]),
        "proposal_ref": dict(record["proposal_ref"]),
        "requirements_ref": dict(record["requirements_ref"]),
        "acknowledged_findings": [],
        "approver": dict(approver),
        "decided_at": decided_at,
        "reason": reason,
        "revokes": {
            "approval_id": record["approval_id"],
            "digest": record["digest"],
        },
        "predecessor": (
            None
            if predecessor is None
            else {
                "approval_id": predecessor["approval_id"],
                "digest": predecessor["digest"],
            }
        ),
    }
    return settle(document, approval_record_errors, subject="approval revocation")


def approval_ineffectiveness_reasons(
    record: Mapping[str, Any],
    *,
    configuration: Mapping[str, Any] | None = None,
    proposal: Mapping[str, Any] | None = None,
    ledger: ApprovalLedger | None = None,
) -> list[str]:
    """Return every deterministic reason ``record`` is not effective.

    An empty list means effective. Nothing here is stored state: the answer is
    derived from the referenced documents, the live registry and the ledger.
    """
    document = plain(record)
    if not isinstance(document, Mapping):
        return [f"{CODE_APPROVAL_NOT_EFFECTIVE}: the record is not an object"]
    reasons: list[str] = []
    if document.get("decision") != GRANT:
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: decision is "
            f"{document.get('decision')!r}, not 'granted'"
        )
    if not verify_digest(document):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the approval record does not match "
            "its declared digest"
        )
    if ledger is not None:
        for later in ledger.records_after(document):
            revokes = plain(later).get("revokes") or {}
            if revokes.get("approval_id") == document.get(
                "approval_id"
            ) and revokes.get("digest") == document.get("digest"):
                reasons.append(
                    f"{CODE_APPROVAL_NOT_EFFECTIVE}: revoked by "
                    f"{later.get('approval_id')!r}"
                )
    reference = document.get("configuration_ref") or {}
    if configuration is None:
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the referenced configuration "
            "version is not available"
        )
    else:
        if configuration.get("configuration_id") != reference.get("configuration_id"):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: configuration_id "
                f"{configuration.get('configuration_id')!r} is not the approved "
                f"{reference.get('configuration_id')!r}"
            )
        if configuration.get("digest") != reference.get("digest"):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: configuration digest "
                f"{configuration.get('digest')!r} differs from the approved "
                f"{reference.get('digest')!r} — any technical change creates a "
                "new version and requires a new approval"
            )
        if not verify_digest(configuration):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the configuration document does "
                "not match its declared digest"
            )
        drift = component_reference_errors(configuration.get("components"))
        if drift:
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the canonical Component Registry "
                "no longer admits the approved references: " + "; ".join(drift)
            )
    proposal_reference = document.get("proposal_ref") or {}
    if proposal is None:
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the referenced proposal is not available"
        )
    else:
        if proposal.get("proposal_id") != proposal_reference.get(
            "proposal_id"
        ) or proposal.get("digest") != proposal_reference.get("digest"):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the proposal is not the analysed "
                "proposal (id/digest mismatch)"
            )
        if not verify_digest(proposal):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the proposal document does not "
                "match its declared digest"
            )
        findings = proposal.get("findings") or []
        if errors_of(findings):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the proposal carries blocking "
                "errors — an error can never be acknowledged or bypassed"
            )
        acknowledged = list(document.get("acknowledged_findings") or [])
        expected = warning_ids(findings)
        if acknowledged != expected:
            reasons.append(
                f"{CODE_ACKNOWLEDGEMENT_MISMATCH}: acknowledged findings "
                f"{acknowledged} differ from the proposal's warning set {expected}"
            )
    return sorted(set(reasons))


def approval_effective(
    record: Mapping[str, Any],
    *,
    configuration: Mapping[str, Any] | None = None,
    proposal: Mapping[str, Any] | None = None,
    ledger: ApprovalLedger | None = None,
) -> bool:
    """True when every derived condition of effectiveness holds."""
    return not approval_ineffectiveness_reasons(
        record, configuration=configuration, proposal=proposal, ledger=ledger
    )


def verify_ledger(
    records: Iterable[Mapping[str, Any]], *, project_ref: str | None = None
) -> list[str]:
    """Verify a loaded ledger chain: contracts, digests and predecessor links."""
    errors: list[str] = []
    previous: dict[str, Any] | None = None
    for index, record in enumerate(records):
        document = plain(record)
        problems = approval_record_errors(document)
        errors.extend(f"records[{index}]: {problem}" for problem in problems)
        if not verify_digest(document):
            errors.append(f"records[{index}]: the record digest does not verify")
        if project_ref is not None and document.get("project_ref") != project_ref:
            errors.append(
                f"records[{index}]: the record belongs to project "
                f"{document.get('project_ref')!r}, not {project_ref!r}"
            )
        expected = (
            None
            if previous is None
            else {"approval_id": previous["approval_id"], "digest": previous["digest"]}
        )
        if document.get("predecessor") != expected:
            errors.append(
                f"records[{index}]: the predecessor link is broken (expected "
                f"{expected}, found {document.get('predecessor')})"
            )
        previous = document
    return errors


#: Resolver used by the ledger's audit queries: ``kind`` is ``"configuration"``
#: or ``"proposal"``, ``ref`` is the typed reference, the answer is the document
#: or ``None`` when it is not available.
DocumentResolver = Callable[[str, Mapping[str, Any]], "Mapping[str, Any] | None"]


@dataclass(frozen=True)
class ApprovalLedger:
    """An append-only approval ledger for one project (immutable object).

    The ledger holds frozen record mappings and exposes no update or delete
    operation. ``append`` returns a *new* ledger; the previous one — and every
    record it holds — stays byte-identical.
    """

    project_ref: str
    records: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def empty(cls, project_ref: str) -> ApprovalLedger:
        """Return an empty ledger for ``project_ref``."""
        return cls(project_ref=project_ref)

    @classmethod
    def load(
        cls, project_ref: str, records: Iterable[Mapping[str, Any]]
    ) -> ApprovalLedger:
        """Build a ledger by re-validating and re-appending every record."""
        ledger = cls.empty(project_ref)
        for record in records:
            ledger = ledger.append(record)
        return ledger

    @property
    def approval_id(self) -> str:
        """Ledger identity: ``<project>/approvals``."""
        return f"{self.project_ref}/approvals"

    def head(self) -> Mapping[str, Any] | None:
        """Return the newest record, or ``None`` for an empty ledger."""
        return self.records[-1] if self.records else None

    def head_ref(self) -> dict[str, Any] | None:
        """Return the predecessor reference a new record must declare."""
        head = self.head()
        return None if head is None else approval_reference(head)

    def documents(self) -> tuple[dict[str, Any], ...]:
        """Return plain (JSON-serializable) copies of every record, in order."""
        return tuple(plain(record) for record in self.records)

    def index_of(self, approval_id: str) -> int | None:
        """Return the chain position of ``approval_id``, or ``None``."""
        for index, record in enumerate(self.records):
            if record.get("approval_id") == approval_id:
                return index
        return None

    def records_after(
        self, record_or_id: Mapping[str, Any] | str
    ) -> tuple[Mapping[str, Any], ...]:
        """Return every record appended after the given one (chain order)."""
        identifier = (
            record_or_id.get("approval_id")
            if isinstance(record_or_id, Mapping)
            else record_or_id
        )
        index = self.index_of(str(identifier))
        if index is None:
            return ()
        return self.records[index + 1 :]

    def append(self, record: Mapping[str, Any]) -> ApprovalLedger:
        """Return a new ledger with ``record`` appended (fail closed)."""
        document = plain(record)
        if not isinstance(document, Mapping):
            raise ApprovalError("an approval record must be an object")
        problems = approval_record_errors(document)
        if problems:
            joined = "; ".join(problems)
            raise ApprovalError(f"cannot append an invalid approval record: {joined}")
        if not verify_digest(document):
            message = "cannot append an approval record whose digest does not verify"
            raise ApprovalError(message)
        if str(document.get("approval_id")).rsplit("/", 1)[0] != self.approval_id:
            message = (
                f"record {document.get('approval_id')!r} does not belong to "
                f"ledger {self.approval_id!r}"
            )
            raise ApprovalError(message)
        if document.get("project_ref") != self.project_ref:
            message = (
                f"record belongs to project {document.get('project_ref')!r}, not "
                f"{self.project_ref!r}"
            )
            raise ApprovalError(message)
        expected = self.head_ref()
        if document.get("predecessor") != expected:
            message = (
                "append-only ledger: the record must declare the current head as "
                f"its predecessor (expected {expected}, found "
                f"{document.get('predecessor')})"
            )
            raise ApprovalError(message)
        head = self.head()
        if head is not None and _sequence_of(document) != _sequence_of(head) + 1:
            message = (
                "append-only ledger: sequence numbers must continue the chain "
                f"({_sequence_of(head)} -> {_sequence_of(document)})"
            )
            raise ApprovalError(message)
        return replace(self, records=(*self.records, _freeze(document)))

    def ineffectiveness_reasons(
        self,
        record_or_id: Mapping[str, Any] | str,
        resolve: DocumentResolver,
    ) -> list[str]:
        """Return the deterministic reasons one record is not effective."""
        identifier = (
            record_or_id.get("approval_id")
            if isinstance(record_or_id, Mapping)
            else record_or_id
        )
        index = self.index_of(str(identifier))
        if index is None:
            return [f"{CODE_APPROVAL_NOT_EFFECTIVE}: the record is not in this ledger"]
        document = plain(self.records[index])
        return approval_ineffectiveness_reasons(
            document,
            configuration=resolve("configuration", document["configuration_ref"]),
            proposal=resolve("proposal", document["proposal_ref"]),
            ledger=self,
        )

    def effective_records(
        self, resolve: DocumentResolver
    ) -> tuple[dict[str, Any], ...]:
        """Return the plain records whose derived effectiveness holds, in order."""
        return tuple(
            document
            for document in self.documents()
            if not self.ineffectiveness_reasons(document, resolve)
        )

    def audit_view(self, resolve: DocumentResolver) -> tuple[dict[str, Any], ...]:
        """Return a deterministic audit view of the whole ledger.

        One entry per record, ascending by sequence, carrying the decision, the
        exact references, what was acknowledged and the derived effectiveness
        with its reasons — the trace an auditor needs, reproduced identically
        on every run.
        """
        entries: list[dict[str, Any]] = []
        for document in self.documents():
            reasons = self.ineffectiveness_reasons(document, resolve)
            entries.append(
                {
                    "sequence": _sequence_of(document),
                    "approval_id": document["approval_id"],
                    "digest": document["digest"],
                    "decision": document["decision"],
                    "configuration_ref": document["configuration_ref"],
                    "proposal_ref": document["proposal_ref"],
                    "approver": document["approver"],
                    "decided_at": document["decided_at"],
                    "acknowledged_findings": document["acknowledged_findings"],
                    "effective": not reasons,
                    "ineffective_reasons": reasons,
                }
            )
        entries.sort(key=lambda entry: entry["sequence"])
        return tuple(entries)
