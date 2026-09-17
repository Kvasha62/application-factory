"""Test double: content that imitates the pinned component without being it.

This module is deliberately *not* the content a deployment bound to its runtime
process, and it is written to survive every check that only trusts what a
runtime says about itself: it reports exactly the pinned component identity,
version and platform, and it answers ``/health`` and ``/ready`` positively.

Deployment & Operations must still refuse the ``realized``/``deployed`` claim,
because the content actually loaded into that process is not the content the
engine bound before launch (ADR-0016 §9, §10; ADR-0017 §43.10).

Constructing the component leaves ``substituted-content.marker`` in the
process's working directory, so a test can prove that this content really did
run rather than that it merely could have.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION

#: Written where the process runs, proving this content was constructed.
MARKER_NAME = "substituted-content.marker"


class _SubstitutedForBoundContent:
    def __init__(self, configuration: dict[str, Any]) -> None:
        self._configuration = configuration
        Path(MARKER_NAME).write_text("substituted content ran\n", encoding="utf-8")

    def contract_app(self) -> Any:
        app = FastAPI(title="substituted content double", version=COMPONENT_VERSION)
        platform_id = self._configuration.get("platform_id")

        @app.get("/health")
        def health() -> dict:
            return {
                "status": "ok",
                "component_id": COMPONENT_ID,
                "version": COMPONENT_VERSION,
                "platform_id": platform_id,
            }

        @app.get("/ready")
        def ready() -> dict:
            return {"status": "ready", "component_id": COMPONENT_ID}

        return app


def build_component(configuration: dict[str, Any]) -> Any:
    return _SubstitutedForBoundContent(configuration)
