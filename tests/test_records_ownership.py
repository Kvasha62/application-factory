"""Invariant 1 — every resource has exactly one data owner (IS-004, Issue #14).

Ownership is singular by construction and enforced at every step: the store
refuses a resource without an owner and refuses to re-own one, and the
enforcement chain refuses to serve anything whose single owner is not this
component. All proofs are behavioral: registrations raise, serving refuses,
owned-data counters stay at zero.
"""

from __future__ import annotations

import pytest

from records_service.consumed import DecisionAnswer
from records_service.contracts import OWNER_COMPONENT, OwnDenyReason
from records_service.errors import AccessRefused
from records_service.models import OwnedResource
from records_service.store import RecordsStore
from tests.conftest import (
    CountingRecordsStore,
    StubAuthorizationPort,
    monolith,
    records_deployment,
)

TOKEN_A = "token-human-a"

ALLOW = DecisionAnswer(
    decision="ALLOW", reason="permitted", subject_id="idn_human_a", tenant_id="ten_a"
)


def make(
    resource_id: str = "rec_x", owner: str = "records", tenant: str = "ten_a"
) -> OwnedResource:
    return OwnedResource(
        resource_id=resource_id,
        resource_type="record",
        owner_component=owner,
        tenant_id=tenant,
        state="active",
    )


def test_registration_requires_exactly_one_owner():
    store = RecordsStore()

    with pytest.raises(ValueError):
        store.register(make(owner=""))
    with pytest.raises(ValueError):
        store.register(make(owner="   "))

    # Singular: the same resource id cannot be registered twice — ownership
    # is not redefined, silently replaced, or shared.
    store.register(make("rec_x"))
    with pytest.raises(ValueError):
        store.register(make("rec_x"))
    with pytest.raises(ValueError):
        store.register(make("rec_x", owner="identity"))


def test_every_seeded_resource_has_exactly_one_owner_and_it_is_this_component():
    instance = monolith()
    resources = instance.records.store.resources
    assert resources, "the demo deployment owns resources"
    for resource in resources.values():
        assert resource.owner_component == OWNER_COMPONENT
        # One owner per resource: the field holds exactly one value, and the
        # registry is keyed by resource id — no duplicate ownership exists.
        assert isinstance(resource.owner_component, str)
    assert len({r.resource_id for r in resources.values()}) == len(resources)


def test_a_foreign_owner_resource_cannot_be_read_or_written():
    store = CountingRecordsStore()
    deployment = records_deployment(StubAuthorizationPort(ALLOW), store=store)
    store.resources["rec_foreign"] = make("rec_foreign", owner="identity")

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_resource(TOKEN_A, "rec_foreign")
    assert refused.value.reason == OwnDenyReason.OWNER_MISMATCH
    assert refused.value.status_code == 403

    with pytest.raises(AccessRefused):
        deployment.engine.transition_resource(TOKEN_A, "rec_foreign", "activate")

    assert store.served_reads == 0
    assert store.applied_transitions == 0


def test_ownership_is_checked_before_the_decision_is_asked():
    """A foreign-owner resource costs the authority no question."""
    port = StubAuthorizationPort(ALLOW)
    store = CountingRecordsStore()
    deployment = records_deployment(port, store=store)
    store.resources["rec_foreign"] = make("rec_foreign", owner="tenant_authority")

    with pytest.raises(AccessRefused):
        deployment.engine.read_resource(TOKEN_A, "rec_foreign")
    assert port.calls == []


def test_the_served_view_names_its_owner():
    """The published representation carries the ownership fact itself."""
    instance = monolith()
    view = instance.records_client().read_resource(TOKEN_A, "rec_a1")
    assert view.owner_component == OWNER_COMPONENT
