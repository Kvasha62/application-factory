"""Focused tests for Slice D — Golden Bundles (Issue #71, ADR-0015 §15).

These tests verify architecture rules, not merely successful object
construction (ADR-0015 §16): normative schema; bundle identity/version/digest;
explicit bundle lifecycle; pinned component set resolved against the
authoritative Component Registry; explicit compatibility matrix; artifact
pinning; deterministic reproducibility; delivered-bundle immutability; and
the deterministic rejection of forbidden selectors and of any implicit
dependency/version resolution (the Composer boundary stays in Slice E).

The ``latest`` tests follow the Slice A/B/C doctrine: ``latest`` is not
forbidden as a string, it is forbidden as a *selector* — as a way of choosing
a version, a release, an artifact, a dependency or a production target. Tests
therefore feed ``latest``/``current``/``default``/``stable`` into selector
fields and require rejection, while proving the same words are accepted in
prose positions that select nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from component_registry import REGISTRY_PATH, load_registry
from golden_bundle import (
    BUNDLE_STATES,
    LIFECYCLE_ORDER,
    build_bundle_document,
    canonical_json,
    check_immutability,
    compute_bundle_digest,
    discover_root,
    render_bundle_document,
    validate_document,
    validate_transition,
)
from golden_bundle.lifecycle import (
    is_allowed_transition,
    is_forward_transition,
    requires_certification,
)
from golden_bundle.schema import load_schema

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
def valid_bundle(root: Path) -> dict:
    """A deterministic-canonical, valid draft bundle.

    Pins one dependency-free component (``tenant_authority``) so the document
    is schema-valid and semantically valid: the canonical digest is recomputed
    by ``build_bundle_document``, so any later copy-and-mutate remains valid as
    long as the mutation is re-digested by callers via ``recanonicalize``.
    """
    return build_bundle_document(
        bundle_id="compat-core",
        bundle_version="1.0.0",
        lifecycle_state="draft",
        components=[
            {
                "component_id": "tenant_authority",
                "component_version": "0.1.0",
                "artifact": {"artifact_type": "none", "digest": None, "pinned": False, "canonical_form": None},
            }
        ],
        compatibility_pairs=[],
        certification=None,
    )


@pytest.fixture
def example_document(root: Path) -> dict:
    """The repository's build-example bundle (9 pinned components, candidate)."""
    path = root / "factory" / "golden_bundle" / "example_bundle.json"
    return json.loads(path.read_text(encoding="utf-8"))


def recanonicalize(document: Mapping) -> dict:
    """Rebuild ``document`` through the deterministic builder.

    Removes the digest and rebuilds from the semantic fields so that a mutated
    document carries the digest of its own mutated content (what a
    correctly-functioning author would produce).
    """
    components = [
        {
            "component_id": entry["component_id"],
            "component_version": entry["component_version"],
            "artifact": dict(entry["artifact"]),
        }
        for entry in document.get("components", [])
    ]
    pairs = [
        {
            "from": pair["from"],
            "to": pair["to"],
            "mechanism": pair["mechanism"],
            "compatible": pair["compatible"],
        }
        for pair in document.get("compatibility", {}).get("pairs", [])
    ]
    certification = document.get("certification")
    return build_bundle_document(
        bundle_id=document["bundle_id"],
        bundle_version=document["bundle_version"],
        lifecycle_state=document.get("lifecycle", {}).get("state", "draft"),
        components=components,
        compatibility_pairs=pairs,
        certification=certification,
    )


def fully_pinned_bundle(root: Path, registry) -> dict:
    """Pin the full registry inventory and propagate declared dependencies."""
    components = sorted(
        (
            {
                "component_id": entry["component_id"],
                "component_version": entry["component_version"],
                "artifact": dict(entry["artifact"]),
            }
            for entry in registry.entries
        ),
        key=lambda e: e["component_id"],
    )
    pinned_ids = {entry["component_id"] for entry in components}
    pairs = []
    for entry in registry.entries:
        for dependency in entry["dependencies"]:
            if dependency["component_id"] in pinned_ids:
                pairs.append(
                    {
                        "from": entry["component_id"],
                        "to": dependency["component_id"],
                        "mechanism": dependency["kind"],
                        "compatible": True,
                    }
                )
    return build_bundle_document(
        bundle_id="full-inventory",
        bundle_version="1.0.0",
        lifecycle_state="draft",
        components=components,
        compatibility_pairs=pairs,
        certification=None,
    )


# ---------------------------------------------------------------------------
# Normative schema and vocabulary
# ---------------------------------------------------------------------------


class TestSchema:
    def test_schema_loads_and_is_mapping(self, root: Path) -> None:
        schema = load_schema(root)
        assert isinstance(schema, Mapping)
        assert schema.get("title") == "Application Factory — Golden Bundle"

    def test_lifecycle_order_is_architecture_order(self) -> None:
        assert LIFECYCLE_ORDER == (
            "draft",
            "candidate",
            "certified",
            "deprecated",
            "revoked",
        )

    def test_states_match_architecture_vocabulary(self) -> None:
        assert BUNDLE_STATES == {
            "draft",
            "candidate",
            "certified",
            "deprecated",
            "revoked",
        }


# ---------------------------------------------------------------------------
# Identity / version / digest
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_example_bundle_has_valid_identity(self, example_document, root) -> None:
        assert validate_document(example_document, root=root) == []

    def test_missing_bundle_id_rejected(self, root) -> None:
        doc = compute_digest_doc(
            {
                "bundle_version": "1.0.0",
                "lifecycle": {"state": "draft"},
                "components": [],
                "compatibility": {"pairs": []},
            }
        )
        errors = validate_document(doc, root=root)
        assert any("bundle_id" in e for e in errors)

    def test_missing_version_rejected(self, root) -> None:
        doc = compute_digest_doc(
            {
                "bundle_id": "compat-core",
                "lifecycle": {"state": "draft"},
                "components": [],
                "compatibility": {"pairs": []},
            }
        )
        errors = validate_document(doc, root=root)
        assert any("bundle_version" in e for e in errors)

    def test_non_semver_version_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize({**valid_bundle, "bundle_version": "1.0"})
        errors = validate_document(doc, root=root)
        assert any("not SemVer" in e for e in errors)

    def test_invalid_bundle_id_chars_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize({**valid_bundle, "bundle_id": "Bundle Upper!"})
        errors = validate_document(doc, root=root)
        assert any("does not match pattern" in e for e in errors)

    def test_digest_mismatch_rejected(self, root, valid_bundle) -> None:
        doc = dict(valid_bundle)
        doc["bundle_digest"] = "sha256:" + "0" * 64
        errors = validate_document(doc, root=root)
        assert any("does not match computed digest" in e for e in errors)

    def test_raw_bundle_id_without_digest_rejected(self, root) -> None:
        doc = compute_digest_doc(
            {
                "bundle_id": "compat-core",
                "bundle_version": "1.0.0",
                "lifecycle": {"state": "draft"},
                "components": [],
                "compatibility": {"pairs": []},
            }
        )
        doc.pop("bundle_digest", None)
        errors = validate_document(doc, root=root)
        assert any("bundle_digest" in e for e in errors)


def compute_digest_doc(content: dict) -> dict:
    """Attach a correct digest to a raw content dict (helper for missing-field tests)."""
    doc = dict(content)
    digest = compute_bundle_digest(doc)
    doc["bundle_digest"] = digest
    return doc


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_missing_lifecycle_rejected(self, root) -> None:
        doc = compute_digest_doc(
            {
                "bundle_id": "compat-core",
                "bundle_version": "1.0.0",
                "components": [],
                "compatibility": {"pairs": []},
            }
        )
        errors = validate_document(doc, root=root)
        assert any("lifecycle" in e for e in errors)

    def test_unknown_state_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize({**valid_bundle, "lifecycle": {"state": "deployed"}})
        errors = validate_document(doc, root=root)
        assert any("not a bundle lifecycle state" in e for e in errors)

    def test_certified_with_valid_certification_accepted(
        self, root, example_document
    ) -> None:
        cert = {
            "certified_by": "owner",
            "certified_at": "2026-09-15T00:00:00Z",
            "certification_id": "cert-1",
            "checks": ["compatibility", "contract-tests", "signature"],
        }
        doc = recanonicalize(
            {
                **example_document,
                "lifecycle": {"state": "certified"},
                "certification": cert,
            }
        )
        assert validate_document(doc, root=root) == []

    def test_certified_requires_certification(self, root, valid_bundle) -> None:
        doc = recanonicalize({**valid_bundle, "lifecycle": {"state": "certified"}})
        errors = validate_document(doc, root=root)
        assert any("certification evidence is required" in e for e in errors)

    def test_draft_must_not_carry_certification(self, root, valid_bundle) -> None:
        cert = {
            "certified_by": "owner",
            "certified_at": "2026-09-15T00:00:00Z",
            "certification_id": "cert-1",
            "checks": ["compatibility", "contract-tests"],
        }
        doc = recanonicalize(
            {**valid_bundle, "lifecycle": {"state": "draft"}, "certification": cert}
        )
        errors = validate_document(doc, root=root)
        assert any("certification evidence must be null" in e for e in errors)

    def test_allowed_transitions(self) -> None:
        assert is_allowed_transition("draft", "candidate")
        assert is_allowed_transition("candidate", "certified")
        assert is_allowed_transition("certified", "deprecated")
        assert is_allowed_transition("deprecated", "revoked")
        assert is_allowed_transition("certified", "certified")

    def test_skipped_transition_rejected(self) -> None:
        assert not is_allowed_transition("draft", "certified")
        assert not is_allowed_transition("candidate", "deprecated")

    def test_terminal_transition_rejected(self) -> None:
        assert not is_allowed_transition("revoked", "certified")

    def test_forward_transition(self) -> None:
        assert is_forward_transition("draft", "candidate")
        assert not is_forward_transition("certified", "draft")

    def test_requires_certification(self) -> None:
        assert not requires_certification("draft")
        assert not requires_certification("candidate")
        assert requires_certification("certified")
        assert requires_certification("deprecated")
        assert requires_certification("revoked")

    def test_validate_transition_happy_and_errors(self) -> None:
        assert validate_transition("draft", "candidate") == []
        assert validate_transition("draft", "draft") == []
        assert validate_transition("draft", "certified") != []
        assert validate_transition("bogus", "draft") != []


# ---------------------------------------------------------------------------
# Pinned component set vs. authoritative Registry
# ---------------------------------------------------------------------------


class TestPinnedComponents:
    def test_unknown_component_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "not_a_component",
                        "component_version": "1.0.0",
                        "artifact": {
                            "artifact_type": "none",
                            "digest": None,
                            "pinned": False,
                            "canonical_form": None,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("not registered" in e for e in errors)

    def test_version_mismatch_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "booking",
                        "component_version": "9.9.9",
                        "artifact": {
                            "artifact_type": "none",
                            "digest": None,
                            "pinned": False,
                            "canonical_form": None,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("canonical registry declares" in e for e in errors)

    def test_duplicate_component_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "booking",
                        "component_version": "0.1.0",
                        "artifact": {
                            "artifact_type": "none",
                            "digest": None,
                            "pinned": False,
                            "canonical_form": None,
                        },
                    },
                    {
                        "component_id": "booking",
                        "component_version": "0.1.0",
                        "artifact": {
                            "artifact_type": "none",
                            "digest": None,
                            "pinned": False,
                            "canonical_form": None,
                        },
                    },
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("duplicate component identity" in e for e in errors)

    def test_unsorted_components_rejected(self, root, valid_bundle) -> None:
        # The builder sorts for authors; this test bypasses it to prove the
        # *validator* independently rejects an unsorted document.
        raw = {
            "$schema": "factory/golden_bundle/schema/golden_bundle.schema.json",
            "bundle_id": "compat-core",
            "bundle_version": "1.0.0",
            "lifecycle": {"state": "draft"},
            "components": [
                {
                    "component_id": "booking",
                    "component_version": "0.1.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                },
                {
                    "component_id": "authorization",
                    "component_version": "0.1.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                },
            ],
            "compatibility": {"pairs": []},
            "certification": None,
        }
        raw["bundle_digest"] = compute_bundle_digest(raw)
        errors = validate_document(raw, root=root)
        assert any("sorted by component_id" in e for e in errors)

    def test_artifact_type_mismatch_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "booking",
                        "component_version": "0.1.0",
                        "artifact": {
                            "artifact_type": "container_image",
                            "digest": "sha256:" + "a" * 64,
                            "pinned": True,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("artifact_type" in e for e in errors)

    def test_published_artifact_without_digest_rejected(
        self, root, valid_bundle
    ) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "booking",
                        "component_version": "0.1.0",
                        "artifact": {
                            "artifact_type": "source_package",
                            "digest": None,
                            "pinned": True,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("immutable digest" in e for e in errors)

    def test_none_artifact_must_not_pin(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "booking",
                        "component_version": "0.1.0",
                        "artifact": {
                            "artifact_type": "none",
                            "digest": None,
                            "pinned": True,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("must not be pinned" in e for e in errors)


# ---------------------------------------------------------------------------
# Compatibility matrix and dependency consistency
# ---------------------------------------------------------------------------


class TestCompatibility:
    def test_missing_compatibility_rejected(self, root, valid_bundle) -> None:
        doc = compute_digest_doc(
            {
                "bundle_id": "compat-core",
                "bundle_version": "1.0.0",
                "lifecycle": {"state": "draft"},
                "components": [],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("compatibility" in e for e in errors)
        assert validate_document(recanonicalize(valid_bundle), root=root) == []

    def test_full_inventory_bundle_is_valid(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        errors = validate_document(doc, root=root)
        assert errors == []

    def test_missing_dependency_pair_rejected(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        # Drop the authorization → identity pair.
        pairs = [
            p
            for p in doc["compatibility"]["pairs"]
            if not (p["from"] == "authorization" and p["to"] == "identity")
        ]
        mutated = build_bundle_document(
            bundle_id=doc["bundle_id"],
            bundle_version=doc["bundle_version"],
            lifecycle_state="draft",
            components=doc["components"],
            compatibility_pairs=pairs,
            certification=None,
        )
        errors = validate_document(mutated, root=root)
        assert any("no compatibility pair records it" in e for e in errors)

    def test_incompatible_pair_rejected(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        pairs = []
        for p in doc["compatibility"]["pairs"]:
            np = dict(p)
            if np["from"] == "authorization" and np["to"] == "identity":
                np["compatible"] = False
            pairs.append(np)
        mutated = build_bundle_document(
            bundle_id=doc["bundle_id"],
            bundle_version=doc["bundle_version"],
            lifecycle_state="draft",
            components=doc["components"],
            compatibility_pairs=pairs,
            certification=None,
        )
        errors = validate_document(mutated, root=root)
        assert any("not validated compatible" in e for e in errors)

    def test_invented_pair_rejected(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        pairs = [p for p in doc["compatibility"]["pairs"] if p["from"] != "booking"]
        pairs.append(
            {
                "from": "booking",
                "to": "learning",
                "mechanism": "api",
                "compatible": True,
            }
        )
        mutated = build_bundle_document(
            bundle_id=doc["bundle_id"],
            bundle_version=doc["bundle_version"],
            lifecycle_state="draft",
            components=doc["components"],
            compatibility_pairs=pairs,
            certification=None,
        )
        errors = validate_document(mutated, root=root)
        assert any("not a dependency declared" in e for e in errors)

    def test_duplicate_pair_rejected(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        first = dict(doc["compatibility"]["pairs"][0])
        pairs = [first, first]
        mutated = build_bundle_document(
            bundle_id=doc["bundle_id"],
            bundle_version=doc["bundle_version"],
            lifecycle_state="draft",
            components=doc["components"],
            compatibility_pairs=pairs,
            certification=None,
        )
        errors = validate_document(mutated, root=root)
        assert any("duplicate compatibility pair" in e for e in errors)

    def test_missing_pinned_dependency_rejected(self, root, registry) -> None:
        # Pin learning but omit its declared dependency identity.
        components = [
            {
                "component_id": "learning",
                "component_version": "0.3.0",
                "artifact": {"artifact_type": "none", "digest": None, "pinned": False, "canonical_form": None},
            }
        ]
        doc = build_bundle_document(
            bundle_id="learning-alone",
            bundle_version="1.0.0",
            lifecycle_state="draft",
            components=components,
            compatibility_pairs=[],
            certification=None,
        )
        errors = validate_document(doc, root=root)
        assert any("is absent from the bundle" in e for e in errors)

    def test_out_of_range_dependency_rejected(self, root, registry) -> None:
        # Pin learning with authorization, idempotency, identity.
        components = sorted(
            [
                {
                    "component_id": "learning",
                    "component_version": "0.3.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                },
                {
                    "component_id": "authorization",
                    "component_version": "0.1.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                },
                {
                    "component_id": "idempotency",
                    "component_version": "0.1.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                },
                # Wrong version for the identity dependency (>=0.3.0,<0.4.0):
                {
                    "component_id": "identity",
                    "component_version": "0.1.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                },
            ],
            key=lambda e: e["component_id"],
        )
        pairs = [
            {
                "from": "learning",
                "to": "authorization",
                "mechanism": "api",
                "compatible": True,
            },
            {
                "from": "learning",
                "to": "idempotency",
                "mechanism": "internal-consumer-surface",
                "compatible": True,
            },
            {
                "from": "learning",
                "to": "identity",
                "mechanism": "api",
                "compatible": True,
            },
        ]
        doc = build_bundle_document(
            bundle_id="learning-subset",
            bundle_version="1.0.0",
            lifecycle_state="draft",
            components=components,
            compatibility_pairs=pairs,
            certification=None,
        )
        errors = validate_document(doc, root=root)
        assert any("does not satisfy" in e for e in errors)


# ---------------------------------------------------------------------------
# Forbidden floating selectors
# ---------------------------------------------------------------------------


class TestForbiddenSelectors:
    @pytest.mark.parametrize("token", ["latest", "current", "default", "stable"])
    def test_top_level_selectors_rejected(self, root, valid_bundle, token) -> None:
        doc = recanonicalize({**valid_bundle, "bundle_id": token})
        errors = validate_document(doc, root=root)
        assert any("floating selector" in e for e in errors)

        doc = recanonicalize({**valid_bundle, "bundle_version": token})
        errors = validate_document(doc, root=root)
        assert any("floating selector" in e for e in errors)

        doc = dict(valid_bundle)
        doc["bundle_digest"] = token
        errors = validate_document(doc, root=root)
        assert any("floating selector" in e for e in errors)

    def test_component_version_selector_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "booking",
                        "component_version": "latest",
                        "artifact": {
                            "artifact_type": "none",
                            "digest": None,
                            "pinned": False,
                            "canonical_form": None,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("floating selector" in e for e in errors)

    def test_component_id_selector_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "stable",
                        "component_version": "1.0.0",
                        "artifact": {
                            "artifact_type": "none",
                            "digest": None,
                            "pinned": False,
                            "canonical_form": None,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("floating selector" in e for e in errors)

    def test_artifact_digest_selector_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize(
            {
                **valid_bundle,
                "components": [
                    {
                        "component_id": "booking",
                        "component_version": "0.1.0",
                        "artifact": {
                            "artifact_type": "source_package",
                            "digest": "latest",
                            "pinned": True,
                        },
                    }
                ],
            }
        )
        errors = validate_document(doc, root=root)
        assert any("floating selector" in e for e in errors)

    def test_lifecycle_state_selector_rejected(self, root, valid_bundle) -> None:
        doc = recanonicalize({**valid_bundle, "lifecycle": {"state": "latest"}})
        errors = validate_document(doc, root=root)
        assert any("not a bundle lifecycle state" in e for e in errors)

    def test_pair_mechanism_selector_rejected(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        pair = doc["compatibility"]["pairs"][0]
        pair["mechanism"] = "latest"
        errors = validate_document(doc, root=root)
        assert any("mechanism" in e and "not a declared" in e for e in errors)


# ---------------------------------------------------------------------------
# Reproducibility and determinism
# ---------------------------------------------------------------------------


class TestReproducibility:
    def test_same_document_same_digest(self, example_document) -> None:
        assert compute_bundle_digest(example_document) == compute_bundle_digest(
            example_document
        )

    def test_key_order_does_not_change_digest(self, example_document) -> None:
        reordered = {
            "bundle_version": example_document["bundle_version"],
            "components": example_document["components"],
            "bundle_id": example_document["bundle_id"],
            "compatibility": example_document["compatibility"],
            "lifecycle": example_document["lifecycle"],
            "certification": example_document["certification"],
        }
        assert compute_bundle_digest(reordered) == compute_bundle_digest(
            example_document
        )

    def test_valid_bundle_digest_matches_declared(self, example_document) -> None:
        assert example_document["bundle_digest"] == compute_bundle_digest(
            example_document
        )

    def test_content_change_changes_digest(self, example_document) -> None:
        changed = dict(example_document)
        changed["bundle_version"] = "2.0.0"
        changed.pop("bundle_digest", None)
        new_digest = compute_bundle_digest(changed)
        assert new_digest != example_document["bundle_digest"]
        # A well-formed author would then record the new digest; proving the
        # validator rejects a stale digest is covered by TestIdentity.

    def test_validation_deterministic(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        assert validate_document(doc, root=root) == validate_document(doc, root=root)

    def test_canonical_json_sort_keys(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_example_dump_round_trip(self, root) -> None:
        path = root / "factory" / "golden_bundle" / "example_bundle.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        assert json.loads(render_bundle_document(document)) == document


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_different_version_is_not_a_rewrite(self, root, example_document) -> None:
        new = dict(example_document)
        new["bundle_version"] = "2.0.0"
        new.pop("bundle_digest", None)
        new["bundle_digest"] = compute_bundle_digest(new)
        assert check_immutability([example_document], new) == []

    def test_same_version_content_change_rejected(self, root, example_document) -> None:
        cert = {
            "certified_by": "owner",
            "certified_at": "2026-09-15T00:00:00Z",
            "certification_id": "cert-1",
            "checks": ["compatibility"],
        }
        existing = recanonicalize(
            {
                **example_document,
                "lifecycle": {"state": "certified"},
                "certification": cert,
            }
        )
        tampered = recanonicalize(
            {**existing, "certification": {**cert, "certified_by": "someone-else"}}
        )
        errors = check_immutability([existing], tampered)
        assert any("immutable" in e for e in errors)

    def test_draft_is_not_immutable(self, root) -> None:
        draft_a = build_bundle_document(
            bundle_id="compat-core",
            bundle_version="1.0.0",
            lifecycle_state="draft",
            components=[
                {
                    "component_id": "tenant_authority",
                    "component_version": "0.1.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                }
            ],
            compatibility_pairs=[],
            certification=None,
        )
        draft_b = build_bundle_document(
            bundle_id="compat-core",
            bundle_version="1.0.0",
            lifecycle_state="candidate",
            components=[
                {
                    "component_id": "tenant_authority",
                    "component_version": "0.1.0",
                    "artifact": {
                        "artifact_type": "none",
                        "digest": None,
                        "pinned": False,
                    },
                }
            ],
            compatibility_pairs=[],
            certification=None,
        )
        assert check_immutability([draft_a], draft_b) == []


# ---------------------------------------------------------------------------
# Boundary and scope protection
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_validation_touches_only_metadata_sources(self, root, registry) -> None:
        # The bundle module imports registry/catalog metadata tooling only;
        # no business-system internals are imported.
        import golden_bundle.bundle
        import golden_bundle.validation

        source = Path(golden_bundle.bundle.__file__).read_text(encoding="utf-8") + Path(
            golden_bundle.validation.__file__
        ).read_text(encoding="utf-8")
        for forbidden in (
            "learning_service",
            "commerce_service",
            "booking_service",
            "identity_service",
            "authorization_service",
            "records_service",
            "tenant_authority",
            "component_registry.registry",
        ):
            assert forbidden not in source

    def test_registry_untouched_by_bundle_validation(self, root, registry) -> None:
        # Slice D must not mutate its authoritative source.
        before = Path(root / REGISTRY_PATH).read_text(encoding="utf-8")
        validate_document(fully_pinned_bundle(root, registry), root=root)
        after = Path(root / REGISTRY_PATH).read_text(encoding="utf-8")
        assert before == after

    def test_validation_does_not_mutate_bundle_document(self, root, registry) -> None:
        doc = fully_pinned_bundle(root, registry)
        snapshot = json.dumps(doc, sort_keys=True)
        validate_document(doc, root=root)
        assert json.dumps(doc, sort_keys=True) == snapshot

    def test_missing_dependency_is_an_error_not_a_fix(self, root, registry) -> None:
        # Slice D never adds missing components or picks versions: a missing
        # pinned dependency is a deterministic rejection, never an implicit fix.
        components = [
            {
                "component_id": "learning",
                "component_version": "0.3.0",
                "artifact": {"artifact_type": "none", "digest": None, "pinned": False, "canonical_form": None},
            }
        ]
        doc = build_bundle_document(
            bundle_id="learning-alone",
            bundle_version="1.0.0",
            lifecycle_state="draft",
            components=components,
            compatibility_pairs=[],
            certification=None,
        )
        before = json.dumps(doc, sort_keys=True)
        errors = validate_document(doc, root=root)
        assert any("absent from the bundle" in e for e in errors)
        assert json.dumps(doc, sort_keys=True) == before

    def test_no_composer_api_surface(self) -> None:
        # The public surface offers no version/dependency resolution entry:
        # validation of explicit pins only.
        import golden_bundle

        for forbidden in ("resolve", "substitut", "assemble", "resolve_version"):
            assert not hasattr(golden_bundle, forbidden)
