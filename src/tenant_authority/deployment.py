"""Composition-internal deployment of the Tenant Authority component.

This module assembles the engine, the owned store and the published contract app
of one Platform Instance. It is **not** a consumer surface: a consumer is handed
a :class:`tenant_authority.reader.TenantAuthorityClient` (see
:meth:`TenantAuthorityDeployment.publish`). Reaching for the deployment object,
the engine or the store from another component is a boundary violation
(invariant T-009, ARCHITECTURE.md §1.1, LAW-04).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tenant_authority.config import TenantAuthorityConfig
from tenant_authority.engine import TenantAuthorityEngine
from tenant_authority.store import TenantAuthorityStore


@dataclass
class TenantAuthorityDeployment:
    """Owned internals of one Tenant Authority instance, for the composition root."""

    engine: TenantAuthorityEngine
    config: TenantAuthorityConfig
    store: TenantAuthorityStore
    http_app: Any = None

    @property
    def current_platform_id(self) -> str:
        return self.config.platform_id

    def contract_app(self) -> Any:
        """The published HTTP contract of this component (ASGI application)."""
        if self.http_app is None:
            from tenant_authority.api import create_app

            self.http_app = create_app(self)
        return self.http_app

    def publish(
        self,
        *,
        credential: str,
        expected_platform_id: str | None = None,
    ):
        """Hand a consumer the value-only client over the published contract.

        This is the supported way to give another component access to Tenant
        state: the consumer receives no engine, no store, no audit journal and no
        mutation operation — and its own service identity is what gets audited.
        """
        from tenant_authority.reader import build_client

        return build_client(
            self.contract_app(),
            credential=credential,
            expected_platform_id=expected_platform_id,
        )


def build_deployment(
    configuration: Mapping[str, Any],
    *,
    seed_demo: bool = False,
    store: TenantAuthorityStore | None = None,
    with_http: bool = False,
) -> TenantAuthorityDeployment:
    config = TenantAuthorityConfig.from_mapping(configuration)
    tenant_store = store or TenantAuthorityStore()
    engine = TenantAuthorityEngine(store=tenant_store, config=config)
    if seed_demo:
        tenant_store.seed_demo(config.platform_id, timestamp=engine.clock())
    deployment = TenantAuthorityDeployment(
        engine=engine, config=config, store=tenant_store, http_app=None
    )
    if with_http:
        deployment.contract_app()
    return deployment


def build_demo_deployment(platform_id: str = "plt_demo", *, environment: str = "standalone"):
    """Demo deployment of the standalone component (and of the Level 0 monolith)."""
    return build_deployment(
        {"platform_id": platform_id, "environment": environment},
        seed_demo=True,
        with_http=True,
    )
