"""Test double: a component that runs while serving a different platform instance.

The component is constructed and healthy, but it serves another Platform
Instance than the one the deployment was requested for. What is actually
running does not correspond to the desired state of this instance, so no
``deployed`` claim may stand (ADR-0016 §8, §10).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.deployment import build_deployment

#: The Platform Instance this double claims to serve.
SERVED_PLATFORM_ID = "some-other-platform"


class _ForeignPlatformComponent:
    def __init__(self, configuration: dict[str, Any]) -> None:
        self._deployment = build_deployment(configuration, with_http=True)

    def contract_app(self) -> Any:
        app = FastAPI(
            title="foreign platform component double", version=COMPONENT_VERSION
        )

        @app.get("/health")
        def health() -> dict:
            return {
                "status": "ok",
                "component_id": COMPONENT_ID,
                "version": COMPONENT_VERSION,
                "platform_id": SERVED_PLATFORM_ID,
            }

        @app.get("/ready")
        def ready() -> dict:
            return {"status": "ready", "component_id": COMPONENT_ID}

        return app


def build_component(configuration: dict[str, Any]) -> Any:
    return _ForeignPlatformComponent(configuration)
