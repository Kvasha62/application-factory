"""IS-006 Saga / Workflow Consistency Boundary — Platform Service (class B), Level 0.

This component proves the minimal Saga boundary required by
``docs/ARCHITECTURE.md`` §6.4: a multi-step business operation has explicit,
observable and bounded workflow state and always reaches a defined terminal
state — ``COMPLETED``, ``COMPENSATED`` or ``FAILED`` — after success, retry,
compensation or failure.

It is deliberately **not** a workflow platform. There is no broker, no worker
pool, no scheduler, no external engine, no distributed coordinator and no
event bus: one bounded in-memory store of workflow state and one deterministic
in-process executor (Issue #18, "Level 0 implementation allowance").

The component owns exactly one kind of data — the state of the workflows it
drives — and consumes exactly two boundaries:

* **IS-001** for the effective tenant context of a delivery. The tenant of a
  saga is resolved once from the verified identity, is immutable for the whole
  workflow and is re-verified on every delivery; IS-006 derives no tenant of its
  own (invariant 9);
* **IS-005** for the exactly-once delivery of every step effect and every
  compensation. Repeated delivery of a step replays instead of re-applying, and
  IS-006 implements no second idempotency mechanism (invariants 4 and 10).

Business steps are the callables a workflow declares; a composition root wires
them to the published contract of whichever component owns the data, so the
path is always ``Saga -> component contract -> component data owner``. This
component holds no store handle, session or internal type of any other
component and stores no copy of business data (invariant 12).

Published surface (see ``components/saga/contract/component_contract.json``)::

    SagaDefinition, StepDefinition, RetryPolicy   -- the workflow declaration
    SagaExecutor                                  -- start / run / deliver_step
    SagaState, StepState                          -- the explicit state model
    SagaSnapshot, StepSnapshot, FailureInfo       -- the published reads
    StepContext                                   -- what a step handler receives
    errors                                        -- the component's refusals
"""

from saga.errors import (
    IllegalStateTransition,
    SagaContextViolation,
    SagaDefinitionError,
    SagaError,
    SagaNotFound,
    SagaRefused,
    SagaStoreExhausted,
    SagaTerminalState,
    StepConflict,
    StepFailure,
    StepUnknown,
)
from saga.executor import SagaExecutor
from saga.models import (
    COMPONENT_CLASS,
    COMPONENT_ID,
    COMPONENT_VERSION,
    PHASE_COMPENSATE,
    PHASE_EXECUTE,
    SAGA_TRANSITIONS,
    STEP_TRANSITIONS,
    TERMINAL_SAGA_STATES,
    FailureInfo,
    FailureKind,
    RetryPolicy,
    SagaAuditEvent,
    SagaContext,
    SagaDefinition,
    SagaSnapshot,
    SagaState,
    StepContext,
    StepDefinition,
    StepSnapshot,
    StepState,
)
from saga.store import SagaStore

__all__ = [
    "COMPONENT_CLASS",
    "COMPONENT_ID",
    "COMPONENT_VERSION",
    "PHASE_COMPENSATE",
    "PHASE_EXECUTE",
    "SAGA_TRANSITIONS",
    "STEP_TRANSITIONS",
    "TERMINAL_SAGA_STATES",
    "FailureInfo",
    "FailureKind",
    "IllegalStateTransition",
    "RetryPolicy",
    "SagaAuditEvent",
    "SagaContext",
    "SagaContextViolation",
    "SagaDefinition",
    "SagaDefinitionError",
    "SagaError",
    "SagaExecutor",
    "SagaNotFound",
    "SagaRefused",
    "SagaSnapshot",
    "SagaState",
    "SagaStore",
    "SagaStoreExhausted",
    "SagaTerminalState",
    "StepConflict",
    "StepContext",
    "StepDefinition",
    "StepFailure",
    "StepSnapshot",
    "StepState",
    "StepUnknown",
]
