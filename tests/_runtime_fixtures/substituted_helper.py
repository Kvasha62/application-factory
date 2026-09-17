"""Test double: content that imitates component-owned helper content.

This content is never part of a verified execution closure: it is planted in
the runtime workspace, in another root, or behind a link. It leaves
``substituted-<where>.marker`` where it runs, so a test can tell it apart from
the verified content the deployment bound — and prove that it never ran.
"""

from __future__ import annotations

from pathlib import Path


def record(where: str) -> None:
    """Prove substituted content ran, if it ever does."""
    Path(f"substituted-{where}.marker").write_text(
        "substituted content\n", encoding="utf-8"
    )
