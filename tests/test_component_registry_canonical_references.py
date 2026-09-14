"""Adversarial tests for canonical reference semantics of the Component Registry.

These tests defend one invariant that a plain "the file exists and declares the
right component_id" check does **not** defend:

> a registry reference identifies a document by the canonical repository path
> the architecture assigns to that component — not by a file that merely
> happens to declare matching metadata somewhere else.

Every adversarial case below therefore builds a *real file with correct
metadata* at a *non-canonical* path (a decoy or duplicate descriptor) and
requires deterministic rejection.

The suite runs against a synthetic repository root so decoys, duplicates,
version drift and malformed contracts can be created freely without touching
the real component contracts.
"""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from component_registry import (
    COMPATIBILITY_POINTER,
    OWNERSHIP_POINTER,
    canonical_contract_path,
    canonical_openapi_path,
    discover_root,
    validate_document,
)
from component_registry.schema import SCHEMA_PATH

ALPHA = "alpha"
BETA = "beta"
ALPHA_VERSION = "0.1.0"
BETA_VERSION = "0.2.0"

OWNER = "@owner"

#: Reference fields exercised by the traversal / absolute / URL cases.
REFERENCE_FIELDS = (
    "component_contract",
    "openapi",
    "ownership_source",
    "compatibility_source",
    "dependency_contract",
)


def published_contract(
    component_id: str,
    version: str,
    *,
    datasets: list[dict] | None = None,
    policy: dict | None = None,
    ownership: object = None,
) -> dict:
    """A minimal but honest published component contract descriptor."""
    return {
        "component_id": component_id,
        "component_version": version,
        "class": "platform_service",
        "data_ownership": (
            {
                "owner": component_id,
                "logical_schema": component_id,
                "datasets": datasets or [{"name": "items", "scope": "tenant-scoped"}],
            }
            if ownership is None
            else ownership
        ),
        "compatibility_policy": (
            {
                "api": "additive-minor-breaking-major",
                "events": "tolerant-reader",
                "supported_api_majors": 1,
            }
            if policy is None
            else policy
        ),
        "dependencies": [],
        "api": {"openapi": canonical_openapi_path(component_id)},
    }


def registry_entry(
    component_id: str, version: str, *, dependency: dict | None = None
) -> dict:
    """A registry entry whose every reference is canonical."""
    contract = canonical_contract_path(component_id)
    return {
        "component_id": component_id,
        "component_version": version,
        "class": "platform_service",
        "owner": OWNER,
        "contracts": {
            "component_contract": contract,
            "openapi": canonical_openapi_path(component_id),
        },
        "data_ownership": {
            "owner": component_id,
            "logical_schema": component_id,
            "data_scopes": ["tenant-scoped"],
            "datasets": [{"name": "items", "scope": "tenant-scoped"}],
            "source": f"{contract}#{OWNERSHIP_POINTER}",
        },
        "dependencies": [] if dependency is None else [dependency],
        "compatibility": {
            "api_policy": "additive-minor-breaking-major",
            "event_policy": "tolerant-reader",
            "supported_api_majors": 1,
            "breaking_change_requires": "major_version",
            "source": f"{contract}#{COMPATIBILITY_POINTER}",
        },
        "artifact": {"artifact_type": "none", "digest": None, "pinned": False},
        "lifecycle": {
            "registry_state": "registered",
            "deployable": False,
            "publishable": False,
            "publishability_blockers": ["deployment_artifact_absent"],
        },
    }


def alpha_dependency() -> dict:
    """``alpha`` depends on ``beta`` through beta's canonical contract."""
    return {
        "component_id": BETA,
        "version_range": ">=0.2.0,<0.3.0",
        "kind": "api",
        "contract": canonical_contract_path(BETA),
    }


@dataclass
class Synthetic:
    """A throwaway repository root plus its registry document."""

    base: Path
    document: dict = field(default_factory=dict)

    # -- mutation helpers -------------------------------------------------
    def clone(self) -> dict:
        return copy.deepcopy(self.document)

    def errors(self, document: dict | None = None, **kwargs: object) -> list[str]:
        return validate_document(
            document if document is not None else self.clone(),
            root=self.base,
            **kwargs,  # type: ignore[arg-type]
        )

    def write(self, relative: str, payload: object) -> Path:
        """Write a JSON document at ``relative`` inside the synthetic root."""
        target = self.base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target

    def write_text(self, relative: str, text: str) -> Path:
        target = self.base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def patch(self, component_id: str, **fields: object) -> dict:
        clone = self.clone()
        for entry in clone["components"]:
            if entry["component_id"] == component_id:
                entry.update(fields)
                return clone
        raise AssertionError(f"no such component: {component_id}")

    def patch_section(self, component_id: str, section: str, **fields: object) -> dict:
        clone = self.clone()
        for entry in clone["components"]:
            if entry["component_id"] == component_id:
                entry[section].update(fields)
                return clone
        raise AssertionError(f"no such component: {component_id}")


@pytest.fixture(scope="module")
def real_root() -> Path:
    return discover_root()


@pytest.fixture
def synth(tmp_path: Path, real_root: Path) -> Synthetic:
    """Build a two-component synthetic repository root."""
    base = tmp_path / "repo"

    # The normative schema is copied verbatim: these tests must exercise the
    # real schema, never a stand-in.
    schema_target = base / SCHEMA_PATH
    schema_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(real_root / SCHEMA_PATH, schema_target)

    synthetic = Synthetic(base=base)

    synthetic.write(
        canonical_contract_path(ALPHA), published_contract(ALPHA, ALPHA_VERSION)
    )
    synthetic.write(
        canonical_contract_path(BETA), published_contract(BETA, BETA_VERSION)
    )
    synthetic.write_text(canonical_openapi_path(ALPHA), "openapi: 3.0.0\n")
    synthetic.write_text(canonical_openapi_path(BETA), "openapi: 3.0.0\n")

    synthetic.document = {
        "registry_id": "synthetic-component-registry",
        "registry_schema_version": "1.0.0",
        "components": [
            registry_entry(ALPHA, ALPHA_VERSION, dependency=alpha_dependency()),
            registry_entry(BETA, BETA_VERSION),
        ],
    }
    return synthetic


def set_reference(document: dict, field_name: str, value: str) -> dict:
    """Point one reference field of the ``alpha`` entry at ``value``."""
    clone = copy.deepcopy(document)
    alpha = clone["components"][0]
    if field_name == "component_contract":
        alpha["contracts"]["component_contract"] = value
    elif field_name == "openapi":
        alpha["contracts"]["openapi"] = value
    elif field_name == "ownership_source":
        # the document path is what the case attacks; the fragment stays valid
        alpha["data_ownership"]["source"] = (
            value if "#" in value else f"{value}#{OWNERSHIP_POINTER}"
        )
    elif field_name == "compatibility_source":
        alpha["compatibility"]["source"] = (
            value if "#" in value else f"{value}#{COMPATIBILITY_POINTER}"
        )
    elif field_name == "dependency_contract":
        alpha["dependencies"][0]["contract"] = value
    else:
        raise AssertionError(f"unknown reference field: {field_name}")
    return clone


def rejects(synth: Synthetic, document: dict, fragment: str) -> None:
    errors = synth.errors(document)
    assert errors, "expected validation to fail, but the document was accepted"
    assert any(
        fragment in error for error in errors
    ), f"expected an error containing {fragment!r}, got:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# Positive: canonical references are accepted
# ---------------------------------------------------------------------------


def test_the_synthetic_registry_with_canonical_references_is_valid(
    synth: Synthetic,
) -> None:
    assert synth.errors() == []


def test_canonical_component_contract_is_accepted(synth: Synthetic) -> None:
    assert (
        canonical_contract_path(ALPHA)
        == f"components/{ALPHA}/contract/component_contract.json"
    )
    assert synth.errors() == []


def test_canonical_ownership_source_is_accepted(synth: Synthetic) -> None:
    contract = canonical_contract_path(ALPHA)
    assert synth.errors() == []
    rejects_source = f"{contract}#{OWNERSHIP_POINTER}"
    clone = synth.patch_section(ALPHA, "data_ownership", source=rejects_source)
    assert synth.errors(clone) == []


def test_canonical_compatibility_source_is_accepted(synth: Synthetic) -> None:
    source = f"{canonical_contract_path(ALPHA)}#{COMPATIBILITY_POINTER}"
    clone = synth.patch_section(ALPHA, "compatibility", source=source)
    assert synth.errors(clone) == []


def test_canonical_dependency_contract_is_accepted(synth: Synthetic) -> None:
    clone = synth.clone()
    clone["components"][0]["dependencies"][0]["contract"] = canonical_contract_path(
        BETA
    )
    assert synth.errors(clone) == []


# ---------------------------------------------------------------------------
# Test 1 — decoy contract at a non-canonical path
# ---------------------------------------------------------------------------


def test_component_contract_must_use_canonical_path(synth: Synthetic) -> None:
    """A decoy that exists and declares correct metadata is still rejected."""
    synth.write(
        "somewhere/alpha_contract.json", published_contract(ALPHA, ALPHA_VERSION)
    )
    clone = synth.patch_section(
        ALPHA, "contracts", component_contract="somewhere/alpha_contract.json"
    )
    rejects(synth, clone, "is not the canonical contract")


# ---------------------------------------------------------------------------
# Test 2 — duplicate descriptor
# ---------------------------------------------------------------------------


def test_component_contract_duplicate_descriptor_is_rejected(synth: Synthetic) -> None:
    """A byte-identical duplicate descriptor at another path is not the canonical one."""
    canonical = synth.base / canonical_contract_path(ALPHA)
    duplicate = synth.base / "duplicates" / "alpha_copy.json"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(canonical, duplicate)
    assert json.loads(duplicate.read_text(encoding="utf-8"))["component_id"] == ALPHA

    clone = synth.patch_section(
        ALPHA, "contracts", component_contract="duplicates/alpha_copy.json"
    )
    rejects(synth, clone, "is not the canonical contract")


# ---------------------------------------------------------------------------
# Test 3 / 4 — ownership and compatibility source canonicality
# ---------------------------------------------------------------------------


def test_data_ownership_source_must_match_component_contract(synth: Synthetic) -> None:
    source = f"{canonical_contract_path(BETA)}#{OWNERSHIP_POINTER}"
    clone = synth.patch_section(ALPHA, "data_ownership", source=source)
    rejects(synth, clone, "is not the canonical contract")


def test_compatibility_source_must_match_component_contract(synth: Synthetic) -> None:
    source = f"{canonical_contract_path(BETA)}#{COMPATIBILITY_POINTER}"
    clone = synth.patch_section(ALPHA, "compatibility", source=source)
    rejects(synth, clone, "is not the canonical contract")


def test_data_ownership_source_rejects_a_decoy_descriptor(synth: Synthetic) -> None:
    synth.write(
        "somewhere/alpha_contract.json", published_contract(ALPHA, ALPHA_VERSION)
    )
    clone = synth.patch_section(
        ALPHA,
        "data_ownership",
        source=f"somewhere/alpha_contract.json#{OWNERSHIP_POINTER}",
    )
    rejects(synth, clone, "is not the canonical contract")


# ---------------------------------------------------------------------------
# Test 5 — fragment semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        # missing fragment
        (
            canonical_contract_path(ALPHA),
            "must be the canonical component contract plus exactly one",
        ),
        # wrong fragment (a real pointer of the wrong section)
        (f"{canonical_contract_path(ALPHA)}#/compatibility_policy", "does not address"),
        # non-existent JSON Pointer
        (f"{canonical_contract_path(ALPHA)}#/nonexistent", "does not address"),
        # not a JSON Pointer at all
        (f"{canonical_contract_path(ALPHA)}#data_ownership", "does not address"),
        # two fragments
        (
            f"{canonical_contract_path(ALPHA)}#/data_ownership#/owner",
            "exactly one JSON Pointer fragment",
        ),
    ],
)
def test_source_fragment_is_validated(
    source: str, fragment: str, synth: Synthetic
) -> None:
    clone = synth.patch_section(ALPHA, "data_ownership", source=source)
    rejects(synth, clone, fragment)


def test_source_fragment_must_address_the_expected_section(synth: Synthetic) -> None:
    clone = synth.patch_section(
        ALPHA, "data_ownership", source=f"{canonical_contract_path(ALPHA)}#/class"
    )
    rejects(synth, clone, f"does not address {OWNERSHIP_POINTER!r}")


def test_source_fragment_must_resolve_to_an_object(synth: Synthetic) -> None:
    """A pointer that resolves to a scalar is not a valid section reference."""
    synth.write(
        canonical_contract_path(ALPHA),
        published_contract(ALPHA, ALPHA_VERSION, ownership="not-an-object"),
    )
    errors = synth.errors()
    assert any("does not address an object" in error for error in errors), errors


def test_a_missing_canonical_contract_is_reported(synth: Synthetic) -> None:
    """Canonical path, absent file: the reference is well-formed but broken."""
    (synth.base / canonical_contract_path(BETA)).unlink()
    errors = synth.errors()
    assert any("does not exist" in error for error in errors), errors


# ---------------------------------------------------------------------------
# Test 6 / 7 — dependency contract canonicality and version consistency
# ---------------------------------------------------------------------------


def test_dependency_contract_must_use_canonical_target_contract(
    synth: Synthetic,
) -> None:
    synth.write("somewhere/beta_contract.json", published_contract(BETA, BETA_VERSION))
    clone = synth.clone()
    clone["components"][0]["dependencies"][0][
        "contract"
    ] = "somewhere/beta_contract.json"
    rejects(synth, clone, "is not the canonical contract")


def test_dependency_contract_rejects_another_components_canonical_contract(
    synth: Synthetic,
) -> None:
    clone = synth.clone()
    clone["components"][0]["dependencies"][0]["contract"] = canonical_contract_path(
        ALPHA
    )
    rejects(synth, clone, "is not the canonical contract")


def test_dependency_contract_version_must_match_registered_target(
    synth: Synthetic,
) -> None:
    """The dependency contract must carry the version the registry registered."""
    synth.write(
        canonical_contract_path(BETA),
        published_contract(BETA, "0.2.9"),  # drifted from the registered 0.2.0
    )
    errors = synth.errors()
    assert any(
        "is not the registered version" in error for error in errors
    ), f"expected a dependency version-consistency error, got: {errors}"


def test_dependency_contract_version_must_match_even_when_the_range_admits_it(
    synth: Synthetic,
) -> None:
    """A range may still admit the drifted version; identity must still fail."""
    synth.write(canonical_contract_path(BETA), published_contract(BETA, "0.2.5"))
    errors = synth.errors()
    assert any("is not the registered version" in error for error in errors), errors


def test_dependency_contract_may_not_carry_a_fragment(synth: Synthetic) -> None:
    clone = synth.clone()
    clone["components"][0]["dependencies"][0][
        "contract"
    ] = f"{canonical_contract_path(BETA)}#/api"
    rejects(synth, clone, "must not carry a fragment")


# ---------------------------------------------------------------------------
# Test 8 — malformed artifact (BLOCKER 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed", ["broken", 42, [], None, {"artifact_type": "none"}]
)
def test_malformed_artifact_with_deployable_is_rejected_without_exception(
    malformed: object, synth: Synthetic
) -> None:
    """The validator must report, never raise, on a malformed artifact."""
    clone = synth.patch(ALPHA, artifact=malformed)
    clone["components"][0]["lifecycle"]["deployable"] = True

    errors = synth.errors(clone)  # must not raise AttributeError
    assert errors, f"malformed artifact {malformed!r} was accepted"
    assert any("deployable" in error for error in errors), errors


def test_malformed_artifact_is_reported_even_when_not_deployable(
    synth: Synthetic,
) -> None:
    clone = synth.patch(ALPHA, artifact="broken")
    errors = synth.errors(clone)
    assert any(".artifact:" in error for error in errors), errors


# ---------------------------------------------------------------------------
# Cases I / J / K — traversal, absolute paths and URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", REFERENCE_FIELDS)
def test_path_traversal_is_rejected(field_name: str, synth: Synthetic) -> None:
    value = f"../{canonical_contract_path(ALPHA)}"
    clone = set_reference(synth.clone(), field_name, value)
    rejects(synth, clone, "path traversal or normalization segment")


@pytest.mark.parametrize("field_name", REFERENCE_FIELDS)
def test_absolute_path_is_rejected(field_name: str, synth: Synthetic) -> None:
    value = f"/{canonical_contract_path(ALPHA)}"
    clone = set_reference(synth.clone(), field_name, value)
    rejects(synth, clone, "absolute filesystem path")


@pytest.mark.parametrize("field_name", REFERENCE_FIELDS)
def test_url_like_reference_is_rejected(field_name: str, synth: Synthetic) -> None:
    value = "https://example.com/component_contract.json"
    clone = set_reference(synth.clone(), field_name, value)
    rejects(synth, clone, "URL-like reference")


@pytest.mark.parametrize("field_name", REFERENCE_FIELDS)
def test_unnormalized_path_is_rejected(field_name: str, synth: Synthetic) -> None:
    """A second spelling of the canonical path is not the canonical path."""
    value = canonical_contract_path(ALPHA).replace("/contract/", "//contract/")
    clone = set_reference(synth.clone(), field_name, value)
    rejects(synth, clone, "not a normalized repository-relative path")


@pytest.mark.parametrize("field_name", REFERENCE_FIELDS)
def test_current_directory_segment_is_rejected(
    field_name: str, synth: Synthetic
) -> None:
    value = f"./{canonical_contract_path(ALPHA)}"
    clone = set_reference(synth.clone(), field_name, value)
    rejects(synth, clone, "not a normalized repository-relative path")


def test_file_scheme_is_rejected(synth: Synthetic) -> None:
    clone = synth.patch_section(
        ALPHA,
        "contracts",
        component_contract=f"file://{synth.base}/{canonical_contract_path(ALPHA)}",
    )
    rejects(synth, clone, "URL-like reference")


def test_openapi_must_use_the_canonical_path(synth: Synthetic) -> None:
    synth.write_text("somewhere/openapi.yaml", "openapi: 3.0.0\n")
    clone = synth.patch_section(ALPHA, "contracts", openapi="somewhere/openapi.yaml")
    rejects(synth, clone, "is not the canonical contract")


def test_openapi_canonical_path_is_accepted(synth: Synthetic) -> None:
    clone = synth.patch_section(
        ALPHA, "contracts", openapi=canonical_openapi_path(ALPHA)
    )
    assert synth.errors(clone) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_adversarial_validation_is_deterministic(synth: Synthetic) -> None:
    """The same broken document always yields exactly the same report."""
    synth.write(
        "somewhere/alpha_contract.json", published_contract(ALPHA, ALPHA_VERSION)
    )
    broken = synth.patch_section(
        ALPHA, "contracts", component_contract="somewhere/alpha_contract.json"
    )
    first = synth.errors(broken)
    second = synth.errors(copy.deepcopy(broken))
    assert first == second
    assert first == sorted(set(first))
