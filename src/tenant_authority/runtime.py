"""Composition root for the Level 0 modular monolith.

An in-process deployment still crosses a component boundary, so the wiring is
kept here — a consumer receives a published engine through its public contract,
never another component's internals (T-009, ARCHITECTURE.md §1.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from tenant_authority.config import TenantAuthorityConfig
from tenant_authority.contracts import TenantAuthorityReader
from tenant_authority.engine import TenantAuthorityEngine
from tenant_authority.store import TenantAuthorityStore


@dataclass(frozen=True, slots=True)
class TenantAuthorityRuntime:
    engine: TenantAuthorityEngine
    config: TenantAuthorityConfig
    store: TenantAuthorityStore

    @property
    def current_platform_id(self) -> str:
        return self.config.platform_id

    @property
    def reader(self) -> TenantAuthorityReader:
        """The only object other components are handed: published reads, no store."""
        return TenantAuthorityReader(self.engine)


def build_runtime(
    configuration: Mapping[str, Any],
    *,
    seed_demo: bool = False,
    store: TenantAuthorityStore | None = None,
) -> TenantAuthorityRuntime:
    config = TenantAuthorityConfig.from_mapping(configuration)
    tenant_store = store or TenantAuthorityStore()
    engine = TenantAuthorityEngine(store=tenant_store, config=config)
    if seed_demo:
        tenant_store.seed_demo(config.platform_id, timestamp=engine.clock())
    return TenantAuthorityRuntime(
        engine=engine,
        config=config,
        store=tenant_store,
    )
