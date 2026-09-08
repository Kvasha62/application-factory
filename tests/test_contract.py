import json
from pathlib import Path

REQUIRED = {
    "api",
    "events",
    "data_export_cdc",
    "ui",
    "configuration_schema",
    "data_ownership",
    "authn",
    "authz",
    "compatibility_policy",
    "dependencies",
}

CONTRACT = Path("components/identity/contract/component_contract.json")


def test_component_contract_minimum_fields():
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))
    missing = REQUIRED - set(data)
    assert not missing
    assert data["class"] == "platform_service"
    assert data["data_ownership"]["owner"] == "identity"
    scopes = {d["name"]: d["scope"] for d in data["data_ownership"]["datasets"]}
    assert scopes["protected_records"] == "tenant-scoped"
    assert data["authz"]["authentication_does_not_imply_authorization"] is True
    assert data["authz"]["boundary"] == "data_owner"
    assert data["events"]["published"] == []
    assert data["events"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"


def test_openapi_exists():
    assert Path("components/identity/contract/openapi.yaml").is_file()
