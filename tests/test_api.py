"""HTTP-контракт IS-001: собирается композиционным корнем теста.

The composed application comes from `tests.conftest.monolith()` — assembling
Identity over Tenant Authority is the job of a composition root, never of
`identity_service.api`.
"""

from fastapi.testclient import TestClient

from tests.conftest import monolith

_instance = monolith()
client = TestClient(_instance.identity_app)
identity_engine = _instance.identity


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["component_id"] == "identity"


def test_me_human():
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer token-human-a"})
    assert r.status_code == 200
    assert r.json()["kind"] == "HUMAN"


def test_me_missing_identity_is_audited():
    before = len(identity_engine.store.audit)
    r = client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "missing_identity"
    assert any(
        ev.action == "identity.authenticate" and ev.reason == "missing_identity"
        for ev in identity_engine.store.audit[before:]
    )


def test_http_mismatch():
    r = client.get(
        "/api/v1/records/rec_a1",
        headers={"Authorization": "Bearer token-human-a", "X-Tenant-Id": "ten_b"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "tenant_mismatch"
