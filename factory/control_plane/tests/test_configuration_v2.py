"""S2-1 proofs: ``control-plane/configuration/v2`` and the fail-closed v1 boundary.

Proves: the v1 generation stays valid and unchanged; v2 satisfies the existing
Composition Request; a v1 configuration submitted to composition is rejected
deterministically; and no compatibility/bypass path fabricates a request for a
v1 (or v1-shaped) document.
"""

from __future__ import annotations

import copy
import json

import pytest
from factory_control_plane import configuration_v2
from factory_control_plane.configuration_v2 import (
    NOT_PROJECTABLE_TEMPLATE,
    ConfigurationProjectionError,
)
from factory_control_plane.digests import verify_digest
from factory_control_plane.validation import (
    SCHEMA_CONFIGURATION_V2,
    ControlPlaneError,
    configuration_version_errors,
    configuration_version_v2_errors,
)
from slice2_fixtures import EXAMPLES, REPOSITORY_ROOT, build_v2, load

from composer import (
    compose_diagnostics,
    compose_request_document,
    render_request_document,
    validate_request_document,
)

#: The Slice 1 demonstration digest — a v1 document is immutable and its digest
#: is its identity, so this value has changed exactly once, on 2026-10-05, under
#: explicit owner authorization (PR #138 comments `5998321766` / `5998764930` /
#: `5999402241`): the ratified identity `0.4.0` cascade forced
#: `configurations/demo_shop_c1.json` to be amended in place, an exception
#: recorded in `factory/control_plane/README.md`. It is not an update path — the
#: value must not change again without a new owner decision.
SLICE1_V1_DIGEST = (
    "sha256:d9a0d3a5b2e7d0d153dc5c7a28dff036fbe9c8a78a8c2c9930f90c163b553132"
)

COMPOSER_PAYLOAD_KEYS = frozenset(
    {
        "$schema",
        "manifest",
        "components",
        "golden_bundle",
        "configuration",
        "extensions",
        "branding",
    }
)


def test_v1_example_remains_valid_and_unchanged():
    version = load("v1_version")
    assert configuration_version_errors(version) == []
    assert verify_digest(version) is True
    assert version["digest"] == SLICE1_V1_DIGEST
    assert version["schema_version"] == "control-plane/configuration/v1"


def test_v1_generation_rejects_v2_only_shapes():
    version = load("v1_version")

    with_manifest_version = copy.deepcopy(version)
    with_manifest_version["manifest"]["manifest_version"] = "1.0.0"
    errors = configuration_version_errors(with_manifest_version)
    assert any("unknown field 'manifest_version'" in item for item in errors)

    with_extension_list = copy.deepcopy(version)
    with_extension_list["extensions"] = [{"extension_id": "checkout"}]
    errors = configuration_version_errors(with_extension_list)
    assert any("extensions must be null or an object" in item for item in errors)


def test_v2_builder_settles_an_immutable_valid_version():
    version = build_v2()
    assert version["schema_version"] == SCHEMA_CONFIGURATION_V2
    assert verify_digest(version) is True
    assert configuration_version_v2_errors(version) == []
    assert version["manifest"] == {
        "manifest_id": "demo_shop_platform",
        "manifest_version": "1.0.0",
        "predecessor": None,
    }
    assert version["attachments"] == {
        "proposal": None,
        "approval": None,
        "build": None,
        "release": None,
    }
    assert version["predecessor"] is None
    assert version["configuration_id"] == "demo_shop/configurations/1"


def test_v2_successor_extends_the_configuration_chain():
    first = load("v2_version")
    second = build_v2(predecessor=first, manifest_version="1.0.1")
    assert second["configuration_id"] == "demo_shop/configurations/2"
    assert second["predecessor"] == {
        "configuration_id": first["configuration_id"],
        "digest": first["digest"],
    }
    assert verify_digest(second) is True
    assert second["digest"] != first["digest"]


def test_v2_satisfies_the_existing_composition_request():
    version = load("v2_version")
    assert configuration_v2.projection_violations(version, root=REPOSITORY_ROOT) == []
    payload = configuration_v2.generate_request_payload(version, root=REPOSITORY_ROOT)
    assert set(payload) <= COMPOSER_PAYLOAD_KEYS
    assert payload["manifest"] == {
        "manifest_id": "demo_shop_platform",
        "manifest_version": "1.0.0",
    }
    assert payload["components"] == [
        {"component_id": "identity", "component_version": "0.4.0"},
        {"component_id": "tenant_authority", "component_version": "0.1.0"},
    ]
    assert validate_request_document(payload, root=REPOSITORY_ROOT) == []
    assert compose_diagnostics(payload, root=REPOSITORY_ROOT) == []


def test_composition_from_a_v2_configuration_produces_a_draft_manifest():
    version = load("v2_version")
    payload = configuration_v2.generate_request_payload(version, root=REPOSITORY_ROOT)
    composition = compose_request_document(payload, root=REPOSITORY_ROOT)
    assert composition.lifecycle_state == "draft"
    assert composition.manifest_id == "demo_shop_platform"
    assert composition.manifest_version == "1.0.0"
    assert composition.document["approval"] is None
    assert composition.document["publication"] is None


def test_projection_is_deterministic_and_byte_stable():
    version = load("v2_version")
    first = configuration_v2.generate_request_payload(version, root=REPOSITORY_ROOT)
    second = configuration_v2.generate_request_payload(
        json.loads(EXAMPLES["v2_version"].read_text()), root=REPOSITORY_ROOT
    )
    assert first == second
    assert render_request_document(first) == render_request_document(second)


def test_v1_configuration_is_not_projectable_fail_closed():
    version = load("v1_version")
    expected = NOT_PROJECTABLE_TEMPLATE.format(schema="control-plane/configuration/v1")
    assert configuration_v2.not_projectable_errors(version) == [expected]
    assert configuration_v2.projection_violations(version, root=REPOSITORY_ROOT) == [
        expected
    ]
    with pytest.raises(ConfigurationProjectionError) as rejection:
        configuration_v2.generate_request_payload(version, root=REPOSITORY_ROOT)
    message = str(rejection.value)
    assert expected in message
    assert "v2 successor" in message


def test_v1_shaped_v2_document_is_refused_and_never_projected():
    """No bypass path: relabelling the v1 payload does not make it a request."""
    version = copy.deepcopy(load("v1_version"))
    version["schema_version"] = SCHEMA_CONFIGURATION_V2

    structural = configuration_version_v2_errors(version)
    assert any("manifest_version" in item for item in structural)
    assert any("extensions" in item for item in structural)

    violations = configuration_v2.projection_violations(version, root=REPOSITORY_ROOT)
    assert violations
    with pytest.raises(ConfigurationProjectionError):
        configuration_v2.generate_request_payload(version, root=REPOSITORY_ROOT)


def test_v2_structural_rules_hold_without_weakening_v1():
    with pytest.raises(ControlPlaneError) as unknown_component:
        build_v2(
            components=[
                {"component_id": "no_such_component", "component_version": "1.0.0"}
            ],
            configuration={},
        )
    assert "no_such_component" in str(unknown_component.value)

    with pytest.raises(ControlPlaneError) as floating:
        build_v2(
            components=[{"component_id": "identity", "component_version": "0.3.x"}],
            configuration={},
        )
    assert "selector-free" in str(floating.value)

    with pytest.raises(ControlPlaneError) as prerelease_manifest:
        build_v2(manifest_version="1.0.0-rc1")
    assert "MAJOR.MINOR.PATCH" in str(prerelease_manifest.value)

    with pytest.raises(ControlPlaneError) as non_list_extensions:
        build_v2(extensions={"checkout": {"enable": True}})
    assert "extension list" in str(non_list_extensions.value)


def test_projection_errors_are_deterministic_for_composer_rejections():
    """A payload the Composer itself refuses is reported, never repaired."""
    version = build_v2(
        configuration={
            "identity": {
                "current_platform_id": "demo_shop_platform",
                "session_ttl_seconds": 3600,
            }
        }
    )
    assert configuration_version_v2_errors(version) == []
    first = configuration_v2.projection_violations(version, root=REPOSITORY_ROOT)
    second = configuration_v2.projection_violations(version, root=REPOSITORY_ROOT)
    assert first == second
    assert any("session_ttl_seconds" in item for item in first)
    with pytest.raises(ConfigurationProjectionError):
        configuration_v2.generate_request_payload(version, root=REPOSITORY_ROOT)
