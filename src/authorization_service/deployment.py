"""Composition-internal deployment of the Authorization Boundary component.

This module assembles the engine, the owned store and the published contract app
of one Platform Instance. It is **not** a consumer surface: a consumer is handed
an :class:`authorization_service.reader.AuthorizationClient` (see
:meth:`AuthorizationDeployment.publish`). Reaching for the deployment object, the
engine or the store from another component is a boundary violation (invariant 10,
ARCHITECTURE.md §1.1, LAW-04).

The ports of IS-001 and IS-002 are supplied from outside: this component composes
no other component, and it holds nothing of them but their published clients.
That is also why this module — unlike the standalone components — publishes no
module-level demo application: a demo of IS-003 requires a composed Platform
Instance, and composing one is the job of a composition root, never of a
component.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from authorization_service.config import AuthorizationConfig
from authorization_service.engine import AuthorizationEngine
from authorization_service.ports import IdentityContextPort, TenantAuthorityPort
from authorization_service.store import AuthorizationStore


@dataclass
class AuthorizationDeployment:
    """Owned internals of one Authorization Boundary instance, for the composition root."""

    engine: AuthorizationEngine
    config: AuthorizationConfig
    store: AuthorizationStore
    http_app: Any = None

    @property
    def current_platform_id(self) -> str:
        return self.config.platform_id

    def contract_app(self) -> Any:
        """The published HTTP contract of this component (ASGI application)."""
        if self.http_app is None:
            from authorization_service.api import create_app

            self.http_app = create_app(self)
        return self.http_app

    def publish(self, *, credential: str):
        """Hand a consumer the value-only client over the published contract.

        This is the supported way to give a data-owning component access to
        decisions: the consumer receives no engine, no grant store, no audit
        journal and no way to grant itself anything — and its own service
        identity is what gets audited.
        """
        from authorization_service.reader import build_client

        return build_client(self.contract_app(), credential=credential)


def build_deployment(
    configuration: Mapping[str, Any],
    *,
    identity: IdentityContextPort,
    tenant_authority: TenantAuthorityPort,
    seed_demo: bool = False,
    store: AuthorizationStore | None = None,
    with_http: bool = False,
) -> AuthorizationDeployment:
    """Assemble one Authorization Boundary over the published clients of IS-001/IS-002."""
    config = AuthorizationConfig.from_mapping(configuration)
    grant_store = store or AuthorizationStore()
    engine = AuthorizationEngine(
        store=grant_store,
        config=config,
        identity=identity,
        tenant_authority=tenant_authority,
    )
    if seed_demo:
        grant_store.seed_demo(config.platform_id)
    deployment = AuthorizationDeployment(
        engine=engine, config=config, store=grant_store, http_app=None
    )
    if with_http:
        deployment.contract_app()
    return deployment
