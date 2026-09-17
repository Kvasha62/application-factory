"""Test double: a component whose migration imports content by name.

The deployment binds this module as the component's migration declaration and
``helper`` beside it, from the verified execution root. The migration reaches
for ``helper`` — a component-owned name — so what executes is decided by the
execution boundary, never by what happens to be importable at the time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import helper  # component-owned dependency, resolved by ordinary import

from tenant_authority.deployment import build_deployment


def build_component(configuration: dict[str, Any]) -> Any:
    """Construct the pinned component's own deployment."""
    return build_deployment(configuration, with_http=True)


def _forward(deployment: Any) -> None:
    """Component-owned forward migration that needs its own helper module."""
    helper.record("migration")
    Path("migration-outcome.txt").write_text("loaded\n", encoding="utf-8")


MIGRATIONS = ({"migration_id": "0001-seed-platform", "forward": _forward},)
