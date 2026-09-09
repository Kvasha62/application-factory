"""Published data contract of the Learning component (SCS-001).

This module publishes the whole vocabulary a consumer needs: the immutable
value an allowed access returns (:class:`SubmissionView`), the closed
reason sets a refusal may carry, and the error classes. Internal modules
(``engine``, ``store``, ``models``, ``api``, ``deployment``, ``transport``,
``ports``, ``adapters``) are not part of the contract and must not be
imported or reached through the published surface (ARCHITECTURE.md §1.1,
LAW-04).

The object a consumer receives is ``learning_service.reader.LearningClient``
— one read operation over the published API, values in and values out.

Two facts about the model are deliberate:

* :class:`SubmissionView` is the whole submission and nothing more: the
  ten fields of the business representation. A view is produced only by
  the owned-data operation at the end of the enforcement chain, so holding
  a view means the access was allowed — and a refusal can never be mistaken
  for one. No student profile/account data is carried: ``student_identity_id``
  is an opaque reference owned outside Learning;
* the published reason vocabulary is closed: three reasons of this
  component's own boundary plus the published denial reasons of IS-003
  passed through unchanged. A refusal outside that vocabulary cannot
  happen: a non-authoritative answer of the dependency is reported as
  ``authorization_unavailable`` and denied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from learning_service.consumed import DENY_REASONS as _DECISION_DENIALS
from learning_service.errors import AccessRefused, ConfigurationError, ContractViolation

#: This component: the single data owner of every record it serves.
OWNER_COMPONENT = "learning"

#: Resource types this component owns in this slice.
RESOURCE_TYPE_ASSIGNMENT = "assignment"
RESOURCE_TYPE_SUBMISSION = "submission"

#: The grant vocabulary the data owner asks IS-003 about. The single read
#: changes no state beyond the append-only audit journal; the review command
#: is the state-changing operation of Slice 3, guarded by IS-005.
OPERATION_READ = "learning.submissions.read"
OPERATION_REVIEW = "learning.submissions.review"


class OwnDenyReason:
    """Reasons of this component's own enforcement boundary (closed set).

    ``submission_unknown`` — no such submission is owned here.
    ``owner_mismatch`` — a record whose single owner is another component
    can never be served through this boundary.
    ``authorization_unavailable`` — the decision dependency did not answer,
    or answered outside its published contract: fail closed.
    ``invalid_state_transition`` — a domain refusal after an ALLOW: only a
    ``SUBMITTED`` submission may be reviewed, and review never changes the
    lifecycle state.
    ``already_reviewed`` — a domain refusal after an ALLOW: the review fact
    is immutable, so a review command against an already reviewed submission
    (a different command, i.e. a different ``Idempotency-Key``) can never
    overwrite ``reviewed_by`` / ``reviewed_at``.
    ``idempotency_key_required`` — a state-changing command was sent without
    the mandatory ``Idempotency-Key`` header.
    ``idempotency_conflict`` — the ``Idempotency-Key`` was already used with
    a different binding (identity, tenant, operation, target or command).
    """

    SUBMISSION_UNKNOWN = "submission_unknown"
    OWNER_MISMATCH = "owner_mismatch"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    ALREADY_REVIEWED = "already_reviewed"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


#: The closed set of own reasons, as values.
OWN_DENY_REASONS: frozenset[str] = frozenset(
    {
        OwnDenyReason.SUBMISSION_UNKNOWN,
        OwnDenyReason.OWNER_MISMATCH,
        OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
        OwnDenyReason.INVALID_STATE_TRANSITION,
        OwnDenyReason.ALREADY_REVIEWED,
        OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
        OwnDenyReason.IDEMPOTENCY_CONFLICT,
    }
)

#: Published denial reasons of IS-003 this boundary passes through unchanged
#: when the decision denies: the fact itself belongs to the authority.
PASSED_THROUGH_DENIALS: frozenset[str] = _DECISION_DENIALS

#: Every reason a published refusal may carry.
PUBLISHED_DENY_REASONS: frozenset[str] = OWN_DENY_REASONS | PASSED_THROUGH_DENIALS


@dataclass(frozen=True, slots=True)
class SubmissionView:
    """The published representation of one owned submission.

    Exactly the ten business fields — ``submission_id``,
    ``assignment_id``, ``student_identity_id``, ``attempt``, ``content``,
    ``status``, ``created_at``, ``updated_at``, ``reviewed_by``,
    ``reviewed_at`` — and nothing else. No tenant, owner or profile data is
    exposed: the view is the value an allowed access returns, immutable and
    granting nothing by existing. ``reviewed_by`` / ``reviewed_at`` are
    ``None`` until the first successful review and are the only review fact
    published.
    """

    submission_id: str
    assignment_id: str
    student_identity_id: str
    attempt: int
    content: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None


__all__ = [
    "OPERATION_READ",
    "OPERATION_REVIEW",
    "OWN_DENY_REASONS",
    "OwnDenyReason",
    "OWNER_COMPONENT",
    "PASSED_THROUGH_DENIALS",
    "PUBLISHED_DENY_REASONS",
    "RESOURCE_TYPE_ASSIGNMENT",
    "RESOURCE_TYPE_SUBMISSION",
    "AccessRefused",
    "ConfigurationError",
    "ContractViolation",
    "SubmissionView",
]
