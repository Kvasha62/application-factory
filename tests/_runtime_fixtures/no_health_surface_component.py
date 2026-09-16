"""Test double: a component that runs but publishes no health surface.

This capability does not invent a health verdict for a component that provides
none: a runtime whose health cannot be established is not ``ready``
(ADR-0016 §18, §19; ADR-0017 §33).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI


class _SilentComponent:
    def contract_app(self) -> Any:
        return FastAPI(title="component without a health surface")


def build_component(configuration: dict[str, Any]) -> Any:
    return _SilentComponent()
