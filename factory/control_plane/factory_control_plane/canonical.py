"""Canonical serialization for control-plane documents.

Scope: Requirements/Configuration documents of the Application Factory
Control Plane only. This module defines one deterministic byte form —
sorted keys, compact separators, UTF-8 — so that equivalent objects built
in any key order produce identical bytes and therefore identical digests
(ratified digest rules, Slice 0 package, owner ratification of 2026-10-03).

This canonicalization is NOT a substitute for Manifest/Instance digests,
D&O verification digests, or ADR-0018 artifact identity: those remain
separate, non-substitutable authorities.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    """Return the deterministic UTF-8 byte form of ``document``.

    Key order is normalized recursively (``sort_keys``), separators are
    compact, non-ASCII characters stay readable (``ensure_ascii=False``),
    and non-finite floats are rejected so the byte form is total.
    """
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
