"""Test double: component-owned helper content, bound and verified as A.

The deployment binds this module from the verified execution root. Which
content actually ran is proven by the marker it leaves in the process working
directory: ``honest-<where>.marker`` is written by the verified content, and
``substituted-<where>.marker`` by the double that imitates it.
"""

from __future__ import annotations

from pathlib import Path


def record(where: str) -> None:
    """Prove this content ran, and where it was reached from."""
    Path(f"honest-{where}.marker").write_text("verified content\n", encoding="utf-8")
