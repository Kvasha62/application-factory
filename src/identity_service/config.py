"""Configuration of the Identity / Tenant Context component.

ARCHITECTURE.md §20: an unknown configuration key is a build error, so the loader
rejects anything it does not declare instead of silently ignoring it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any


class ConfigurationError(Exception):
    """Invalid or unknown configuration of the identity component."""


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    current_platform_id: str
    environment: str = "standalone"
    token_prefix: str = "token-"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> IdentityConfig:
        payload = dict(data or {})
        declared = {f.name for f in fields(cls)}
        unknown = sorted(set(payload) - declared)
        if unknown:
            raise ConfigurationError("unknown configuration key(s): " + ", ".join(unknown))

        platform_id = payload.get("current_platform_id")
        if not isinstance(platform_id, str) or not platform_id.strip():
            raise ConfigurationError(
                "`current_platform_id` is required and must be a non-empty string"
            )
        environment = payload.get("environment", "standalone")
        prefix = payload.get("token_prefix", "token-")
        if not isinstance(environment, str) or not environment.strip():
            raise ConfigurationError("`environment` must be a non-empty string")
        if not isinstance(prefix, str) or not prefix:
            raise ConfigurationError("`token_prefix` must be a non-empty string")
        return cls(
            current_platform_id=platform_id,
            environment=environment,
            token_prefix=prefix,
        )
