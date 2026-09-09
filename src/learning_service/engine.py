"""Learning engine: the enforcement chain of the data owner.

This component is the final enforcement boundary for the Learning data it
owns (ARCHITECTURE.md §5.3, §6.2): IS-003 decides, and this component
applies the decision at its own boundary — including refusing when the
decision cannot be obtained at all. The chain is fixed, published, and
every step can only deny:

```text
Request (subject credential, record id, operation, [claimed tenant])
      ↓
ownership_boundary       the record exists here and its single owner is
                         this component                  else DENY
      ↓
authorization_decision   IS-003 decides through the port; the default
                         outcome is DENY; a dependency that does not answer
                         or answers outside its contract fails closed
      ↓
owned_data_operation     only now is the owned data read
```

An ``ALLOW`` is not data access: the owned-data operation runs only here,
at the end, and a request denied earlier never reaches it. Both the served
accesses and every refusal are audited with ``request_id`` and
``correlation_id``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from learning_service import COMPONENT_ID, COMPONENT_VERSION
from learning_service.config import LearningConfig
from learning_service.consumed import ALLOW, DENY, DENY_REASONS, PERMITTED, DependencyRefusal
from learning_service.contracts import (
    OPERATION_LIST,
    OPERATION_READ,
    OwnDenyReason,
    SubmissionView,
)
from learning_service.errors import AccessRefused
from learning_service.models import AccessAuditEvent, ObservabilityContext, OwnedSubmission
from learning_service.ports import AuthorizationPort
from learning_service.store import LearningStore

#: The published order of enforcement. Also used by the contract tests: a
#: change of order is a change of behaviour and must be visible.
ENFORCEMENT_CHAIN: tuple[str, ...] = (
    "ownership_boundary",
    "authorization_decision",
    "owned_data_operation",
)

#: The audit action of a request the published schema rejected before any
#: handler ran: an access attempt that could not even be read.
ACTION_UNREADABLE = "learning.access"

#: Identity-family denial reasons of IS-003 map to 401; every other denial
#: is an authorization refusal (403). An unmapped reason never falls through
#: to a permissive status.
_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _status_for(reason: str) -> int:
    """HTTP status of a refusal: 401 authentication, 404 unknown, 503
    fail-closed dependency, 403 everything else."""
    if reason in _AUTHENTICATION_DENIALS:
        return 401
    if reason in {
        OwnDenyReason.ASSIGNMENT_UNKNOWN,
        OwnDenyReason.SUBMISSION_UNKNOWN,
    }:
        return 404
    if reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE:
        return 503
    return 403


def _view_of(submission: OwnedSubmission) -> SubmissionView:
    return SubmissionView(
        submission_id=submission.submission_id,
        assignment_id=submission.assignment_id,
        student_identity_id=submission.student_identity_id,
        attempt=submission.attempt,
        content=dict(submission.content),
        status=submission.status,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
    )


@dataclass
class LearningEngine:
    """The enforcement boundary of Learning in one Platform Instance.

    It owns assignments, submissions and an access audit journal. It owns no
    identity, no permission, no tenant registry and no tenant state: the
    decision is read, per access, from the published contract of IS-003
    through the port.
    """

    store: LearningStore
    config: LearningConfig
    authorization: AuthorizationPort
    clock: Callable[[], str] = field(default=_utc_now)

    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (never a caller claim)."""
        return self.config.platform_id

    # ------------------------------------------------------------- operations
    def read_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[SubmissionView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned submission — and only after the full chain allowed it."""
        obs = self.observability(
            tenant_id=None, subject_id=None,
            request_id=request_id, correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        submission = self.store.ownership_of_submission(submission_id)
        if submission is None:
            raise self._refuse(
                OwnDenyReason.SUBMISSION_UNKNOWN,
                action=OPERATION_READ,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=submission_id,
                resource_tenant_id=None,
                assignment_id=None,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if submission.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=OPERATION_READ,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": submission.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=OPERATION_READ,
            resource_type="submission",
            resource_id=submission.submission_id,
            resource_tenant_id=submission.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=OPERATION_READ,
            assignment_id=submission.assignment_id,
            submission_id=submission.submission_id,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served = self.store.serve_submission(submission.submission_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.SUBMISSION_UNKNOWN,
                action=OPERATION_READ,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=submission_id,
                resource_tenant_id=submission.tenant_id,
                assignment_id=submission.assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        event = self.audit(
            action=OPERATION_READ,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=submission_id,
            resource_tenant_id=submission.tenant_id,
            assignment_id=submission.assignment_id,
            submission_id=submission.submission_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_view_of(served), obs, event)

    def list_submissions(
        self,
        subject_credential: str | None,
        assignment_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[list[SubmissionView], ObservabilityContext, AccessAuditEvent]:
        """List the submissions of one Assignment — teacher discovery.

        The same enforcement chain as the single read, applied to the
        Assignment: the record must be owned here before IS-003 is asked
        (an unknown assignment fails closed without a decision), and the
        grant question is ``learning.submissions.list`` against the
        Assignment's own tenant.
        """
        obs = self.observability(
            tenant_id=None, subject_id=None,
            request_id=request_id, correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        assignment = self.store.ownership_of_assignment(assignment_id)
        if assignment is None:
            raise self._refuse(
                OwnDenyReason.ASSIGNMENT_UNKNOWN,
                action=OPERATION_LIST,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=assignment_id,
                resource_tenant_id=None,
                assignment_id=assignment_id,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if assignment.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=OPERATION_LIST,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=assignment_id,
                resource_tenant_id=assignment.tenant_id,
                assignment_id=assignment_id,
                submission_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": assignment.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=OPERATION_LIST,
            resource_type="assignment",
            resource_id=assignment.assignment_id,
            resource_tenant_id=assignment.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=OPERATION_LIST,
            assignment_id=assignment.assignment_id,
            submission_id=None,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        served = self.store.list_submissions_for_assignment(assignment.assignment_id)

        event = self.audit(
            action=OPERATION_LIST,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=assignment_id,
            resource_tenant_id=assignment.tenant_id,
            assignment_id=assignment.assignment_id,
            submission_id=None,
            claimed_tenant_id=claimed_tenant_id,
        )
        return ([_view_of(item) for item in served], obs, event)

    def refuse_unreadable_request(
        self,
        *,
        path: str,
        schema_problems: list[str],
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AccessRefused:
        """Refuse — and audit — a request the published schema rejected."""
        obs = self.observability(
            tenant_id=None, subject_id=None,
            request_id=request_id, correlation_id=correlation_id,
        )
        exc = AccessRefused(
            "malformed_request",
            status_code=422,
            details={
                "refused": "malformed_request",
                "schema_problems": list(schema_problems),
                "path": path,
            },
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        self.audit(
            action=ACTION_UNREADABLE,
            decision=DENY,
            reason="malformed_request",
            obs=obs,
            subject_id=None,
            tenant_id=None,
            resource_id=None,
            resource_tenant_id=None,
            assignment_id=None,
            submission_id=None,
            claimed_tenant_id=None,
            details=dict(exc.details),
        )
        return exc

    # ------------------------------------------------------------------ chain
    def _decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        obs: ObservabilityContext,
        action: str,
        assignment_id: str | None,
        submission_id: str | None,
    ) -> tuple[ObservabilityContext, str | None, str | None]:
        """Ask the port and enforce the answer; returns the decided context.

        Raises the audited refusal for every outcome that is not an explicit
        authoritative ALLOW. The owned-data operation runs only after this
        returns.
        """
        try:
            answer = self.authorization.decide(
                subject_credential,
                operation=operation,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:
            code = getattr(exc, "reason", None)
            code = getattr(code, "value", code)
            answer = DependencyRefusal(code if isinstance(code, str) and code else None)
        if isinstance(answer, DependencyRefusal):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                assignment_id=assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_code": answer.reason_code},
            )
        if answer.decision == DENY and answer.reason in DENY_REASONS:
            raise self._refuse(
                answer.reason,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                assignment_id=assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if not (answer.decision == ALLOW and answer.reason == PERMITTED):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                assignment_id=assignment_id,
                submission_id=submission_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "nonauthoritative_answer": {
                        "decision": answer.decision,
                        "reason": answer.reason,
                    }
                },
            )
        decided_obs = self.observability(
            tenant_id=answer.tenant_id,
            subject_id=answer.subject_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        return (decided_obs, answer.subject_id, answer.tenant_id)

    # -------------------------------------------------------------- outcomes
    def _refuse(
        self,
        reason: str,
        *,
        action: str,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        assignment_id: str | None,
        submission_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessRefused:
        """Refuse one access attempt and audit the refusal on the way out."""
        self.audit(
            action=action,
            decision=DENY,
            reason=reason,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            assignment_id=assignment_id,
            submission_id=submission_id,
            claimed_tenant_id=claimed_tenant_id,
            details=details,
        )
        return AccessRefused(
            reason,
            status_code=_status_for(reason),
            details=details,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )

    # --------------------------------------------------------- observability
    def observability(
        self,
        *,
        tenant_id: str | None,
        subject_id: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ObservabilityContext:
        rid = request_id or _new_id("req")
        cid = correlation_id or rid
        return ObservabilityContext(
            timestamp=self.clock(),
            environment=self.config.environment,
            platform_id=self.config.platform_id,
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            request_id=rid,
            trace_id=rid,
            correlation_id=cid,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    def audit(
        self,
        *,
        action: str,
        decision: str,
        reason: str | None,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        assignment_id: str | None,
        submission_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessAuditEvent:
        event = AccessAuditEvent(
            event_id=_new_id("aud"),
            action=action,
            decision=decision,
            reason=reason,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            assignment_id=assignment_id,
            submission_id=submission_id,
            claimed_tenant_id=claimed_tenant_id,
            platform_id=obs.platform_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            timestamp=self.clock(),
            details=details or {},
        )
        self.store.audit.append(event)
        return event


__all__ = ["ACTION_UNREADABLE", "ENFORCEMENT_CHAIN", "LearningEngine"]
