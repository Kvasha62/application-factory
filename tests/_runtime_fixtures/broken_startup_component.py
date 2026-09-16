"""Test double: a component that cannot construct itself in this environment.

A component that does not start is a fail-closed startup failure (ADR-0016
§18, §20): the deployment operation stops, the record says so, and no
``realized`` / ``deployed`` claim is fixed.
"""

from __future__ import annotations

from typing import Any


def build_component(configuration: dict[str, Any]) -> Any:
    message = "the component cannot be constructed in this environment"
    raise RuntimeError(message)
