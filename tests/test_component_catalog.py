"""Focused tests for Slice B — Component Catalog (Issue #64, ADR-0015 §5).

These tests verify architecture rules, not merely successful object
construction: the catalog is a *derived view* of the canonical Component
Registry and the published Component Contracts, divergence from those
canonical sources is rejected deterministically, discovery and filtering
serve composition decisions, and no component internals are ever touched.

The ``latest`` tests follow the Slice A doctrine: ``latest`` is not forbidden
as a string, it is forbidden as a *selector* — as a way of choosing a
version, a release, an artifact, a dependency or a production target.
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from component_catalog import (
    CATALOG_ID,
    CATALOG_PATH,
    CATALOG_SCHEMA_PATH,
    Catalog,
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
    build_catalog_document,
    entry_digest,
    load_catalog,
    load_catalog_document,
    render_catalog_document,
    validate_document,
)
from component_catalog.__main__ import main as cli_main
from component_registry import REGISTRY_PATH, Registry, load_registry
from component_registry.errors import RegistryNotFoundError, RegistryValidationError

#: The verified component inventory (DOCUMENTATION_BASELINE.md §4).
EXPECTED_INVENTORY = (
    "authorization",
    "identity",
    "tenant_authority",
    "records",
    "learning",
    "saga",
    "idempotency",
    "commerce",
    "booking",
)

#: Business systems that earned Factory Gate #1.
BUSINESS_SYSTEMS = {"learning": "SCS-001", "commerce": "SCS-002", "booking": "SCS-003"}

#: The exact key set of a catalog entry — the lean discovery view.
ENTRY_KEYS = {
    "component_id",
    "component_version",
    "class",
    "scs_id",
    "maturity_level",
    "owner",
    "data_scopes",
    "lifecycle",
    "contracts",
    "summary",
    "entry_digest",
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root() -> Path:
    from component_registry import discover_root

    return discover_root()


@pytest.fixture(scope="module")
def registry(root: Path) -> Registry:
    return load_registry(root)


@pytest.fixture(scope="module")
def document(root: Path) -> Mapping:
    return load_catalog_document(root / CATALOG_PATH)


@pytest.fixture(scope="module")
def catalog(root: Path) -> Catalog:
    return load_catalog(root)


def errors_for(catalog_document: object, root: Path) -> list[str]:
    return validate_document(catalog_document, root=root)


def with_entry(catalog_document: Mapping, target: str, **fields: object) -> Mapping:
    """Return a deep copy whose entry ``target`` has ``fields`` merged in."""
    clone = copy.deepcopy(catalog_document)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry.update(fields)
            return clone
    raise AssertionError(f"no such component: {target}")


def with_nested(
    catalog_document: Mapping, target: str, section: str, **fields: object
) -> Mapping:
    """Return a deep copy whose ``section`` block of one entry is merged."""
    clone = copy.deepcopy(catalog_document)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry[section].update(fields)
            return clone
    raise AssertionError(f"no such component: {target}")


def without_key(catalog_document: Mapping, target: str, key: str) -> Mapping:
    clone = copy.deepcopy(catalog_document)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry.pop(key, None)
            return clone
    raise AssertionError(f"no such component: {target}")


def rejects(catalog_document: object, root: Path, fragment: str) -> None:
    """Assert validation rejects ``document`` with an error containing ``fragment``."""
    errors = errors_for(catalog_document, root)
    assert errors, "expected validation to fail, but the document was accepted"
    assert any(
        fragment in error for error in errors
    ), f"expected an error containing {fragment!r}, got:\n" + "\n".join(errors)


def materialize(tmp_path: Path) -> Path:
    """Copy the canonical sources into an isolated throwaway repository root."""
    from component_registry import discover_root

    source = discover_root()
    root = tmp_path / "repo"
    shutil.copytree(source / "components", root / "components")
    shutil.copytree(source / "factory", root / "factory")
    shutil.copy(source / "pyproject.toml", root / "pyproject.toml")
    return root


def write_registry_document(root: Path, registry_document: Mapping) -> None:
    path = root / REGISTRY_PATH
    path.write_text(json.dumps(registry_document, indent=2) + "\n", encoding="utf-8")


def write_catalog_document(root: Path, catalog_document: Mapping) -> None:
    path = root / CATALOG_PATH
    path.write_text(json.dumps(catalog_document, indent=2) + "\n", encoding="utf-8")


def patch_discover_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point every discover_root binding of the catalog CLI at ``root``."""
    monkeypatch.setattr("component_registry.registry.discover_root", lambda: root)
    monkeypatch.setattr("component_catalog.__main__.discover_root", lambda: root)


def contract_task(root: Path, component_id: str) -> str | None:
    path = root / "components" / component_id / "contract" / "component_contract.json"
    task = json.loads(path.read_text(encoding="utf-8")).get("task")
    return task if isinstance(task, str) and task.strip() else None


# ---------------------------------------------------------------------------
# Positive: the canonical derived catalog
# ---------------------------------------------------------------------------


def test_the_canonical_catalog_exists_and_is_machine_readable(root: Path) -> None:
    path = root / CATALOG_PATH
    assert path.is_file(), "the canonical catalog document must exist"
    loaded = load_catalog_document(path)
    assert isinstance(loaded, Mapping)


def test_the_canonical_schema_exists(root: Path) -> None:
    assert (root / CATALOG_SCHEMA_PATH).is_file(), "the catalog schema must exist"


def test_the_catalog_readme_exists(root: Path) -> None:
    readme = root / "factory" / "catalog" / "README.md"
    assert readme.is_file(), "the catalog rules must be documented"


def test_the_canonical_catalog_is_valid(catalog: Catalog) -> None:
    assert catalog.validate() == []


def test_the_canonical_catalog_loads_strictly(root: Path) -> None:
    loaded = load_catalog(root)
    assert loaded.component_ids == EXPECTED_INVENTORY


def test_the_catalog_is_demonstrably_derived_from_the_registry(
    document: Mapping, registry: Registry
) -> None:
    """Acceptance criterion 1: the stored document IS the derived view."""
    assert document == build_catalog_document(registry)


def test_derivation_is_deterministic(registry: Registry) -> None:
    first = build_catalog_document(registry)
    second = build_catalog_document(registry)
    assert first == second
    assert render_catalog_document(first) == render_catalog_document(second)


def test_rebuilding_the_canonical_file_is_byte_stable(
    root: Path, registry: Registry
) -> None:
    rendered = render_catalog_document(build_catalog_document(registry))
    assert (root / CATALOG_PATH).read_text(encoding="utf-8") == rendered


def test_the_catalog_describes_exactly_the_registered_inventory(
    catalog: Catalog, registry: Registry
) -> None:
    assert catalog.component_ids == registry.component_ids
    assert catalog.component_ids == EXPECTED_INVENTORY


def test_catalog_entries_follow_canonical_registry_order(
    catalog: Catalog, registry: Registry
) -> None:
    assert catalog.component_ids == tuple(registry.component_ids)


def test_every_entry_has_exactly_the_lean_view_shape(catalog: Catalog) -> None:
    for entry in catalog.entries:
        assert set(entry) == ENTRY_KEYS, entry.get("component_id")
        assert "datasets" not in entry, "the view must not duplicate ownership detail"
        assert (
            "dependencies" not in entry
        ), "the view must not duplicate dependency detail — the registry stays authoritative"


def test_every_entry_is_pinned_to_its_registry_entry_by_digest(
    catalog: Catalog, registry: Registry
) -> None:
    for component_id in catalog.component_ids:
        view = catalog.entry(component_id)
        assert view["entry_digest"] == entry_digest(registry.entry(component_id))


def test_the_digest_covers_the_whole_registry_entry(
    catalog: Catalog, registry: Registry
) -> None:
    """The digest is computed over the full registry entry, not the view."""
    for component_id in catalog.component_ids:
        registry_entry = registry.entry(component_id)
        assert (
            entry_digest(registry_entry) == catalog.entry(component_id)["entry_digest"]
        )
        assert (
            set(registry_entry) - ENTRY_KEYS
        ), "the registry entry is richer than the view"


def test_every_version_matches_the_registry(
    catalog: Catalog, registry: Registry
) -> None:
    for component_id in catalog.component_ids:
        assert catalog.version(component_id) == registry.version(component_id)


def test_every_class_matches_the_registry(catalog: Catalog, registry: Registry) -> None:
    for component_id in catalog.component_ids:
        assert (
            catalog.entry(component_id)["class"]
            == registry.entry(component_id)["class"]
        )


def test_scs_identifiers_match_the_registry(
    catalog: Catalog, registry: Registry
) -> None:
    for component_id, scs_id in BUSINESS_SYSTEMS.items():
        assert catalog.entry(component_id)["scs_id"] == scs_id
        assert (
            catalog.entry(component_id)["scs_id"]
            == registry.entry(component_id)["scs_id"]
        )
    for component_id in catalog.component_ids:
        if component_id not in BUSINESS_SYSTEMS:
            assert catalog.entry(component_id)["scs_id"] is None


def test_every_owner_matches_the_registry(catalog: Catalog, registry: Registry) -> None:
    for component_id in catalog.component_ids:
        assert (
            catalog.entry(component_id)["owner"]
            == registry.entry(component_id)["owner"]
        )


def test_data_scopes_match_the_registry(catalog: Catalog, registry: Registry) -> None:
    for component_id in catalog.component_ids:
        ownership = registry.entry(component_id)["data_ownership"]
        assert catalog.entry(component_id)["data_scopes"] == ownership["data_scopes"]


def test_lifecycle_views_match_the_registry_verbatim(
    catalog: Catalog, registry: Registry
) -> None:
    for component_id in catalog.component_ids:
        assert (
            catalog.entry(component_id)["lifecycle"]
            == registry.entry(component_id)["lifecycle"]
        )


def test_contract_references_match_the_registry(
    catalog: Catalog, registry: Registry, root: Path
) -> None:
    for component_id in catalog.component_ids:
        view = catalog.entry(component_id)["contracts"]
        authoritative = dict(registry.entry(component_id)["contracts"])
        assert view == authoritative
        assert (root / view["component_contract"]).is_file()


def test_summaries_mirror_the_published_contracts(catalog: Catalog, root: Path) -> None:
    for component_id in catalog.component_ids:
        assert catalog.entry(component_id)["summary"] == contract_task(
            root, component_id
        )


def test_a_contract_without_a_task_statement_summarizes_to_null(
    catalog: Catalog,
) -> None:
    """identity publishes no task statement; the catalog says so honestly."""
    assert catalog.entry("identity")["summary"] is None


def test_components_without_openapi_publish_null_openapi(catalog: Catalog) -> None:
    assert catalog.entry("saga")["contracts"]["openapi"] is None
    assert catalog.entry("idempotency")["contracts"]["openapi"] is None


def test_derived_from_links_to_the_canonical_registry(
    document: Mapping, registry: Registry
) -> None:
    derived_from = document["derived_from"]
    assert derived_from["canonical_registry"] == REGISTRY_PATH
    assert derived_from["registry_id"] == registry.document["registry_id"]
    assert derived_from["component_contracts_root"] == "components"
    assert document["catalog_id"] == CATALOG_ID


def test_validation_is_deterministic(document: Mapping, root: Path) -> None:
    assert errors_for(document, root) == errors_for(document, root)


def test_the_registry_entry_is_reachable_and_authoritative(
    catalog: Catalog, registry: Registry
) -> None:
    for component_id in catalog.component_ids:
        assert catalog.registry_entry(component_id) == registry.entry(component_id)


# ---------------------------------------------------------------------------
# Positive: machine-readable discovery and filtering
# ---------------------------------------------------------------------------


def test_discovery_without_filters_returns_every_entry(catalog: Catalog) -> None:
    assert catalog.filter() == catalog.entries


def test_discovery_by_component_class(catalog: Catalog) -> None:
    business = catalog.filter(component_class="business_system")
    assert tuple(entry["component_id"] for entry in business) == (
        "learning",
        "commerce",
        "booking",
    )
    platform = catalog.filter(component_class="platform_service")
    assert len(platform) == 6
    assert len(business) + len(platform) == 9


def test_discovery_by_data_scope(catalog: Catalog) -> None:
    tenant = catalog.filter(data_scope="tenant-scoped")
    assert tuple(entry["component_id"] for entry in tenant) == (
        "authorization",
        "identity",
        "records",
        "learning",
        "saga",
        "commerce",
        "booking",
    )
    assert len(catalog.filter(data_scope="platform-scoped")) == 9


def test_discovery_by_lifecycle_state(catalog: Catalog) -> None:
    registered = catalog.filter(lifecycle_state="registered")
    assert len(registered) == 9


def test_discovery_by_owner(catalog: Catalog) -> None:
    assert len(catalog.filter(owner="@Kvasha62")) == 9


def test_discovery_by_scs(catalog: Catalog) -> None:
    for component_id, scs_id in BUSINESS_SYSTEMS.items():
        selected = catalog.filter(scs_id=scs_id)
        assert tuple(entry["component_id"] for entry in selected) == (component_id,)


def test_filters_compose(catalog: Catalog) -> None:
    selected = catalog.filter(
        component_class="business_system", data_scope="tenant-scoped"
    )
    assert tuple(entry["component_id"] for entry in selected) == (
        "learning",
        "commerce",
        "booking",
    )


def test_filter_results_preserve_canonical_order(catalog: Catalog) -> None:
    selected = catalog.filter(component_class="platform_service")
    positions = [
        catalog.component_ids.index(entry["component_id"]) for entry in selected
    ]
    assert positions == sorted(positions)


def test_entry_lookup_of_an_unknown_component_fails(catalog: Catalog) -> None:
    with pytest.raises(KeyError):
        catalog.entry("payments")


def test_registry_entry_lookup_of_an_unknown_component_fails(
    catalog: Catalog,
) -> None:
    with pytest.raises(KeyError):
        catalog.registry_entry("payments")


def test_the_filter_dimensions_are_exactly_the_composition_ones() -> None:
    from component_catalog import FILTER_DIMENSIONS

    assert set(FILTER_DIMENSIONS) == {
        "component_class",
        "data_scope",
        "lifecycle_state",
        "owner",
        "scs_id",
    }


# ---------------------------------------------------------------------------
# Negative: structural rejection
# ---------------------------------------------------------------------------


def test_a_non_object_catalog_document_is_rejected(root: Path) -> None:
    errors = errors_for(["not", "an", "object"], root)
    assert errors
    assert all(error.startswith("$:") for error in errors)


@pytest.mark.parametrize(
    "missing", ["catalog_id", "catalog_schema_version", "derived_from", "components"]
)
def test_missing_required_top_level_fields_are_rejected(
    document: Mapping, root: Path, missing: str
) -> None:
    broken = copy.deepcopy(document)
    broken.pop(missing)
    rejects(broken, root, f"missing required property {missing!r}")


def test_unknown_top_level_properties_are_rejected(
    document: Mapping, root: Path
) -> None:
    broken = copy.deepcopy(document)
    broken["annotations"] = {"hand": "edit"}
    rejects(broken, root, "unexpected property 'annotations'")


def test_hand_edited_entry_annotations_are_rejected(
    document: Mapping, root: Path
) -> None:
    broken = with_entry(document, "booking", note="hand-written discovery note")
    rejects(broken, root, "unexpected property 'note'")


def test_an_entry_that_is_not_an_object_is_rejected(
    document: Mapping, root: Path
) -> None:
    broken = copy.deepcopy(document)
    broken["components"][0] = "booking"
    rejects(broken, root, "catalog entry must be an object")


def test_an_empty_catalog_is_rejected(document: Mapping, root: Path) -> None:
    broken = copy.deepcopy(document)
    broken["components"] = []
    rejects(broken, root, "expected at least 1 item(s)")
    rejects(broken, root, "is missing from the catalog")


def test_a_non_list_components_field_is_rejected(document: Mapping, root: Path) -> None:
    broken = copy.deepcopy(document)
    broken["components"] = {"booking": {}}
    rejects(broken, root, "expected type 'array'")


# ---------------------------------------------------------------------------
# Negative: divergence from the canonical registry
# ---------------------------------------------------------------------------


def test_an_unknown_component_is_rejected(document: Mapping, root: Path) -> None:
    broken = copy.deepcopy(document)
    invented = copy.deepcopy(broken["components"][-1])
    invented["component_id"] = "payments"
    broken["components"].append(invented)
    rejects(
        broken,
        root,
        "catalog describes 'payments', which the canonical registry does not register",
    )


def test_a_missing_component_is_rejected(document: Mapping, root: Path) -> None:
    broken = copy.deepcopy(document)
    broken["components"] = [
        entry for entry in broken["components"] if entry["component_id"] != "booking"
    ]
    rejects(broken, root, "registered component 'booking' is missing from the catalog")


def test_a_duplicate_component_identity_is_rejected(
    document: Mapping, root: Path
) -> None:
    broken = copy.deepcopy(document)
    broken["components"].append(copy.deepcopy(broken["components"][-1]))
    rejects(broken, root, "duplicate component identity 'booking'")


def test_an_order_divergence_is_rejected(document: Mapping, root: Path) -> None:
    broken = copy.deepcopy(document)
    broken["components"].reverse()
    rejects(broken, root, "catalog entries do not follow the canonical registry order")


def test_a_version_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", component_version="9.9.9")
    rejects(broken, root, "catalog declares '9.9.9'")
    rejects(broken, root, "the canonical registry declares '0.1.0' for 'booking'")


def test_a_class_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", **{"class": "platform_service"})
    rejects(broken, root, "catalog declares 'platform_service'")


def test_an_owner_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", owner="@SomeoneElse")
    rejects(broken, root, "catalog declares '@SomeoneElse'")


def test_a_scs_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", scs_id="SCS-999")
    rejects(broken, root, "the canonical registry declares 'SCS-003' for 'booking'")


def test_a_lowercase_scs_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", scs_id="scs-003")
    rejects(broken, root, "the canonical registry declares 'SCS-003' for 'booking'")


def test_a_maturity_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", maturity_level="level_2")
    rejects(broken, root, "the canonical registry declares 'level_0_modular_monolith'")


def test_a_data_scope_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(
        document,
        "booking",
        data_scopes=["platform-scoped", "tenant-scoped", "system-scoped"],
    )
    rejects(
        broken,
        root,
        "catalog declares ['platform-scoped', 'tenant-scoped', 'system-scoped']",
    )


def test_a_missing_data_scope_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", data_scopes=["platform-scoped"])
    rejects(broken, root, "catalog declares ['platform-scoped']")


def test_a_lifecycle_state_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_nested(document, "booking", "lifecycle", registry_state="deprecated")
    rejects(broken, root, "catalog declares 'deprecated'")


def test_a_deployable_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_nested(document, "booking", "lifecycle", deployable=True)
    rejects(broken, root, "catalog declares True")


def test_a_blockers_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_nested(document, "booking", "lifecycle", publishability_blockers=[])
    rejects(broken, root, "catalog declares []")


def test_a_contract_reference_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_nested(
        document,
        "booking",
        "contracts",
        component_contract="components/learning/contract/component_contract.json",
    )
    rejects(
        broken,
        root,
        "the canonical registry declares 'components/booking/contract/component_contract.json'",
    )


def test_an_openapi_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_nested(
        document,
        "saga",
        "contracts",
        openapi="components/saga/contract/openapi.yaml",
    )
    rejects(broken, root, "the canonical registry declares None")


def test_a_digest_drift_is_rejected(document: Mapping, root: Path) -> None:
    forged = "sha256:" + "0" * 64
    broken = with_entry(document, "booking", entry_digest=forged)
    rejects(broken, root, "does not pin the canonical registry entry of 'booking'")


def test_a_summary_drift_is_rejected(document: Mapping, root: Path) -> None:
    broken = with_entry(document, "booking", summary="Hand-written summary.")
    rejects(broken, root, "does not mirror the task statement")


def test_a_summary_with_a_trailing_space_is_rejected(
    document: Mapping, root: Path
) -> None:
    task = contract_task(root, "booking")
    assert task is not None
    broken = with_entry(document, "booking", summary=f"{task} ")
    rejects(broken, root, "does not mirror the task statement")


def test_derived_from_registry_path_drift_is_rejected(
    document: Mapping, root: Path
) -> None:
    broken = copy.deepcopy(document)
    broken["derived_from"]["canonical_registry"] = "factory/registry/other.json"
    rejects(broken, root, "is not the canonical registry")


def test_derived_from_registry_id_drift_is_rejected(
    document: Mapping, root: Path
) -> None:
    broken = copy.deepcopy(document)
    broken["derived_from"]["registry_id"] = "some-other-registry"
    rejects(broken, root, "does not link to the canonical registry identity")


def test_derived_from_contracts_root_drift_is_rejected(
    document: Mapping, root: Path
) -> None:
    broken = copy.deepcopy(document)
    broken["derived_from"]["component_contracts_root"] = "contracts"
    rejects(broken, root, "is not the canonical component contracts root")


def test_a_catalog_identity_drift_is_rejected(document: Mapping, root: Path) -> None:
    """Only the derived-form check catches a changed catalog identity."""
    broken = copy.deepcopy(document)
    broken["catalog_id"] = "application-factory-component-catalog-v2"
    errors = errors_for(broken, root)
    assert errors == [
        (
            "catalog document: not the deterministic derived view of the canonical "
            "registry and the published contracts; "
            "regenerate it (python -m component_catalog build)"
        )
    ]


def test_a_missing_entry_field_is_rejected(document: Mapping, root: Path) -> None:
    broken = without_key(document, "booking", "entry_digest")
    rejects(broken, root, "missing required property 'entry_digest'")


# ---------------------------------------------------------------------------
# Negative: the canonical sources remain authoritative
# ---------------------------------------------------------------------------


def test_the_registry_remains_authoritative(
    tmp_path: Path, registry: Registry, document: Mapping
) -> None:
    """A registry change must surface as catalog divergence (criterion 3)."""
    root = materialize(tmp_path)
    mutated = copy.deepcopy(registry.document)
    for entry in mutated["components"]:
        if entry["component_id"] == "booking":
            entry["owner"] = "@NewOwner"
    write_registry_document(root, mutated)

    rejects(document, root, "catalog declares '@Kvasha62'")
    rejects(document, root, "the canonical registry declares '@NewOwner' for 'booking'")


def test_a_stale_digest_after_an_unmirrored_registry_change_is_rejected(
    tmp_path: Path, registry: Registry, document: Mapping
) -> None:
    """The digest pins fields the view does not even copy."""
    root = materialize(tmp_path)
    mutated = copy.deepcopy(registry.document)
    for entry in mutated["components"]:
        if entry["component_id"] == "booking":
            entry["owner_source"] = "docs/README.md"
    write_registry_document(root, mutated)

    rejects(document, root, "does not pin the canonical registry entry of 'booking'")


def test_the_published_contract_remains_authoritative(
    tmp_path: Path, document: Mapping
) -> None:
    root = materialize(tmp_path)
    contract = root / "components" / "booking" / "contract" / "component_contract.json"
    published = json.loads(contract.read_text(encoding="utf-8"))
    published["task"] = "A brand new booking task statement."
    contract.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")

    rejects(document, root, "does not mirror the task statement")


def test_a_decoy_contract_does_not_satisfy_derivation(
    tmp_path: Path, document: Mapping
) -> None:
    """A contract outside the canonical path proves nothing (Slice A doctrine)."""
    root = materialize(tmp_path)
    decoy = root / "components" / "booking" / "contract" / "decoy_contract.json"
    decoy.write_text(json.dumps({"task": "DECOY TASK"}) + "\n", encoding="utf-8")
    broken = with_entry(document, "booking", summary="DECOY TASK")

    rejects(broken, root, "does not mirror the task statement")


def test_a_missing_registry_is_reported(tmp_path: Path, document: Mapping) -> None:
    root = materialize(tmp_path)
    (root / REGISTRY_PATH).unlink()
    rejects(
        document,
        root,
        "the canonical registry is not loadable and valid",
    )


def test_a_missing_registry_fails_the_strict_load(tmp_path: Path) -> None:
    root = materialize(tmp_path)
    (root / REGISTRY_PATH).unlink()
    with pytest.raises(RegistryNotFoundError):
        load_catalog(root)


def test_an_invalid_registry_fails_as_a_registry_error(
    tmp_path: Path, registry: Registry
) -> None:
    root = materialize(tmp_path)
    mutated = copy.deepcopy(registry.document)
    mutated["components"][0]["component_version"] = "latest"
    write_registry_document(root, mutated)

    with pytest.raises(RegistryValidationError):
        load_catalog(root)


def test_the_catalog_does_not_validate_against_invalid_sources(
    tmp_path: Path, registry: Registry, document: Mapping
) -> None:
    root = materialize(tmp_path)
    mutated = copy.deepcopy(registry.document)
    mutated["components"][0]["component_version"] = "latest"
    write_registry_document(root, mutated)

    errors = errors_for(document, root)
    assert errors
    assert all("canonical registry is not loadable" in error for error in errors)


# ---------------------------------------------------------------------------
# Negative: loading failures
# ---------------------------------------------------------------------------


def test_a_missing_catalog_file_raises_catalog_not_found(tmp_path: Path) -> None:
    root = materialize(tmp_path)
    (root / CATALOG_PATH).unlink()
    with pytest.raises(CatalogNotFoundError):
        load_catalog_document(root / CATALOG_PATH)
    with pytest.raises(CatalogNotFoundError):
        load_catalog(root)


def test_an_unreadable_catalog_raises_catalog_not_found(tmp_path: Path) -> None:
    root = materialize(tmp_path)
    (root / CATALOG_PATH).write_text("{ not json", encoding="utf-8")
    with pytest.raises(CatalogNotFoundError):
        load_catalog_document(root / CATALOG_PATH)


def test_a_non_object_catalog_raises_catalog_not_found(tmp_path: Path) -> None:
    root = materialize(tmp_path)
    (root / CATALOG_PATH).write_text("[]", encoding="utf-8")
    with pytest.raises(CatalogNotFoundError):
        load_catalog_document(root / CATALOG_PATH)


def test_a_diverged_catalog_fails_the_strict_load(
    tmp_path: Path, document: Mapping
) -> None:
    root = materialize(tmp_path)
    write_catalog_document(root, with_entry(document, "booking", owner="@SomeoneElse"))
    with pytest.raises(CatalogValidationError):
        load_catalog(root)


def test_a_non_strict_load_reports_violations_instead(
    tmp_path: Path, document: Mapping
) -> None:
    root = materialize(tmp_path)
    write_catalog_document(root, with_entry(document, "booking", owner="@SomeoneElse"))
    loaded = load_catalog(root, strict=False)
    assert loaded.validate()


def test_catalog_errors_share_one_hierarchy() -> None:
    assert issubclass(CatalogNotFoundError, CatalogError)
    assert issubclass(CatalogValidationError, CatalogError)


# ---------------------------------------------------------------------------
# Negative: selectors and versions
# ---------------------------------------------------------------------------


def test_latest_is_rejected_as_a_component_version(
    document: Mapping, root: Path
) -> None:
    broken = with_entry(document, "booking", component_version="latest")
    rejects(broken, root, "floating selector")
    rejects(broken, root, "an explicit SemVer version is required")


@pytest.mark.parametrize(
    "version",
    [
        "current",
        "stable",
        "default",
        "0.1.*",
        "*",
        "0.1",
        "v0.1.0",
        "0.1.0-rc1",
        "0.1.0.0",
    ],
)
def test_malformed_and_floating_versions_are_rejected(
    document: Mapping, root: Path, version: str
) -> None:
    broken = with_entry(document, "booking", component_version=version)
    rejects(broken, root, "the canonical registry declares '0.1.0' for 'booking'")


def test_a_floating_component_identity_is_rejected(
    document: Mapping, root: Path
) -> None:
    broken = with_entry(document, "booking", component_id="latest")
    rejects(broken, root, "is a floating selector, not a stable component identity")


@pytest.mark.parametrize(
    "digest",
    [
        "latest",
        "0" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "0" * 63,
        "sha256:" + "0" * 65,
        "md5:" + "0" * 64,
    ],
)
def test_malformed_digests_are_rejected(
    document: Mapping, root: Path, digest: str
) -> None:
    broken = with_entry(document, "booking", entry_digest=digest)
    rejects(broken, root, "does not pin the canonical registry entry of 'booking'")


def test_the_canonical_document_selects_nothing_by_a_floating_selector(
    document: Mapping,
) -> None:
    from component_registry import is_floating_selector

    for entry in document["components"]:
        for field in (
            "component_id",
            "component_version",
            "class",
            "scs_id",
            "owner",
        ):
            assert not is_floating_selector(entry[field]), (
                entry["component_id"],
                field,
            )
        assert not is_floating_selector(entry["lifecycle"]["registry_state"])
        assert not is_floating_selector(entry["entry_digest"])
        for reference in entry["contracts"].values():
            assert not is_floating_selector(reference)


def test_the_word_latest_in_prose_is_not_a_selector(
    document: Mapping, root: Path
) -> None:
    """A prose summary mentioning latest is not a selector — but it is drift."""
    task = contract_task(root, "booking")
    assert task is not None
    broken = with_entry(document, "booking", summary=f"{task} Not the latest.")
    errors = errors_for(broken, root)
    assert errors
    assert not any("floating selector" in error for error in errors)
    assert any("does not mirror the task statement" in error for error in errors)


# ---------------------------------------------------------------------------
# Negative: adversarial references
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        "https://evil.example/booking/contract/component_contract.json",
        "../booking/contract/component_contract.json",
        "./components/booking/contract/component_contract.json",
        "components//booking/contract/component_contract.json",
        "components/booking/contract/component_contract.json#task",
    ],
)
def test_non_canonical_contract_references_are_rejected(
    document: Mapping, root: Path, reference: str
) -> None:
    broken = with_nested(document, "booking", "contracts", component_contract=reference)
    errors = errors_for(broken, root)
    assert errors
    assert any(
        "does not match pattern" in error or "catalog declares" in error
        for error in errors
    )


# ---------------------------------------------------------------------------
# Boundary: filter semantics
# ---------------------------------------------------------------------------


def test_a_valid_scope_with_no_components_matches_nothing(catalog: Catalog) -> None:
    assert catalog.filter(data_scope="system-scoped") == ()


def test_an_unknown_owner_matches_nothing(catalog: Catalog) -> None:
    assert catalog.filter(owner="@nobody") == ()


def test_an_unknown_scs_matches_nothing(catalog: Catalog) -> None:
    assert catalog.filter(scs_id="SCS-999") == ()


@pytest.mark.parametrize(
    ("dimension", "value"),
    [
        ("component_class", "galaxy_system"),
        ("component_class", "business"),
        ("data_scope", "tenant"),
        ("data_scope", "galaxy-scoped"),
        ("lifecycle_state", "active"),
        ("lifecycle_state", "certified"),
    ],
)
def test_invalid_closed_vocabulary_filters_are_rejected(
    catalog: Catalog, dimension: str, value: str
) -> None:
    with pytest.raises(ValueError, match="is not a valid"):
        catalog.filter(**{dimension: value})


@pytest.mark.parametrize(
    ("dimension", "value"),
    [("owner", ""), ("owner", "   "), ("scs_id", ""), ("scs_id", " \t ")],
)
def test_empty_open_value_filters_are_rejected(
    catalog: Catalog, dimension: str, value: str
) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        catalog.filter(**{dimension: value})


@pytest.mark.parametrize(
    ("dimension", "value"),
    [
        ("owner", "latest"),
        ("owner", "current"),
        ("scs_id", "main"),
        ("scs_id", "*"),
        ("data_scope", "default"),
        ("component_class", "stable"),
        ("lifecycle_state", "head"),
    ],
)
def test_floating_selectors_are_rejected_as_filter_values(
    catalog: Catalog, dimension: str, value: str
) -> None:
    with pytest.raises(ValueError):
        catalog.filter(**{dimension: value})


def test_owner_latest_is_rejected_as_a_floating_selector(
    catalog: Catalog,
) -> None:
    with pytest.raises(ValueError, match="floating selector"):
        catalog.filter(owner="latest")


# ---------------------------------------------------------------------------
# Boundary: the command line tool
# ---------------------------------------------------------------------------


def test_cli_validate_is_green_on_the_canonical_catalog(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["validate"]) == 0
    assert "valid" in capsys.readouterr().out


def test_cli_build_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = materialize(tmp_path)
    patch_discover_root(monkeypatch, root)
    before = (root / CATALOG_PATH).read_text(encoding="utf-8")

    assert cli_main(["build"]) == 0
    assert (root / CATALOG_PATH).read_text(encoding="utf-8") == before


def test_cli_build_regenerates_a_diverged_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: Mapping
) -> None:
    root = materialize(tmp_path)
    patch_discover_root(monkeypatch, root)
    write_catalog_document(root, with_entry(document, "booking", owner="@SomeoneElse"))
    assert cli_main(["validate"]) == 1

    assert cli_main(["build"]) == 0
    assert cli_main(["validate"]) == 0


def test_cli_without_a_command_fails() -> None:
    with pytest.raises(SystemExit):
        cli_main([])


# ---------------------------------------------------------------------------
# Boundary: architecture — ownership and no internals
# ---------------------------------------------------------------------------

#: The only modules the catalog implementation may depend on: the standard
#: library and the Slice A factory tooling whose public surface it derives
#: from. Everything else — component internals, databases, drivers, shell
#: outs — is out of bounds (ADR-0015 §5, §11; ARCHITECTURE.md §1.1, §18).
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "argparse",
        "collections",
        "dataclasses",
        "hashlib",
        "json",
        "pathlib",
        "component_registry",
        "component_catalog",
    }
)

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "authorization_service",
        "booking_service",
        "commerce_service",
        "identity_service",
        "learning_service",
        "records_service",
        "tenant_authority",
        "saga",
        "idempotency",
        "sqlite3",
        "psycopg",
        "subprocess",
        "os",
    }
)


def test_the_catalog_implementation_imports_only_canonical_sources(
    root: Path,
) -> None:
    package = root / "src" / "component_catalog"
    sources = sorted(package.glob("*.py"))
    assert sources, "the catalog package must exist"
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                root_module = name.split(".")[0]
                assert (
                    root_module in ALLOWED_IMPORT_ROOTS
                ), f"{source.name} imports {name!r}, which is not an allowed source"
                assert root_module not in FORBIDDEN_IMPORT_ROOTS


def test_the_catalog_never_touches_a_component_database(root: Path) -> None:
    package = root / "src" / "component_catalog"
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8").lower()
        for keyword in ("sqlite", "psycopg", ".execute(", "cursor(", "create table"):
            assert keyword not in text, (source.name, keyword)


def test_the_catalog_document_has_no_extra_sections(document: Mapping) -> None:
    """The document carries the derived view and its linkage — nothing else."""
    assert set(document) == {
        "$schema",
        "catalog_id",
        "catalog_schema_version",
        "derived_from",
        "components",
    }
