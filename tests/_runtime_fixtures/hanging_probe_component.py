"""Test double: a component whose health probe hangs when a test asks it to.

An honest, healthy component — with one difference: when the marker file
``restart-hang.marker`` exists in the process's working directory (the
component's runtime workspace), its ``/health`` route sleeps for the number of
seconds the marker names before it answers. Without the marker the component
answers immediately, so the very same bound content serves both an initial
deployment that must succeed and a restart whose health/readiness verification
must then run into its deadline.

That deadline is a fact, not a simulation (ADR-0017 §33–§34): a runtime that
does not answer its health/readiness surface within the deadline the operation
bound is not a ready platform, and an operation that waits for it must fail
closed rather than inherit the readiness it verified before.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from tenant_authority import COMPONENT_ID, COMPONENT_VERSION

#: Read from the process's working directory: seconds ``/health`` sleeps for.
MARKER_NAME = "restart-hang.marker"


def _hang_seconds() -> float:
    """How long the health surface must stay silent, as the test states it."""
    marker = Path(MARKER_NAME)
    if not marker.is_file():
        return 0.0
    try:
        declared = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0
    return declared if declared > 0 else 0.0


class _HangingProbeComponent:
    """A component that reports the pinned identity and can hang while doing it."""

    def __init__(self, configuration: dict[str, Any]) -> None:
        self._configuration = configuration

    def contract_app(self) -> Any:
        app = FastAPI(title="hanging probe double", version=COMPONENT_VERSION)
        platform_id = self._configuration.get("platform_id")

        @app.get("/health")
        def health() -> dict:
            delay = _hang_seconds()
            if delay:
                time.sleep(delay)
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
    """Construct the component from its pinned configuration."""
    return _HangingProbeComponent(configuration)
