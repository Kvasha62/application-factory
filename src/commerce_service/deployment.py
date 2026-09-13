"""Composition-internal deployment of the Commerce component.

This module assembles the engine, the owned store and the published
contract app of Commerce in one Platform Instance. It is **not** a
consumer surface: a consumer is handed a
:class:`commerce_service.reader.CommerceClient`
(see :meth:`CommerceDeployment.publish`). Reaching for the deployment
object, the engine or the store from another component is a boundary
violation (ARCHITECTURE.md §1.1, LAW-04).

The port of IS-003 is supplied from outside: this component composes no
other component, and it holds nothing of it but the published client
handed to the adapter. That is also why this module publishes no
module-level demo application: a demo of a data owner needs a composed
Platform Instance — the decision dependency must exist — and composing
one is the job of a composition root, never of a component.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from commerce_service.config import CommerceConfig
from commerce_service.engine import CommerceEngine
from commerce_service.ports import (
    AuthorizationPort,
    CommandSafetyPort,
    TenantContextPort,
)
from commerce_service.store import CommerceStore


@dataclass
class CommerceDeployment:
    """Owned internals of one Commerce instance, for the composition root."""

    engine: CommerceEngine
    config: CommerceConfig
    store: CommerceStore
    http_app: Any = None

    @property
    def current_platform_id(self) -> str:
        return self.config.platform_id

    def contract_app(self) -> Any:
        """The published HTTP contract of this component (ASGI application)."""
        if self.http_app is None:
            from commerce_service.api import create_app

            self.http_app = create_app(self)
        return self.http_app

    def publish(self) -> Any:
        """Hand a consumer the value-only client over the published contract.

        This is the supported way to give a consumer access to the owned
        data: the consumer receives no engine, no store, no audit journal
        and no path around the enforcement chain — every access it makes is
        decided and audited exactly like an HTTP request.
        """
        from commerce_service.reader import build_client

        return build_client(self.contract_app())


def build_deployment(
    configuration: Mapping[str, Any],
    *,
    authorization: AuthorizationPort,
    seed_demo: bool = False,
    store: CommerceStore | None = None,
    with_http: bool = False,
    idempotency: CommandSafetyPort | None = None,
    tenant_context: TenantContextPort | None = None,
) -> CommerceDeployment:
    """Assemble one Commerce boundary over the published client of IS-003.

    ``idempotency`` is the IS-005 command-safety port; when omitted the
    engine wires the IS-005 guard itself — no second idempotency mechanism.
    ``tenant_context`` is the IS-001 tenant-context port consumed by the
    create-product command, whose target does not exist yet; when omitted,
    that command fails closed (a port that is not wired is a dependency that
    cannot answer).
    """
    config = CommerceConfig.from_mapping(configuration)
    commerce_store = store or CommerceStore()
    engine = CommerceEngine(
        store=commerce_store,
        config=config,
        authorization=authorization,
        idempotency=idempotency,
        tenant_context=tenant_context,
    )
    if seed_demo:
        commerce_store.seed_demo()
    deployment = CommerceDeployment(
        engine=engine, config=config, store=commerce_store, http_app=None
    )
    if with_http:
        deployment.contract_app()
    return deployment
