"""Builders for immutable control-plane documents (Slice 1).

Every builder validates its output and settles the digest before
returning. There is no update function for RequirementsVersion or
ConfigurationVersion: any technical change produces a NEW version with a
new digest and a predecessor link. Mutable state lives only in the
Project record and the Configuration aggregate's ``current`` pointer,
which builders copy rather than mutate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from factory_control_plane.digests import with_digest
from factory_control_plane.validation import (
    ControlPlaneError,
    configuration_index_errors,
    configuration_version_errors,
    project_errors,
    requirements_errors,
)

SCHEMA_PROJECT = "control-plane/project/v1"
SCHEMA_REQUIREMENTS = "control-plane/requirements/v1"
SCHEMA_CONFIGURATION = "control-plane/configuration/v1"
SCHEMA_CONFIGURATION_INDEX = "control-plane/configuration-index/v1"

ATTACHMENT_KEYS = ("proposal", "approval", "build", "release")


def _settled(document: Mapping[str, Any], validator) -> dict[str, Any]:
    """Digest the document, then validate the settled form (fail closed)."""
    settled = with_digest(document)
    errors = validator(settled)
    if errors:
        joined = "; ".join(errors)
        message = f"invalid control-plane document: {joined}"
        raise ControlPlaneError(message)
    return settled


def _attachments() -> dict[str, None]:
    """Attachment slots for future Proposal/Approval/Build/Release records."""
    return dict.fromkeys(ATTACHMENT_KEYS)


def _successor_sequence(predecessor_id: str | None) -> int:
    if predecessor_id is None:
        return 1
    tail = predecessor_id.rsplit("/", 1)[-1]
    if not tail.isdigit():
        message = f"predecessor id {predecessor_id!r} has no numeric sequence"
        raise ControlPlaneError(message)
    return int(tail) + 1


def new_project(
    *, project_id: str, title: str, created_at: str, status: str = "active"
) -> dict[str, Any]:
    """Create a Project container (mutable aggregate; identity by id)."""
    document: dict[str, Any] = {
        "schema_version": SCHEMA_PROJECT,
        "project_id": project_id,
        "title": title,
        "status": status,
        "created_at": created_at,
        "pointers": {"requirements": None, "configuration": None},
    }
    errors = project_errors(document)
    if errors:
        joined = "; ".join(errors)
        message = f"invalid project: {joined}"
        raise ControlPlaneError(message)
    return document


def point_project(
    project: Mapping[str, Any],
    *,
    requirements: Mapping[str, Any] | None = None,
    configuration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a NEW Project with updated pointers (input never mutated)."""
    updated = {
        "schema_version": project["schema_version"],
        "project_id": project["project_id"],
        "title": project["title"],
        "status": project["status"],
        "created_at": project["created_at"],
        "pointers": dict(project["pointers"]),
    }
    if requirements is not None:
        updated["pointers"]["requirements"] = {
            "requirements_id": requirements["requirements_id"],
            "digest": requirements["digest"],
        }
    if configuration is not None:
        updated["pointers"]["configuration"] = {
            "configuration_id": configuration["configuration_id"],
            "digest": configuration["digest"],
        }
    errors = project_errors(updated)
    if errors:
        joined = "; ".join(errors)
        message = f"invalid project pointers: {joined}"
        raise ControlPlaneError(message)
    return updated


def new_requirements_version(
    *,
    project_ref: str,
    entries: Sequence[Mapping[str, Any]],
    predecessor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the next immutable RequirementsVersion for a project."""
    previous_id = None
    previous_digest = None
    if predecessor is not None:
        previous_id = predecessor["requirements_id"]
        previous_digest = predecessor["digest"]
    sequence = _successor_sequence(previous_id)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_REQUIREMENTS,
        "requirements_id": f"{project_ref}/requirements/{sequence}",
        "project_ref": project_ref,
        "entries": [dict(entry) for entry in entries],
        "predecessor": (
            None
            if previous_id is None
            else {"requirements_id": previous_id, "digest": previous_digest}
        ),
    }
    return _settled(document, requirements_errors)


def new_configuration_version(
    *,
    project_ref: str,
    requirements_ref: Mapping[str, Any],
    manifest_id: str,
    components: Sequence[Mapping[str, Any]],
    configuration: Mapping[str, Any],
    predecessor: Mapping[str, Any] | None = None,
    template_ref: str | None = None,
    variant_ref: str | None = None,
    golden_bundle: Mapping[str, Any] | None = None,
    extensions: Mapping[str, Any] | None = None,
    branding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the next immutable, selector-free ConfigurationVersion."""
    previous_id = None
    previous_digest = None
    if predecessor is not None:
        previous_id = predecessor["configuration_id"]
        previous_digest = predecessor["digest"]
    sequence = _successor_sequence(previous_id)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_CONFIGURATION,
        "configuration_id": f"{project_ref}/configurations/{sequence}",
        "project_ref": project_ref,
        "requirements_ref": {
            "requirements_id": requirements_ref["requirements_id"],
            "digest": requirements_ref["digest"],
        },
        "template_ref": template_ref,
        "variant_ref": variant_ref,
        "manifest": {"manifest_id": manifest_id},
        "components": [dict(component) for component in components],
        "configuration": dict(configuration),
        "golden_bundle": None if golden_bundle is None else dict(golden_bundle),
        "extensions": None if extensions is None else dict(extensions),
        "branding": None if branding is None else dict(branding),
        "attachments": _attachments(),
        "predecessor": (
            None
            if previous_id is None
            else {"configuration_id": previous_id, "digest": previous_digest}
        ),
    }
    return _settled(document, configuration_version_errors)


def new_configuration_index(
    *, project_ref: str, version: Mapping[str, Any]
) -> dict[str, Any]:
    """Create the mutable Configuration aggregate pointing at ``version``."""
    document: dict[str, Any] = {
        "schema_version": SCHEMA_CONFIGURATION_INDEX,
        "configuration_id": f"{project_ref}/configurations",
        "project_ref": project_ref,
        "current": {
            "configuration_id": version["configuration_id"],
            "digest": version["digest"],
        },
    }
    errors = configuration_index_errors(document)
    if errors:
        joined = "; ".join(errors)
        message = f"invalid configuration index: {joined}"
        raise ControlPlaneError(message)
    return document


def set_current(index: Mapping[str, Any], version: Mapping[str, Any]) -> dict[str, Any]:
    """Return a NEW index whose ``current`` points at ``version``.

    The input index and the version document are never mutated: moving
    the pointer is not a technical change to the immutable version.
    """
    document: dict[str, Any] = {
        "schema_version": index["schema_version"],
        "configuration_id": index["configuration_id"],
        "project_ref": index["project_ref"],
        "current": {
            "configuration_id": version["configuration_id"],
            "digest": version["digest"],
        },
    }
    errors = configuration_index_errors(document)
    if errors:
        joined = "; ".join(errors)
        message = f"invalid configuration index: {joined}"
        raise ControlPlaneError(message)
    if document["project_ref"] != version["project_ref"]:
        message = "version belongs to a different project"
        raise ControlPlaneError(message)
    return document
