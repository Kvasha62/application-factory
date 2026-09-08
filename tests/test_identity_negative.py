import pytest

from identity_service.errors import AccessDenied
from identity_service.models import Decision, DenyReason
from tests.conftest import engine


def test_missing_identity_deny():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record(None, "rec_a1", None)
    assert exc.value.reason is DenyReason.MISSING_IDENTITY
    assert e.store.audit[-1].decision is Decision.DENY
    assert e.store.audit[-1].reason == DenyReason.MISSING_IDENTITY.value


def test_invalid_identity_deny():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-invalid", "rec_a1", None)
    assert exc.value.reason is DenyReason.INVALID_IDENTITY


def test_unknown_identity_deny():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-unknown", "rec_a1", None)
    assert exc.value.reason is DenyReason.UNKNOWN_IDENTITY


def test_missing_tenant_context_deny():
    e = engine()
    e.store.associations.clear()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-human-a", "rec_a1", None)
    assert exc.value.reason is DenyReason.MISSING_TENANT_CONTEXT


def test_tenant_mismatch_deny():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-human-a", "rec_a1", "ten_b")
    assert exc.value.reason is DenyReason.TENANT_MISMATCH
    assert e.store.audit[-1].reason == DenyReason.TENANT_MISMATCH.value


def test_suspended_tenant_deny():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-human-a", "rec_a1", "ten_suspended")
    assert exc.value.reason is DenyReason.TENANT_SUSPENDED


def test_deleted_tenant_deny():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-human-a", "rec_a1", "ten_deleted")
    assert exc.value.reason is DenyReason.TENANT_DELETED


def test_insufficient_authorization_deny():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.write_record("token-service", "rec_x", "nope", "ten_a")
    assert exc.value.reason is DenyReason.INSUFFICIENT_AUTHORIZATION
    assert e.store.audit[-1].reason == DenyReason.INSUFFICIENT_AUTHORIZATION.value


def test_authn_without_authz_does_not_grant_access():
    e = engine()
    ident = e.verify_identity("token-service")
    tenant = e.resolve_tenant_context(ident, "ten_a")
    with pytest.raises(AccessDenied) as exc:
        e.authorize(ident, tenant, "records.write")
    assert exc.value.reason is DenyReason.INSUFFICIENT_AUTHORIZATION
