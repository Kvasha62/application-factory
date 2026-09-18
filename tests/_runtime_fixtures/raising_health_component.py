"""Test double: a component whose health surface fails when it is asked.

An unavailable runtime cannot be ``ready`` (ADR-0016 §19): the deployment
operation must treat the failed probe as a health/readiness failure and fix no
deployment claim.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION
from tenant_authority.deployment import build_deployment


class _FailingProbeComponent:
    def __init__(self, configuration: dict[str, Any]) -> None:
        self._deployment = build_deployment(configuration, with_http=True)

    def contract_app(self) -> Any:
        app = FastAPI(title="failing probe component double", version=COMPONENT_VERSION)

        @app.get("/health")
        def health() -> dict:
            message = "the health surface cannot answer right now"
            raise RuntimeError(message)

        @app.get("/ready")
        def ready() -> dict:
            return {"status": "ready", "component_id": COMPONENT_ID}

        return app


def build_component(configuration: dict[str, Any]) -> Any:
    return _FailingProbeComponent(configuration)
