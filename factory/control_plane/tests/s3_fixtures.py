"""Shared builders for the Slice 3 (S3) tests.

Mirrors the style of ``slice2_fixtures``: every helper builds documents through
the real builders, so a test never asserts against a hand-made document the
builders would refuse.
"""

from __future__ import annotations

from typing import Any

from factory_control_plane import requirements_intake
from factory_control_plane.findings import warning_ids
from factory_control_plane.proposals import new_proposal_version
from slice2_fixtures import (
    APPROVER,
    DECIDED_AT,
    REASON,
    REPOSITORY_ROOT,
    build_v2,
    load,
)

PROJECT_REF = "demo_shop"


def base_candidates() -> list[dict[str, Any]]:
    return [
        {
            "kind": "capability",
            "statement": {
                "summary": "Shoppers can search and check out with a basket."
            },
        }
    ]


def build_context(
    *,
    channel: str = "form",
    candidates: list[dict[str, Any]] | None = None,
    configuration_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a valid S3 context: requirements (from intake) + v2 config + project."""
    candidates = candidates if candidates is not None else base_candidates()
    requirements = requirements_intake.intake_requirements_version(
        project_ref=PROJECT_REF, channel=channel, candidates=candidates
    )
    overrides: dict[str, Any] = {"requirements_ref": requirements}
    if configuration_overrides:
        overrides.update(configuration_overrides)
    configuration = build_v2(**overrides)
    project = load("project")
    return {
        "project_ref": PROJECT_REF,
        "channel": channel,
        "candidates": candidates,
        "requirements": requirements,
        "configuration": configuration,
        "project": project,
        "approver": dict(APPROVER),
        "decided_at": DECIDED_AT,
        "reason": REASON,
        "root": REPOSITORY_ROOT,
    }


def warning_ids_for(context: dict[str, Any]) -> list[str]:
    """Derive the proposal the S3 flow would build and return its warning ids."""
    proposal = new_proposal_version(
        project_ref=context["project_ref"],
        configuration=context["configuration"],
        requirements=context["requirements"],
        project=context["project"],
        root=context["root"],
    )
    return warning_ids(proposal["findings"])
