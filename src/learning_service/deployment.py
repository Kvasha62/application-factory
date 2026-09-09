"""Composition-internal deployment of the Learning component.

This module assembles the engine, the owned store and the published
contract app of Learning in one Platform Instance. It is **not** a
consumer surface: a consumer is handed a
:class:`learning_service.reader.LearningClient`
(see :meth:`LearningDeployment.publish`). Reaching for the deployment
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

from dataclasses import dataclass
from typing import Any, Mapping

from learning_service.config import LearningConfig
from learning_service.engine import LearningEngine
from learning_service.ports import (
    AuthorizationPort,
    CommandSafetyPort,
    TenantContextPort,
)
from learning_service.store import LearningStore


@dataclass
class LearningDeployment:
    """Owned internals of one Learning instance, for the composition root."""

    engine: LearningEngine
    config: LearningConfig
    store: LearningStore
    http_app: Any = None

    @property
    def current_platform_id(self) -> str:
        return self.config.platform_id

    def contract_app(self) -> Any:
        """The published HTTP contract of this component (ASGI application)."""
        if self.http_app is None:
            from learning_service.api import create_app

            self.http_app = create_app(self)
        return self.http_app

    def publish(self) -> Any:
        """Hand a consumer the value-only client over the published contract.

        This is the supported way to give a consumer access to the owned
        data: the consumer receives no engine, no store, no audit journal
        and no path around the enforcement chain — every access it makes is
        decided and audited exactly like an HTTP request.
        """
        from learning_service.reader import build_client

        return build_client(self.contract_app())


def build_deployment(
    configuration: Mapping[str, Any],
    *,
    authorization: AuthorizationPort,
    seed_demo: bool = False,
    store: LearningStore | None = None,
    with_http: bool = False,
    idempotency: CommandSafetyPort | None = None,
    tenant_context: TenantContextPort | None = None,
) -> LearningDeployment:
    """Assemble one Learning boundary over the published client of IS-003.

    ``idempotency`` is the IS-005 command-safety port; when omitted the
    engine wires the IS-005 guard itself — no second idempotency mechanism.
    ``tenant_context`` is the IS-001 tenant-context port consumed by the
    create-course command, whose target does not exist yet; when omitted,
    that command fails closed (a port that is not wired is a dependency that
    cannot answer).
    """
    config = LearningConfig.from_mapping(configuration)
    learning_store = store or LearningStore()
    engine = LearningEngine(
        store=learning_store,
        config=config,
        authorization=authorization,
        idempotency=idempotency,
        tenant_context=tenant_context,
    )
    if seed_demo:
        learning_store.seed_demo()
    deployment = LearningDeployment(
        engine=engine, config=config, store=learning_store, http_app=None
    )
    if with_http:
        deployment.contract_app()
    return deployment
