"""Owned data of the Learning component (logical schema `learning`).

This module is internal. No other component has direct access to it:
assignments, submissions and the audit journal are reachable only through
the published contract of this component (ARCHITECTURE.md §1.1, LAW-04).

The store separates the two ways its data is touched, and the separation
is the enforcement story of the component:

* :meth:`LearningStore.ownership_of_assignment` and
  :meth:`LearningStore.ownership_of_submission` — enforcement metadata:
  whether a record exists here and who its single owner is. The engine
  reads them to form the question it asks IS-003; they serve nothing to a
  caller;
* :meth:`LearningStore.serve_submission` — the owned-data operation
  itself. The engine calls it only after the enforcement chain produced
  an ``ALLOW``, and the tests count exactly these calls to prove that a
  denied request never reaches them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from learning_service.models import AccessAuditEvent, OwnedAssignment, OwnedSubmission

#: Submission lifecycle of this slice — exactly these two states. No
#: grading/review states exist.
SUBMISSION_STATES: tuple[str, ...] = ("DRAFT", "SUBMITTED")

#: Minimal Assignment lifecycle vocabulary of this slice.
ASSIGNMENT_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED", "ARCHIVED")


class DomainRefusal(Exception):
    """A domain-level refusal of an otherwise allowed operation.

    Two cases: the review command does not apply to the submission's
    current state (only ``SUBMITTED`` may be reviewed), and the review fact
    is immutable (an already reviewed submission cannot be reviewed again).
    It is raised only inside the owned-data step of the chain and is
    audited like every refusal.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class LearningStore:
    """In-memory owned storage of assignments, submissions and the audit journal."""

    assignments: dict[str, OwnedAssignment] = field(default_factory=dict)
    submissions: dict[str, OwnedSubmission] = field(default_factory=dict)
    audit: list[AccessAuditEvent] = field(default_factory=list)

    # -------------------------------------------------------------- ownership
    def register_assignment(self, assignment: OwnedAssignment) -> OwnedAssignment:
        """Register one owned assignment; ownership is singular and explicit."""
        owner = assignment.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("an assignment must have exactly one data owner")
        if not assignment.assignment_id or not str(assignment.assignment_id).strip():
            raise ValueError("an assignment must have an assignment_id")
        if not assignment.tenant_id or not str(assignment.tenant_id).strip():
            raise ValueError("an assignment must belong to exactly one tenant")
        if assignment.status not in ASSIGNMENT_STATES:
            raise ValueError(f"unknown assignment status {assignment.status!r}")
        if assignment.assignment_id in self.assignments:
            raise ValueError(
                f"assignment {assignment.assignment_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        self.assignments[assignment.assignment_id] = assignment
        return assignment

    def register_submission(self, submission: OwnedSubmission) -> OwnedSubmission:
        """Register one owned submission under its assignment.

        The submission's tenant must equal its assignment's tenant: a
        submission cannot belong to another Tenant than its Assignment.
        The assignment must already be registered — a submission without a
        known parent is refused outright.
        """
        owner = submission.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("a submission must have exactly one data owner")
        if not submission.submission_id or not str(submission.submission_id).strip():
            raise ValueError("a submission must have a submission_id")
        if submission.status not in SUBMISSION_STATES:
            raise ValueError(f"unknown submission status {submission.status!r}")
        if submission.submission_id in self.submissions:
            raise ValueError(
                f"submission {submission.submission_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        parent = self.assignments.get(submission.assignment_id)
        if parent is None:
            raise ValueError(
                f"assignment {submission.assignment_id!r} is unknown: "
                "a submission needs a registered parent"
            )
        if submission.tenant_id != parent.tenant_id:
            raise ValueError(
                "a submission must belong to the same tenant as its assignment"
            )
        if not isinstance(submission.attempt, int) or submission.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if not isinstance(submission.content, dict):
            raise ValueError("content must be an object")
        self.submissions[submission.submission_id] = submission
        return submission

    def ownership_of_assignment(self, assignment_id: str) -> OwnedAssignment | None:
        """Enforcement metadata for an assignment: existence and owner/tenant.

        Reading this is part of forming the authorization question, not an
        owned-data operation.
        """
        return self.assignments.get(assignment_id)

    def ownership_of_submission(self, submission_id: str) -> OwnedSubmission | None:
        """Enforcement metadata for a submission: existence and owner/tenant."""
        return self.submissions.get(submission_id)

    # ------------------------------------------------------------ owned data
    def serve_submission(self, submission_id: str) -> OwnedSubmission:
        """The owned-data single-read operation. Called only after an ALLOW."""
        return self.submissions[submission_id]

    def assert_reviewable(self, submission_id: str) -> OwnedSubmission:
        """The state rule of the review command, enforced before idempotency.

        Only a ``SUBMITTED`` submission may be reviewed; review never changes
        the lifecycle state. Raises ``KeyError`` when the submission vanished
        after the decision and :class:`DomainRefusal` when its state is not
        ``SUBMITTED``.
        """
        submission = self.submissions[submission_id]
        if submission.status != "SUBMITTED":
            raise DomainRefusal("invalid_state_transition")
        return submission

    def apply_review(
        self, submission_id: str, reviewed_by: str, reviewed_at: str
    ) -> OwnedSubmission:
        """The owned-data review effect. Called only after an ALLOW and an IS-005
        decision to execute.

        Records the review fact — the verified Teacher identity and the
        timestamp — and leaves the submission ``SUBMITTED``. The state rule
        and the immutability of the review fact are re-checked here as well
        (defence in depth): the store never applies a review to a
        non-``SUBMITTED`` submission and never overwrites an existing review
        fact, whatever the calling command's binding is.
        """
        submission = self.submissions[submission_id]
        if submission.status != "SUBMITTED":
            raise DomainRefusal("invalid_state_transition")
        if submission.reviewed_by is not None or submission.reviewed_at is not None:
            # The review fact is a one-time immutable fact: a second review
            # command with a different Idempotency-Key is a new command that
            # must not overwrite the first review. (An exact replay of the
            # first command never reaches this method — IS-005 returns the
            # recorded result.)
            raise DomainRefusal("already_reviewed")
        updated = replace(
            submission, reviewed_by=reviewed_by, reviewed_at=reviewed_at
        )
        self.submissions[submission_id] = updated
        return updated

    # -------------------------------------------------------------- demo data
    def seed_demo(self) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        Tenant identifiers are the demo ones of IS-002: this component does
        not define Tenants, it only states which Tenant each record belongs
        to. Identity references are the demo ones of IS-001 (opaque ids —
        no profile data is stored here). The subjects/permissions are the
        demo ones of IS-003, granted by the composition root, so the full
        decision chain is exercisable against this store.
        """
        self.assignments = {}
        self.submissions = {}
        self.audit = []

        def add_assignment(assignment_id: str, tenant_id: str, status: str = "PUBLISHED") -> None:
            self.register_assignment(
                OwnedAssignment(
                    assignment_id=assignment_id,
                    tenant_id=tenant_id,
                    owner_component="learning",
                    status=status,
                    created_at="2026-09-08T00:00:00+00:00",
                    updated_at="2026-09-08T00:00:00+00:00",
                )
            )

        def add_submission(
            submission_id: str,
            assignment_id: str,
            tenant_id: str,
            student_identity_id: str,
            attempt: int,
            status: str,
            content: dict[str, Any] | None = None,
        ) -> None:
            self.register_submission(
                OwnedSubmission(
                    submission_id=submission_id,
                    assignment_id=assignment_id,
                    tenant_id=tenant_id,
                    student_identity_id=student_identity_id,
                    attempt=attempt,
                    content=dict(content or {}),
                    status=status,
                    created_at="2026-09-08T00:00:00+00:00",
                    updated_at="2026-09-08T00:00:00+00:00",
                    owner_component="learning",
                )
            )

        add_assignment("asg_a1", "ten_a")
        add_assignment("asg_a2", "ten_a")
        add_assignment("asg_b1", "ten_b")

        add_submission("sub_a1_1", "asg_a1", "ten_a", "idn_human_c", 1, "SUBMITTED")
        add_submission("sub_a1_2", "asg_a1", "ten_a", "idn_human_c", 2, "DRAFT")
        add_submission("sub_a2_1", "asg_a2", "ten_a", "idn_human_c", 1, "SUBMITTED")
        add_submission("sub_b1_1", "asg_b1", "ten_b", "idn_human_b", 1, "SUBMITTED")
