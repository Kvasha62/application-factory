"""Machine-verifiable boundaries of the shared-library component classification.

ADR-0021 §2.2 decision D2=B classifies ``idempotency`` and ``saga`` as shared
in-process libraries rather than independently deployed Level-0 runtime
components, and ``docs/work-items/D2B-library-reclassification.md`` implements
that classification. These tests make the classification verifiable across
every surface the decision names — the registry schema, the canonical
registry, the derived catalog, the canonical documentation, the Platform
Instance definition and the environment-binding completeness rule — so a later
reintroduction of a library as an independently deployed member fails a check
instead of passing review.

No test here deploys anything, reads business data or reaches into component
internals: the assertions are about the factory's canonical artifacts and the
factory's own fail-closed validation surfaces.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from _deployment_helpers import ROOT

from component_catalog import entry_digest, load_catalog
from component_registry import (
    REGISTRY_PATH,
    SHARED_LIBRARY_CLASS,
    is_shared_library,
    load_registry,
)
from component_registry.errors import RegistryValidationError
from component_registry.schema import SCHEMA_PATH as REGISTRY_SCHEMA_PATH
from composer import (
    build_request_document,
    compose_diagnostics,
    compose_request_document,
)
from deployment_operations import verify_instance
from deployment_operations.environment import load_environment, validate_environment
from golden_bundle import load_bundle_document
from golden_bundle import validate_document as validate_bundle_document
from platform_instance import (
    EXAMPLE_INSTANCE_PATH,
    assemble_document,
    canonical_json,
    compute_instance_digest,
    load_instance_document,
    validate_instance_document,
)
from platform_manifest import (
    build_manifest_document,
    compute_manifest_digest,
    validate_document,
)

#: The exact classification of every registered component after D2=B. Any
#: change here is a classification change and must be an owner decision
#: (ADR-0021 §2.2; this work item changes exactly `idempotency` and `saga`).
EXPECTED_CLASSES = {
    "authorization": "platform_service",
    "identity": "platform_service",
    "tenant_authority": "platform_service",
    "records": "platform_service",
    "learning": "business_system",
    "saga": SHARED_LIBRARY_CLASS,
    "idempotency": SHARED_LIBRARY_CLASS,
    "commerce": "business_system",
    "booking": "business_system",
}

#: The shared in-process libraries, in registry order.
SHARED_LIBRARY_IDS = ("saga", "idempotency")

#: The only dependency mechanism through which a library is reached: the
#: consumer imports the published surface in-process (ADR-0021 §2.2).
IN_PROCESS_MECHANISM = "internal-consumer-surface"

#: The prior example instance (assembled before D2=B) is preserved byte for
#: byte as immutability evidence (ADR-0016 §5, §7; work item §2.6): its
#: content hash and recorded digests are pinned here.
PRIOR_INSTANCE_PATH = ROOT / "factory/platform_instance/example_instance.json"
#: `sha256` of the preserved bytes (the file may not be edited in place) and the
#: recorded `instance_digest` of its content.
PRIOR_INSTANCE_FILE_SHA256 = (
    "sha256:de198ed20ff986abb348dfe7ddc8fa72e61bbdf7c3f9044ad021b1c8c9075d20"
)
PRIOR_INSTANCE_SHA256 = (
    "sha256:2be690d9f3c091d23e62b77e32a07c5c0100f98719dedec2f5571498d4508212"
)
PRIOR_MANIFEST_DIGEST = (
    "sha256:a5a04028adeae7f7d41978d3bc32dbbf0cf72ceca2350eff7966381b5619c912"
)

EXAMPLE_REQUEST_PATH = ROOT / "factory/composer/example_request.json"
EXAMPLE_PLATFORM_ID = "example-platform"

ARCHITECTURE_PATH = ROOT / "docs" / "ARCHITECTURE.md"
BASELINE_PATH = ROOT / "docs" / "DOCUMENTATION_BASELINE.md"

ENVIRONMENT_PATH = (
    ROOT / "factory/environments/example_runtime_dependency_environment.json"
)
ENVIRONMENT_INSTANCE_PATH = (
    ROOT / "factory/environments/example_runtime_dependency_instance.json"
)
ENVIRONMENT_MANIFEST_PATH = (
    ROOT / "factory/environments/example_runtime_dependency_manifest.json"
)


def _materialize(tmp_path: Path) -> Path:
    """Copy the canonical sources into an isolated throwaway repository root."""
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "components", root / "components")
    shutil.copytree(ROOT / "factory", root / "factory")
    shutil.copy(ROOT / "pyproject.toml", root / "pyproject.toml")
    return root


def _component_document(root: Path, component_id: str) -> dict[str, Any]:
    """One manifest component entry copied from the authoritative registry."""
    entry = load_registry(root).entry(component_id)
    return {
        "component_id": component_id,
        "component_version": entry["component_version"],
        "artifact": dict(entry["artifact"]),
    }


def _request_component(root: Path, component_id: str) -> dict[str, str]:
    """One composition-request component entry (identity and version only)."""
    entry = load_registry(root).entry(component_id)
    return {
        "component_id": component_id,
        "component_version": entry["component_version"],
    }


def _class_enums(node: object) -> Iterator[list[Any]]:
    """Yield every schema ``enum`` that lists component classes."""
    if isinstance(node, Mapping):
        enum = node.get("enum")
        if isinstance(enum, list) and "business_system" in enum:
            yield enum
        for value in node.values():
            yield from _class_enums(value)
    elif isinstance(node, list):
        for value in node:
            yield from _class_enums(value)


def _section(document: str, start: str, end: str) -> str:
    return document.split(start, 1)[1].split(end, 1)[0]


# ---------------------------------------------------------------------------
# Registry schema and canonical registry (criteria 1, 2, 9)
# ---------------------------------------------------------------------------


def test_registry_schema_represents_the_shared_library_class() -> None:
    """Criterion 1: the class enumeration can represent a library."""
    schema = json.loads((ROOT / REGISTRY_SCHEMA_PATH).read_text(encoding="utf-8"))
    enums = list(_class_enums(schema))
    assert enums, "the registry schema must enumerate the component classes"
    for enum in enums:
        assert SHARED_LIBRARY_CLASS in enum, enum
        for expected in set(EXPECTED_CLASSES.values()):
            assert expected in enum, (expected, enum)
    # The canonical registry itself still validates against that schema:
    # `load_registry` is fail-closed and raises on any schema error.
    registry = load_registry(ROOT)
    assert registry.component_ids


def test_only_idempotency_and_saga_are_reclassified() -> None:
    """Criterion 2: no other component's classification changes."""
    registry = load_registry(ROOT)
    actual = {entry["component_id"]: entry["class"] for entry in registry.entries}
    assert actual == EXPECTED_CLASSES
    assert sorted(actual) == sorted(registry.component_ids)


def test_component_identity_semantics_are_unchanged() -> None:
    """Criterion 9: stable identity, no reuse, no floating selector.

    Reclassification changes exactly the class: the two entries keep their
    component identity, version, artifact and lifecycle claims untouched.
    """
    registry = load_registry(ROOT)
    assert len(registry.component_ids) == len(set(registry.component_ids)) == 9
    for component_id in SHARED_LIBRARY_IDS:
        entry = registry.entry(component_id)
        assert entry["component_id"] == component_id
        assert entry["component_version"] == "0.1.0"
        assert entry["artifact"] == {
            "artifact_type": "none",
            "digest": None,
            "pinned": False,
            "canonical_form": None,
        }
        lifecycle = entry["lifecycle"]
        assert lifecycle["registry_state"] == "registered"
        assert lifecycle["deployable"] is False
        assert lifecycle["publishable"] is False
        assert lifecycle["publishability_blockers"] == [
            "deployment_artifact_absent",
            "migrations_absent",
        ]


def test_libraries_are_reached_only_through_the_in_process_surface() -> None:
    """Every registry edge into a library is an in-process consumer surface."""
    registry = load_registry(ROOT)
    inbound = 0
    for entry in registry.entries:
        for dependency in entry["dependencies"]:
            if dependency["component_id"] not in SHARED_LIBRARY_IDS:
                continue
            inbound += 1
            assert dependency["kind"] == IN_PROCESS_MECHANISM, dependency
    # learning, saga, commerce and booking reach idempotency in process.
    assert inbound == 4


# ---------------------------------------------------------------------------
# Fail-closed membership surfaces (criterion 7)
# ---------------------------------------------------------------------------


def test_a_manifest_declaring_a_library_member_is_rejected() -> None:
    for component_id in SHARED_LIBRARY_IDS:
        document = build_manifest_document(
            manifest_id="library-member-platform",
            manifest_version="1.0.0",
            lifecycle_state="draft",
            components=[_component_document(ROOT, component_id)],
        )
        errors = validate_document(document, root=ROOT)
        assert any(
            SHARED_LIBRARY_CLASS in error and component_id in error for error in errors
        ), (component_id, errors)


def test_a_composition_request_naming_a_library_is_rejected() -> None:
    for component_id in SHARED_LIBRARY_IDS:
        document = build_request_document(
            manifest_id="library-platform",
            manifest_version="1.0.0",
            components=[_request_component(ROOT, component_id)],
        )
        diagnostics = compose_diagnostics(document, root=ROOT)
        assert any(
            SHARED_LIBRARY_CLASS in error and component_id in error
            for error in diagnostics
        ), (component_id, diagnostics)


def test_an_unsatisfiable_library_range_is_refused(tmp_path: Path) -> None:
    """A declared library range the registry cannot satisfy is refused.

    A library is not a composition member, but its declared range is still
    enforced — against the authoritative registry version, the only version of
    an in-process library there is. The range is never dropped, repaired or
    re-pointed; the unsatisfiable declaration is published in a throwaway root
    and the canonical loader refuses it deterministically.
    """
    root = _materialize(tmp_path)
    registry_document = json.loads((root / REGISTRY_PATH).read_text(encoding="utf-8"))
    for entry in registry_document["components"]:
        if entry["component_id"] != "learning":
            continue
        for dependency in entry["dependencies"]:
            if dependency["component_id"] == "idempotency":
                dependency["version_range"] = ">=9.0.0,<10.0.0"
    (root / REGISTRY_PATH).write_text(
        json.dumps(registry_document, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(RegistryValidationError) as raised:
        load_registry(root)
    assert any(
        "idempotency" in error and "9.0.0" in error for error in raised.value.errors
    ), raised.value.errors


# ---------------------------------------------------------------------------
# Derived catalog (criterion 3)
# ---------------------------------------------------------------------------


def test_catalog_mirrors_the_registry_classification_and_digests() -> None:
    registry = load_registry(ROOT)
    catalog = load_catalog(ROOT)
    assert catalog.validate() == []

    classes = {entry["component_id"]: entry["class"] for entry in registry.entries}
    digests = {entry["component_id"]: entry_digest(entry) for entry in registry.entries}
    for entry in catalog.entries:
        component_id = entry["component_id"]
        assert entry["class"] == classes[component_id]
        assert entry["entry_digest"] == digests[component_id]

    assert (
        tuple(
            entry["component_id"]
            for entry in catalog.filter(component_class=SHARED_LIBRARY_CLASS)
        )
        == SHARED_LIBRARY_IDS
    )


# ---------------------------------------------------------------------------
# Canonical documentation (criterion 4)
# ---------------------------------------------------------------------------


def test_canonical_documentation_agrees_with_the_registry() -> None:
    architecture = ARCHITECTURE_PATH.read_text(encoding="utf-8")
    assert "E. Shared In-process Library" in architecture
    section_30 = _section(architecture, "## 30.", "## 31.")
    assert "Shared In-process Library" in section_30
    assert "ADR-0021" in section_30
    for component_id in SHARED_LIBRARY_IDS:
        assert f"`{component_id}`" in section_30

    baseline = BASELINE_PATH.read_text(encoding="utf-8")
    section_4 = _section(baseline, "## 4.", "## 5.")
    assert "Shared In-process Library" in section_4
    assert "ADR-0021" in section_4
    for component_id in SHARED_LIBRARY_IDS:
        assert f"`{component_id}`" in section_4

    for relative in (
        "README.md",
        "factory/registry/README.md",
        "components/idempotency/README.md",
        "components/saga/README.md",
        "factory/platform_instance/README.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "Shared In-process Library" in text, relative


# ---------------------------------------------------------------------------
# Platform Instance definition (criteria 5, 6)
# ---------------------------------------------------------------------------


def _example_manifest() -> dict[str, Any]:
    """The example manifest advanced to `validated` exactly as the shipped
    example instance was assembled (the fixed Slice C/E lifecycle act)."""
    request = json.loads(EXAMPLE_REQUEST_PATH.read_text(encoding="utf-8"))
    document = copy.deepcopy(
        dict(compose_request_document(request, root=ROOT).document)
    )
    document["lifecycle"] = {"state": "validated"}
    document["validation_attestation"] = {
        "validated_by": "factory-example-validator",
        "validated_at": "2026-09-16T00:00:00Z",
        "checks": ["schema", "registry", "composition"],
    }
    document["manifest_digest"] = compute_manifest_digest(document)
    assert validate_document(document, root=ROOT) == []
    return document


def test_the_current_example_instance_declares_no_library_member() -> None:
    """Criterion 5: no admissible instance declares a library as a member."""
    manifest = _example_manifest()
    instance = load_instance_document(ROOT / EXAMPLE_INSTANCE_PATH)
    members = tuple(entry["component_id"] for entry in instance["components"])
    assert not set(members) & set(SHARED_LIBRARY_IDS)
    assert instance["instance_digest"] == compute_instance_digest(instance)

    assert validate_instance_document(instance, manifest, root=ROOT) == []
    assembled = assemble_document(manifest, platform_id=EXAMPLE_PLATFORM_ID, root=ROOT)
    assert canonical_json(instance) == canonical_json(assembled.document)


def test_the_prior_instance_is_preserved_and_no_longer_admissible() -> None:
    """Criterion 6: a new instance, not an in-place mutation of the old one."""
    raw = PRIOR_INSTANCE_PATH.read_bytes()
    assert f"sha256:{hashlib.sha256(raw).hexdigest()}" == PRIOR_INSTANCE_FILE_SHA256
    prior = json.loads(raw.decode("utf-8"))
    assert prior["instance_digest"] == PRIOR_INSTANCE_SHA256
    assert prior["manifest"]["manifest_digest"] == PRIOR_MANIFEST_DIGEST
    assert "idempotency" in [entry["component_id"] for entry in prior["components"]]

    current = load_instance_document(ROOT / EXAMPLE_INSTANCE_PATH)
    assert current["instance_digest"] != prior["instance_digest"]

    # The prior composition is refused by the current law: it declares a shared
    # in-process library as an independently deployed member.
    manifest = build_manifest_document(
        manifest_id=prior["manifest"]["manifest_id"],
        manifest_version=prior["manifest"]["manifest_version"],
        lifecycle_state="draft",
        components=[
            {
                "component_id": entry["component_id"],
                "component_version": entry["component_version"],
                "artifact": dict(entry["artifact"]),
            }
            for entry in prior["components"]
        ],
    )
    errors = validate_document(manifest, root=ROOT)
    assert any(
        SHARED_LIBRARY_CLASS in error and "idempotency" in error for error in errors
    ), errors


def test_a_bundle_may_pin_a_library_without_it_becoming_a_member() -> None:
    """A Golden Bundle pins versions; a Platform Instance declares members.

    A bundle must pin every declared dependency of every component it pins, so
    the shipped example bundle carries the shared in-process libraries in its
    certified version set — while the example instance, whose composition
    imports them in-process, contains no library member (ADR-0021 §2.2 D2=B).
    """
    bundle = load_bundle_document(ROOT / "factory/golden_bundle/example_bundle.json")
    assert validate_bundle_document(bundle, root=ROOT) == []
    pinned = {entry["component_id"] for entry in bundle["components"]}
    assert set(SHARED_LIBRARY_IDS) <= pinned

    instance = load_instance_document(ROOT / EXAMPLE_INSTANCE_PATH)
    members = {entry["component_id"] for entry in instance["components"]}
    assert not members & set(SHARED_LIBRARY_IDS)


# ---------------------------------------------------------------------------
# Environment-binding completeness (criterion 8)
# ---------------------------------------------------------------------------


def test_environment_binding_completeness_is_unchanged() -> None:
    """The environment must still bind every instance member, and only members.

    D2=B does not weaken the rule: the contradiction is resolved by
    membership — a shared in-process library is not an instance member and
    therefore needs no binding, while a member without a binding is still a
    fail-closed rejection.
    """
    document = json.loads(ENVIRONMENT_PATH.read_text(encoding="utf-8"))
    environment = load_environment(document, base_dir=ENVIRONMENT_PATH.parent)
    verification = verify_instance(
        json.loads(ENVIRONMENT_INSTANCE_PATH.read_text(encoding="utf-8")),
        json.loads(ENVIRONMENT_MANIFEST_PATH.read_text(encoding="utf-8")),
        root=ROOT,
    )
    assert verification.errors == ()
    assert validate_environment(environment, verification) == []

    # No member of the committed F-5 instance is a shared in-process library:
    # the classification changes nothing about this composition.
    registry = load_registry(ROOT)
    for component_id in verification.component_ids:
        assert not is_shared_library(registry.entry(component_id))

    # Remove one member's binding: the environment is refused, deterministically.
    trimmed = dict(document)
    trimmed["bindings"] = [
        entry
        for entry in document["bindings"]
        if entry["component_id"] != "tenant_authority"
    ]
    errors = validate_environment(
        load_environment(trimmed, base_dir=ENVIRONMENT_PATH.parent), verification
    )
    assert any(
        "no runtime binding for component 'tenant_authority'" in error
        for error in errors
    ), errors
