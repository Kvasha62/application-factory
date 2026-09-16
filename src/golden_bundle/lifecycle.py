"""Golden Bundle lifecycle model (Slice D).

Lifecycle states per ARCHITECTURE.md §14:

    draft → candidate → certified → deprecated → revoked

Semantics:

* ``certified`` means the bundle passed the approved set of compatibility and
  security checks and carries certification evidence;
* a ``draft``, ``candidate``, ``deprecated`` or ``revoked`` bundle never
  implies approval or deployability (ADR-0015 §15 A/B/C doctrine: a state
  asserts only what it names);
* ``revoked`` forbids new deliveries but never touches running instances —
  this slice records the state and performs no delivery behavior whatsoever.

This module defines the vocabulary and allowed transitions. It is pure
metadata: it never touches a component database or internal modules.
"""

from __future__ import annotations

from typing import Final

BUNDLE_STATES: Final[frozenset[str]] = frozenset(
    {
        "draft",
        "candidate",
        "certified",
        "deprecated",
        "revoked",
    }
)

# Linear order per ARCHITECTURE.md §14.
LIFECYCLE_ORDER: Final[tuple[str, ...]] = (
    "draft",
    "candidate",
    "certified",
    "deprecated",
    "revoked",
)

# Index for quick order checks.
_STATE_INDEX: Final[dict[str, int]] = {
    state: index for index, state in enumerate(LIFECYCLE_ORDER)
}

# Strict immediate-next transitions (the arrows in §14).
_ALLOWED_NEXT: Final[dict[str, frozenset[str]]] = {
    "draft": frozenset({"candidate"}),
    "candidate": frozenset({"certified"}),
    "certified": frozenset({"deprecated"}),
    "deprecated": frozenset({"revoked"}),
    "revoked": frozenset(),
}


def is_valid_state(state: object) -> bool:
    """True when ``state`` is a declared bundle lifecycle state."""
    return isinstance(state, str) and state in BUNDLE_STATES


def state_index(state: str) -> int:
    """Return the order index of ``state`` (0 = draft, 4 = revoked)."""
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
    """True when ``previous → new`` is allowed per Slice D rules.

    Slice D enforces strict linear progression: only the immediate next state
    is allowed, plus staying in the same state (idempotent update). Skipping
    states is rejected so that ``created`` is never implicitly ``certified``
    and ``certified`` is never implicitly ``deprecated``.
    """
    if previous == new:
        return True
    return is_allowed_immediate_transition(previous, new)


def requires_certification(state: str) -> bool:
    """True when ``state`` requires explicit certification evidence.

    Per ARCHITECTURE.md §14, ``certified`` means the bundle passed the approved
    check set: such a bundle must carry evidence. Pre-certification states must
    not carry evidence, because evidence exists only on certification.
    """
    if state not in _STATE_INDEX:
        return False
    return _STATE_INDEX[state] >= _STATE_INDEX["certified"]
