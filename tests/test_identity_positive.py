from identity_service.models import Decision, IdentityKind
from tests.conftest import engine


def test_verified_human_identity():
    e = engine()
    ident = e.verify_identity("token-human-a")
    assert ident.kind is IdentityKind.HUMAN
    assert ident.identity_id == "idn_human_a"


def test_verified_service_identity():
    e = engine()
    ident = e.verify_identity("token-service")
    assert ident.kind is IdentityKind.SERVICE


def test_tenant_context_from_verified_identity():
    e = engine()
    ident = e.verify_identity("token-human-a")
    ctx = e.resolve_tenant_context(ident, None)
    assert ctx.tenant_id == "ten_a"
    assert ctx.source == "verified_identity"


def test_allow_with_authorization():
    e = engine()
    record, obs, audit = e.read_record("token-human-a", "rec_a1", None)
    assert record.body == "tenant-a-secret"
    assert audit.decision is Decision.ALLOW
    assert obs.tenant_id == "ten_a"
    assert obs.actor_id == "idn_human_a"


def test_audit_trail_on_allow():
    e = engine()
    e.read_record("token-human-a", "rec_a1", None)
    assert any(ev.decision is Decision.ALLOW for ev in e.store.audit)


def test_observability_has_tenant_context():
    e = engine()
    _, obs, _ = e.read_record("token-human-a", "rec_a1", "ten_a")
    assert obs.tenant_id == "ten_a"
    assert obs.request_id
    assert obs.correlation_id
    assert obs.component_id == "identity"


def test_idempotent_write():
    e = engine()
    r1, _, _a1 = e.write_record(
        "token-human-a", "rec_new", "once", "ten_a", idempotency_key="ik-1"
    )
    r2, _, a2 = e.write_record(
        "token-human-a", "rec_new", "once", "ten_a", idempotency_key="ik-1"
    )
    assert r1.body == r2.body == "once"
    assert a2.reason == "idempotent_replay"
