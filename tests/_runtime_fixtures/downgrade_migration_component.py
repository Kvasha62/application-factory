"""Test double: a component that declares a migration with a reverse path.

Production schema develops forward only (LAW-08; ADR-0016 §14). A declaration
that carries a downgrade path is not a forward migration, and the orchestration
boundary must refuse it instead of executing half of it.
"""

from __future__ import annotations

from typing import Any

from tenant_authority.deployment import build_deployment


def build_component(configuration: dict[str, Any]) -> Any:
    return build_deployment(configuration, with_http=True)


def _forward(deployment: Any) -> None:
    return None


def _reverse(deployment: Any) -> None:
    return None


MIGRATIONS = (
    {
        "migration_id": "0001-add-column",
        "forward": _forward,
        "reverse": _reverse,
    },
)
