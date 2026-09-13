"""Configuration of the Commerce component.

ARCHITECTURE.md §20: an unknown configuration key is a build error, so the
loader rejects anything it does not declare instead of ignoring it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from commerce_service.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class CommerceConfig:
    platform_id: str
    environment: str = "standalone"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> CommerceConfig:
        payload = dict(data or {})
        declared = {f.name for f in fields(cls)}
        unknown = sorted(set(payload) - declared)
        if unknown:
            raise ConfigurationError(
                "unknown configuration key(s): " + ", ".join(unknown)
            )
        platform_id = payload.get("platform_id")
        if not isinstance(platform_id, str) or not platform_id.strip():
            raise ConfigurationError(
                "`platform_id` is required and must be a non-empty string"
            )
        environment = payload.get("environment", "standalone")
        if not isinstance(environment, str) or not environment.strip():
            raise ConfigurationError("`environment` must be a non-empty string")
        return cls(platform_id=platform_id, environment=environment)
