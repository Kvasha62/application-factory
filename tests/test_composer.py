"""Focused tests for Slice E — Composer (Issue #73, ADR-0015 §15).

These tests verify architecture rules, not merely successful object
construction (ADR-0015 §16): request schema validity; explicit component
identity/version/constraint rules; registry availability resolved fail-closed;
deterministic dependency resolution and version selection; rejection of
incompatible or contract-invalid platform assemblies; configuration keys
(ARCHITECTURE.md §20); extensions through published contracts (§21); Golden
Bundle evidence (§14, §31); manifest reproducibility; and the boundary that the
Composer assembles a *draft Platform Manifest* and nothing else.

The ``latest`` tests follow the Slice A/B/C/D doctrine: ``latest`` is not
forbidden as a string, it is forbidden as a *selector* — as a way of choosing a
version, a release, a dependency or a production target. Tests therefore feed
``latest``/``current``/``default``/``stable`` and ``*`` into selector fields and
require rejection, while proving the same words are accepted where they select
nothing.

The compatibility rules are presented with an explicitly declared incompatible
dependency (a registry-shaped object), because the authoritative registry is
itself validated by Slice A: this proves the Composer rejects deterministically
instead of repairing, and never depends on the registry being correct.
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from component_registry import REGISTRY_PATH, load_registry
from composer import (
    EXAMPLE_REQUEST_PATH,
    EXTENSION_MECHANISMS,
    FORBIDDEN_DEPENDENCY_KINDS,
    PRODUCED_LIFECYCLE_STATE,
    ComposerNotFoundError,
    ComposerValidationError,
    Composition,
    CompositionRejectedError,
    build_request_document,
    compose,
    compose_diagnostics,
    compose_request_document,
    discover_root,
    load_request,
    load_request_document,
    render_request_document,
    resolve_closure,
    validate_request_document,
    verify_composition,
)
from composer.request import CompositionRequest
from composer.resolution import (
    SELECTED_AS_DEPENDENCY,
    SELECTED_AS_EXPLICIT_RANGE,
    SELECTED_AS_EXPLICIT_VERSION,
    Resolution,
)
from composer.schema import load_schema
from composer.verification import verify_dependency_compatibility
from golden_bundle import build_bundle_document, compute_bundle_digest
from platform_manifest import compute_manifest_digest

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

#: The declared dependency closure of ``learning`` in the canonical registry.
LEARNING_CLOSURE = (
    "authorization",
    "idempotency",
    "identity",
    "learning",
    "tenant_authority",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def registry(root: Path):
    return load_registry(root)


@pytest.fixture
def example_request(root: Path) -> dict:
    """The repository's deterministic example request (three Business Systems)."""
    return json.loads((root / EXAMPLE_REQUEST_PATH).read_text(encoding="utf-8"))


@pytest.fixture
def learning_request(root: Path) -> dict:
    """A minimal request naming one Business System explicitly."""
    return build_request_document(
        manifest_id="learning-platform",
        manifest_version="1.0.0",
        components=[{"component_id": "learning", "component_version": "0.3.0"}],
    )


def clone(document: Mapping) -> dict:
    return copy.deepcopy(dict(document))


def diagnostics(document: object, root: Path) -> list[str]:
    return compose_diagnostics(document, root=root)


def rejects(document: object, root: Path, fragment: str) -> list[str]:
    errors = diagnostics(document, root=root)
    assert errors, "expected the composition request to be rejected"
    assert any(
        fragment in error for error in errors
    ), f"expected an error containing {fragment!r}, got:\n" + "\n".join(errors)
    return errors


def composes(document: object, root: Path) -> Composition:
    errors = diagnostics(document, root=root)
    assert errors == [], "expected a valid composition, got:\n" + "\n".join(errors)
    return compose_request_document(document, root=root)


def materialize(tmp_path: Path) -> Path:
    """Copy the canonical sources into an isolated throwaway repository root."""
    source = discover_root()
    root = tmp_path / "repo"
    shutil.copytree(source / "components", root / "components")
    shutil.copytree(source / "factory", root / "factory")
    shutil.copy(source / "pyproject.toml", root / "pyproject.toml")
    return root


def write_registry(root: Path, document: Mapping) -> None:
    (root / REGISTRY_PATH).write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )


def registry_document(root: Path) -> dict:
    return json.loads((root / REGISTRY_PATH).read_text(encoding="utf-8"))


def certified_bundle_document(root: Path, component_ids: Sequence[str]) -> dict:
    """Build a certified bundle pinning ``component_ids`` and their edges."""
    registry = load_registry(root)
    pinned = sorted(component_ids)
    components = [
        {
            "component_id": component_id,
            "component_version": registry.version(component_id),
            "artifact": dict(registry.entry(component_id)["artifact"]),
        }
        for component_id in pinned
    ]
    pinned_set = set(pinned)
    pairs = [
        {
            "from": entry["component_id"],
            "to": dependency["component_id"],
            "mechanism": dependency["kind"],
            "compatible": True,
        }
        for entry in registry.entries
        for dependency in entry["dependencies"]
        if entry["component_id"] in pinned_set
        and dependency.get("component_id") in pinned_set
    ]
    return build_bundle_document(
        bundle_id="learning-known-good",
        bundle_version="1.0.0",
        lifecycle_state="certified",
        components=components,
        compatibility_pairs=pairs,
        certification={
            "certified_by": "project-owner",
            "certified_at": "2026-09-16T00:00:00Z",
            "certification_id": "certification-1",
            "checks": ["compatibility-matrix", "contract-tests"],
        },
    )


def write_bundle(root: Path, document: Mapping, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def bundle_reference(document: Mapping, relative: str) -> dict:
    return {
        "bundle_id": document["bundle_id"],
        "bundle_version": document["bundle_version"],
        "bundle_digest": compute_bundle_digest(document),
        "path": relative,
    }


@pytest.fixture
def certified_root(tmp_path: Path) -> tuple[Path, dict, str]:
    """A throwaway root carrying a certified bundle for the learning closure."""
    root = materialize(tmp_path)
    document = certified_bundle_document(root, LEARNING_CLOSURE)
    relative = "factory/golden_bundle/certified_learning.json"
    write_bundle(root, document, relative)
    return root, document, relative


class StubRegistry:
    """A registry-shaped object presenting explicitly declared metadata.

    Slice A rejects a registry whose declared dependency cannot be satisfied by
    the registered version, so an incompatible dependency is presented here
    explicitly: the Composer must reject it deterministically and never repair
    the composition, regardless of how the metadata was produced.
    """

    def __init__(self, entries: Sequence[Mapping]) -> None:
        self.entries = tuple(entries)

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(entry["component_id"] for entry in self.entries)

    def entry(self, component_id: str) -> Mapping:
        for entry in self.entries:
            if entry["component_id"] == component_id:
                return entry
        raise KeyError(component_id)

    def version(self, component_id: str) -> str:
        return self.entry(component_id)["component_version"]

    def dependencies(self, component_id: str) -> tuple[Mapping, ...]:
        return tuple(self.entry(component_id).get("dependencies", ()))


ARTIFACT = {"artifact_type": "none", "digest": None, "pinned": False}


def stub_component(
    component_id: str, version: str, dependencies: Sequence[Mapping] = ()
) -> dict:
    return {
        "component_id": component_id,
        "component_version": version,
        "artifact": dict(ARTIFACT),
        "dependencies": list(dependencies),
    }


def stub_resolution(stub: StubRegistry, requested: Sequence[str]):
    """Resolve a stub composition exactly as ``compose`` resolves one."""
    resolution, errors = resolve_closure(
        stub,
        [
            {
                "component_id": component_id,
                "component_version": stub.version(component_id),
            }
            for component_id in requested
        ],
    )
    assert errors == [], errors
    return resolution


def stub_verification(stub: StubRegistry, requested: Sequence[str], root: Path):
    """Verify a stub composition exactly as ``compose`` verifies one."""
    resolution = stub_resolution(stub, requested)
    return resolution, verify_composition(
        root,
        stub,
        resolution,
        configuration={},
        extensions=(),
        golden_bundle=None,
    )


# ---------------------------------------------------------------------------
# Request schema (ARCHITECTURE.md §17; ADR-0015 §8)
# ---------------------------------------------------------------------------


class TestRequestSchema:
    def test_schema_is_loadable(self, root: Path) -> None:
        schema = load_schema(root)
        assert schema["type"] == "object"
        assert "components" in schema["required"]

    def test_minimal_request_is_valid(self, root: Path, learning_request) -> None:
        assert validate_request_document(learning_request, root=root) == []

    def test_example_request_is_valid(self, root: Path, example_request) -> None:
        assert validate_request_document(example_request, root=root) == []

    def test_unknown_top_level_property_rejected(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["approval"] = {"approved_by": "owner"}
        rejects(broken, root, "unexpected property 'approval'")

    def test_missing_manifest_rejected(self, root: Path, learning_request) -> None:
        broken = clone(learning_request)
        broken.pop("manifest")
        rejects(broken, root, "missing required property 'manifest'")

    def test_missing_components_rejected(self, root: Path, learning_request) -> None:
        broken = clone(learning_request)
        broken.pop("components")
        rejects(broken, root, "missing required property 'components'")

    def test_empty_components_rejected(self, root: Path, learning_request) -> None:
        broken = clone(learning_request)
        broken["components"] = []
        rejects(broken, root, "expected at least 1 item(s)")

    def test_non_object_request_rejected(self, root: Path) -> None:
        assert diagnostics(["not", "a", "request"], root) == [
            "$: composition request must be a JSON object"
        ]

    def test_extension_without_contract_rejected(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["extensions"] = [
            {"extension_id": "ext", "component_id": "learning", "mechanism": "webhook"}
        ]
        rejects(broken, root, "missing required property 'contract'")

    def test_manifest_version_must_be_semver(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["manifest"]["manifest_version"] = "1.0"
        rejects(broken, root, "does not match pattern")

    def test_component_version_must_be_semver(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["components"][0]["component_version"] = "0.3"
        rejects(broken, root, "does not match pattern")

    def test_request_identity_must_not_be_a_floating_selector(
        self, root: Path, learning_request
    ) -> None:
        for token in (
            "latest",
            "current",
            "default",
            "stable",
            "main",
            "master",
            "edge",
            "tip",
            "*",
        ):
            broken = clone(learning_request)
            broken["manifest"]["manifest_id"] = token
            errors = diagnostics(broken, root)
            assert errors, token
            assert any(
                "does not match pattern" in e or "floating selector" in e
                for e in errors
            ), (
                token,
                errors,
            )


# ---------------------------------------------------------------------------
# Explicit selections (ARCHITECTURE.md §1.3, §11; ADR-0015 §8)
# ---------------------------------------------------------------------------


class TestExplicitSelection:
    def test_exactly_one_selector_is_required(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["components"][0]["version_range"] = ">=0.3.0,<0.4.0"
        rejects(broken, root, "exactly one of component_version")

    def test_no_selector_is_rejected(self, root: Path, learning_request) -> None:
        broken = clone(learning_request)
        broken["components"][0].pop("component_version")
        rejects(broken, root, "exactly one of component_version")

    def test_explicit_range_is_accepted(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[
                {"component_id": "learning", "version_range": ">=0.3.0,<0.4.0"}
            ],
        )
        composition = composes(document, root)
        assert (
            composition.resolution.selected[
                [c.component_id for c in composition.resolution.selected].index(
                    "learning"
                )
            ].selected_as
            == SELECTED_AS_EXPLICIT_RANGE
        )

    def test_pin_selector_rejected(self, root: Path, learning_request) -> None:
        for token in ("latest", "current", "stable", "main", "*"):
            broken = clone(learning_request)
            broken["components"][0]["component_version"] = token
            errors = diagnostics(broken, root)
            assert any("floating selector" in e for e in errors), (token, errors)

    def test_range_selector_rejected(self, root: Path, learning_request) -> None:
        for token in ("latest", ">=*", "current"):
            broken = clone(learning_request)
            broken["components"][0].pop("component_version")
            broken["components"][0]["version_range"] = token
            errors = diagnostics(broken, root)
            assert errors, token
            assert any(
                "floating selector" in e or "not an explicit version range" in e
                for e in errors
            ), (token, errors)

    def test_malformed_range_rejected(self, root: Path, learning_request) -> None:
        broken = clone(learning_request)
        broken["components"][0].pop("component_version")
        broken["components"][0]["version_range"] = "0.3.0"
        rejects(broken, root, "not an explicit version range")

    def test_unsorted_components_rejected(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[
                {"component_id": "learning", "component_version": "0.3.0"},
                {"component_id": "booking", "component_version": "0.1.0"},
            ],
        )
        broken = clone(document)
        broken["components"] = list(reversed(broken["components"]))
        rejects(broken, root, "must be sorted by component_id")

    def test_duplicate_component_rejected(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        broken = clone(document)
        broken["components"].append(dict(broken["components"][0]))
        rejects(broken, root, "duplicate requested component")

    def test_component_identity_must_match_the_registry_grammar(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["components"][0]["component_id"] = "Learning"
        rejects(broken, root, "does not match pattern")

    def test_selector_words_remain_allowed_where_they_select_nothing(
        self, root: Path
    ) -> None:
        # The rule is about selectors, not about substrings: an identity that
        # merely contains a token selects nothing and stays legal.
        document = build_request_document(
            manifest_id="latest-platform-lineage",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        assert validate_request_document(document, root=root) == []


# ---------------------------------------------------------------------------
# Authoritative registry availability (ADR-0015 §4, §11)
# ---------------------------------------------------------------------------


class TestRegistryAvailability:
    def test_unknown_component_rejected(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="unknown-platform",
            manifest_version="1.0.0",
            components=[
                {"component_id": "not_registered", "component_version": "0.1.0"}
            ],
        )
        rejects(
            document, root, "is not registered in the authoritative Component Registry"
        )

    def test_unpublished_version_is_rejected_not_substituted(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["components"][0]["component_version"] = "9.9.9"
        rejects(broken, root, "never substitutes or invents versions")

    def test_range_excluding_the_registered_version_is_rejected(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["components"][0].pop("component_version")
        broken["components"][0]["version_range"] = ">=9.0.0,<10.0.0"
        rejects(broken, root, "never substitutes or invents versions")

    def test_registered_inventory_is_composable(self, root: Path, registry) -> None:
        document = build_request_document(
            manifest_id="full-inventory",
            manifest_version="1.0.0",
            components=[
                {
                    "component_id": entry["component_id"],
                    "component_version": entry["component_version"],
                }
                for entry in registry.entries
            ],
        )
        composition = composes(document, root)
        assert composition.component_ids == tuple(sorted(EXPECTED_INVENTORY))

    def test_missing_registry_fails_closed(self, tmp_path: Path) -> None:
        root = materialize(tmp_path)
        (root / REGISTRY_PATH).unlink()
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        rejects(document, root, "unavailable or invalid")

    def test_invalid_registry_fails_closed(self, tmp_path: Path) -> None:
        root = materialize(tmp_path)
        document = registry_document(root)
        document["components"][0]["component_version"] = "not-a-version"
        write_registry(root, document)
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        rejects(request, root, "unavailable or invalid")

    def test_registry_divergence_blocks_compilation(self, tmp_path: Path) -> None:
        root = materialize(tmp_path)
        # A registry that claims a dependency the published contract does not
        # declare diverges from its contracts, and a divergent registry is not
        # a valid basis for a composition.
        document = registry_document(root)
        for entry in document["components"]:
            if entry["component_id"] == "records":
                entry["dependencies"] = [
                    {
                        "component_id": "learning",
                        "version_range": ">=0.3.0,<0.4.0",
                        "kind": "api",
                        "contract": "components/learning/contract/component_contract.json",
                    }
                ]
        write_registry(root, document)
        request = build_request_document(
            manifest_id="records-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "records", "component_version": "0.1.0"}],
        )
        rejects(request, root, "declare different dependencies")


# ---------------------------------------------------------------------------
# Deterministic resolution (ARCHITECTURE.md §17, §18)
# ---------------------------------------------------------------------------


class TestResolution:
    def test_declared_dependencies_are_resolved_into_the_composition(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        assert composition.component_ids == LEARNING_CLOSURE

    def test_every_added_component_is_recorded_as_a_dependency(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        by_id = {
            decision["component_id"]: decision for decision in composition.decisions
        }
        assert by_id["learning"]["selected_as"] == SELECTED_AS_EXPLICIT_VERSION
        assert by_id["learning"]["required_by"] == []
        for component_id in (
            "authorization",
            "idempotency",
            "identity",
            "tenant_authority",
        ):
            assert by_id[component_id]["selected_as"] == SELECTED_AS_DEPENDENCY
            assert by_id[component_id]["required_by"], component_id
        assert by_id["identity"]["required_by"] == ["authorization", "learning"]

    def test_resolution_is_deterministic(self, root: Path, example_request) -> None:
        first = composes(example_request, root)
        second = composes(example_request, root)
        assert first.decisions == second.decisions
        assert first.resolution.selected == second.resolution.selected

    def test_example_resolves_the_three_business_systems(
        self, root: Path, example_request
    ) -> None:
        composition = composes(example_request, root)
        assert composition.resolution.explicit_ids() == (
            "booking",
            "commerce",
            "learning",
        )
        assert composition.component_ids == (
            "authorization",
            "booking",
            "commerce",
            "idempotency",
            "identity",
            "learning",
            "tenant_authority",
        )

    def test_artifact_identity_is_repeated_from_the_registry(
        self, root: Path, registry, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        for entry in composition.document["components"]:
            assert entry["artifact"] == dict(
                registry.entry(entry["component_id"])["artifact"]
            )

    def test_resolution_does_not_mutate_input_components(
        self, root: Path, learning_request
    ) -> None:
        snapshot = json.dumps(learning_request, sort_keys=True)
        composes(learning_request, root)
        assert json.dumps(learning_request, sort_keys=True) == snapshot

    def test_unregistered_dependency_target_is_rejected(self) -> None:
        stub = StubRegistry(
            [
                stub_component(
                    "learning",
                    "0.3.0",
                    [
                        {
                            "component_id": "ghost",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "api",
                        }
                    ],
                )
            ]
        )
        _, errors = resolve_closure(
            stub, [{"component_id": "learning", "component_version": "0.3.0"}]
        )
        assert any("not registered" in error for error in errors), errors

    def test_declared_edges_come_only_from_the_registry(
        self, root: Path, registry
    ) -> None:
        # A component with no declared dependency stays alone: resolution never
        # discovers an undeclared edge.
        document = build_request_document(
            manifest_id="idempotency-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "idempotency", "component_version": "0.1.0"}],
        )
        composition = composes(document, root)
        assert composition.component_ids == ("idempotency",)
        assert registry.dependencies("idempotency") == ()


# ---------------------------------------------------------------------------
# Produced Platform Manifest (ARCHITECTURE.md §16, §17)
# ---------------------------------------------------------------------------


class TestProducedManifest:
    def test_produced_manifest_is_a_draft(self, root: Path, learning_request) -> None:
        composition = composes(learning_request, root)
        assert PRODUCED_LIFECYCLE_STATE == "draft"
        assert composition.lifecycle_state == "draft"

    def test_produced_manifest_is_contract_valid(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        assert composition.validate() == []

    def test_produced_manifest_digest_matches_its_content(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        assert composition.manifest_digest == compute_manifest_digest(
            composition.document
        )

    def test_produced_manifest_is_deterministic(
        self, root: Path, learning_request
    ) -> None:
        first = composes(learning_request, root)
        second = composes(learning_request, root)
        assert first.document == second.document
        assert first.render() == second.render()
        assert first.summary() == second.summary()

    def test_composer_never_approves_or_publishes(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        assert composition.document["approval"] is None
        assert composition.document["publication"] is None
        assert composition.document["validation_attestation"] is None

    def test_predecessor_is_passed_through(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.1.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            predecessor={
                "manifest_id": "learning-platform",
                "manifest_version": "1.0.0",
            },
        )
        composition = composes(document, root)
        assert composition.document["predecessor"] == {
            "manifest_id": "learning-platform",
            "manifest_version": "1.0.0",
        }

    def test_contradictory_predecessor_is_rejected_by_the_handoff(
        self, root: Path
    ) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            predecessor={
                "manifest_id": "learning-platform",
                "manifest_version": "1.0.0",
            },
        )
        rejects(document, root, "predecessor must not be self")

    def test_branding_is_passed_through(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            branding={"product_name": "Example School Platform"},
        )
        composition = composes(document, root)
        assert composition.document["branding"] == {
            "product_name": "Example School Platform"
        }

    def test_composition_report_names_uncertified_status(
        self, root: Path, learning_request
    ) -> None:
        summary = composes(learning_request, root).summary()
        assert summary["certified"] is False
        assert summary["golden_bundle"] is None
        assert summary["lifecycle_state"] == "draft"


# ---------------------------------------------------------------------------
# Configuration (ARCHITECTURE.md §20)
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_declared_configuration_is_accepted(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"platform_id": "example-school"}},
        )
        composition = composes(document, root)
        assert composition.document["configuration"] == {
            "learning": {"platform_id": "example-school"}
        }

    def test_unknown_configuration_key_is_a_build_error(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"platform_id": "example-school", "unknown": 1}},
        )
        rejects(document, root, "unknown configuration key")

    def test_secrets_have_no_representation(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"api_key": "secret"}},
        )
        rejects(document, root, "unknown configuration key")

    def test_required_configuration_key_is_required(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"environment": "standalone"}},
        )
        rejects(document, root, "required configuration key 'platform_id' is absent")

    def test_configuration_type_is_enforced(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"platform_id": 7}},
        )
        rejects(document, root, "expected type 'string', got int")

    def test_configuration_min_length_is_enforced(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"platform_id": ""}},
        )
        rejects(document, root, "shorter than minLength 1")

    def test_configuration_constant_is_enforced(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="authorization-platform",
            manifest_version="1.0.0",
            components=[
                {"component_id": "authorization", "component_version": "0.1.0"}
            ],
            configuration={
                "authorization": {
                    "platform_id": "example-school",
                    "service_token_prefix": "wrong-",
                }
            },
        )
        rejects(document, root, "expected constant 'authz-svc-token-'")

    def test_configuration_minimum_is_enforced(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="saga-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "saga", "component_version": "0.1.0"}],
            configuration={"saga": {"max_sagas": 0}},
        )
        rejects(document, root, "below minimum")

    def test_configuration_for_a_component_outside_the_composition_is_rejected(
        self, root: Path
    ) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"records": {"platform_id": "example-school"}},
        )
        rejects(document, root, "not part of the composed platform")

    def test_component_without_declared_keys_rejects_every_key(
        self, root: Path
    ) -> None:
        document = build_request_document(
            manifest_id="idempotency-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "idempotency", "component_version": "0.1.0"}],
            configuration={"idempotency": {"anything": 1}},
        )
        rejects(document, root, "unknown configuration key")

    def test_floating_selector_inside_configuration_is_rejected(
        self, root: Path
    ) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"platform_id": "latest"}},
        )
        rejects(document, root, "floating selector")

    def test_defaults_are_not_injected(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"platform_id": "example-school"}},
        )
        composition = composes(document, root)
        assert "environment" not in composition.document["configuration"]["learning"]

    def test_absent_configuration_stays_absent(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        assert "configuration" not in composition.document


# ---------------------------------------------------------------------------
# Extensions (ARCHITECTURE.md §21)
# ---------------------------------------------------------------------------


class TestExtensions:
    def extension(
        self, component_id: str, contract: str, mechanism: str = "ui_widget"
    ) -> dict:
        return {
            "extension_id": f"{component_id}-extension",
            "extension_version": "1.0.0",
            "component_id": component_id,
            "mechanism": mechanism,
            "contract": contract,
        }

    def test_extension_through_a_published_contract_is_accepted(
        self, root: Path
    ) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            extensions=[
                self.extension("learning", "components/learning/contract/openapi.yaml")
            ],
        )
        composition = composes(document, root)
        assert (
            composition.document["extensions"][0]["extension_id"]
            == "learning-extension"
        )

    def test_every_permitted_mechanism_is_accepted(self, root: Path) -> None:
        assert EXTENSION_MECHANISMS == {
            "webhook",
            "custom_fields",
            "ui_widget",
            "theme",
            "plugin",
            "official_extension_api",
        }
        for mechanism in sorted(EXTENSION_MECHANISMS):
            document = build_request_document(
                manifest_id="learning-platform",
                manifest_version="1.0.0",
                components=[{"component_id": "learning", "component_version": "0.3.0"}],
                extensions=[
                    self.extension(
                        "learning",
                        "components/learning/contract/openapi.yaml",
                        mechanism,
                    )
                ],
            )
            assert diagnostics(document, root) == [], mechanism

    def test_forbidden_mechanism_is_rejected(self, root: Path) -> None:
        for mechanism in (
            "database",
            "internal_code",
            "private_schema",
            "internal_queue",
        ):
            document = build_request_document(
                manifest_id="learning-platform",
                manifest_version="1.0.0",
                components=[{"component_id": "learning", "component_version": "0.3.0"}],
                extensions=[
                    self.extension(
                        "learning",
                        "components/learning/contract/openapi.yaml",
                        mechanism,
                    )
                ],
            )
            rejects(document, root, "is not one of")

    def test_extension_never_introduces_a_component(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            extensions=[
                self.extension("records", "components/records/contract/openapi.yaml")
            ],
        )
        rejects(document, root, "not part of the composed platform")

    def test_extension_must_name_a_published_contract(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            extensions=[
                self.extension("learning", "components/identity/contract/openapi.yaml")
            ],
        )
        rejects(
            document,
            root,
            "is not a contract the authoritative Component Registry publishes",
        )

    def test_extension_cannot_reach_into_component_internals(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            extensions=[self.extension("learning", "src/learning_service/store.py")],
        )
        rejects(
            document,
            root,
            "is not a contract the authoritative Component Registry publishes",
        )

    def test_duplicate_extension_identity_is_rejected(self, root: Path) -> None:
        extension = self.extension(
            "learning", "components/learning/contract/openapi.yaml"
        )
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            extensions=[extension, dict(extension)],
        )
        rejects(document, root, "duplicate extension identity")

    def test_extension_mechanism_vocabulary_excludes_composition_bypasses(self) -> None:
        assert not (EXTENSION_MECHANISMS & FORBIDDEN_DEPENDENCY_KINDS)


# ---------------------------------------------------------------------------
# Compatibility and contract validity (ARCHITECTURE.md §1.4, §18; ADR-0015 §8)
# ---------------------------------------------------------------------------


class TestCompatibilityRules:
    """Compatibility rules are presented with explicitly declared metadata.

    Slice A rejects a registry whose declared dependency cannot be satisfied by
    the registered version, so an incompatible dependency is presented here
    explicitly: the Composer must reject it deterministically and never repair
    the composition, regardless of how the metadata was produced.
    """

    def incompatible_stub(self) -> StubRegistry:
        """The learning closure with exactly one incompatible declared edge."""
        return StubRegistry(
            [
                stub_component("idempotency", "0.1.0"),
                stub_component("tenant_authority", "0.1.0"),
                stub_component(
                    "identity",
                    "0.3.0",
                    [
                        {
                            "component_id": "tenant_authority",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "api",
                        }
                    ],
                ),
                stub_component(
                    "authorization",
                    "0.1.0",
                    [
                        {
                            "component_id": "identity",
                            "version_range": ">=0.3.0,<0.4.0",
                            "kind": "api",
                        },
                        {
                            "component_id": "tenant_authority",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "api",
                        },
                    ],
                ),
                stub_component(
                    "learning",
                    "0.3.0",
                    [
                        {
                            "component_id": "authorization",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "api",
                        },
                        {
                            "component_id": "idempotency",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "internal-consumer-surface",
                        },
                        {
                            "component_id": "identity",
                            "version_range": ">=9.0.0,<10.0.0",
                            "kind": "api",
                        },
                    ],
                ),
            ]
        )

    def test_incompatible_dependency_is_reported(self, root: Path) -> None:
        stub = self.incompatible_stub()
        resolution = stub_resolution(stub, ["learning"])
        errors = verify_dependency_compatibility(stub, resolution)
        assert any("incompatible dependency" in error for error in errors), errors
        assert any("never substitutes" in error for error in errors), errors

    def test_incompatible_dependency_rejects_the_composition(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = self.incompatible_stub()
        monkeypatch.setattr("composer.composer.load_registry", lambda root=None: stub)
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        with pytest.raises(CompositionRejectedError) as raised:
            compose_request_document(document, root=root)
        assert any("incompatible dependency" in error for error in raised.value.errors)

    def test_selection_is_not_repaired_by_an_incompatible_dependency(
        self, root: Path
    ) -> None:
        stub = self.incompatible_stub()
        resolution = stub_resolution(stub, ["learning"])
        # The declared incompatible dependency is neither silently upgraded nor
        # dropped: the registered versions stay exactly as published.
        assert resolution.component_ids == LEARNING_CLOSURE
        assert {
            component.component_id: component.component_version
            for component in resolution.selected
        } == {
            "authorization": "0.1.0",
            "idempotency": "0.1.0",
            "identity": "0.3.0",
            "learning": "0.3.0",
            "tenant_authority": "0.1.0",
        }

    def test_forbidden_dependency_mechanism_is_rejected(self) -> None:
        stub = StubRegistry(
            [
                stub_component("authorization", "0.1.0"),
                stub_component(
                    "learning",
                    "0.3.0",
                    [
                        {
                            "component_id": "authorization",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "database",
                        }
                    ],
                ),
            ]
        )
        resolution = stub_resolution(stub, ["learning"])
        errors = verify_dependency_compatibility(stub, resolution)
        assert any(
            "forbidden dependency mechanism" in error for error in errors
        ), errors

    def test_declared_dependency_without_an_explicit_range_is_rejected(self) -> None:
        stub = StubRegistry(
            [
                stub_component("authorization", "0.1.0"),
                stub_component(
                    "learning",
                    "0.3.0",
                    [
                        {
                            "component_id": "authorization",
                            "version_range": "latest",
                            "kind": "api",
                        }
                    ],
                ),
            ]
        )
        _, errors = resolve_closure(
            stub, [{"component_id": "learning", "component_version": "0.3.0"}]
        )
        assert any("not an explicit version range" in error for error in errors), errors

    def test_declared_dependency_absent_from_the_composition_is_reported(self) -> None:
        stub = StubRegistry(
            [
                stub_component("authorization", "0.1.0"),
                stub_component(
                    "learning",
                    "0.3.0",
                    [
                        {
                            "component_id": "authorization",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "api",
                        }
                    ],
                ),
            ]
        )
        # A composition that lacks a declared dependency is never accepted: the
        # Composer does not drop a dependency to make an assembly succeed.
        resolution = Resolution(
            selected=(stub_resolution(stub, ["learning"]).selected[1],)
        )
        errors = verify_dependency_compatibility(stub, resolution)
        assert any("absent from the composition" in error for error in errors), errors

    def test_compatible_dependency_is_accepted(self) -> None:
        stub = StubRegistry(
            [
                stub_component("authorization", "0.1.0"),
                stub_component(
                    "learning",
                    "0.3.0",
                    [
                        {
                            "component_id": "authorization",
                            "version_range": ">=0.1.0,<0.2.0",
                            "kind": "api",
                        }
                    ],
                ),
            ]
        )
        resolution = stub_resolution(stub, ["learning"])
        assert verify_dependency_compatibility(stub, resolution) == []
        assert resolution.component_ids == ("authorization", "learning")

    def test_contract_divergence_from_the_registry_is_rejected(
        self, tmp_path: Path
    ) -> None:
        root = materialize(tmp_path)
        contract_path = (
            root / "components" / "learning" / "contract" / "component_contract.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["dependencies"] = [
            dependency
            for dependency in contract["dependencies"]
            if dependency["component_id"] != "idempotency"
        ]
        contract_path.write_text(
            json.dumps(contract, indent=2) + "\n", encoding="utf-8"
        )

        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        rejects(document, root, "declare different dependencies")

    def test_contract_version_mismatch_is_rejected(self, tmp_path: Path) -> None:
        root = materialize(tmp_path)
        contract_path = (
            root / "components" / "learning" / "contract" / "component_contract.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["component_version"] = "9.9.9"
        contract_path.write_text(
            json.dumps(contract, indent=2) + "\n", encoding="utf-8"
        )

        registry = registry_document(root)
        for entry in registry["components"]:
            if entry["component_id"] == "learning":
                entry["component_version"] = "9.9.9"
        write_registry(root, registry)

        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        rejects(document, root, "never substitutes or invents versions")

    def test_missing_published_contract_is_rejected(self, root: Path) -> None:
        stub = StubRegistry(
            [
                {
                    **stub_component("learning", "0.3.0"),
                    "contracts": {
                        "component_contract": "components/learning/contract/absent.json"
                    },
                }
            ]
        )
        _, result = stub_verification(stub, ["learning"], root)
        assert result.rejected
        assert any(
            "cannot be composed as a contract-valid component" in error
            for error in result.errors
        ), result.errors


# ---------------------------------------------------------------------------
# Golden Bundle evidence (ARCHITECTURE.md §14, §31; ADR-0015 §8)
# ---------------------------------------------------------------------------


class TestGoldenBundleEvidence:
    def test_composition_without_a_bundle_is_explicitly_uncertified(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        assert composition.golden_bundle is None
        assert composition.document["golden_bundle"] is None
        assert composition.certified is False

    def test_certified_bundle_matching_the_composition_is_accepted(
        self, certified_root
    ) -> None:
        root, document, relative = certified_root
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle=bundle_reference(document, relative),
        )
        composition = composes(request, root)
        assert composition.certified is True
        assert composition.document["golden_bundle"] == {
            "bundle_id": "learning-known-good",
            "bundle_version": "1.0.0",
            "bundle_digest": compute_bundle_digest(document),
        }

    def test_uncertified_bundle_is_rejected(self, root: Path, learning_request) -> None:
        bundle = json.loads(
            (root / "factory" / "golden_bundle" / "example_bundle.json").read_text(
                encoding="utf-8"
            )
        )
        request = clone(learning_request)
        request["golden_bundle"] = {
            "bundle_id": bundle["bundle_id"],
            "bundle_version": bundle["bundle_version"],
            "bundle_digest": bundle["bundle_digest"],
            "path": "factory/golden_bundle/example_bundle.json",
        }
        errors = rejects(request, root, "only a certified Golden Bundle")
        assert any("candidate" in error for error in errors), errors

    def test_deprecated_bundle_is_rejected(self, certified_root) -> None:
        root, document, relative = certified_root
        deprecated = clone(document)
        deprecated["lifecycle"] = {"state": "deprecated"}
        deprecated = build_bundle_document(
            bundle_id=deprecated["bundle_id"],
            bundle_version=deprecated["bundle_version"],
            lifecycle_state="deprecated",
            components=[dict(entry) for entry in deprecated["components"]],
            compatibility_pairs=[
                dict(pair) for pair in deprecated["compatibility"]["pairs"]
            ],
            certification=dict(deprecated["certification"]),
        )
        relative = "factory/golden_bundle/deprecated_learning.json"
        write_bundle(root, deprecated, relative)
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle=bundle_reference(deprecated, relative),
        )
        errors = rejects(request, root, "only a certified Golden Bundle")
        assert any("'deprecated'" in error for error in errors), errors

    def test_bundle_digest_mismatch_is_rejected(self, certified_root) -> None:
        root, document, relative = certified_root
        reference = bundle_reference(document, relative)
        reference["bundle_digest"] = "sha256:" + "0" * 64
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle=reference,
        )
        rejects(request, root, "does not match the content digest")

    def test_bundle_version_mismatch_is_rejected(self, certified_root) -> None:
        root, document, relative = certified_root
        reference = bundle_reference(document, relative)
        reference["bundle_version"] = "2.0.0"
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle=reference,
        )
        rejects(request, root, "is not the version of the bundle")

    def test_missing_bundle_is_rejected(self, certified_root) -> None:
        root, document, relative = certified_root
        reference = bundle_reference(document, relative)
        reference["path"] = "factory/golden_bundle/absent.json"
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle=reference,
        )
        rejects(request, root, "referenced Golden Bundle is missing")

    def test_bundle_path_traversal_is_rejected(
        self, root: Path, learning_request
    ) -> None:
        request = clone(learning_request)
        request["golden_bundle"] = {
            "bundle_id": "learning-known-good",
            "bundle_version": "1.0.0",
            "bundle_digest": "sha256:" + "0" * 64,
            "path": "../../etc/passwd",
        }
        rejects(request, root, "matches 0 of 2 oneOf branches")

    def test_composition_may_not_be_wider_than_the_certified_bundle(
        self, certified_root
    ) -> None:
        root, document, relative = certified_root
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[
                {"component_id": "learning", "component_version": "0.3.0"},
                {"component_id": "saga", "component_version": "0.1.0"},
            ],
            golden_bundle=bundle_reference(document, relative),
        )
        rejects(request, root, "the composition contains")

    def test_composition_may_not_be_narrower_than_the_certified_bundle(
        self, certified_root
    ) -> None:
        root, _document, _relative = certified_root
        replacement = certified_bundle_document(root, EXPECTED_INVENTORY)
        relative = "factory/golden_bundle/full_inventory.json"
        write_bundle(root, replacement, relative)
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle=bundle_reference(replacement, relative),
        )
        rejects(request, root, "which the composition does not contain")

    def test_invalid_bundle_document_is_rejected(self, certified_root) -> None:
        root, document, relative = certified_root
        broken = clone(document)
        broken["components"][0]["component_version"] = "9.9.9"
        write_bundle(root, broken, relative)
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle=bundle_reference(document, relative),
        )
        rejects(request, root, "the referenced Golden Bundle is invalid")


# ---------------------------------------------------------------------------
# Determinism and reporting (ADR-0015 §6, §8)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_request_same_manifest(self, root: Path, example_request) -> None:
        first = composes(example_request, root)
        second = composes(example_request, root)
        assert first.manifest_digest == second.manifest_digest
        assert render_request_document(first.document) == render_request_document(
            second.document
        )

    def test_key_order_does_not_change_the_manifest(self, root: Path) -> None:
        document = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        reordered = {key: document[key] for key in reversed(list(document))}
        assert composes(document, root).document == composes(reordered, root).document

    def test_diagnostics_are_total_and_sorted(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["components"][0]["component_version"] = "9.9.9"
        broken["manifest"]["manifest_version"] = "nope"
        first = diagnostics(broken, root)
        second = diagnostics(broken, root)
        assert first == second
        assert first == sorted(first)

    def test_composition_decisions_are_sorted_by_component_id(
        self, root: Path, example_request
    ) -> None:
        composition = composes(example_request, root)
        identifiers = [decision["component_id"] for decision in composition.decisions]
        assert identifiers == sorted(identifiers)

    def test_example_request_file_matches_its_rebuild(self, root: Path) -> None:
        from composer.__main__ import (
            EXAMPLE_MANIFEST_ID,
            EXAMPLE_MANIFEST_VERSION,
            _example_configuration,
        )

        shipped = json.loads((root / EXAMPLE_REQUEST_PATH).read_text(encoding="utf-8"))
        registry = load_registry(root)
        business_systems = sorted(
            entry["component_id"]
            for entry in registry.entries
            if entry.get("class") == "business_system"
        )
        components = [
            {
                "component_id": component_id,
                "component_version": registry.version(component_id),
            }
            for component_id in business_systems
        ]
        provisional = build_request_document(
            manifest_id=EXAMPLE_MANIFEST_ID,
            manifest_version=EXAMPLE_MANIFEST_VERSION,
            components=components,
        )
        resolution, errors = resolve_closure(registry, provisional["components"])
        assert errors == []
        rebuilt = build_request_document(
            manifest_id=EXAMPLE_MANIFEST_ID,
            manifest_version=EXAMPLE_MANIFEST_VERSION,
            components=components,
            configuration=_example_configuration(root, registry, resolution) or None,
        )
        assert rebuilt == shipped

    def test_validation_does_not_mutate_the_request(
        self, root: Path, example_request
    ) -> None:
        snapshot = json.dumps(example_request, sort_keys=True)
        validate_request_document(example_request, root=root)
        composes(example_request, root)
        assert json.dumps(example_request, sort_keys=True) == snapshot


# ---------------------------------------------------------------------------
# Boundary and scope protection (ADR-0015 §8, §11, §13)
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_implementation_imports_only_allowed_modules(self, root: Path) -> None:
        package = root / "src" / "composer"
        sources = sorted(package.glob("*.py"))
        assert sources, "composer package must exist"

        allowed_roots = frozenset(
            {
                "__future__",
                "argparse",
                "collections",
                "copy",
                "dataclasses",
                "json",
                "pathlib",
                "re",
                "sys",
                "typing",
                "component_registry",
                "composer",
                "golden_bundle",
                "platform_manifest",
            }
        )
        forbidden_roots = frozenset(
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
                        root_module in allowed_roots
                    ), f"{source.name} imports {name!r}"
                    assert root_module not in forbidden_roots

    def test_composer_never_touches_a_component_database(self, root: Path) -> None:
        for source in (root / "src" / "composer").glob("*.py"):
            text = source.read_text(encoding="utf-8").lower()
            for keyword in (
                "sqlite",
                "psycopg",
                ".execute(",
                "cursor(",
                "create table",
            ):
                assert keyword not in text, (source.name, keyword)

    def test_composer_composes_manifests_and_nothing_else(self, root: Path) -> None:
        import composer

        for forbidden in (
            "deploy",
            "rollout",
            "provision",
            "publish",
            "approve",
            "assemble_instance",
            "release_train",
        ):
            assert not hasattr(composer, forbidden), forbidden

    def test_produced_manifest_carries_no_business_data(
        self, root: Path, learning_request
    ) -> None:
        composition = composes(learning_request, root)
        assert set(composition.document) <= {
            "$schema",
            "manifest_id",
            "manifest_version",
            "manifest_digest",
            "predecessor",
            "lifecycle",
            "components",
            "golden_bundle",
            "configuration",
            "extensions",
            "branding",
            "validation_attestation",
            "approval",
            "publication",
        }

    def test_rejection_never_produces_a_manifest(
        self, root: Path, learning_request
    ) -> None:
        broken = clone(learning_request)
        broken["components"][0]["component_version"] = "9.9.9"
        request = CompositionRequest(document=broken, root=root)
        with pytest.raises(ComposerValidationError):
            compose(request, root=root)

    def test_impossible_composition_raises_a_rejection(self, certified_root) -> None:
        root, document, relative = certified_root
        request = build_request_document(
            manifest_id="certified-platform",
            manifest_version="1.0.0",
            components=[
                {"component_id": "learning", "component_version": "0.3.0"},
                {"component_id": "saga", "component_version": "0.1.0"},
            ],
            golden_bundle=bundle_reference(document, relative),
        )
        with pytest.raises(CompositionRejectedError) as raised:
            compose_request_document(request, root=root)
        assert raised.value.errors
        assert list(raised.value.errors) == sorted(raised.value.errors)

    def test_a_missing_request_document_is_reported(
        self, root: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(ComposerNotFoundError):
            load_request_document(tmp_path / "absent.json")

    def test_registry_and_sources_are_untouched_by_composition(
        self, root: Path, learning_request
    ) -> None:
        before = (root / REGISTRY_PATH).read_text(encoding="utf-8")
        composes(learning_request, root)
        after = (root / REGISTRY_PATH).read_text(encoding="utf-8")
        assert before == after

    def test_load_request_returns_a_validated_request(self, root: Path) -> None:
        request = load_request(root / EXAMPLE_REQUEST_PATH, root)
        assert request.manifest_id == "example-platform"
        assert request.require_valid() == []
