"""Owned data of the Resource Boundary component (logical schema `records`).

This module is internal. No other component has direct access to it: resources
and the audit journal are reachable only through the published contract of
this component (invariant 8, ARCHITECTURE.md §1.1, LAW-04).

The store separates the two ways its data is touched, and the separation is
the enforcement story of the component:

* :meth:`RecordsStore.ownership_of` — enforcement metadata: whether a resource
  exists here and who its single owner is. The engine reads it to form the
  question it asks IS-003; it serves nothing to a caller;
* :meth:`RecordsStore.serve_resource` and :meth:`RecordsStore.apply_transition`
  — the owned-data operations themselves. The engine calls them only after the
  enforcement chain produced an ``ALLOW`` (invariant 3), and the tests count
  exactly these calls to prove that a denied request never reaches them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from records_service.models import AccessAuditEvent, OwnedResource

#: States of the proof resource model and the closed transition map between
#: them. A transition names an operation of the state machine, not a target
#: state to assign: states are never written by hand.
STATES: tuple[str, ...] = ("draft", "active", "archived")

TRANSITIONS: dict[str, tuple[str, str]] = {
    "activate": ("draft", "active"),
    "archive": ("active", "archived"),
}


class DomainRefusal(Exception):
    """A domain-level refusal of an otherwise allowed operation.

    Currently one case: the requested transition does not apply to the
    resource's current state. It is raised only inside the owned-data step of
    the chain and is audited like every refusal.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class RecordsStore:
    """In-memory owned storage of proof resources and the access audit journal."""

    resources: dict[str, OwnedResource] = field(default_factory=dict)
    audit: list[AccessAuditEvent] = field(default_factory=list)

    # -------------------------------------------------------------- ownership
    def register(self, resource: OwnedResource) -> OwnedResource:
        """Register one owned resource; ownership is singular and explicit.

        A resource must have exactly one data owner, and a resource id cannot
        be re-owned or redefined here: the second registration of the same id
        is refused outright (invariant 1).
        """
        owner = resource.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("a resource must have exactly one data owner")
        if not resource.resource_id or not str(resource.resource_id).strip():
            raise ValueError("a resource must have a resource_id")
        if resource.resource_id in self.resources:
            raise ValueError(
                f"resource {resource.resource_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        self.resources[resource.resource_id] = resource
        return resource

    def ownership_of(self, resource_id: str) -> OwnedResource | None:
        """Enforcement metadata: what this owner knows about ``resource_id``.

        Reading this is part of forming the authorization question, not an
        owned-data operation: it returns nothing a caller could not have asked
        about, and no view of the resource crosses the boundary because of it.
        """
        return self.resources.get(resource_id)

    # ------------------------------------------------------------ owned data
    def serve_resource(self, resource_id: str) -> OwnedResource:
        """The owned-data read operation. Called only after an ALLOW.

        Raises ``KeyError`` when the resource vanished between the decision
        and the serving step — which the engine reports as a failure, never
        as a served resource.
        """
        return self.resources[resource_id]

    def apply_transition(self, resource_id: str, transition: str) -> OwnedResource:
        """The owned-data write operation. Called only after an ALLOW.

        The state machine is enforced here as well, not only in the engine:
        the final state of a resource is decided by its owner, and a
        transition that does not apply is a domain refusal, not a write.
        """
        resource = self.resources[resource_id]
        expected_from, target = TRANSITIONS[transition]
        if resource.state != expected_from:
            raise DomainRefusal("invalid_transition")
        updated = replace(resource, state=target)
        self.resources[resource_id] = updated
        return updated

    # -------------------------------------------------------------- demo data
    def seed_demo(self) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        Tenant identifiers are the demo ones of IS-002: this component does
        not define Tenants, it only states which Tenant each of its resources
        belongs to. One resource deliberately lives in a suspended Tenant and
        the subjects/permissions are the demo ones of IS-003, so the full
        decision chain is exercisable against this store.
        """
        self.resources = {}

        def add(resource_id: str, tenant_id: str, state: str) -> None:
            self.register(
                OwnedResource(
                    resource_id=resource_id,
                    resource_type="record",
                    owner_component="records",
                    tenant_id=tenant_id,
                    state=state,
                )
            )

        add("rec_a1", "ten_a", "active")
        add("rec_a2", "ten_a", "draft")
        add("rec_b1", "ten_b", "active")
        add("rec_b2", "ten_b", "draft")
        add("rec_s1", "ten_suspended", "active")
        self.audit = []
