"""Test double: a component that declares component-owned forward migrations.

The deployment environment of a test may bind the component ``tenant_authority``
of an accepted Platform Instance to this module instead of to the component's
own entrypoint. What it demonstrates is the migration seam of ADR-0016 §14: the
migration *content* lives with the component (here: with this component-owned
module, reaching the component's own store through the component's own
deployment object) and Deployment & Operations only orders its execution.

Every executed forward migration leaves a marker file in the component's
runtime workspace (the process working directory), so a test can prove that the
migration ran inside the component's own runtime process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tenant_authority.deployment import build_deployment

SEED_TIMESTAMP = "2026-09-16T00:00:00Z"


def build_component(configuration: dict[str, Any]) -> Any:
    """Construct the component's own deployment from its pinned configuration."""
    return build_deployment(configuration, with_http=True)


def _marker(name: str) -> None:
    Path(f"migration-{name}.done").write_text("executed\n", encoding="utf-8")


def _seed_platform(deployment: Any) -> None:
    """Component-owned migration: seed this component's own demo data set."""
    deployment.store.seed_demo(deployment.config.platform_id, timestamp=SEED_TIMESTAMP)
    _marker("0001-seed-platform")


def _record_platform_identity(deployment: Any) -> None:
    """Component-owned migration: record the platform identity in the workspace."""
    platform_id = deployment.config.platform_id
    Path("platform-identity.txt").write_text(platform_id, encoding="utf-8")
    _marker("0002-record-platform-identity")


MIGRATIONS = (
    {"migration_id": "0001-seed-platform", "forward": _seed_platform},
    {
        "migration_id": "0002-record-platform-identity",
        "forward": _record_platform_identity,
    },
)
