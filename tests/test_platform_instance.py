"""Focused tests for Slice F — Platform Instance Assembly (Issue #74, ADR-0015 §15).

These tests verify architecture rules, not merely successful object
construction (ADR-0015 §16): a validated Platform Manifest assembles into a
concrete Platform Instance representation bound exactly to the manifest
identity/version/digest (LAW-07, LAW-09); component identities, versions and
artifact identities are preserved verbatim from the authoritative manifest;
configuration, extensions, branding and component-declared environment keys
are preserved exactly (ARCHITECTURE.md §2.2, §20, §21, §31); the assembly is
deterministic (no time, randomness, machine or network dependence); invalid,
tampered or non-assemblable inputs are rejected deterministically; and the
boundary holds — assembly is a representation step, not a deployment step,
touches no component internals, mutates no authoritative source and
introduces no second source of truth.

The ``latest`` tests follow the Slice A–E doctrine: ``latest`` is not
forbidden as a string, it is forbidden as a *selector*. Tests therefore feed
``latest``/``current``/``default``/``stable``/``*`` into identity and
selection fields and require rejection.

The authoritative surfaces are reused, not reimplemented: manifest validity
is judged by the normative Slice C validation (fail-closed against the
authoritative Component Registry), and the manifest's fixed composition is
re-checked through the existing Composer verification surfaces (§18, §20,
§21, contract validity). Where an inconsistency is not detectable by an
existing authoritative surface, no invented check is claimed here.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from component_registry import load_registry
from composer import (
    EXAMPLE_REQUEST_PATH,
    compose_diagnostics,
    compose_request_document,
)
from composer.request import build_request_document
from golden_bundle import build_bundle_document, compute_bundle_digest
from platform_instance import (
    ASSEMBLABLE_MANIFEST_STATES,
    EXAMPLE_INSTANCE_PATH,
    AssemblyRejectedError,
    InstanceNotFoundError,
    assemble_document,
    assembly_diagnostics,
    build_instance_document,
    compute_instance_digest,
    discover_root,
    load_instance_document,
    render_instance_document,
    validate_instance_document,
)
from platform_manifest import (
    LIFECYCLE_STATES,
    compute_manifest_digest,
    validate_document,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

#: The declared dependency closure of ``learning`` in the canonical registry.
LEARNING_CLOSURE = (
    "authorization",
    "idempotency",
    "identity",
    "learning",
    "tenant_authority",
)


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def example_request(root: Path) -> dict:
    """The repository's deterministic example request (three Business Systems)."""
    return json.loads((root / EXAMPLE_REQUEST_PATH).read_text(encoding="utf-8"))


def validated(document: Mapping, *, state: str = "validated") -> dict:
    """Advance a composed draft manifest to ``state`` deterministically.

    This is a Platform Manifest lifecycle act (ARCHITECTURE.md §16), performed
    here with fixed values exactly as the Slice C/E tests do: Slice F itself
    never advances a manifest. The content digest is recomputed so the
    document stays canonically consistent.
    """
    advanced = copy.deepcopy(dict(document))
    advanced["lifecycle"] = {"state": state}
    if state != "draft":
        advanced["validation_attestation"] = {
            "validated_by": "factory-example-validator",
            "validated_at": "2026-09-16T00:00:00Z",
            "checks": ["schema", "registry", "composition"],
        }
    if state in ("approved", "published", "deployed", "superseded", "retired"):
        advanced["approval"] = {
            "approved_by": "project-owner",
            "approved_at": "2026-09-16T00:00:00Z",
            "approval_id": "approval-1",
        }
    if state in ("published", "deployed", "superseded", "retired"):
        advanced["publication"] = {
            "published_by": "project-owner",
            "published_at": "2026-09-16T00:00:00Z",
            "publication_id": "publication-1",
        }
    advanced["manifest_digest"] = compute_manifest_digest(advanced)
    return advanced


@pytest.fixture(scope="module")
def example_manifest(root: Path, example_request: dict) -> dict:
    """The example platform manifest, validated (draft → validated)."""
    manifest = compose_request_document(example_request, root=root).document
    document = validated(manifest)
    assert validate_document(document, root=root) == []
    return document


@pytest.fixture(scope="module")
def minimal_manifest(root: Path) -> dict:
    """A validated manifest with one explicit component and no configuration."""
    request = build_request_document(
        manifest_id="learning-platform",
        manifest_version="1.0.0",
        components=[{"component_id": "learning", "component_version": "0.3.0"}],
    )
    manifest = compose_request_document(request, root=root).document
    document = validated(manifest)
    assert validate_document(document, root=root) == []
    return document


def rejects(
    manifest: object,
    platform_id: object,
    root: Path,
    fragment: str,
) -> list[str]:
    errors = assembly_diagnostics(manifest, platform_id=platform_id, root=root)
    assert errors, "expected the assembly to be rejected"
    assert any(
        fragment in error for error in errors
    ), f"expected an error containing {fragment!r}, got:\n" + "\n".join(errors)
    return errors


def clone(document: Mapping) -> dict:
    return copy.deepcopy(dict(document))


def materialize(tmp_path: Path) -> Path:
    """Copy the canonical sources into an isolated throwaway repository root."""
    source = discover_root()
    target = tmp_path / "repo"
    shutil.copytree(source / "components", target / "components")
    shutil.copytree(source / "factory", target / "factory")
    shutil.copy(source / "pyproject.toml", target / "pyproject.toml")
    return target


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Positive: valid manifest → successful assembly
# ---------------------------------------------------------------------------


class TestPositiveAssembly:
    def test_valid_manifest_assembles(self, root: Path, example_manifest: dict) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert instance.platform_id == "example-platform"
        assert instance.manifest_id == "example-platform"
        assert instance.manifest_version == "1.0.0"
        assert instance.manifest_state == "validated"
        assert instance.instance_digest.startswith("sha256:")
        assert instance.component_ids == tuple(
            entry["component_id"] for entry in example_manifest["components"]
        )

    def test_minimal_manifest_assembles_without_optional_sections(
        self, root: Path, minimal_manifest: dict
    ) -> None:
        instance = assemble_document(
            minimal_manifest, platform_id="learning-platform", root=root
        )
        assert instance.component_ids == LEARNING_CLOSURE
        for absent in ("configuration", "extensions", "branding"):
            assert absent not in instance.document, absent
        # Explicit uncertified status (ARCHITECTURE.md §31).
        assert instance.document["golden_bundle"] is None
        assert instance.certified is False

    def test_exact_manifest_binding(self, root: Path, example_manifest: dict) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert instance.manifest_binding == {
            "manifest_id": example_manifest["manifest_id"],
            "manifest_version": example_manifest["manifest_version"],
            "manifest_digest": example_manifest["manifest_digest"],
        }

    def test_manifest_binding_digest_is_the_content_digest(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert instance.manifest_digest == compute_manifest_digest(example_manifest)

    def test_component_versions_preserved_exactly(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert canonical(instance.document["components"]) == canonical(
            example_manifest["components"]
        )
        for entry in instance.components:
            manifest_entry = next(
                item
                for item in example_manifest["components"]
                if item["component_id"] == entry["component_id"]
            )
            assert entry["component_version"] == manifest_entry["component_version"]

    def test_artifact_identities_preserved_exactly(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        for entry, manifest_entry in zip(
            instance.components, example_manifest["components"], strict=True
        ):
            assert entry["artifact"] == manifest_entry["artifact"]

    def test_valid_configuration_preserved(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert instance.document["configuration"] == example_manifest["configuration"]

    def test_valid_extensions_preserved(self, root: Path) -> None:
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            extensions=[
                {
                    "extension_id": "learning-webhook",
                    "extension_version": "1.0.0",
                    "component_id": "learning",
                    "mechanism": "webhook",
                    "contract": "components/learning/contract/openapi.yaml",
                }
            ],
        )
        manifest = compose_request_document(request, root=root).document
        document = validated(manifest)
        assert validate_document(document, root=root) == []
        instance = assemble_document(
            document, platform_id="learning-platform", root=root
        )
        assert instance.document["extensions"] == document["extensions"]

    def test_valid_branding_preserved(self, root: Path) -> None:
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            branding={
                "product_name": "Learning Platform",
                "primary_color": "#0044cc",
            },
        )
        manifest = compose_request_document(request, root=root).document
        document = validated(manifest)
        assert validate_document(document, root=root) == []
        instance = assemble_document(
            document, platform_id="learning-platform", root=root
        )
        assert instance.document["branding"] == document["branding"]

    def test_environment_settings_preserved_through_configuration(
        self, root: Path
    ) -> None:
        """Environment-specific settings ride the existing §20 contract path.

        Component contracts declare an ``environment`` configuration key; the
        manifest carries it in component-scoped configuration; the instance
        preserves it verbatim. Slice F introduces no separate
        environment-overlay contract.
        """
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={
                "learning": {
                    "platform_id": "school-42",
                    "environment": "standalone",
                }
            },
        )
        manifest = compose_request_document(request, root=root).document
        document = validated(manifest)
        assert validate_document(document, root=root) == []
        instance = assemble_document(document, platform_id="school-42", root=root)
        assert instance.document["configuration"]["learning"]["environment"] == (
            "standalone"
        )

    def test_certified_bundle_reference_preserved(self, tmp_path: Path) -> None:
        """A certified Golden Bundle reference is inherited, never re-derived."""
        root = materialize(tmp_path)
        registry = load_registry(root)
        pinned = sorted(LEARNING_CLOSURE)
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
        bundle = build_bundle_document(
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
        relative = "factory/golden_bundle/certified_learning.json"
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            golden_bundle={
                "bundle_id": bundle["bundle_id"],
                "bundle_version": bundle["bundle_version"],
                "bundle_digest": compute_bundle_digest(bundle),
                "path": relative,
            },
        )
        manifest = compose_request_document(request, root=root).document
        document = validated(manifest)
        assert validate_document(document, root=root) == []
        instance = assemble_document(
            document, platform_id="learning-platform", root=root
        )
        assert instance.document["golden_bundle"] == {
            "bundle_id": bundle["bundle_id"],
            "bundle_version": bundle["bundle_version"],
            "bundle_digest": document["golden_bundle"]["bundle_digest"],
        }
        assert instance.certified is True

    def test_later_manifest_states_assemble(
        self, root: Path, example_manifest: dict
    ) -> None:
        for state in ("approved", "published", "deployed"):
            document = validated(example_manifest, state=state)
            assert validate_document(document, root=root) == [], state
            instance = assemble_document(
                document, platform_id="example-platform", root=root
            )
            assert instance.manifest_state == state

    def test_example_instance_is_the_exact_assembly(
        self, root: Path, example_manifest: dict
    ) -> None:
        """The shipped example is the deterministic assembly of the shipped example."""
        expected = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        shipped = load_instance_document(root / EXAMPLE_INSTANCE_PATH)
        assert canonical(shipped) == canonical(expected.document)
        assert validate_instance_document(shipped, example_manifest, root=root) == []

    def test_instance_validates_against_its_manifest(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert (
            validate_instance_document(instance.document, example_manifest, root=root)
            == []
        )

    def test_assembled_instance_document_has_no_generated_fields(
        self, root: Path, example_manifest: dict
    ) -> None:
        """Every instance value is inherited or the explicit platform identity."""
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert set(instance.document) <= {
            "$schema",
            "platform_id",
            "instance_digest",
            "manifest",
            "manifest_state",
            "components",
            "golden_bundle",
            "configuration",
            "extensions",
            "branding",
        }
        rendered = instance.render()
        assert "generated" not in rendered
        assert "hostname" not in rendered


# ---------------------------------------------------------------------------
# Negative: deterministic rejection
# ---------------------------------------------------------------------------


class TestNegativeAssembly:
    def test_draft_manifest_is_rejected(
        self, root: Path, example_request: dict
    ) -> None:
        manifest = compose_request_document(example_request, root=root).document
        assert manifest["lifecycle"]["state"] == "draft"
        rejects(
            manifest,
            "example-platform",
            root,
            "is not assemblable",
        )

    @pytest.mark.parametrize("state", ["superseded", "retired"])
    def test_historical_manifest_states_are_rejected(
        self, root: Path, example_manifest: dict, state: str
    ) -> None:
        document = validated(example_manifest, state=state)
        rejects(document, "example-platform", root, "is not assemblable")

    def test_invalid_manifest_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        for entry in document["components"]:
            if entry["component_id"] == "learning":
                entry["component_version"] = "9.9.9"
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "example-platform", root, "canonical registry")

    def test_missing_component_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        document["components"] = [
            entry
            for entry in document["components"]
            if entry["component_id"] != "idempotency"
        ]
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "example-platform", root, "absent from the manifest")

    def test_unregistered_component_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        document["components"][0]["component_id"] = "ghost_component"
        document["components"].sort(key=lambda entry: entry["component_id"])
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "example-platform", root, "ghost_component")

    def test_missing_artifact_identity_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        for entry in document["components"]:
            if entry["component_id"] == "learning":
                entry["artifact"] = {
                    "artifact_type": "container_image",
                    "digest": None,
                    "pinned": False,
                }
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(
            document,
            "example-platform",
            root,
            "requires an immutable digest",
        )

    def test_invalid_configuration_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        document["configuration"]["learning"]["unknown_key"] = 1
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "example-platform", root, "unknown configuration key")

    def test_invalid_configuration_value_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        document["configuration"]["learning"]["platform_id"] = 7
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "example-platform", root, "expected type")

    def test_invalid_extension_is_rejected(self, root: Path) -> None:
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            extensions=[
                {
                    "extension_id": "records-webhook",
                    "component_id": "records",
                    "mechanism": "webhook",
                    "contract": "components/records/contract/openapi.yaml",
                }
            ],
        )
        # The Composer already rejects an extension that introduces a component;
        # build the manifest document directly to test the assembly-time surface.
        manifest = compose_request_document(
            build_request_document(
                manifest_id="learning-platform",
                manifest_version="1.0.0",
                components=[{"component_id": "learning", "component_version": "0.3.0"}],
            ),
            root=root,
        ).document
        manifest["extensions"] = request["extensions"]
        document = validated(manifest)
        rejects(
            document,
            "learning-platform",
            root,
            "not part of the composed platform",
        )

    def test_invalid_branding_is_rejected(self, root: Path) -> None:
        """The Composer already rejects floating selectors at composition time
        (the request above is rejected before a manifest exists); the
        assembly-time surface rejects a forged manifest carrying one."""
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
        )
        assert compose_diagnostics(request, root=root) == []
        manifest = compose_request_document(request, root=root).document
        document = validated(manifest)
        document["branding"] = {"theme": "latest"}
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "learning-platform", root, "floating selector")

    def test_invalid_environment_settings_are_rejected(self, root: Path) -> None:
        """A floating selector cannot hide inside an environment key: the
        Composer request is rejected outright, and a forged manifest carrying
        one is rejected at assembly."""
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={
                "learning": {"platform_id": "school-42", "environment": "latest"}
            },
        )
        assert compose_diagnostics(request, root=root) != []
        plain = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            configuration={"learning": {"platform_id": "school-42"}},
        )
        manifest = compose_request_document(plain, root=root).document
        document = validated(manifest)
        document["configuration"]["learning"]["environment"] = "latest"
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "school-42", root, "floating selector")

    @pytest.mark.parametrize(
        "selector",
        [
            "latest",
            "current",
            "default",
            "stable",
            "edge",
            "main",
            "master",
            "head",
            "tip",
            "*",
        ],
    )
    def test_floating_platform_identity_is_rejected(
        self, root: Path, example_manifest: dict, selector: str
    ) -> None:
        assert assembly_diagnostics(example_manifest, platform_id=selector, root=root)

    def test_missing_platform_identity_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        errors = assembly_diagnostics(example_manifest, platform_id=None, root=root)
        assert errors
        assert any("explicit" in error for error in errors)

    def test_tampered_manifest_identity_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        document["manifest_digest"] = "sha256:" + "0" * 64
        rejects(
            document,
            "example-platform",
            root,
            "does not match the computed content digest",
        )

    def test_tampered_manifest_content_with_restated_digest_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        """A forged manifest (content edited, digest recomputed) cannot pass:
        the composition surfaces re-check it against authoritative metadata."""
        document = clone(example_manifest)
        for entry in document["components"]:
            if entry["component_id"] == "learning":
                entry["component_version"] = "0.2.0"
        document["manifest_digest"] = compute_manifest_digest(document)
        rejects(document, "example-platform", root, "canonical registry")

    def test_instance_bound_to_a_different_manifest_is_rejected(
        self, root: Path, example_manifest: dict, minimal_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        # The example instance claims the example manifest; validate it
        # against a different manifest — the binding cannot hold.
        errors = validate_instance_document(
            instance.document, minimal_manifest, root=root
        )
        assert errors
        assert any("manifest_id" in error for error in errors)

    def test_tampered_instance_content_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        document = clone(instance.document)
        document["components"][0]["component_version"] = "9.9.9"
        errors = validate_instance_document(document, example_manifest, root=root)
        assert errors
        assert any("instance_digest" in error for error in errors)
        assert any("diverges" in error for error in errors)

    def test_tampered_configuration_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        document = clone(instance.document)
        document["configuration"]["learning"]["platform_id"] = "another-platform"
        errors = validate_instance_document(document, example_manifest, root=root)
        assert any("diverges" in error for error in errors)

    def test_tampered_branding_is_rejected(self, root: Path) -> None:
        request = build_request_document(
            manifest_id="learning-platform",
            manifest_version="1.0.0",
            components=[{"component_id": "learning", "component_version": "0.3.0"}],
            branding={"product_name": "Learning Platform"},
        )
        manifest = compose_request_document(request, root=root).document
        document = validated(manifest)
        instance = assemble_document(
            document, platform_id="learning-platform", root=root
        )
        tampered = clone(instance.document)
        tampered["branding"]["product_name"] = "Other Platform"
        errors = validate_instance_document(tampered, document, root=root)
        assert any("diverges" in error for error in errors)

    def test_tampered_digest_with_preserved_content_is_still_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        """The manifest is authoritative: a digest alone proves nothing."""
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        document = clone(instance.document)
        document["components"] = document["components"][:-1]
        document["instance_digest"] = compute_instance_digest(document)
        errors = validate_instance_document(document, example_manifest, root=root)
        assert errors
        assert any("diverges" in error for error in errors)

    def test_manifest_state_mismatch_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        document = clone(instance.document)
        document["manifest_state"] = "approved"
        document["instance_digest"] = compute_instance_digest(document)
        errors = validate_instance_document(document, example_manifest, root=root)
        assert any("manifest_state" in error for error in errors)

    def test_invented_instance_lifecycle_state_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        """No instance-specific lifecycle vocabulary exists."""
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        document = clone(instance.document)
        document["manifest_state"] = "assembling"
        document["instance_digest"] = compute_instance_digest(document)
        errors = validate_instance_document(document, example_manifest, root=root)
        assert any("lifecycle state" in error for error in errors)

    def test_platform_identity_conflict_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        """The manifest's configuration names the platform it serves."""
        rejects(
            example_manifest,
            "another-platform",
            root,
            "names Platform Instance",
        )

    @pytest.mark.parametrize("bad", [None, [], "not-an-object", 42])
    def test_non_object_instance_is_rejected(
        self, root: Path, example_manifest: dict, bad: object
    ) -> None:
        errors = validate_instance_document(bad, example_manifest, root=root)
        assert errors == ["$: platform instance must be a JSON object"]

    @pytest.mark.parametrize("bad", [None, [], "not-an-object"])
    def test_non_object_manifest_is_rejected(self, root: Path, bad: object) -> None:
        errors = assembly_diagnostics(bad, platform_id="example-platform", root=root)
        assert errors == ["$: platform manifest must be a JSON object"]
        errors = validate_instance_document({"platform_id": "x"}, bad, root=root)
        assert any("required" in error for error in errors)

    def test_validation_without_bound_manifest_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        errors = validate_instance_document(instance.document, None, root=root)
        assert any("required" in error for error in errors)

    def test_missing_instance_document_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(InstanceNotFoundError):
            load_instance_document(tmp_path / "absent.json")

    def test_structurally_invalid_instance_is_rejected(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)  # a manifest is not an instance
        errors = validate_instance_document(document, example_manifest, root=root)
        assert errors

    def test_assembly_rejection_produces_no_instance(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        document["manifest_digest"] = "sha256:" + "0" * 64
        with pytest.raises(AssemblyRejectedError) as excinfo:
            assemble_document(document, platform_id="example-platform", root=root)
        assert excinfo.value.errors


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_document_and_digest(
        self, root: Path, example_manifest: dict
    ) -> None:
        first = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        second = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert first.render() == second.render()
        assert first.instance_digest == second.instance_digest
        assert first.document == second.document

    def test_key_order_does_not_change_the_instance(
        self, root: Path, example_manifest: dict
    ) -> None:
        """A canonically equal manifest (different insertion order) yields the
        same instance digest — identity is a function of content, not order."""
        reordered = {
            key: example_manifest[key] for key in sorted(example_manifest, reverse=True)
        }
        reordered["components"] = [
            {key: entry[key] for key in sorted(entry, reverse=True)}
            for entry in example_manifest["components"]
        ]
        reordered["configuration"] = {
            component: {key: value[key] for key in sorted(value, reverse=True)}
            for component, value in example_manifest["configuration"].items()
        }
        assert compute_manifest_digest(reordered) == compute_manifest_digest(
            example_manifest
        )
        first = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        second = assemble_document(reordered, platform_id="example-platform", root=root)
        assert first.render() == second.render()

    def test_identity_has_no_machine_or_path_dependence(
        self, tmp_path: Path, example_request: dict
    ) -> None:
        """The same inputs in a different root on a different path produce the
        same digest: no machine-specific state, no working-directory leakage."""
        root = materialize(tmp_path)
        manifest = compose_request_document(example_request, root=root).document
        document = validated(manifest)
        assert validate_document(document, root=root) == []
        instance = assemble_document(
            document, platform_id="example-platform", root=root
        )
        canonical_root = discover_root()
        canonical_manifest = compose_request_document(
            example_request, root=canonical_root
        ).document
        canonical_document = validated(canonical_manifest)
        other = assemble_document(
            canonical_document,
            platform_id="example-platform",
            root=canonical_root,
        )
        assert instance.instance_digest == other.instance_digest
        assert instance.render() == other.render()

    def test_no_time_or_randomness_in_the_document(
        self, root: Path, example_manifest: dict
    ) -> None:
        """The instance carries none of the manifest's lifecycle/approval/
        publication metadata and adds no timestamp of its own: two assemblies
        at "different times" are identical because no clock is ever read."""
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        for absent in (
            "lifecycle",
            "validation_attestation",
            "approval",
            "publication",
            "predecessor",
        ):
            assert absent not in instance.document, absent
        assert instance.manifest_state == example_manifest["lifecycle"]["state"]

    def test_validation_is_deterministic(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        first = validate_instance_document(
            instance.document, example_manifest, root=root
        )
        second = validate_instance_document(
            instance.document, example_manifest, root=root
        )
        assert first == second == []

    def test_rejections_are_deterministic(
        self, root: Path, example_manifest: dict
    ) -> None:
        document = clone(example_manifest)
        document["components"][0]["component_version"] = "9.9.9"
        document["manifest_digest"] = compute_manifest_digest(document)
        first = assembly_diagnostics(
            document, platform_id="example-platform", root=root
        )
        second = assembly_diagnostics(
            document, platform_id="example-platform", root=root
        )
        assert first == second
        assert first == sorted(first)

    def test_render_is_canonical(self, root: Path, example_manifest: dict) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        assert instance.render() == render_instance_document(instance.document)
        assert (
            instance.render()
            == json.dumps(
                instance.document, indent=2, ensure_ascii=False, sort_keys=True
            )
            + "\n"
        )

    def test_digest_model_matches_the_factory_model(
        self, root: Path, example_manifest: dict
    ) -> None:
        """``instance_digest`` uses the single canonical factory digest model."""
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        content = {
            key: value
            for key, value in instance.document.items()
            if key not in ("instance_digest", "$schema")
        }
        expected = (
            "sha256:" + hashlib.sha256(canonical(content).encode("utf-8")).hexdigest()
        )
        assert instance.instance_digest == expected
        assert instance.instance_digest == compute_instance_digest(instance.document)


# ---------------------------------------------------------------------------
# Architecture boundaries
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_implementation_imports_only_allowed_modules(self, root: Path) -> None:
        package = root / "src" / "platform_instance"
        sources = sorted(package.glob("*.py"))
        assert sources, "platform_instance package must exist"

        allowed_roots = frozenset(
            {
                "__future__",
                "argparse",
                "collections",
                "copy",
                "dataclasses",
                "hashlib",
                "json",
                "pathlib",
                "re",
                "sys",
                "typing",
                "component_registry",
                "composer",
                "platform_instance",
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
                "socket",
                "urllib",
                "requests",
                "httpx",
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

    def test_implementation_never_touches_a_component_database(
        self, root: Path
    ) -> None:
        for source in (root / "src" / "platform_instance").glob("*.py"):
            text = source.read_text(encoding="utf-8").lower()
            for keyword in (
                "sqlite",
                "psycopg",
                ".execute(",
                "cursor(",
                "create table",
            ):
                assert keyword not in text, (source.name, keyword)

    def test_assembly_assembles_and_nothing_else(self, root: Path) -> None:
        import platform_instance

        for forbidden in (
            "deploy",
            "rollout",
            "provision",
            "release_train",
            "approve",
            "publish",
            "set_approval",
            "set_publication",
            "advance_lifecycle",
            "transition",
        ):
            assert not hasattr(platform_instance, forbidden), forbidden

    def test_assembly_does_not_bypass_approval_or_publication(
        self, root: Path, example_manifest: dict
    ) -> None:
        """Assembly records the manifest state; it never writes one."""
        before = canonical(example_manifest)
        assemble_document(example_manifest, platform_id="example-platform", root=root)
        assert canonical(example_manifest) == before

    def test_assembly_never_mutates_its_inputs(
        self, root: Path, example_manifest: dict
    ) -> None:
        snapshot = clone(example_manifest)
        assemble_document(example_manifest, platform_id="example-platform", root=root)
        assert example_manifest == snapshot

    def test_assembly_never_mutates_authoritative_sources(
        self, tmp_path: Path, example_request: dict
    ) -> None:
        """Registry, catalog, bundles and schemas are byte-identical after
        an assembly run against a materialized root."""
        root = materialize(tmp_path)
        manifest = compose_request_document(example_request, root=root).document
        document = validated(manifest)

        def tree_digest() -> bytes:
            accumulator = hashlib.sha256()
            for path in sorted((root / "factory").rglob("*")):
                if path.is_file():
                    accumulator.update(str(path).encode("utf-8"))
                    accumulator.update(path.read_bytes())
            return accumulator.digest()

        before = tree_digest()
        assemble_document(document, platform_id="example-platform", root=root)
        assert tree_digest() == before

    def test_manifest_is_the_only_source_of_composition_truth(
        self, root: Path, example_manifest: dict
    ) -> None:
        """An instance diverging from its manifest is rejected even when its
        own digest is internally consistent — the manifest stays authoritative
        and the instance never becomes a second source of truth."""
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        document = clone(instance.document)
        document["components"] = document["components"][:1]
        document["instance_digest"] = compute_instance_digest(document)
        errors = validate_instance_document(document, example_manifest, root=root)
        assert any("diverges" in error for error in errors)

    def test_no_deployment_or_runtime_semantics_in_the_document(
        self, root: Path, example_manifest: dict
    ) -> None:
        instance = assemble_document(
            example_manifest, platform_id="example-platform", root=root
        )
        rendered = instance.render().lower()
        for keyword in ("replicas", "ingress", "helm", "terraform", "kube"):
            assert keyword not in rendered, keyword

    def test_only_existing_lifecycle_vocabulary_is_used(self, root: Path) -> None:
        assert ASSEMBLABLE_MANIFEST_STATES <= LIFECYCLE_STATES
        assert ASSEMBLABLE_MANIFEST_STATES == {
            "validated",
            "approved",
            "published",
            "deployed",
        }

    def test_ships_no_independent_metadata_inventory(self, root: Path) -> None:
        """``factory/platform_instance`` carries the schema, its rules and the
        deterministic example — no registry, catalog or bundle copy."""
        shipped = {
            str(path.relative_to(root / "factory" / "platform_instance"))
            for path in (root / "factory" / "platform_instance").rglob("*")
            if path.is_file()
        }
        assert shipped == {
            "README.md",
            "example_instance.json",
            "schema/platform_instance.schema.json",
        }

    def test_build_instance_document_is_pure(
        self, root: Path, example_manifest: dict
    ) -> None:
        snapshot = clone(example_manifest)
        first = build_instance_document(
            platform_id="example-platform", manifest_document=example_manifest
        )
        second = build_instance_document(
            platform_id="example-platform", manifest_document=example_manifest
        )
        assert first == second
        assert example_manifest == snapshot
        assert first["instance_digest"] == compute_instance_digest(first)
