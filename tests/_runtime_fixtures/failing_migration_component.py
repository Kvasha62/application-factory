"""Test double: a component whose second forward migration fails.

A failed migration is a fail-closed state (ADR-0016 §14, §20): the deployment
operation must stop, deployment state must say so honestly, and no
``realized`` / ``deployed`` claim may be fixed. The first migration succeeds and
leaves its marker, so a test can also see *where* the sequence stopped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tenant_authority.deployment import build_deployment


def build_component(configuration: dict[str, Any]) -> Any:
    return build_deployment(configuration, with_http=True)


def _first(deployment: Any) -> None:
    Path("migration-0001-first.done").write_text("executed\n", encoding="utf-8")


def _second(deployment: Any) -> None:
    message = "migration 0002: the component's own schema step refused to continue"
    raise RuntimeError(message)


MIGRATIONS = (
    {"migration_id": "0001-first", "forward": _first},
    {"migration_id": "0002-second", "forward": _second},
)
