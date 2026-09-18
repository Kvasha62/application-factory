"""Test double: a component that runs but is not the version the instance pinned.

The running component is healthy and answers, but its published surface reports
a different version than the accepted Platform Instance pins. ``ready`` alone
then justifies nothing: the deployment operation must refuse the
``realized``/``deployed`` claim, because the actual platform does not
correspond to the desired state (ADR-0016 §8, §10; ADR-0017 §34).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from tenant_authority import COMPONENT_ID
from tenant_authority.deployment import build_deployment

#: The version this double reports, deliberately different from the pinned one.
REPORTED_VERSION = "9.9.9"


class _SubstitutedComponent:
    def __init__(self, configuration: dict[str, Any]) -> None:
        self._deployment = build_deployment(configuration, with_http=True)

    def contract_app(self) -> Any:
        app = FastAPI(title="substituted component double", version=REPORTED_VERSION)
        platform_id = self._deployment.config.platform_id

        @app.get("/health")
        def health() -> dict:
            return {
                "status": "ok",
                "component_id": COMPONENT_ID,
                "version": REPORTED_VERSION,
                "platform_id": platform_id,
            }

        @app.get("/ready")
        def ready() -> dict:
            return {"status": "ready", "component_id": COMPONENT_ID}

        return app


def build_component(configuration: dict[str, Any]) -> Any:
    return _SubstitutedComponent(configuration)
