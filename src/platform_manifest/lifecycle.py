"""Lifecycle model for Platform Manifest (Slice C).

Lifecycle states per ARCHITECTURE.md §16:

    draft → validated → approved → published → deployed → superseded → retired

Published is immutable and contains manifest_id + version + digest + predecessor.
Approved contains approval info. Published cannot be silently rewritten under
the same identity/version.

This module defines the vocabulary and allowed transitions. It is pure
metadata: it never touches a component database or internal modules.
"""

from __future__ import annotations

from typing import Final

LIFECYCLE_STATES: Final[frozenset[str]] = frozenset(
    {
        "draft",
        "validated",
        "approved",
        "published",
        "deployed",
        "superseded",
        "retired",
    }
)

# Linear order per ARCHITECTURE.md §16.
LIFECYCLE_ORDER: Final[tuple[str, ...]] = (
    "draft",
    "validated",
    "approved",
    "published",
    "deployed",
    "superseded",
    "retired",
)

# Index for quick order checks.
_STATE_INDEX: Final[dict[str, int]] = {
    state: index for index, state in enumerate(LIFECYCLE_ORDER)
}

# Strict immediate-next transitions (the arrows in §16).
_ALLOWED_NEXT: Final[dict[str, frozenset[str]]] = {
    "draft": frozenset({"validated"}),
    "validated": frozenset({"approved"}),
    "approved": frozenset({"published"}),
    "published": frozenset({"deployed"}),
    "deployed": frozenset({"superseded"}),
    "superseded": frozenset({"retired"}),
    "retired": frozenset(),
}

# For predecessor version chain: what states a predecessor may be in when
# a new manifest is published or later. A predecessor should be at least
# published before it is superseded by a new version.
_PUBLISHED_OR_LATER: Final[frozenset[str]] = frozenset(
    {"published", "deployed", "superseded", "retired"}
)


def is_valid_state(state: object) -> bool:
    """True when ``state`` is a declared lifecycle state."""
    return isinstance(state, str) and state in LIFECYCLE_STATES


def state_index(state: str) -> int:
    """Return the order index of ``state`` (0 = draft, 6 = retired)."""
    return _STATE_INDEX[state]


def is_forward_transition(previous: str, new: str) -> bool:
    """True when ``new`` is strictly forward from ``previous`` in lifecycle order."""
    if previous not in _STATE_INDEX or new not in _STATE_INDEX:
        return False
    return _STATE_INDEX[new] > _STATE_INDEX[previous]


def is_allowed_immediate_transition(previous: str, new: str) -> bool:
    """True when ``new`` is the immediate allowed next state after ``previous``."""
    if previous not in _ALLOWED_NEXT:
        return False
    return new in _ALLOWED_NEXT[previous]


def is_allowed_transition(previous: str, new: str) -> bool:
    """True when ``previous → new`` is allowed per Slice C rules.

    Slice C enforces strict linear progression: only the immediate next state
    is allowed, plus staying in the same state (idempotent update). Skipping
    states is rejected to preserve the explicit approval/publication gates.

    For version-chain validation (predecessor), a separate check
    ``is_valid_predecessor_state`` is used.
    """
    if previous == new:
        return True
    return is_allowed_immediate_transition(previous, new)


def is_valid_predecessor_state(predecessor_state: str) -> bool:
    """True when a predecessor may be superseded by a new manifest.

    A predecessor should have been at least published before a new version
    claims to supersede it.
    """
    return predecessor_state in _PUBLISHED_OR_LATER


def requires_approval(state: str) -> bool:
    """True when ``state`` requires approval metadata."""
    if state not in _STATE_INDEX:
        return False
    return _STATE_INDEX[state] >= _STATE_INDEX["approved"]


def requires_publication(state: str) -> bool:
    """True when ``state`` requires publication metadata."""
    if state not in _STATE_INDEX:
        return False
    return _STATE_INDEX[state] >= _STATE_INDEX["published"]


def is_published_or_later(state: str) -> bool:
    """True when ``state`` is published, deployed, superseded or retired."""
    return state in _PUBLISHED_OR_LATER
