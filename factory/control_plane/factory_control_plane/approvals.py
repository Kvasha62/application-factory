"""Immutable, append-only approval ledger (S2-2) with gated warnings (S2-3).

An approval is an **act**, not a state: one immutable record bound to the exact
Requirements, Configuration and Proposal ``(id, digest)`` references it was
made against, to the exact project and to the warnings it knowingly accepted.
Records are append-only —
there is no update or delete path anywhere in this module, an undo is a new
``revoked`` record, and the ledger is a frozen object whose record mappings
cannot be mutated in place (attempting to does raise ``TypeError``).

Effectiveness is always **derived**, never stored: the exact Requirements,
Configuration, Project and Proposal must validate and cross-reference one
another; the complete Proposal must reproduce from those inputs; and the
Approval must be an exact member of a valid ledger. Its acknowledgement set
must still equal the Proposal's warning set, the canonical Component Registry
must still admit the approved references, and no later record may revoke it.
Any changed linked input therefore requires a new Proposal/Approval; registry
drift is a derived invalidation with no writes anywhere.

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
from pathlib import Path
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
from factory_control_plane.proposals import verify_proposal_version
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
    requirements: Mapping[str, Any],
    project: Mapping[str, Any],
    proposal: Mapping[str, Any],
    decision: str,
    approver: Mapping[str, Any],
    decided_at: str,
    reason: str,
    acknowledged_findings: Iterable[str] = (),
    predecessor: Mapping[str, Any] | None = None,
    proposal_predecessor: Mapping[str, Any] | None = None,
    root: Path | None = None,
    authority: AuthorityPolicy | None = None,
) -> dict[str, Any]:
    """Create an immutable ``granted``/``rejected`` ledger record (fail closed).

    ``granted`` first requires a Proposal exactly re-derived from the supplied
    Requirements, Configuration and Project documents. It then requires a
    ``ready`` proposal **and** an acknowledgement set equal to the proposal's
    warning set: a warning cannot proceed unacknowledged, and an ``error``
    cannot be acknowledged at all. The inputs are verification-only and do not
    change the serialized Approval contract.
    """
    if decision not in _BUILDER_DECISIONS:
        message = (
            f"decision must be one of {list(_BUILDER_DECISIONS)}; a revocation "
            "is a separate appended record"
        )
        raise ApprovalError(message)

    proposal_problems = verify_proposal_version(
        proposal,
        configuration=configuration,
        requirements=requirements,
        project=project,
        predecessor=proposal_predecessor,
        root=root,
    )
    if proposal_problems:
        joined = "; ".join(proposal_problems)
        raise ApprovalError(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: proposal/input chain failed "
            f"re-derivation: {joined}"
        )
    if project.get("project_id") != project_ref:
        raise ApprovalError("the supplied project does not match project_ref")
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
    requirements: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
    proposal: Mapping[str, Any] | None = None,
    proposal_predecessor: Mapping[str, Any] | None = None,
    ledger: ApprovalLedger | None = None,
    root: Path | None = None,
) -> list[str]:
    """Return every deterministic reason ``record`` is not effective.

    Effectiveness requires the complete typed input chain and an Approval that
    is an exact member of a valid ledger. Missing inputs, a digest that does not
    verify, or an internally consistent but unreproduced Proposal all fail
    closed. Nothing is stored here: the answer is derived on every call.
    """
    document = plain(record)
    if not isinstance(document, Mapping):
        return [f"{CODE_APPROVAL_NOT_EFFECTIVE}: the record is not an object"]
    reasons: list[str] = []

    record_problems = approval_record_errors(document)
    if record_problems:
        reasons.extend(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: invalid approval record: {problem}"
            for problem in record_problems
        )
    if not verify_digest(document):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the approval record does not match "
            "its declared digest"
        )
    if document.get("decision") != GRANT:
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: decision is "
            f"{document.get('decision')!r}, not 'granted'"
        )

    # The ledger is a required part of the proof, not an optional revocation
    # lookup. Revalidate even directly constructed ApprovalLedger instances.
    exact_member = False
    if not isinstance(ledger, ApprovalLedger):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: a verified ApprovalLedger is required"
        )
    else:
        if ledger.project_ref != document.get("project_ref"):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the ledger belongs to a different project"
            )
        ledger_problems = verify_ledger(
            ledger.documents(), project_ref=ledger.project_ref
        )
        reasons.extend(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: invalid ledger: {problem}"
            for problem in ledger_problems
        )
        identifier = document.get("approval_id")
        index = ledger.index_of(identifier) if isinstance(identifier, str) else None
        if index is None:
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the record is not in this ledger"
            )
        else:
            ledger_record = plain(ledger.records[index])
            exact_member = ledger_record == document
            if not exact_member:
                reasons.append(
                    f"{CODE_APPROVAL_NOT_EFFECTIVE}: the ledger does not contain "
                    "this exact approval id/digest and record content"
                )
            if exact_member:
                for later in ledger.records_after(identifier):
                    later_document = plain(later)
                    revokes = later_document.get("revokes") or {}
                    if revokes.get("approval_id") == document.get(
                        "approval_id"
                    ) and revokes.get("digest") == document.get("digest"):
                        reasons.append(
                            f"{CODE_APPROVAL_NOT_EFFECTIVE}: revoked by "
                            f"{later_document.get('approval_id')!r}"
                        )

    configuration_ref_value = document.get("configuration_ref")
    proposal_ref_value = document.get("proposal_ref")
    requirements_ref_value = document.get("requirements_ref")
    configuration_ref = (
        configuration_ref_value if isinstance(configuration_ref_value, Mapping) else {}
    )
    proposal_ref = proposal_ref_value if isinstance(proposal_ref_value, Mapping) else {}
    requirements_ref = (
        requirements_ref_value if isinstance(requirements_ref_value, Mapping) else {}
    )

    if not isinstance(configuration, Mapping):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the referenced configuration "
            "version is not available"
        )
    else:
        actual_configuration_ref = {
            "configuration_id": configuration.get("configuration_id"),
            "digest": configuration.get("digest"),
        }
        if actual_configuration_ref.get("configuration_id") != configuration_ref.get(
            "configuration_id"
        ):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: configuration_id "
                f"{actual_configuration_ref.get('configuration_id')!r} is not the "
                f"approved {configuration_ref.get('configuration_id')!r}"
            )
        if actual_configuration_ref.get("digest") != configuration_ref.get("digest"):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: configuration digest "
                f"{actual_configuration_ref.get('digest')!r} differs from the "
                f"approved {configuration_ref.get('digest')!r}"
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

    if not isinstance(requirements, Mapping):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the exact RequirementsVersion is required"
        )
    else:
        actual_requirements_ref = {
            "requirements_id": requirements.get("requirements_id"),
            "digest": requirements.get("digest"),
        }
        if actual_requirements_ref != requirements_ref:
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the supplied RequirementsVersion "
                "id/digest does not equal the Approval requirements_ref"
            )
        if not verify_digest(requirements):
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the RequirementsVersion digest "
                "does not verify"
            )

    if not isinstance(project, Mapping):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the exact Project document is required"
        )
    elif project.get("project_id") != document.get("project_ref"):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the supplied Project does not match "
            "the Approval project_ref"
        )

    if not isinstance(proposal, Mapping):
        reasons.append(
            f"{CODE_APPROVAL_NOT_EFFECTIVE}: the referenced Proposal is not available"
        )
    else:
        actual_proposal_ref = {
            "proposal_id": proposal.get("proposal_id"),
            "digest": proposal.get("digest"),
        }
        if actual_proposal_ref != proposal_ref:
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the proposal is not the analysed "
                "proposal (id/digest mismatch with Approval proposal_ref)"
            )
        proposal_problems = (
            verify_proposal_version(
                proposal,
                configuration=configuration,
                requirements=requirements,
                project=project,
                predecessor=proposal_predecessor,
                root=root,
            )
            if (
                isinstance(configuration, Mapping)
                and isinstance(requirements, Mapping)
                and isinstance(project, Mapping)
            )
            else ["complete Requirements/Configuration/Project inputs are required"]
        )
        if proposal_problems:
            reasons.append(
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: the Proposal is not reproducible "
                "from the exact typed inputs: " + "; ".join(proposal_problems)
            )
        else:
            findings = proposal.get("findings") or []
            if errors_of(findings):
                reasons.append(
                    f"{CODE_APPROVAL_NOT_EFFECTIVE}: the Proposal carries blocking "
                    "errors — an error can never be acknowledged or bypassed"
                )
            acknowledged = list(document.get("acknowledged_findings") or [])
            expected = warning_ids(findings)
            if acknowledged != expected:
                reasons.append(
                    f"{CODE_ACKNOWLEDGEMENT_MISMATCH}: acknowledged findings "
                    f"{acknowledged} differ from the Proposal warning set {expected}"
                )

    return sorted(set(reasons))


def approval_effective(
    record: Mapping[str, Any],
    *,
    configuration: Mapping[str, Any] | None = None,
    requirements: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
    proposal: Mapping[str, Any] | None = None,
    proposal_predecessor: Mapping[str, Any] | None = None,
    ledger: ApprovalLedger | None = None,
    root: Path | None = None,
) -> bool:
    """True only when the complete derived input and ledger proof holds."""
    return not approval_ineffectiveness_reasons(
        record,
        configuration=configuration,
        requirements=requirements,
        project=project,
        proposal=proposal,
        proposal_predecessor=proposal_predecessor,
        ledger=ledger,
        root=root,
    )


def verify_ledger(
    records: Iterable[Mapping[str, Any]], *, project_ref: str | None = None
) -> list[str]:
    """Verify record contracts, digests, sequence/predecessor links and revokes."""
    errors: list[str] = []
    previous: dict[str, Any] | None = None
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    revoked: set[tuple[str, str]] = set()
    expected_project = project_ref

    for index, record in enumerate(records):
        document = plain(record)
        if not isinstance(document, Mapping):
            errors.append(f"records[{index}]: the record must be a JSON object")
            continue

        problems = approval_record_errors(document)
        errors.extend(f"records[{index}]: {problem}" for problem in problems)
        if not verify_digest(document):
            errors.append(f"records[{index}]: the record digest does not verify")

        current_project = document.get("project_ref")
        if expected_project is None and isinstance(current_project, str):
            expected_project = current_project
        if expected_project is not None and current_project != expected_project:
            errors.append(
                f"records[{index}]: the record belongs to project "
                f"{current_project!r}, not {expected_project!r}"
            )

        expected_predecessor = (
            None
            if previous is None
            else {
                "approval_id": previous.get("approval_id"),
                "digest": previous.get("digest"),
            }
        )
        if document.get("predecessor") != expected_predecessor:
            errors.append(
                f"records[{index}]: the predecessor link is broken (expected "
                f"{expected_predecessor}, found {document.get('predecessor')})"
            )

        try:
            sequence = _sequence_of(document)
            expected_sequence = 1 if previous is None else _sequence_of(previous) + 1
            if sequence != expected_sequence:
                errors.append(
                    f"records[{index}]: sequence must continue the chain "
                    f"(expected {expected_sequence}, found {sequence})"
                )
        except (ApprovalError, KeyError, TypeError):
            errors.append(f"records[{index}]: approval_id has no valid sequence")

        if document.get("decision") == REVOKE:
            reference = document.get("revokes")
            if isinstance(reference, Mapping):
                target = (reference.get("approval_id"), reference.get("digest"))
                target_record = seen.get(target)
                if target_record is None:
                    errors.append(
                        f"records[{index}]: revocation target {target!r} is not an "
                        "exact earlier record in this ledger"
                    )
                elif target_record.get("decision") == REVOKE:
                    errors.append(
                        f"records[{index}]: a revocation cannot revoke another "
                        "revocation"
                    )
                elif target in revoked:
                    errors.append(
                        f"records[{index}]: the exact approval target was already revoked"
                    )
                else:
                    revoked.add(target)

        identifier = document.get("approval_id")
        digest = document.get("digest")
        if isinstance(identifier, str) and isinstance(digest, str):
            seen[(identifier, digest)] = dict(document)
        previous = dict(document)

    return errors


#: Resolver used by the ledger's audit queries. ``kind`` is one of
#: ``"requirements"``, ``"configuration"``, ``"project"`` or ``"proposal"``;
#: ``ref`` is the typed reference (the Project ref contains ``project_id``).
#: Return the document or ``None`` when it is unavailable.
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
        existing_problems = verify_ledger(
            self.documents(), project_ref=self.project_ref
        )
        if existing_problems:
            joined = "; ".join(existing_problems)
            raise ApprovalError(f"cannot append to an invalid ledger: {joined}")

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
        expected_sequence = 1 if head is None else _sequence_of(head) + 1
        if _sequence_of(document) != expected_sequence:
            message = (
                "append-only ledger: sequence numbers must continue the chain "
                f"(expected {expected_sequence}, found {_sequence_of(document)})"
            )
            raise ApprovalError(message)

        extended_records = (*self.documents(), dict(document))
        new_problems = verify_ledger(extended_records, project_ref=self.project_ref)
        if new_problems:
            joined = "; ".join(new_problems)
            raise ApprovalError(f"cannot append an invalid ledger record: {joined}")
        return replace(self, records=(*self.records, _freeze(document)))

    def ineffectiveness_reasons(
        self,
        record_or_id: Mapping[str, Any] | str,
        resolve: DocumentResolver,
        *,
        root: Path | None = None,
    ) -> list[str]:
        """Return the deterministic reasons one exact record is not effective."""
        ledger_problems = verify_ledger(self.documents(), project_ref=self.project_ref)
        if ledger_problems:
            return [
                f"{CODE_APPROVAL_NOT_EFFECTIVE}: invalid ledger: "
                + "; ".join(ledger_problems)
            ]
        identifier = (
            record_or_id.get("approval_id")
            if isinstance(record_or_id, Mapping)
            else record_or_id
        )
        index = self.index_of(str(identifier))
        if index is None:
            return [f"{CODE_APPROVAL_NOT_EFFECTIVE}: the record is not in this ledger"]
        document = plain(self.records[index])
        if isinstance(record_or_id, Mapping) and plain(record_or_id) != document:
            return [
                (
                    f"{CODE_APPROVAL_NOT_EFFECTIVE}: the supplied record is not the "
                    "exact immutable ledger member"
                )
            ]

        proposal = resolve("proposal", document["proposal_ref"])
        predecessor = None
        if isinstance(proposal, Mapping) and isinstance(
            proposal.get("predecessor"), Mapping
        ):
            predecessor = resolve("proposal", proposal["predecessor"])
        return approval_ineffectiveness_reasons(
            document,
            configuration=resolve("configuration", document["configuration_ref"]),
            requirements=resolve("requirements", document["requirements_ref"]),
            project=resolve("project", {"project_id": document["project_ref"]}),
            proposal=proposal,
            proposal_predecessor=predecessor,
            ledger=self,
            root=root,
        )

    def effective_records(
        self, resolve: DocumentResolver, *, root: Path | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return the plain records whose derived effectiveness holds, in order."""
        return tuple(
            document
            for document in self.documents()
            if not self.ineffectiveness_reasons(document, resolve, root=root)
        )

    def audit_view(
        self, resolve: DocumentResolver, *, root: Path | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return a deterministic audit view of the whole ledger.

        One entry per record, ascending by sequence, carrying the decision, the
        exact references, what was acknowledged and the derived effectiveness
        with its reasons — the trace an auditor needs, reproduced identically
        on every run.
        """
        entries: list[dict[str, Any]] = []
        for document in self.documents():
            reasons = self.ineffectiveness_reasons(document, resolve, root=root)
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
