"""Composition-internal deployment of the Resource Boundary component.

This module assembles the engine, the owned store and the published contract
app of one data owner in one Platform Instance. It is **not** a consumer
surface: a consumer is handed a :class:`records_service.reader.RecordsClient`
(see :meth:`RecordsDeployment.publish`). Reaching for the deployment object,
the engine or the store from another component is a boundary violation
(invariant 8, ARCHITECTURE.md §1.1, LAW-04).

The port of IS-003 is supplied from outside: this component composes no other
component, and it holds nothing of it but the published client handed to the
adapter. That is also why this module publishes no module-level demo
application: a demo of a data owner needs a composed Platform Instance — the
decision dependency must exist — and composing one is the job of a
composition root, never of a component.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from records_service.config import RecordsConfig
from records_service.engine import RecordsEngine
from records_service.ports import AuthorizationPort
from records_service.store import RecordsStore


@dataclass
class RecordsDeployment:
    """Owned internals of one Resource Boundary instance, for the composition root."""

    engine: RecordsEngine
    config: RecordsConfig
    store: RecordsStore
    http_app: Any = None

    @property
    def current_platform_id(self) -> str:
        return self.config.platform_id

    def contract_app(self) -> Any:
        """The published HTTP contract of this component (ASGI application)."""
        if self.http_app is None:
            from records_service.api import create_app

            self.http_app = create_app(self)
        return self.http_app

    def publish(self) -> Any:
        """Hand a consumer the value-only client over the published contract.

        This is the supported way to give a consumer access to the owned
        resources: the consumer receives no engine, no store, no audit journal
        and no path around the enforcement chain — every access it makes is
        decided and audited exactly like an HTTP request.
        """
        from records_service.reader import build_client

        return build_client(self.contract_app())


def build_deployment(
    configuration: Mapping[str, Any],
    *,
    authorization: AuthorizationPort,
    seed_demo: bool = False,
    store: RecordsStore | None = None,
    with_http: bool = False,
) -> RecordsDeployment:
    """Assemble one Resource Boundary over the published client of IS-003."""
    config = RecordsConfig.from_mapping(configuration)
    resource_store = store or RecordsStore()
    engine = RecordsEngine(
        store=resource_store,
        config=config,
        authorization=authorization,
    )
    if seed_demo:
        resource_store.seed_demo()
    deployment = RecordsDeployment(
        engine=engine, config=config, store=resource_store, http_app=None
    )
    if with_http:
        deployment.contract_app()
    return deployment
