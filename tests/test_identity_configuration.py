"""Конфигурация IS-001: объявленная конфигурация реально используется (Issue #10, §4).

Раздел 20 ARCHITECTURE.md: неизвестный configuration key — BUILD ERROR.
"""

from __future__ import annotations

import pytest

from identity_service.config import ConfigurationError, IdentityConfig
from identity_service.errors import AccessDenied
from tests.conftest import composed


def test_unknown_configuration_key_is_rejected():
    with pytest.raises(ConfigurationError):
        IdentityConfig.from_mapping({"current_platform_id": "plt_a", "unexpected": 1})


def test_platform_binding_is_required():
    with pytest.raises(ConfigurationError):
        IdentityConfig.from_mapping({"environment": "test"})
    with pytest.raises(ConfigurationError):
        IdentityConfig.from_mapping({"current_platform_id": "   "})


def test_declared_token_prefix_is_the_configured_one():
    _, authority = composed()
    identity_engine, _ = composed()
    config = IdentityConfig.from_mapping(
        {
            "current_platform_id": authority.current_platform_id,
            "environment": "test",
            "token_prefix": "identity-token-",
        }
    )
    from identity_service.engine import IdentityEngine
    from identity_service.store import IdentityStore

    store = IdentityStore()
    store.seed_demo()
    store.tokens = {
        f"identity-token-{key[6:]}": value for key, value in store.tokens.items() if key.startswith("token-")
    }
    engine = IdentityEngine(store=store, tenant_authority=authority.reader, config=config)

    assert engine.verify_identity("identity-token-human-a").identity_id == "idn_human_a"
    # The previously valid prefix is no longer accepted by this configuration.
    with pytest.raises(AccessDenied) as exc:
        engine.verify_identity("token-human-a")
    assert exc.value.reason.value == "invalid_identity"
