"""Deployment of the Learning component — composition-internal assembly.

The deployment object assembles engine, business store, IS-005 guard and the
contract application for the composition root and for this component's own
tests. It is never handed to a consumer: a consumer receives the value-only
client from :meth:`LearningDeployment.publish`, and nothing else of this
component crosses the boundary.

The ports are wired by the composition root from the published consumer
surfaces of IS-001 and IS-003 through :mod:`learning_service.adapters`; the
IS-005 guard is the published :class:`idempotency.guard.IdempotencyGuard`
itself, auditing into this component's own journal. No module of Learning
imports ``identity_service`` or ``authorization_service``, so the dependency
is on the published contracts, not on any provider's internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from idempotency.guard import IdempotencyGuard

from learning_service.config import LearningConfig
from learning_service.engine import LearningEngine
from learning_service.errors import ConfigurationError
from learning_service.ports import AuthorizationPort, IdentityContextPort
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

        This is the supported way to give a consumer access to the business
        operations: the consumer receives no engine, no store, no audit
        journal and no path around the enforcement chain — every operation it
        makes is decided, guarded and audited exactly like an HTTP request.
        """
        from learning_service.reader import build_client

        return build_client(self.contract_app())


def build_deployment(
    configuration: Mapping[str, Any],
    *,
    identity: IdentityContextPort,
    authorization: AuthorizationPort,
    store: LearningStore | None = None,
    guard: IdempotencyGuard | None = None,
    seed_demo: bool = False,
    with_http: bool = False,
) -> LearningDeployment:
    """Assemble one Learning deployment over the published ports.

    ``identity`` — the published context reader of IS-001, adapted by
    ``learning_service.adapters.identity_port``;

    ``authorization`` — the published decision reader of IS-003, adapted by
    ``learning_service.adapters.authorization_port``;

    ``guard`` — an explicit IS-005 guard, if the composition root owns one;
    by default the engine builds its own and audits into its journal.
    """
    config = LearningConfig.from_mapping(configuration)
    if store is None:
        store = LearningStore()
    engine = LearningEngine(
        store=store,
        config=config,
        identity=identity,
        authorization=authorization,
        guard=guard,
    )
    if seed_demo:
        store.seed_demo()
    deployment = LearningDeployment(
        engine=engine, config=config, store=store, http_app=None
    )
    if with_http:
        deployment.contract_app()
    return deployment


__all__ = ["LearningDeployment", "build_deployment", "ConfigurationError"]
