from fastapi.testclient import TestClient

from identity_service.api import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["component_id"] == "identity"


def test_me_human():
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer token-human-a"})
    assert r.status_code == 200
    assert r.json()["kind"] == "HUMAN"


def test_http_mismatch():
    r = client.get(
        "/api/v1/records/rec_a1",
        headers={"Authorization": "Bearer token-human-a", "X-Tenant-Id": "ten_b"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "tenant_mismatch"
