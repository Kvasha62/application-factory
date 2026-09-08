"""Shared test fixtures.

The Level 0 modular monolith composes IS-001 (Identity / Tenant Context) over
IS-002 (Tenant Authority). Each component keeps its own owned store; identity
reaches the Tenant Registry only through the published Tenant Authority port.
"""

from __future__ import annotations

from identity_service.config import IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.store import IdentityStore
from tenant_authority.contracts import TenantState
from tenant_authority.lifecycle import LIFECYCLE_CHAIN
from tenant_authority.runtime import TenantAuthorityRuntime, build_runtime

PLATFORM_ID = "plt_demo"
DEMO_CONFIG = {"platform_id": PLATFORM_ID, "environment": "test"}


def tenant_authority(seed_demo: bool = True) -> TenantAuthorityRuntime:
    """Standalone Tenant Authority runtime (IS-002 under test)."""
    return build_runtime(DEMO_CONFIG, seed_demo=seed_demo)


def composed() -> tuple[IdentityEngine, TenantAuthorityRuntime]:
    """Identity engine wired to its own Tenant Authority instance."""
    authority = tenant_authority()
    store = IdentityStore()
    store.seed_demo()
    identity_engine = IdentityEngine(
        store=store,
        tenant_authority=authority.reader,
        config=IdentityConfig.from_mapping(
            {
                "current_platform_id": authority.current_platform_id,
                "environment": authority.config.environment,
            }
        ),
    )
    return identity_engine, authority


def engine() -> IdentityEngine:
    return composed()[0]


def provision_tenant(
    authority,
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
