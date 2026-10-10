"""Rehearsal reference producer: a stand-in Running Platform observer.

REHEARSAL / NOT PRODUCTION. The producer is a separate process that reads only
its own runtime root and answers one request per process::

    {"op": "observe", "handle": "<opaque>"}

It receives no binding token, platform id, digest, environment, attempt or any
other expected deployment state, and it refuses a request that carries
anything beyond ``op`` and ``handle``. It imports nothing but the standard
library, computes no digest and enumerates only ``<root>/components`` (ADR-0020
§6, §22, §23).

Runtime root layout, written by whatever realized the platform and never by
this module::

    <root>/platform.json
    <root>/components/<name>/component.json

Every optional identity-bearing fact carries an explicit state. A fact that is
missing or malformed is never defaulted: the observation is refused, because
silence is not absence (ADR-0020 §16, §21).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from running_platform_rehearsal import REHEARSAL_LABEL

PROTOCOL = "rehearsal-producer/v1"
PROVENANCE = "MEASURED"
SURFACES = (
    "platform_id",
    "membership",
    "component_identity",
    "artifact_identity",
    "manifest",
    "configuration",
    "golden_bundle",
    "branding",
    "extensions",
)
_REQUEST_FIELDS = frozenset({"op", "handle"})


class ObservationRefused(Exception):
    """A required fact cannot be established, so nothing valid is published."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        message = f"{path.name} is unavailable"
        raise ObservationRefused(message) from error
    if not isinstance(document, dict):
        message = f"{path.name} is not an object"
        raise ObservationRefused(message)
    return document


def _text(document: dict[str, Any], key: str, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        message = f"{where}: {key} is unavailable"
        raise ObservationRefused(message)
    return value


def _stated(document: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    """Return an explicitly stated PRESENT or ABSENT fact, never a guess."""
    entry = document.get(key)
    if not isinstance(entry, dict):
        message = f"{where}: {key} has no explicit state"
        raise ObservationRefused(message)
    if entry.get("state") == "PRESENT" and entry.get("value") is not None:
        return {"state": "PRESENT", "value": entry["value"]}
    if entry.get("state") == "ABSENT" and "value" not in entry:
        return {"state": "ABSENT"}
    message = f"{where}: {key} is neither PRESENT with a value nor ABSENT"
    raise ObservationRefused(message)


def _members(root: Path) -> tuple[list[dict[str, Any]], bool]:
    """Enumerate by scope, not by an expected list, and report every member.

    A member that cannot be identified makes the membership incomplete; it is
    never skipped silently. Multiplicity is preserved: two members with the
    same id stay two entries.
    """
    scope = root / "components"
    if not scope.is_dir():
        message = "the component scope is unavailable"
        raise ObservationRefused(message)
    members: list[dict[str, Any]] = []
    complete = True
    for entry in sorted(scope.iterdir(), key=lambda item: item.name):
        if entry.is_symlink() or not entry.is_dir():
            complete = False
            continue
        try:
            document = _read_object(entry / "component.json")
            where = f"components/{entry.name}"
            members.append(
                {
                    "component_id": _text(document, "component_id", where),
                    "component_version": _text(document, "component_version", where),
                    "artifact": _stated(document, "artifact", where),
                }
            )
        except ObservationRefused:
            complete = False
    return members, complete


def _surface(basis: str, **fact: Any) -> dict[str, Any]:
    return {"provenance": PROVENANCE, "basis": basis, **fact}


def observe(root: Path, handle: str) -> dict[str, Any]:
    """Observe the nine identity surfaces of the platform under ``root``."""
    platform = _read_object(root / "platform.json")
    manifest = platform.get("manifest")
    if not isinstance(manifest, dict):
        message = "platform.json: manifest is unavailable"
        raise ObservationRefused(message)
    golden = _stated(platform, "golden_bundle", "platform.json")
    inventory = platform["golden_bundle"].get("inventory_complete") is True
    members, complete = _members(root)
    read = "read from platform.json in the producer-owned runtime root"
    scan = "scope scan of the producer-owned runtime root"
    return {
        "protocol": PROTOCOL,
        "label": REHEARSAL_LABEL,
        "rehearsal": True,
        "handle": handle,
        "status": "ok",
        "surfaces": {
            "platform_id": _surface(
                read, value=_text(platform, "platform_id", "platform.json")
            ),
            "membership": _surface(
                scan,
                value={
                    "complete": complete,
                    "components": [member["component_id"] for member in members],
                },
            ),
            "component_identity": _surface(
                "read from each component-owned component.json",
                value=[
                    {
                        "component_id": member["component_id"],
                        "component_version": member["component_version"],
                    }
                    for member in members
                ],
            ),
            "artifact_identity": _surface(
                "explicit artifact state in each component-owned component.json",
                value=[member["artifact"] for member in members],
            ),
            "manifest": _surface(
                read,
                value={
                    "manifest_id": _text(manifest, "manifest_id", "manifest"),
                    "manifest_version": _text(manifest, "manifest_version", "manifest"),
                    "manifest_digest": _text(manifest, "manifest_digest", "manifest"),
                    "manifest_state": _text(
                        platform, "manifest_state", "platform.json"
                    ),
                },
            ),
            "configuration": _surface(
                read, **_stated(platform, "configuration", "platform.json")
            ),
            "golden_bundle": _surface(read, inventory_complete=inventory, **golden),
            "branding": _surface(
                read, **_stated(platform, "branding", "platform.json")
            ),
            "extensions": _surface(
                read, **_stated(platform, "extensions", "platform.json")
            ),
        },
    }


def _refusal(handle: object, reason: str) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "label": REHEARSAL_LABEL,
        "rehearsal": True,
        "handle": handle if isinstance(handle, str) else None,
        "status": "refused",
        "reason": reason,
    }


def answer(root: Path, line: str) -> dict[str, Any]:
    """Answer one request line. Any unexpected field refuses the request."""
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        return _refusal(None, "the request is not JSON")
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        return _refusal(None, "the request must carry exactly op and handle")
    handle = request["handle"]
    if request["op"] != "observe" or not isinstance(handle, str) or not handle:
        return _refusal(handle, "the request is not a valid observe request")
    try:
        return observe(root, handle)
    except ObservationRefused as error:
        return _refusal(handle, str(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m running_platform_rehearsal.producer"
    )
    parser.add_argument("--root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    sys.stdout.write(json.dumps(answer(arguments.root, sys.stdin.readline())) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
