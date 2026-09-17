"""Test double: a migration that resolves content the engine cannot see.

The migration declaration reaches for its helper through a name that is not a
literal in this source, so no engine-side closure can bind it in advance. What
may execute is decided at resolution time by the boundary the deployment
established — and content outside the verified execution root never executes.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from tenant_authority.deployment import build_deployment


def build_component(configuration: dict[str, Any]) -> Any:
    """Construct the pinned component's own deployment."""
    return build_deployment(configuration, with_http=True)


def _forward(deployment: Any) -> None:
    """Component-owned forward migration resolving its helper at run time."""
    helper = importlib.import_module("hel" + "per")
    helper.record("migration")
    Path("migration-outcome.txt").write_text("loaded\n", encoding="utf-8")


MIGRATIONS = ({"migration_id": "0001-seed-platform", "forward": _forward},)
