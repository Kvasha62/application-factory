"""Error surface of the Saga / Workflow Consistency Boundary (IS-006).

Every error here is a workflow fact with a stable machine-readable reason, and
every one of them is audited with the correlation context of the saga. None of
them can be mistaken for progress: nothing in this module signals that a step
succeeded.

The vocabulary is deliberately small, because the component is deliberately
small:

* :class:`StepFailure` — the only error a step handler is expected to raise.
  It states *that* the step failed, whether the failure may be retried and
  whether it is security-sensitive; the reason string belongs to the component
  that refused or failed, and is stored unchanged;
* :class:`SagaRefused` / :class:`SagaContextViolation` — the tenant context
  could not be established, or a delivery presented another identity/tenant
  than the immutable one of the saga. Both are security-sensitive and both
  happen *before* any business effect;
* the rest are workflow-state refusals of the executor itself.

There is no permissive exception and no error type borrowed from another
component: a refusal of a business contract arrives as :class:`StepFailure`,
translated by the composition root that wired the step, so this component
depends on no foreign error vocabulary.
"""

from __future__ import annotations

from typing import Any


class SagaError(Exception):
    """Base class of the component's own refusals."""


class SagaDefinitionError(SagaError):
    """The workflow declaration is not usable (empty, ambiguous, incomplete)."""


class SagaNotFound(SagaError):
    """No such ``saga_id``: an unknown saga is never invented."""

    def __init__(self, saga_id: str) -> None:
        self.saga_id = saga_id
        self.reason = "saga_not_found"
        super().__init__(f"unknown saga_id {saga_id!r}")


class SagaTerminalState(SagaError):
    """The saga reached a terminal state and accepts no further delivery."""

    def __init__(self, saga_id: str, state: str) -> None:
        self.saga_id = saga_id
        self.state = state
        self.reason = "terminal_state"
        super().__init__(f"saga {saga_id!r} is terminal ({state})")


class SagaRefused(SagaError):
    """No saga was created: the effective tenant context could not be established.

    IS-001 is the source of the effective tenant context (invariant 9). When it
    refuses — unknown identity, tenant mismatch, tenant not servable — IS-006
    creates nothing at all: there is no workflow state without a verified
    tenant, and no fallback tenant of its own.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"tenant context refused: {reason}")


class SagaContextViolation(SagaError):
    """A delivery does not match the immutable context of the saga.

    Raised when the credential presented for a delivery resolves to another
    identity or another tenant than the one the saga was started with. The
    delivery is refused before any step runs, so no business effect happens.
    """

    def __init__(self, saga_id: str, reason: str) -> None:
        self.saga_id = saga_id
        self.reason = reason
        super().__init__(f"saga {saga_id!r}: {reason}")


class StepConflict(SagaError):
    """The same step identity was delivered with a different payload.

    This is the IS-005 conflict (its invariants I-003..I-005) surfaced at the
    workflow boundary: the step keeps the state it had, the effect is not
    executed, and the conflict is audited.
    """

    def __init__(
        self, saga_id: str, step_id: str, details: dict[str, Any] | None = None
    ) -> None:
        self.saga_id = saga_id
        self.step_id = step_id
        self.details = details or {}
        self.reason = "idempotency_conflict"
        super().__init__(
            f"step {step_id!r} of saga {saga_id!r} was already delivered with "
            "another context or payload"
        )


class StepUnknown(SagaError):
    """The saga does not declare that ``step_id``; steps are never added later."""

    def __init__(self, saga_id: str, step_id: str) -> None:
        self.saga_id = saga_id
        self.step_id = step_id
        self.reason = "step_unknown"
        super().__init__(f"saga {saga_id!r} declares no step {step_id!r}")


class IllegalStateTransition(SagaError):
    """An internal invariant was about to be violated, so nothing was written.

    Reaching this error means a bug in the executor, not a business outcome:
    the state machines of :mod:`saga.models` are closed, and a workflow that
    would be left in an undefined partial state is refused instead.
    """

    def __init__(self, message: str) -> None:
        self.reason = "illegal_state_transition"
        super().__init__(message)


class SagaStoreExhausted(SagaError):
    """The bounded in-memory saga store is full.

    The Level 0 store is bounded by design; a full store refuses a new workflow
    instead of growing without limit or evicting workflow state silently.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.reason = "saga_store_exhausted"
        super().__init__(f"the saga store is bounded to {limit} sagas")


class StepFailure(SagaError):
    """A step failed. This is the only failure a step handler is expected to raise.

    ``reason`` is a stable machine-readable string stated by the party that
    failed or refused (a business contract's published deny reason, a
    dependency code, ...). IS-006 stores it unchanged and never interprets it
    as a permission, a tenant or a lifecycle fact.

    ``retryable`` declares the recovery path: only a failure declared retryable
    is retried, and only within the step's declared :class:`saga.models.RetryPolicy`.
    A security refusal must not be marked retryable — repeating a denied
    operation is not recovery.

    ``security_sensitive`` marks refusals that must stay observable: tenant
    mismatch, missing permission, unknown identity. They are audited with the
    full correlation context (invariant 11).
    """

    def __init__(
        self,
        reason: str,
        *,
        retryable: bool = False,
        security_sensitive: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a step failure requires a machine-readable reason")
        self.reason = reason
        self.retryable = bool(retryable)
        self.security_sensitive = bool(security_sensitive)
        self.details: dict[str, Any] = details or {}
        super().__init__(reason)


__all__ = [
    "IllegalStateTransition",
    "SagaContextViolation",
    "SagaDefinitionError",
    "SagaError",
    "SagaNotFound",
    "SagaRefused",
    "SagaStoreExhausted",
    "SagaTerminalState",
    "StepConflict",
    "StepFailure",
    "StepUnknown",
]
