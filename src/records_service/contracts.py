"""Published data contract of the Resource Boundary component (IS-004).

This module publishes the whole vocabulary a consumer needs: the immutable
value an allowed access returns (:class:`ResourceView`), the closed reason
sets a refusal may carry, and the error classes. Internal modules
(``engine``, ``store``, ``models``, ``api``, ``deployment``, ``transport``,
``ports``, ``adapters``) are not part of the contract and must not be
imported or reached through the published surface (ARCHITECTURE.md §1.1,
LAW-04).

The object a consumer receives is ``records_service.reader.RecordsClient`` —
two operations over the published API, values in and values out.

Two facts about the model are deliberate:

* :class:`ResourceView` is the whole resource and nothing more than the
  resource: the five fields of the proof model. A view is produced only by
  the owned-data operation at the end of the enforcement chain, so holding a
  view means the access was allowed — and a refusal can never be mistaken for
  one;
* the published reason vocabulary is closed: three reasons of this
  component's own boundary plus the published denial reasons of IS-003 passed
  through unchanged. A refusal outside that vocabulary cannot happen: a
  non-authoritative answer of the dependency is reported as
  ``authorization_unavailable`` and denied.
"""

from __future__ import annotations

from dataclasses import dataclass

from records_service.consumed import DENY_REASONS as _DECISION_DENIALS
from records_service.errors import AccessRefused, ConfigurationError, ContractViolation

#: This component: the single data owner of every resource it serves.
OWNER_COMPONENT = "records"

#: The only resource type this component owns in this slice.
RESOURCE_TYPE = "record"

#: The single grant vocabulary the data owner asks IS-003 about.
OPERATION_READ = "records.read"
OPERATION_WRITE = "records.write"


class OwnDenyReason:
    """Reasons of this component's own enforcement boundary (closed set).

    ``resource_unknown`` — no such resource is owned here; the default
    outcome is DENY and nothing is invented about it.
    ``owner_mismatch`` — a resource whose single owner is another component
    can never be served through this boundary (invariant 1).
    ``authorization_unavailable`` — the decision dependency did not answer,
    or answered outside its published contract: fail closed (invariant 4).
    ``invalid_transition`` — a domain refusal after an ALLOW: the requested
    state transition does not apply to the resource's current state.
    """

    RESOURCE_UNKNOWN = "resource_unknown"
    OWNER_MISMATCH = "owner_mismatch"
    AUTHORIZATION_UNAVAILABLE = "authorization_unavailable"
    INVALID_TRANSITION = "invalid_transition"


#: The closed set of own reasons, as values.
OWN_DENY_REASONS: frozenset[str] = frozenset(
    {
        OwnDenyReason.RESOURCE_UNKNOWN,
        OwnDenyReason.OWNER_MISMATCH,
        OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
        OwnDenyReason.INVALID_TRANSITION,
    }
)

#: Published denial reasons of IS-003 this boundary passes through unchanged
#: when the decision denies: the fact itself belongs to the authority.
PASSED_THROUGH_DENIALS: frozenset[str] = _DECISION_DENIALS

#: Every reason a published refusal may carry.
PUBLISHED_DENY_REASONS: frozenset[str] = OWN_DENY_REASONS | PASSED_THROUGH_DENIALS


@dataclass(frozen=True, slots=True)
class ResourceView:
    """The published representation of one owned resource.

    Exactly the five fields of the proof resource model — ``resource_id``,
    ``resource_type``, ``owner_component``, ``tenant_id``, ``state`` — and
    nothing else: this slice is not a data platform. A view is immutable and
    grants nothing by existing; it is the value an allowed access returns.
    """

    resource_id: str
    resource_type: str
    owner_component: str
    tenant_id: str
    state: str


__all__ = [
    "OPERATION_READ",
    "OPERATION_WRITE",
    "OWN_DENY_REASONS",
    "OwnDenyReason",
    "OWNER_COMPONENT",
    "PASSED_THROUGH_DENIALS",
    "PUBLISHED_DENY_REASONS",
    "RESOURCE_TYPE",
    "AccessRefused",
    "ConfigurationError",
    "ContractViolation",
    "ResourceView",
]
