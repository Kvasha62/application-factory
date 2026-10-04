"""Slice 2 boundary proofs: what the subtree may depend on, and what it may touch.

Proves: the control plane imports only the composer's public surface and never
the lifecycle mutators, runtime or service layers; nothing under ``src/``
imports the control plane; the package holds no write or network capability;
running the whole S2 chain byte-for-byte changes no file in the repository
(so no manifest, registry or example is ever rewritten); the frozen Slice 1
files are byte-identical to ``HEAD`` and the additive Slice 1 files grew by
appending only; the new schemas and documents contain no Actual-state or secret
keys; and every shipped Slice 2 example validates against its shipped schema.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

from conftest import SCHEMA_DIR
from factory_control_plane.approvals import ApprovalLedger, new_approval_record
from factory_control_plane.composition_requests import (
    new_request_record,
    request_fingerprint,
    verify_request_record,
)
from factory_control_plane.findings import warning_ids
from factory_control_plane.validation import (
    ACTUAL_STATE_KEYS,
    SECRET_KEY_NAMES,
    SECRET_VALUE_PREFIXES,
)
from slice2_fixtures import (
    APPROVER,
    DECIDED_AT,
    EXAMPLES,
    REASON,
    REPOSITORY_ROOT,
    SUBTREE_ROOT,
    build_proposal,
    build_v2,
)
from test_validation import schema_accepts

PACKAGE_DIR = SUBTREE_ROOT / "factory_control_plane"
SLICE2_MODULES = (
    "support.py",
    "findings.py",
    "configuration_v2.py",
    "proposals.py",
    "approvals.py",
    "composition_requests.py",
)
NEW_CONTRACTS = {
    "configuration_version_v2": ("configuration_version_v2.schema.json", "v2_version"),
    "proposal_version": ("proposal_version.schema.json", "proposal"),
    "approval_record": ("approval_record.schema.json", "approval"),
    "composition_request_record": ("composition_request_record.schema.json", "request"),
}
NEW_EXAMPLES = ("v2_version", "proposal", "approval", "request")

#: Roots the control plane may never import (each exists once, elsewhere).
FORBIDDEN_IMPORT_ROOTS = (
    "deployment_operations",
    "running_platform",
    "platform_manifest",
    "platform_instance",
    "factory_artifact",
    "component_catalog",
    "golden_bundle",
)
FORBIDDEN_NAME_FRAGMENTS = ("_service", "lifecycle", "publish")

FROZEN_BYTE_IDENTICAL = (
    "factory/control_plane/factory_control_plane/canonical.py",
    "factory/control_plane/factory_control_plane/digests.py",
    "factory/control_plane/factory_control_plane/registry_reference.py",
    "factory/control_plane/tests/conftest.py",
    "factory/control_plane/tests/test_canonical_digest.py",
    "factory/control_plane/tests/test_desired_actual_separation.py",
    "factory/control_plane/tests/test_registry_reference.py",
    "factory/control_plane/tests/test_validation.py",
    "factory/control_plane/tests/test_versions_and_linkage.py",
    "factory/control_plane/projects/demo_shop.json",
    "factory/control_plane/requirements/demo_shop_r1.json",
    "factory/control_plane/configurations/demo_shop_c1.json",
    "factory/control_plane/configurations/demo_shop_index.json",
)

APPEND_ONLY = (
    "factory/control_plane/factory_control_plane/validation.py",
    "factory/control_plane/factory_control_plane/documents.py",
    "factory/control_plane/factory_control_plane/__init__.py",
    "factory/control_plane/README.md",
)


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _tree_fingerprint() -> dict[str, str]:
    skip = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
    fingerprint = {}
    for path in sorted(REPOSITORY_ROOT.rglob("*")):
        if path.is_file() and not (set(path.parts) & skip):
            fingerprint[str(path.relative_to(REPOSITORY_ROOT))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return fingerprint


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_imports_stay_inside_the_ratified_boundary():
    seen_composer_import = False
    for module in SLICE2_MODULES:
        path = PACKAGE_DIR / module
        assert path.is_file(), f"missing Slice 2 module {module}"
        for imported in _module_imports(path):
            root = imported.split(".")[0].lstrip("_")
            assert root not in FORBIDDEN_IMPORT_ROOTS, f"{module} imports {imported}"
            assert not any(
                fragment in imported for fragment in FORBIDDEN_NAME_FRAGMENTS
            ), f"{module} imports {imported}"
            if root == "composer":
                seen_composer_import = True
                # Only the documented public helpers, never a lifecycle mutator.
                assert imported.split(".")[1:2] == [] or re.fullmatch(
                    r"composer(\.(composer|request))?", imported
                ), f"{module} imports non-public composer path {imported}"
    assert (
        seen_composer_import
    ), "the composer public surface must be the only external dependency"


def test_nothing_under_src_imports_the_control_plane():
    offenders = []
    for path in sorted((REPOSITORY_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "factory_control_plane" in text:
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert offenders == []


def test_the_package_has_no_write_or_network_capability():
    for module in SLICE2_MODULES:
        path = PACKAGE_DIR / module
        text = path.read_text(encoding="utf-8")
        for capability in (
            "write_text",
            "write_bytes",
            "os.remove",
            "os.unlink",
            "shutil",
            "open(",
        ):
            assert capability not in text, f"{module} carries {capability}"
        for imported in _module_imports(path):
            assert imported.split(".")[0] not in {
                "subprocess",
                "socket",
                "urllib",
                "http",
                "requests",
            }, f"{module} imports {imported}"


def test_running_the_whole_chain_changes_no_file_in_the_repository():
    before = _tree_fingerprint()

    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    approval = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=warning_ids(proposal["findings"]),
    )
    ledger = ApprovalLedger.empty("demo_shop").append(approval)
    new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
    )

    after = _tree_fingerprint()
    assert sorted(before) == sorted(after)
    changed = [name for name in before if before[name] != after[name]]
    assert (
        changed == []
    ), f"the control plane wrote outside its value objects: {changed}"
    # Nothing was created either: every canonical store stays read-only.
    assert sorted(after) == sorted(before)


def test_slice1_files_are_frozen_and_additions_are_appends():
    for relative in FROZEN_BYTE_IDENTICAL:
        committed = _git("show", f"HEAD:{relative}")
        current = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        assert current == committed, f"{relative} changed; Slice 1 froze it"

    for relative in APPEND_ONLY:
        diff = _git("diff", "--numstat", "HEAD", "--", relative).strip()
        if not diff:
            continue  # untouched in this slice
        _added, deleted, _ = diff.split("\t", 2)
        assert deleted == "0", f"{relative} lost {deleted} lines; Slice 2 is additive"


def test_new_schemas_and_documents_carry_no_actual_state_or_secrets():
    for name, (filename, _) in NEW_CONTRACTS.items():
        text = (SCHEMA_DIR / filename).read_text(encoding="utf-8")
        schema = json.loads(text)
        keys = set(_keys(schema))
        assert not (keys & ACTUAL_STATE_KEYS), f"{name} declares Actual-state keys"
        assert not (keys & SECRET_KEY_NAMES), f"{name} declares secret keys"
        assert schema["additionalProperties"] is False

    for name in NEW_EXAMPLES:
        document = json.loads(EXAMPLES[name].read_text(encoding="utf-8"))
        for key, value in _pairs(document):
            assert key.lower() not in ACTUAL_STATE_KEYS, f"{name} carries {key!r}"
            assert key.lower() not in SECRET_KEY_NAMES, f"{name} carries {key!r}"
            if isinstance(value, str):
                assert not value.startswith(
                    SECRET_VALUE_PREFIXES
                ), f"{name} carries a secret"


def test_shipped_slice2_examples_validate_against_their_schemas():
    for filename, example in NEW_CONTRACTS.values():
        schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
        assert schema_accepts(
            schema, json.loads(EXAMPLES[example].read_text(encoding="utf-8"))
        ), f"{EXAMPLES[example].name} does not satisfy {filename}"


def test_the_slice1_v1_schema_is_untouched_and_a_v1_example_still_validates():
    committed = _git(
        "show", "HEAD:factory/control_plane/schema/configuration_version.schema.json"
    )
    current = (SCHEMA_DIR / "configuration_version.schema.json").read_text(
        encoding="utf-8"
    )
    assert current == committed

    v1_schema = json.loads(current)
    v1_example = json.loads(EXAMPLES["v1_version"].read_text(encoding="utf-8"))
    assert v1_example["schema_version"] == "control-plane/configuration/v1"
    assert schema_accepts(v1_schema, v1_example)
    assert not schema_accepts(
        v1_schema, json.loads(EXAMPLES["v2_version"].read_text(encoding="utf-8"))
    )


def test_the_subtree_holds_no_mutable_approval_state():
    """PR #138 is a channel; the only approval state is the immutable ledger."""
    text = (PACKAGE_DIR / "approvals.py").read_text(encoding="utf-8")
    assert "github" not in text.lower()
    assert not (
        REPOSITORY_ROOT / "factory" / "control_plane" / "approval_state.json"
    ).exists()
    ledger_fields = {"project_ref", "records"}
    assert ledger_fields == {
        field.split(":")[0].strip()
        for field in _dataclass_fields(PACKAGE_DIR / "approvals.py")["ApprovalLedger"]
    }


def _dataclass_fields(path: Path) -> dict[str, list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            result[node.name] = [
                item.target.id for item in node.body if isinstance(item, ast.AnnAssign)
            ]
    return result


def _keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def _pairs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _pairs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _pairs(child)


#: Digests of the four shipped Slice 2 examples. These are contract values:
#: changing one means a shipped document changed, which must be a deliberate,
#: reviewed act — never a side effect of a refactor.
EXAMPLE_DIGESTS = {
    "v2_version": "sha256:09ed2d1acd3387810c9156d3e2d38e4120f9778a5a8e989397476c268426b0ad",
    "proposal": "sha256:1ade68ba5abc532e789ff923e8f4051fb95a75b3fd36f5c640844bcb681609a0",
    "approval": "sha256:6f42740e0816369fed5a7caad15b782c6357b29ae9b7495435699a996b6be5b6",
    "request": "sha256:486fb214d4cde3a56bf963fdd38ec6d95d477bc90092281f8fa91b7c43cbef31",
}
EXAMPLE_FINGERPRINT = (
    "sha256:1f04f41453887391dafaa167b1da4a8909f053182f31f77315c70ef618a22fee"
)


def test_shipped_slice2_examples_are_reproducible_builder_outputs():
    """The examples are not illustrations: they re-derive from the builders."""
    configuration = json.loads(EXAMPLES["v2_version"].read_text(encoding="utf-8"))
    proposal = json.loads(EXAMPLES["proposal"].read_text(encoding="utf-8"))
    approval = json.loads(EXAMPLES["approval"].read_text(encoding="utf-8"))
    request = json.loads(EXAMPLES["request"].read_text(encoding="utf-8"))

    for name, document in (
        ("v2_version", configuration),
        ("proposal", proposal),
        ("approval", approval),
        ("request", request),
    ):
        assert document["digest"] == EXAMPLE_DIGESTS[name]

    assert build_v2() == configuration
    assert build_proposal(configuration=build_v2()) == proposal

    rebuilt_approval = new_approval_record(
        project_ref=approval["project_ref"],
        configuration=configuration,
        proposal=proposal,
        decision=approval["decision"],
        approver=approval["approver"],
        decided_at=approval["decided_at"],
        reason=approval["reason"],
        acknowledged_findings=approval["acknowledged_findings"],
    )
    assert rebuilt_approval == approval

    ledger = ApprovalLedger.load("demo_shop", [approval])
    rebuilt_request = new_request_record(
        project_ref=request["project_ref"],
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
    )
    assert rebuilt_request == request
    assert request_fingerprint(request) == EXAMPLE_FINGERPRINT
    assert (
        verify_request_record(
            request,
            configuration=configuration,
            proposal=proposal,
            approval=approval,
            ledger=ledger,
            root=REPOSITORY_ROOT,
        )
        == []
    )
