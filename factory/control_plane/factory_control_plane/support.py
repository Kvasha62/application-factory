"""Shared helpers for the Slice 2 builders (digest settlement, sequences).

Slice 1 settled every document inside ``documents._settled``. Slice 2 adds
new immutable document families (configuration/v2, proposal, approval and
composition request) whose builders live in their own modules, so the same
fail-closed settlement — digest first, validate the settled form, refuse on
any error — is exposed once here instead of being re-implemented per module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from factory_control_plane.digests import with_digest
from factory_control_plane.validation import ControlPlaneError

Validator = Callable[[object], list[str]]


def settle(
    document: Mapping[str, Any], validator: Validator, *, subject: str
) -> dict[str, Any]:
    """Digest ``document``, then validate the settled form (fail closed)."""
    settled = with_digest(document)
    errors = validator(settled)
    if errors:
        joined = "; ".join(errors)
        message = f"invalid {subject}: {joined}"
        raise ControlPlaneError(message)
    return settled


def successor_sequence(predecessor_id: str | None) -> int:
    """Return the next sequence number for an append-only version chain.

    The sequence is derived from the predecessor id (``<project>/<kind>/<N>``)
    exactly as Slice 1 derived it for Requirements/Configuration versions, so
    every Slice 2 chain behaves the same way.
    """
    if predecessor_id is None:
        return 1
    tail = predecessor_id.rsplit("/", 1)[-1]
    if not tail.isdigit():
        message = f"predecessor id {predecessor_id!r} has no numeric sequence"
        raise ControlPlaneError(message)
    return int(tail) + 1
