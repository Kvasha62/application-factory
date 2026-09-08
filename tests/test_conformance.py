"""Automated architectural conformance for IS-001."""

import ast
from pathlib import Path

from identity_service.engine import IdentityEngine
from identity_service.store import IdentityStore


SRC_ROOT = Path("src")


def test_no_import_of_foreign_internal_modules():
    forbidden = ("learning", "commerce", "booking", "catalog", "composer")
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden


def test_identity_store_is_not_exported_as_public_api():
    api = Path("src/identity_service/api.py").read_text(encoding="utf-8")
    assert "/internal" not in api
    assert "store.identities" not in api


def test_engine_uses_verified_identity_not_caller_tenant_as_source():
    src = Path("src/identity_service/engine.py").read_text(encoding="utf-8")
    assert "source: str = \"verified_identity\"" in Path(
        "src/identity_service/models.py"
    ).read_text(encoding="utf-8") or "verified_identity" in src


def test_audit_reasons_distinguish_mismatch_and_authz():
    store = IdentityStore()
    store.seed_demo()
    e = IdentityEngine(store)
    from identity_service.errors import AccessDenied

    try:
        e.read_record("token-human-a", "rec_a1", "ten_b")
    except AccessDenied:
        pass
    try:
        e.write_record("token-service", "x", "y", "ten_a")
    except AccessDenied:
        pass
    reasons = {ev.reason for ev in e.store.audit}
    assert "tenant_mismatch" in reasons
    assert "insufficient_authorization" in reasons
    assert "tenant_mismatch" != "insufficient_authorization"
