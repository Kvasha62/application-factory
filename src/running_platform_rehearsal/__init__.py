"""REHEARSAL reference producer and owner-side adapter. NOT PRODUCTION.

This package exercises the approved identity contract (ADR-0019, ADR-0020;
M1-b, an in-band S4 owner-side adapter, AG-2) against a small stand-in for a
Running Platform that lives entirely inside this repository. It is not a
production Running Platform. It names no real environment, owner, producer or
credential, and nothing it emits may be read as production evidence: the
production sign-off items of Issue #128 (RF-5, RF-10, D1, D2, D8) stay open
until a real owner, producer and environment exist.

The module is deliberately light: the producer runs as a separate process and
imports this package first, so nothing heavier than a constant may live here.
"""

from __future__ import annotations

REHEARSAL_LABEL = "REHEARSAL / NOT PRODUCTION"
