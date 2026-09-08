import pytest

from identity_service.errors import AccessDenied
from identity_service.models import DenyReason
from tests.conftest import engine


def test_key_security_claimed_tenant_b_does_not_become_b():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-human-a", "rec_b1", "ten_b")
    assert exc.value.reason is DenyReason.TENANT_MISMATCH
    ident = e.verify_identity("token-human-a")
    ctx = e.resolve_tenant_context(ident, None)
    assert ctx.tenant_id == "ten_a"


def test_cannot_read_foreign_tenant_record_by_id():
    e = engine()
    with pytest.raises(AccessDenied) as exc:
        e.read_record("token-human-a", "rec_b1", "ten_a")
    assert exc.value.reason is DenyReason.UNKNOWN_RESOURCE


def test_inv05_query_param_does_not_cross_tenant():
    e = engine()
    with pytest.raises(AccessDenied):
        e.read_record("token-human-a", "rec_b1", "ten_b")
    record, _, _ = e.read_record("token-human-a", "rec_a1", "ten_a")
    assert record.tenant_id == "ten_a"
