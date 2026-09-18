"""Test double: a component that starts but reports itself unhealthy.

An unavailable or unhealthy runtime cannot be ``ready`` (ADR-0016 §19;
ADR-0017 §33): the deployment operation must stop at health/readiness
verification and fix no deployment claim.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.deployment import build_deployment


class _UnhealthyComponent:
    def __init__(self, configuration: dict[str, Any]) -> None:
        self._deployment = build_deployment(configuration, with_http=True)

    def contract_app(self) -> Any:
        app = FastAPI(title="unhealthy component double", version=COMPONENT_VERSION)
        platform_id = self._deployment.config.platform_id

        @app.get("/health")
        def health() -> dict:
            return {
                "status": "degraded",
                "component_id": COMPONENT_ID,
                "version": COMPONENT_VERSION,
                "platform_id": platform_id,
            }

        @app.get("/ready")
        def ready() -> dict:
            return {"status": "ready", "component_id": COMPONENT_ID}

        return app


def build_component(configuration: dict[str, Any]) -> Any:
    return _UnhealthyComponent(configuration)
