"""Shared builders for the Slice 2 test files (S2-1 / S2-2 / S2-3).

Deliberately not in ``conftest.py``: the Slice 1 conftest is frozen, and these
helpers only describe the Slice 2 documents. Every helper builds documents
through the real builders, so a test can never assert against a hand-made
document that the builders would refuse.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from factory_control_plane import configuration_v2, proposals

SUBTREE_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = SUBTREE_ROOT.parent.parent

EXAMPLES = {
    "project": SUBTREE_ROOT / "projects" / "demo_shop.json",
    "requirements": SUBTREE_ROOT / "requirements" / "demo_shop_r1.json",
    "v1_index": SUBTREE_ROOT / "configurations" / "demo_shop_index.json",
    "v1_version": SUBTREE_ROOT / "configurations" / "demo_shop_c1.json",
    "v2_version": SUBTREE_ROOT / "configurations" / "demo_shop_c2.json",
    "proposal": SUBTREE_ROOT / "proposals" / "demo_shop_p1.json",
    "approval": SUBTREE_ROOT / "approvals" / "demo_shop_a1.json",
    "request": SUBTREE_ROOT / "requests" / "demo_shop_req1.json",
}

#: Stable approver reference used by every Slice 2 example and test.
APPROVER = {"kind": "owner", "reference": "Kvasha62", "authority_ref": None}
DECIDED_AT = "2026-10-04T04:00:00Z"
REASON = "Accept the resolved configuration."


def load(name: str) -> dict[str, Any]:
    """Load one example document."""
    return json.loads(EXAMPLES[name].read_text(encoding="utf-8"))


def build_v2(**overrides: Any) -> dict[str, Any]:
    """Build a valid demonstration ``configuration/v2`` version."""
    arguments: dict[str, Any] = {
        "project_ref": "demo_shop",
        "requirements_ref": load("requirements"),
        "manifest_id": "demo_shop_platform",
        "manifest_version": "1.0.0",
        "components": [
            {"component_id": "identity", "component_version": "0.3.1"},
            {"component_id": "tenant_authority", "component_version": "0.1.0"},
        ],
        "configuration": {
            "identity": {"current_platform_id": "demo_shop_platform"},
            "tenant_authority": {"platform_id": "demo_shop_platform"},
        },
    }
    arguments.update(overrides)
    return configuration_v2.new_configuration_version_v2(**arguments)


def build_proposal(**overrides: Any) -> dict[str, Any]:
    """Build the immutable validation report for a demonstration configuration."""
    arguments: dict[str, Any] = {
        "project_ref": "demo_shop",
        "configuration": build_v2(),
        "requirements": load("requirements"),
        "project": load("project"),
        "root": REPOSITORY_ROOT,
    }
    arguments.update(overrides)
    return proposals.new_proposal_version(**arguments)
