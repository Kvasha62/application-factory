"""Shared test fixtures.

The Level 0 modular monolith composes IS-001 (Identity / Tenant Context),
IS-002 (Tenant Authority) and IS-003 (Authorization Boundary). Each component
owns its store; every cross-component read goes through a published contract
client.

This module is also the composition root used by the tests: building a component
app from another component's deployment happens in the harness, never inside a
component's published API (see ``monolith()``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from authorization_service.contracts import AuthorizationDecision, ResourceRef
from authorization_service.deployment import (
    AuthorizationDeployment,
    build_deployment as build_authorization,
)
from authorization_service.reader import AuthorizationClient
from identity_service.api import create_app as create_identity_app
from identity_service.config import IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.reader import IdentityContextClient, build_client as build_identity_client
from identity_service.store import IdentityStore
from tenant_authority.contracts import TenantState
from tenant_authority.deployment import TenantAuthorityDeployment, build_deployment
from tenant_authority.lifecycle import LIFECYCLE_CHAIN
from tenant_authority.reader import TenantAuthorityClient

PLATFORM_ID = "plt_demo"
DEMO_CONFIG = {"platform_id": PLATFORM_ID, "environment": "test"}
#: Demo service identity of a consumer component: contract reads and nothing else.
CONSUMER_CREDENTIAL = "svc-token-identity"
#: Service identity IS-003 uses when it reads Tenant lifecycle from IS-002.
AUTHORIZATION_TA_CREDENTIAL = "svc-token-authorization"
#: Demo service identity of a data owner allowed to ask IS-003 for decisions.
DATA_OWNER_CREDENTIAL = "authz-svc-token-records"


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
    """One composed Platform Instance: the published APIs over one deployment."""

    identity: IdentityEngine
    authority: TenantAuthorityDeployment
    authorization: AuthorizationDeployment
    tenant_authority_client: TenantAuthorityClient
    identity_context_client: IdentityContextClient
    authorization_client: AuthorizationClient
    identity_app: Any
    authority_app: Any
    authorization_app: Any

    def identity_client(self) -> TestClient:
        return TestClient(self.identity_app)

    def authority_client(self) -> TestClient:
        return TestClient(self.authority_app)

    def authorization_http(self) -> TestClient:
        return TestClient(self.authorization_app)

    def data_owner(self, records: dict[str, str] | None = None) -> "RecordsBoundary":
        """A demo data owner enforcing IS-003 decisions at its own boundary."""
        return RecordsBoundary(
            authorization=self.authorization_client,
            records=dict(records or {"rec_a1": "ten_a", "rec_b1": "ten_b"}),
        )


def monolith() -> Monolith:
    """Compose the three components for the duration of one test.

    Fresh instances every call: a test that suspends a Tenant must not disturb
    the demo state another test relies on.
    """
    authority = tenant_authority()
    store = IdentityStore()
    store.seed_demo()
    identity_config = IdentityConfig.from_mapping(
        {
            # All components are bound to the same Platform Instance; a mismatch
            # would be denied by the contract cross-check, not ignored.
            "current_platform_id": authority.current_platform_id,
            "environment": authority.config.environment,
        }
    )
    client = client_for(authority, expected_platform_id=identity_config.current_platform_id)
    identity = IdentityEngine(store=store, tenant_authority=client, config=identity_config)
    identity_app = create_identity_app(identity)

    # IS-003 consumes both published contracts and owns neither: it receives a
    # value-only client of each, with its own service identity where the provider
    # authenticates one.
    identity_context_client = build_identity_client(identity_app)
    authorization_authority_client = client_for(
        authority,
        AUTHORIZATION_TA_CREDENTIAL,
        expected_platform_id=identity_config.current_platform_id,
    )
    authorization = build_authorization(
        {
            "platform_id": authority.current_platform_id,
            "environment": authority.config.environment,
        },
        identity=identity_context_client,
        tenant_authority=authorization_authority_client,
        seed_demo=True,
        with_http=True,
    )
    return Monolith(
        identity=identity,
        authority=authority,
        authorization=authorization,
        tenant_authority_client=client,
        identity_context_client=identity_context_client,
        authorization_client=authorization.publish(credential=DATA_OWNER_CREDENTIAL),
        identity_app=identity_app,
        authority_app=authority.contract_app(),
        authorization_app=authorization.contract_app(),
    )


class Refused(Exception):
    """A demo data owner's own refusal, carrying the reason it was given."""

    def __init__(self, decision: AuthorizationDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason.value)


@dataclass
class RecordsBoundary:
    """Demo data owner: it asks IS-003 and enforces the answer itself.

    This is the consumer side of the contract boundary (invariant 8). The
    component holds a value-only client, receives a value back and performs the
    refusal on its own: IS-003 never touches a record, and nothing it returns
    can serve data by itself.
    """

    authorization: AuthorizationClient
    records: dict[str, str] = field(default_factory=dict)

    def read(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        return self._access(
            subject_credential,
            record_id,
            operation="records.read",
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def write(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        return self._access(
            subject_credential,
            record_id,
            operation="records.write",
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def decision_for(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        operation: str = "records.read",
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AuthorizationDecision:
        return self.authorization.decide(
            subject_credential,
            operation=operation,
            resource=ResourceRef("record", record_id, self.records.get(record_id)),
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def _access(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        operation: str,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
    ) -> str:
        decision = self.decision_for(
            subject_credential,
            record_id,
            operation=operation,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not decision.allowed:
            # The data owner enforces: the authority only answered.
            raise Refused(decision)
        return f"{record_id}:{self.records[record_id]}"


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
