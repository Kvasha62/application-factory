"""Focused tests for Slice A — Component Registry (Issue #62, ADR-0015 §15).

These tests verify architecture rules, not merely successful object
construction (ADR-0015 §16): canonicality, schema validity, identity
uniqueness, explicit versions, contract references, dependencies,
compatibility, ownership, artifact identity, lifecycle, boundary
preservation and the deterministic rejection of invalid entries.

The ``latest`` tests deserve a note. ``latest`` is not forbidden as a string:
it is forbidden as a *selector* — as a way of choosing a version, a release,
an artifact, a dependency or a production target. Several tests therefore
feed ``latest`` into selector fields and require rejection, while another
test proves the very same word is accepted inside prose.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from component_registry import (
    FLOATING_SELECTOR_TOKENS,
    REGISTRY_PATH,
    Registry,
    RegistryNotFoundError,
    RegistryValidationError,
    discover_root,
    is_floating_selector,
    load_registry,
    load_registry_document,
    parse_semver,
    range_admits,
    validate_document,
)
from component_registry.schema import SCHEMA_PATH, load_schema

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


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def document(root: Path) -> Mapping:
    return load_registry_document(root / REGISTRY_PATH)


@pytest.fixture(scope="module")
def registry(root: Path):
    return load_registry(root)


def errors_for(document: object, root: Path, **kwargs: object) -> list[str]:
    return validate_document(document, root=root, **kwargs)  # type: ignore[arg-type]


def with_entry(document: Mapping, target: str, **fields: object) -> Mapping:
    """Return a deep copy whose entry ``target`` has ``fields`` merged in."""
    clone = copy.deepcopy(document)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry.update(fields)
            return clone
    raise AssertionError(f"no such component: {target}")


def with_nested(
    document: Mapping, target: str, section: str, **fields: object
) -> Mapping:
    """Return a deep copy whose ``section`` block of one entry is merged."""
    clone = copy.deepcopy(document)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry[section].update(fields)
            return clone
    raise AssertionError(f"no such component: {target}")


def without_key(document: Mapping, target: str, key: str) -> Mapping:
    clone = copy.deepcopy(document)
    for entry in clone["components"]:
        if entry.get("component_id") == target:
            entry.pop(key, None)
            return clone
    raise AssertionError(f"no such component: {target}")


def rejects(document: object, root: Path, fragment: str, **kwargs: object) -> None:
    """Assert validation rejects ``document`` with an error containing ``fragment``."""
    errors = errors_for(document, root, **kwargs)  # type: ignore[arg-type]
    assert errors, "expected validation to fail, but the document was accepted"
    assert any(
        fragment in error for error in errors
    ), f"expected an error containing {fragment!r}, got:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# Positive: canonical registry
# ---------------------------------------------------------------------------


def test_the_canonical_registry_exists_and_is_machine_readable(root: Path) -> None:
    path = root / REGISTRY_PATH
    assert path.is_file(), "the canonical registry document must exist"
    loaded = load_registry_document(path)
    assert isinstance(loaded, Mapping)
    assert (root / SCHEMA_PATH).is_file(), "the normative registry schema must exist"


def test_the_canonical_registry_is_valid(registry) -> None:
    assert registry.validate() == []


def test_the_canonical_registry_loads_strictly(root: Path) -> None:
    assert load_registry(root, strict=True).entries


def test_the_registry_registers_exactly_the_verified_nine_components(registry) -> None:
    assert registry.component_ids == EXPECTED_INVENTORY
    assert len(registry.entries) == 9


def test_the_registry_invented_no_component(root: Path, registry) -> None:
    published = {
        path.parent.parent.name
        for path in (root / "components").glob("*/contract/component_contract.json")
    }
    assert set(registry.component_ids) == published


def test_the_three_business_systems_are_identified(registry) -> None:
    for component_id, scs_id in BUSINESS_SYSTEMS.items():
        entry = registry.entry(component_id)
        assert entry["class"] == "business_system"
        assert entry["scs_id"] == scs_id


def test_every_entry_declares_the_full_required_metadata(registry) -> None:
    required = (
        "component_id",
        "component_version",
        "class",
        "owner",
        "contracts",
        "data_ownership",
        "dependencies",
        "compatibility",
        "artifact",
        "lifecycle",
    )
    for entry in registry.entries:
        for key in required:
            assert key in entry, f"{entry.get('component_id')} is missing {key}"


def test_validation_is_deterministic(registry, root: Path, document: Mapping) -> None:
    first = validate_document(document, root=root)
    second = validate_document(document, root=root)
    third = validate_document(copy.deepcopy(document), root=root)
    assert first == second == third == []


# ---------------------------------------------------------------------------
# Positive: identity
# ---------------------------------------------------------------------------


def test_component_identities_are_unique(registry) -> None:
    ids = list(registry.component_ids)
    assert len(ids) == len(set(ids)), f"duplicate identities: {ids}"


def test_component_identities_are_stable_and_deterministic(registry) -> None:
    for component_id in registry.component_ids:
        assert component_id == component_id.strip().lower()
        assert is_floating_selector(component_id) is False
        assert parse_semver(component_id) is None  # an identity is not a version


def test_every_identity_resolves_to_exactly_one_entry(registry) -> None:
    for component_id in registry.component_ids:
        matches = [e for e in registry.entries if e.get("component_id") == component_id]
        assert len(matches) == 1


# ---------------------------------------------------------------------------
# Positive: versioning
# ---------------------------------------------------------------------------


def test_every_version_is_explicit_semver(registry) -> None:
    for entry in registry.entries:
        version = entry["component_version"]
        assert (
            parse_semver(version) is not None
        ), f"{entry['component_id']}: {version!r}"
        assert is_floating_selector(version) is False


def test_no_registered_version_is_a_floating_selector(registry) -> None:
    for entry in registry.entries:
        version = str(entry["component_version"]).strip().lower()
        assert version not in FLOATING_SELECTOR_TOKENS
        assert "*" not in version


def test_registered_versions_match_the_published_contracts(
    root: Path, registry
) -> None:
    for component_id in registry.component_ids:
        entry = registry.entry(component_id)
        contract = root / entry["contracts"]["component_contract"]
        assert contract.is_file()
        assert entry["component_version"] == registry.version(component_id)


# ---------------------------------------------------------------------------
# Positive: contract references
# ---------------------------------------------------------------------------


def test_every_entry_references_its_published_component_contract(
    root: Path, registry
) -> None:
    for component_id in registry.component_ids:
        entry = registry.entry(component_id)
        reference = entry["contracts"]["component_contract"]
        assert isinstance(reference, str) and reference
        assert (root / reference).is_file(), f"broken contract reference: {reference}"
        assert component_id in reference


def test_openapi_references_resolve_when_declared(root: Path, registry) -> None:
    declared = 0
    for entry in registry.entries:
        openapi = entry["contracts"].get("openapi")
        if openapi is None:
            continue
        declared += 1
        assert (root / openapi).is_file(), f"broken contract reference: {openapi}"
    assert declared >= 1, "at least one component publishes an OpenAPI contract"


def test_the_registry_references_contracts_without_restating_them(registry) -> None:
    """The registry points at contracts; it must not embed their content."""
    for entry in registry.entries:
        for key in ("api", "events", "configuration_schema", "authz", "saga_contract"):
            assert (
                key not in entry
            ), f"{entry['component_id']} restates contract content: {key}"


# ---------------------------------------------------------------------------
# Positive: dependencies
# ---------------------------------------------------------------------------


def test_dependencies_are_explicit_and_resolve_inside_the_registry(registry) -> None:
    for component_id in registry.component_ids:
        for dependency in registry.dependencies(component_id):
            target = dependency["component_id"]
            assert (
                target in registry.component_ids
            ), f"{component_id} -> unknown {target}"
            assert target != component_id, f"{component_id} depends on itself"
            assert dependency["version_range"]
            assert dependency["kind"]


def test_every_declared_range_admits_the_registered_version_of_its_target(
    registry,
) -> None:
    for component_id in registry.component_ids:
        for dependency in registry.dependencies(component_id):
            target = dependency["component_id"]
            assert range_admits(
                dependency["version_range"], registry.version(target)
            ), (
                f"{component_id} -> {target} {dependency['version_range']} "
                f"does not admit {registry.version(target)}"
            )


def test_dependency_ranges_are_never_floating(registry) -> None:
    for component_id in registry.component_ids:
        for dependency in registry.dependencies(component_id):
            version_range = dependency["version_range"]
            assert is_floating_selector(version_range) is False
            assert "*" not in version_range


def test_no_dependency_uses_a_forbidden_boundary_mechanism(registry) -> None:
    forbidden = {"database", "internal_code", "private_schema", "internal_queue"}
    for component_id in registry.component_ids:
        for dependency in registry.dependencies(component_id):
            assert dependency["kind"] not in forbidden


# ---------------------------------------------------------------------------
# Positive: compatibility
# ---------------------------------------------------------------------------


def test_compatibility_metadata_is_explicit_and_machine_readable(registry) -> None:
    for entry in registry.entries:
        compatibility = entry["compatibility"]
        assert compatibility["api_policy"] == "additive-minor-breaking-major"
        assert compatibility["breaking_change_requires"] == "major_version"
        assert compatibility["source"]


def test_compatibility_metadata_agrees_with_the_published_contract(registry) -> None:
    for entry in registry.entries:
        compatibility = entry["compatibility"]
        assert f"{entry['contracts']['component_contract']}#/compatibility_policy" == (
            compatibility["source"]
        )


# ---------------------------------------------------------------------------
# Positive: ownership
# ---------------------------------------------------------------------------


def test_every_component_has_an_explicit_owner(registry) -> None:
    for entry in registry.entries:
        owner = entry["owner"]
        assert isinstance(owner, str) and owner.strip()


def test_data_ownership_belongs_to_the_component_that_owns_the_data(registry) -> None:
    for entry in registry.entries:
        assert entry["data_ownership"]["owner"] == entry["component_id"]


def test_data_scopes_are_exactly_one_of_the_architectural_scopes(registry) -> None:
    allowed = {"tenant-scoped", "platform-scoped", "system-scoped"}
    for entry in registry.entries:
        ownership = entry["data_ownership"]
        assert ownership["data_scopes"]
        for scope in ownership["data_scopes"]:
            assert scope in allowed
        for dataset in ownership["datasets"]:
            assert dataset["scope"] in allowed
            assert dataset["name"]


def test_business_data_is_owned_by_the_business_systems(registry) -> None:
    assert "orders" in {
        d["name"] for d in registry.entry("commerce")["data_ownership"]["datasets"]
    }
    assert "courses" in {
        d["name"] for d in registry.entry("learning")["data_ownership"]["datasets"]
    }
    assert "reservations" in {
        d["name"] for d in registry.entry("booking")["data_ownership"]["datasets"]
    }


# ---------------------------------------------------------------------------
# Positive: artifact identity and lifecycle
# ---------------------------------------------------------------------------


def test_artifact_identity_is_explicit_for_every_entry(registry) -> None:
    for entry in registry.entries:
        artifact = entry["artifact"]
        assert artifact["artifact_type"] == "none"
        assert artifact["digest"] is None
        assert artifact["pinned"] is False


def test_no_artifact_uses_a_floating_selector(registry) -> None:
    for entry in registry.entries:
        artifact = entry["artifact"]
        assert is_floating_selector(artifact["digest"]) is False
        assert is_floating_selector(artifact["artifact_type"]) is False


def test_lifecycle_state_is_explicit_for_every_entry(registry) -> None:
    for entry in registry.entries:
        lifecycle = entry["lifecycle"]
        assert lifecycle["registry_state"] == "registered"
        assert lifecycle["publishable"] is False
        assert lifecycle["deployable"] is False
        assert lifecycle["publishability_blockers"]


def test_registered_is_not_deployable_and_not_publishable(registry) -> None:
    """A registry entry records existence; it certifies nothing."""
    for entry in registry.entries:
        assert entry["lifecycle"]["registry_state"] == "registered"
        assert entry["lifecycle"]["deployable"] is False
        assert entry["lifecycle"]["publishable"] is False


# ---------------------------------------------------------------------------
# Negative: malformed registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "not-a-registry",
        42,
        {},
        {"registry_id": "r", "registry_schema_version": "1.0.0"},
        {"registry_id": "r", "registry_schema_version": "1.0.0", "components": {}},
        {"registry_id": "r", "registry_schema_version": "1.0.0", "components": []},
    ],
)
def test_malformed_registries_are_rejected(document: object, root: Path) -> None:
    assert errors_for(document, root)


def test_an_entry_that_is_not_an_object_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"].append("authorization")
    rejects(clone, root, "component entry must be an object")


def test_unknown_properties_are_rejected(root: Path, document: Mapping) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["unexpected"] = "value"
    rejects(clone, root, "unexpected property")


# ---------------------------------------------------------------------------
# Negative: identity
# ---------------------------------------------------------------------------


def test_duplicate_identity_is_rejected(root: Path, document: Mapping) -> None:
    clone = copy.deepcopy(document)
    clone["components"].append(copy.deepcopy(clone["components"][0]))
    rejects(clone, root, "duplicate component identity")


def test_duplicate_identity_under_a_different_position_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    duplicate = copy.deepcopy(clone["components"][3])
    clone["components"].insert(0, duplicate)
    rejects(clone, root, "duplicate component identity")


@pytest.mark.parametrize(
    "identity", ["Authorization", "auth service", "1identity", "", "auth-2"]
)
def test_invalid_identity_is_rejected(
    identity: str, root: Path, document: Mapping
) -> None:
    clone = with_entry(document, "authorization", component_id=identity)
    assert errors_for(clone, root)


def test_a_missing_identity_is_rejected(root: Path, document: Mapping) -> None:
    rejects(
        without_key(document, "authorization", "component_id"),
        root,
        "stable component identity",
    )


# ---------------------------------------------------------------------------
# Negative: version — including the mandatory "latest" rejection
# ---------------------------------------------------------------------------


def test_latest_is_rejected_as_a_component_version(
    root: Path, document: Mapping
) -> None:
    """The mandatory negative case: `latest` is never a version selector."""
    clone = with_entry(document, "authorization", component_version="latest")
    rejects(clone, root, "floating selector")


@pytest.mark.parametrize(
    "version", ["latest", "LATEST", "Latest", " current ", "default", "stable"]
)
def test_floating_selectors_are_rejected_as_versions(
    version: str, root: Path, document: Mapping
) -> None:
    clone = with_entry(document, "authorization", component_version=version)
    rejects(clone, root, "floating selector")


@pytest.mark.parametrize(
    "version", ["1.0", "v1.0.0", "01.0.0", "1.0.0-beta", "", "1..0"]
)
def test_malformed_versions_are_rejected(
    version: str, root: Path, document: Mapping
) -> None:
    clone = with_entry(document, "authorization", component_version=version)
    assert errors_for(clone, root)


def test_a_version_that_disagrees_with_the_published_contract_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_entry(document, "authorization", component_version="9.9.9")
    rejects(clone, root, "component_version")


# ---------------------------------------------------------------------------
# Negative: ownership and contract references
# ---------------------------------------------------------------------------


def test_a_missing_owner_is_rejected(root: Path, document: Mapping) -> None:
    rejects(without_key(document, "authorization", "owner"), root, "owner")


def test_an_empty_owner_is_rejected(root: Path, document: Mapping) -> None:
    rejects(with_entry(document, "authorization", owner="   "), root, "owner")


def test_a_missing_component_contract_reference_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["contracts"].pop("component_contract")
    rejects(clone, root, "component_contract")


def test_a_broken_contract_reference_is_rejected(root: Path, document: Mapping) -> None:
    clone = with_nested(
        document,
        "authorization",
        "contracts",
        component_contract="components/authorization/contract/does_not_exist.json",
    )
    rejects(clone, root, "does not exist")


def test_a_contract_reference_outside_the_repository_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document, "authorization", "contracts", component_contract="../../etc/passwd"
    )
    rejects(clone, root, "escapes the repository")


def test_a_contract_declaring_another_component_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "contracts",
        component_contract="components/identity/contract/component_contract.json",
    )
    rejects(clone, root, "contract declares component_id")


def test_an_invalid_data_scope_is_rejected(root: Path, document: Mapping) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["data_ownership"]["datasets"][0]["scope"] = "everywhere"
    rejects(clone, root, "is not a data scope")


def test_an_invalid_data_scope_summary_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "data_ownership",
        data_scopes=["everywhere"],
    )
    rejects(clone, root, "data_scopes")


def test_a_duplicate_dataset_is_rejected(root: Path, document: Mapping) -> None:
    clone = copy.deepcopy(document)
    ownership = clone["components"][0]["data_ownership"]
    ownership["datasets"].append(copy.deepcopy(ownership["datasets"][0]))
    rejects(clone, root, "duplicate dataset")


def test_data_owned_by_another_component_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(document, "authorization", "data_ownership", owner="identity")
    rejects(clone, root, "must be the component that owns the data")


def test_ownership_metadata_that_diverges_from_the_contract_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    ownership = clone["components"][0]["data_ownership"]
    ownership["datasets"].append(
        {"name": "invented_dataset", "scope": "platform-scoped"}
    )
    ownership["data_scopes"] = sorted({d["scope"] for d in ownership["datasets"]})
    rejects(clone, root, "diverges from the published contract")


# ---------------------------------------------------------------------------
# Negative: dependencies
# ---------------------------------------------------------------------------


def test_a_dependency_on_an_unregistered_component_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"].append(
        {
            "component_id": "payments",
            "version_range": ">=1.0.0",
            "kind": "api",
            "contract": None,
        }
    )
    rejects(clone, root, "is not registered")


def test_a_self_dependency_is_rejected(root: Path, document: Mapping) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"].append(
        {
            "component_id": "authorization",
            "version_range": ">=0.1.0",
            "kind": "api",
            "contract": None,
        }
    )
    rejects(clone, root, "may not depend on itself")


def test_a_duplicate_dependency_is_rejected(root: Path, document: Mapping) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"].append(
        copy.deepcopy(clone["components"][0]["dependencies"][0])
    )
    rejects(clone, root, "duplicate dependency")


@pytest.mark.parametrize(
    "version_range", ["latest", "*", ">=latest", "1.0.0", "", ">=0.1.0,"]
)
def test_invalid_dependency_ranges_are_rejected(
    version_range: str, root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"][0]["version_range"] = version_range
    rejects(clone, root, "version_range")


def test_a_dependency_range_that_excludes_the_registered_version_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"][0]["version_range"] = ">=9.0.0,<10.0.0"
    rejects(clone, root, "does not admit the registered version")


def test_a_dependency_on_a_database_is_rejected(root: Path, document: Mapping) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"][0]["kind"] = "database"
    rejects(clone, root, "crosses a component boundary")


def test_a_dependency_on_internal_code_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"][0]["kind"] = "internal_code"
    rejects(clone, root, "crosses a component boundary")


def test_a_dependency_pointing_at_another_component_contract_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"][0]["dependencies"][0][
        "contract"
    ] = "components/commerce/contract/component_contract.json"
    rejects(clone, root, "dependency targets")


# ---------------------------------------------------------------------------
# Negative: compatibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["latest", "whatever-works", "additive-always"])
def test_invalid_api_policies_are_rejected(
    policy: str, root: Path, document: Mapping
) -> None:
    clone = with_nested(document, "authorization", "compatibility", api_policy=policy)
    rejects(clone, root, "compatibility.api_policy")


def test_latest_is_rejected_as_an_event_policy(root: Path, document: Mapping) -> None:
    clone = with_nested(
        document, "authorization", "compatibility", event_policy="latest"
    )
    rejects(clone, root, "floating selector")


def test_a_breaking_change_without_a_major_version_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "compatibility",
        breaking_change_requires="minor_version",
    )
    rejects(clone, root, "breaking_change_requires")


# ---------------------------------------------------------------------------
# Negative: artifact identity
# ---------------------------------------------------------------------------


def test_latest_is_rejected_as_an_artifact_digest(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "artifact",
        artifact_type="container_image",
        digest="latest",
    )
    rejects(clone, root, "floating selector")


def test_an_unpinned_published_artifact_is_rejected(
    root: Path, document: Mapping
) -> None:
    digest = "sha256:" + "a" * 64
    clone = with_nested(
        document,
        "authorization",
        "artifact",
        artifact_type="container_image",
        digest=digest,
        pinned=False,
    )
    rejects(clone, root, "must be pinned by digest")


def test_a_published_artifact_without_a_digest_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "artifact",
        artifact_type="container_image",
        digest=None,
        pinned=True,
    )
    rejects(clone, root, "requires an immutable digest")


def test_a_digest_on_an_artifact_that_does_not_exist_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "artifact",
        artifact_type="none",
        digest="sha256:" + "b" * 64,
        pinned=True,
    )
    rejects(clone, root, "must not declare a digest")


def test_an_invalid_artifact_type_is_rejected(root: Path, document: Mapping) -> None:
    rejects(
        with_nested(document, "authorization", "artifact", artifact_type="tarball"),
        root,
        "artifact.artifact_type",
    )


# ---------------------------------------------------------------------------
# Negative: lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["certified", "deployable", "active", "latest"])
def test_invalid_lifecycle_states_are_rejected(
    state: str, root: Path, document: Mapping
) -> None:
    clone = with_nested(document, "authorization", "lifecycle", registry_state=state)
    rejects(clone, root, "lifecycle.registry_state")


def test_latest_is_rejected_as_a_release_selector(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(document, "authorization", "lifecycle", registry_state="latest")
    rejects(clone, root, "floating selector")


def test_a_non_publishable_entry_without_a_stated_reason_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "lifecycle",
        publishable=False,
        publishability_blockers=[],
    )
    rejects(clone, root, "must state why")


def test_a_publishable_entry_with_blockers_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(
        document,
        "authorization",
        "lifecycle",
        publishable=True,
        publishability_blockers=["migrations_absent"],
    )
    rejects(clone, root, "must not state blockers")


def test_a_deployable_entry_without_a_pinned_artifact_is_rejected(
    root: Path, document: Mapping
) -> None:
    clone = with_nested(document, "authorization", "lifecycle", deployable=True)
    rejects(clone, root, "requires a pinned artifact")


def test_a_missing_lifecycle_is_rejected(root: Path, document: Mapping) -> None:
    rejects(without_key(document, "authorization", "lifecycle"), root, "lifecycle")


# ---------------------------------------------------------------------------
# Negative: inventory drift
# ---------------------------------------------------------------------------


def test_an_invented_component_is_rejected_when_the_inventory_is_checked(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"].append(
        {
            "component_id": "notifications",
            "component_version": "1.0.0",
            "class": "platform_service",
            "owner": "@Kvasha62",
            "contracts": {
                "component_contract": "components/nowhere/contract.json",
                "openapi": None,
            },
            "data_ownership": {
                "owner": "notifications",
                "data_scopes": ["platform-scoped"],
                "datasets": [{"name": "messages", "scope": "platform-scoped"}],
            },
            "dependencies": [],
            "compatibility": {
                "api_policy": "additive-minor-breaking-major",
                "event_policy": None,
                "supported_api_majors": 1,
                "breaking_change_requires": "major_version",
            },
            "artifact": {"artifact_type": "none", "digest": None, "pinned": False},
            "lifecycle": {
                "registry_state": "registered",
                "deployable": False,
                "publishable": False,
                "publishability_blockers": ["deployment_artifact_absent"],
            },
        }
    )
    rejects(clone, root, "publishes no component contract", check_inventory=True)


def test_a_missing_registered_component_is_rejected_when_the_inventory_is_checked(
    root: Path, document: Mapping
) -> None:
    clone = copy.deepcopy(document)
    clone["components"] = [
        e for e in clone["components"] if e["component_id"] != "booking"
    ]
    rejects(clone, root, "is not registered", check_inventory=True)


# ---------------------------------------------------------------------------
# The semantic rule about `latest`
# ---------------------------------------------------------------------------


def test_latest_is_rejected_as_a_selector_but_allowed_in_prose(
    root: Path, document: Mapping
) -> None:
    """The prohibition is semantic: it forbids selection, not the word."""
    selector = with_entry(document, "authorization", component_version="latest")
    assert errors_for(selector, root), "`latest` must be rejected as a version selector"

    clone = copy.deepcopy(document)
    clone.setdefault("conventions", {})[
        "selector_rule"
    ] = "the word latest is documentation here and selects nothing"
    assert (
        errors_for(clone, root) == []
    ), "the same word must remain acceptable where it performs no selection"


def test_every_floating_token_is_rejected_as_a_version(
    root: Path, document: Mapping
) -> None:
    for token in sorted(FLOATING_SELECTOR_TOKENS):
        clone = with_entry(document, "authorization", component_version=token)
        assert errors_for(
            clone, root
        ), f"{token!r} must be rejected as a version selector"


def test_the_word_latest_never_selects_anything_in_the_canonical_registry(
    root: Path, registry
) -> None:
    """No selector field of the canonical registry carries a floating token."""
    selector_fields = (
        "component_id",
        "component_version",
        "registry_schema_version",
    )
    for entry in registry.entries:
        for field in selector_fields:
            if field in entry:
                assert is_floating_selector(entry[field]) is False
        for dependency in registry.dependencies(str(entry["component_id"])):
            assert is_floating_selector(dependency["version_range"]) is False
            assert is_floating_selector(dependency["component_id"]) is False
        artifact = entry["artifact"]
        assert is_floating_selector(artifact["digest"]) is False
        assert is_floating_selector(artifact["artifact_type"]) is False
        assert is_floating_selector(entry["lifecycle"]["registry_state"]) is False


# ---------------------------------------------------------------------------
# Boundary verification — the registry stays at metadata level
# ---------------------------------------------------------------------------

_STDLIB_TOP_LEVEL = {
    "ast",
    "collections",
    "copy",
    "dataclasses",
    "json",
    "operator",
    "pathlib",
    "re",
    "typing",
    "__future__",
}


FORBIDDEN_IMPORT_PREFIXES = (
    "authorization_service",
    "booking_service",
    "commerce_service",
    "identity_service",
    "learning_service",
    "records_service",
    "tenant_authority",
    "saga",
    "idempotency",
)

DATABASE_MODULES = {
    "sqlite3",
    "psycopg2",
    "psycopg",
    "asyncpg",
    "sqlalchemy",
    "pymongo",
    "motor",
    "redis",
    "cx_Oracle",
    "pymysql",
}


def _package_sources(root: Path) -> Sequence[Path]:
    return sorted((root / "src" / "component_registry").glob("*.py"))


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_registry_implementation_imports_no_component_internals(root: Path) -> None:
    sources = _package_sources(root)
    assert sources, "the registry package must exist"
    for path in sources:
        for name in _imported_names(path):
            top = name.split(".")[0]
            assert top not in FORBIDDEN_IMPORT_PREFIXES, f"{path.name} imports {name}"


def test_the_registry_implementation_uses_no_database_access(root: Path) -> None:
    for path in _package_sources(root):
        for name in _imported_names(path):
            assert (
                name.split(".")[0] not in DATABASE_MODULES
            ), f"{path.name} imports {name}"


def test_the_registry_implementation_imports_only_the_standard_library(
    root: Path,
) -> None:
    allowed_external = {"component_registry"}
    for path in _package_sources(root):
        for name in _imported_names(path):
            top = name.split(".")[0]
            assert (
                top in allowed_external or top in _STDLIB_TOP_LEVEL
            ), f"{path.name} imports a third-party module: {name}"


def _schema_vocabulary(node: object, accumulator: set[str]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in ("properties", "required", "$defs") and isinstance(
                value, (Mapping, list)
            ):
                if isinstance(value, Mapping):
                    accumulator.update(str(item) for item in value)
                else:
                    accumulator.update(str(item) for item in value)
            _schema_vocabulary(value, accumulator)
    elif isinstance(node, list):
        for item in node:
            _schema_vocabulary(item, accumulator)


def _document_keys(node: object, accumulator: set[str], *, skip: set[str]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            accumulator.add(str(key))
            if str(key) in skip:
                continue
            _document_keys(value, accumulator, skip=skip)
    elif isinstance(node, list):
        for item in node:
            _document_keys(item, accumulator, skip=skip)


def test_the_registry_contains_no_field_outside_the_metadata_vocabulary(
    root: Path, document: Mapping
) -> None:
    """No business field can hide in the registry: every key is schema vocabulary."""
    vocabulary: set[str] = set()
    _schema_vocabulary(load_schema(root), vocabulary)

    used: set[str] = set()
    _document_keys(document, used, skip={"conventions"})

    stray = sorted(used - vocabulary)
    assert (
        stray == []
    ), f"registry carries fields outside the metadata vocabulary: {stray}"


def test_the_registry_carries_no_business_records(
    root: Path, document: Mapping
) -> None:
    """Ownership metadata names datasets; it never carries their contents."""
    forbidden = {
        "tenant_id",
        "order_id",
        "course_id",
        "reservation_id",
        "records",
        "rows",
    }
    used: set[str] = set()
    _document_keys(document, used, skip=set())
    assert not (
        used & forbidden
    ), f"business-data fields in the registry: {sorted(used & forbidden)}"
    for entry in document["components"]:
        for dataset in entry["data_ownership"]["datasets"]:
            assert set(dataset) == {"name", "scope"}


def test_the_registry_owns_no_business_data_of_the_components(registry) -> None:
    """The registry names who owns what; it does not become the owner (ADR-0015 §4)."""
    for entry in registry.entries:
        assert entry["data_ownership"]["owner"] == entry["component_id"]
        assert "data" not in entry
        assert "records" not in entry


def test_a_registry_document_that_is_not_an_object_is_refused(
    root: Path, tmp_path: Path
) -> None:
    broken = tmp_path / "registry.json"
    broken.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RegistryNotFoundError):
        load_registry_document(broken)  # decodes, but a registry is an object
    errors = validate_document([1, 2, 3], root=root, check_inventory=False)
    assert errors, "a non-object registry must be rejected"


def test_an_unreadable_registry_file_is_reported(tmp_path: Path) -> None:
    broken = tmp_path / "registry.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryNotFoundError):
        load_registry_document(broken)


def test_a_missing_registry_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(RegistryNotFoundError):
        load_registry_document(tmp_path / "absent.json")


def test_strict_loading_refuses_an_invalid_registry(root: Path) -> None:
    """An invalid registry is never handed out as if it were authoritative."""
    clone = with_entry(
        load_registry_document(root / REGISTRY_PATH),
        "booking",
        component_version="latest",
    )
    registry = Registry(document=clone, root=root, path=root / REGISTRY_PATH)
    with pytest.raises(RegistryValidationError) as raised:
        registry.require_valid()
    assert any("floating selector" in error for error in raised.value.errors)
    assert list(raised.value.errors) == sorted(set(raised.value.errors))


def test_non_strict_loading_still_reports_the_violations(root: Path) -> None:
    clone = with_entry(
        load_registry_document(root / REGISTRY_PATH),
        "booking",
        component_version="latest",
    )
    registry = Registry(document=clone, root=root, path=root / REGISTRY_PATH)
    assert registry.validate()
    assert (
        registry.validate() == registry.validate()
    ), "validation must be deterministic"


def test_validation_reports_every_violation_at_once(
    root: Path, document: Mapping
) -> None:
    clone = with_entry(
        document,
        "authorization",
        component_version="latest",
        owner="",
    )
    errors = errors_for(clone, root)
    assert len(errors) >= 2, f"validation must collect every violation, got: {errors}"
