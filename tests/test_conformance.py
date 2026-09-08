"""Automated architectural conformance for IS-001 — behavioral checks."""

import pytest
from fastapi.testclient import TestClient

from identity_service.errors import AccessDenied
from identity_service.models import Decision, DenyReason
from tests.conftest import engine, monolith


def test_tenant_context_source_is_verified_identity_not_caller_claim():
    e = engine()
    ident = e.verify_identity("token-human-a")
    ctx = e.resolve_tenant_context(ident, "ten_a")
    assert ctx.source == "verified_identity"
    assert ctx.tenant_id == "ten_a"
    with pytest.raises(AccessDenied) as exc:
        e.resolve_tenant_context(ident, "ten_b")
    assert exc.value.reason is DenyReason.TENANT_MISMATCH
    home = e.resolve_tenant_context(ident, None)
    assert home.tenant_id == "ten_a"
    assert home.source == "verified_identity"


def test_caller_tenant_id_is_cross_check_only():
    e = engine()
    ident = e.verify_identity("token-human-a")
    from_identity = e.resolve_tenant_context(ident, None)
    matching_claim = e.resolve_tenant_context(ident, from_identity.tenant_id)
    assert matching_claim.tenant_id == from_identity.tenant_id
    assert matching_claim.source == "verified_identity"
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-human-a", "rec_a1", "ten_b")
    assert exc.value.reason is DenyReason.TENANT_MISMATCH
    record, _, _ = e.read_record("token-human-a", "rec_a1", None)
    assert record.tenant_id == "ten_a"


def test_public_api_does_not_expose_owned_store():
    app = monolith().identity_app
    client = TestClient(app)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any("internal" in path for path in paths)
    assert not any("store" in path for path in paths)
    leaked = client.get("/api/v1/identities")
    assert leaked.status_code == 404
    openapi = client.get("/openapi.json").json()
    for path in openapi["paths"]:
        assert "internal" not in path
        assert "store" not in path


def test_authentication_does_not_imply_authorization_behaviorally():
    e = engine()
    ident = e.verify_identity("token-service")
    tenant = e.resolve_tenant_context(ident, "ten_a")
    with pytest.raises(AccessDenied) as exc:
        e.authorize(ident, tenant, "records.write")
    assert exc.value.reason is DenyReason.INSUFFICIENT_AUTHORIZATION
    record, _, audit = e.read_record("token-service", "rec_a1", "ten_a")
    assert record.tenant_id == "ten_a"
    assert audit.decision is Decision.ALLOW


def test_audit_reasons_distinguish_mismatch_and_authz():
    e = engine()
    try:
        e.read_record("token-human-a", "rec_a1", "ten_b")
    except AccessDenied:
        pass
    try:
        e.write_record("token-service", "x", "y", "ten_a")
    except AccessDenied:
        pass
    mismatch = [ev for ev in e.store.audit if ev.reason == "tenant_mismatch"]
    authz = [ev for ev in e.store.audit if ev.reason == "insufficient_authorization"]
    assert mismatch and authz
    assert mismatch[0].decision is Decision.DENY
    assert authz[0].decision is Decision.DENY
    assert mismatch[0].reason != authz[0].reason
