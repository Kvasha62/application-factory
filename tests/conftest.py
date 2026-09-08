"""Shared test fixtures.

The Level 0 modular monolith composes IS-001 (Identity / Tenant Context) over
IS-002 (Tenant Authority). Each component owns its store; identity reaches the
Tenant Registry only through the published contract client.

This module is also the composition root used by the tests: building a component
app from another component's deployment happens in the harness, never inside a
component's published API (see ``monolith()``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from identity_service.api import create_app as create_identity_app
from identity_service.config import IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.store import IdentityStore
from tenant_authority.contracts import TenantState
from tenant_authority.deployment import TenantAuthorityDeployment, build_deployment
from tenant_authority.lifecycle import LIFECYCLE_CHAIN
from tenant_authority.reader import TenantAuthorityClient

PLATFORM_ID = "plt_demo"
DEMO_CONFIG = {"platform_id": PLATFORM_ID, "environment": "test"}
#: Demo service identity of a consumer component: contract reads and nothing else.
CONSUMER_CREDENTIAL = "svc-token-identity"


def tenant_authority(seed_demo: bool = True) -> TenantAuthorityDeployment:
    """Standalone Tenant Authority deployment (IS-002 under test).

    The deployment is composition-internal: it is what the component's own tests
    and a composition root use. A consumer is handed :func:`client_for`.
    """
    return build_deployment(DEMO_CONFIG, seed_demo=seed_demo, with_http=True)


def client_for(
    authority: TenantAuthorityDeployment,
    credential: str = CONSUMER_CREDENTIAL,
    *,
    expected_platform_id: str | None = PLATFORM_ID,
) -> TenantAuthorityClient:
    """The published consumer surface of ``authority`` — values in, values out."""
    return authority.publish(
        credential=credential,
        expected_platform_id=expected_platform_id,
    )


@dataclass
class Monolith:
    """One composed Platform Instance: both published APIs over one deployment."""

    identity: IdentityEngine
    authority: TenantAuthorityDeployment
    tenant_authority_client: TenantAuthorityClient
    identity_app: Any
    authority_app: Any

    def identity_client(self) -> TestClient:
        return TestClient(self.identity_app)

    def authority_client(self) -> TestClient:
        return TestClient(self.authority_app)


def monolith() -> Monolith:
    """Compose Identity over Tenant Authority for the duration of one test.

    Fresh instances every call: a test that suspends a Tenant must not disturb
    the demo state another test relies on.
    """
    authority = tenant_authority()
    store = IdentityStore()
    store.seed_demo()
    identity_config = IdentityConfig.from_mapping(
        {
            # Both components are bound to the same Platform Instance; a mismatch
            # would be denied by the contract cross-check, not ignored.
            "current_platform_id": authority.current_platform_id,
            "environment": authority.config.environment,
        }
    )
    client = client_for(authority, expected_platform_id=identity_config.current_platform_id)
    identity = IdentityEngine(store=store, tenant_authority=client, config=identity_config)
    return Monolith(
        identity=identity,
        authority=authority,
        tenant_authority_client=client,
        identity_app=create_identity_app(identity),
        authority_app=authority.contract_app(),
    )


def composed() -> tuple[IdentityEngine, TenantAuthorityDeployment]:
    """Identity engine wired to its own Tenant Authority through the contract."""
    instance = monolith()
    return instance.identity, instance.authority


def engine() -> IdentityEngine:
    return composed()[0]


def provision_tenant(
    authority: TenantAuthorityDeployment,
    name: str,
    state: TenantState = TenantState.PROVISIONING,
) -> str:
    """Create a Tenant and bring it to ``state`` using only accepted transitions.

    States are never assigned by hand: a test that fakes a lifecycle state would
    not prove that the state machine works.
    """
    tenant_id = f"ten_{name}"
    authority.engine.create_tenant("svc-token-admin", tenant_id=tenant_id)
    for next_state in LIFECYCLE_CHAIN[1 : LIFECYCLE_CHAIN.index(state) + 1]:
        authority.engine.transition_tenant("svc-token-admin", tenant_id, next_state)
    assert authority.engine.lookup(tenant_id).state is state
    return tenant_id
