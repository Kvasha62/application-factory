"""Shared path bootstrap for the control-plane subtree tests.

The Quality Gate runs ``pytest tests/`` (``testpaths`` in
pyproject.toml), which does not collect this subtree; these tests are run
explicitly (``python -m pytest factory/control_plane/tests``) as reported
in the Slice 1 implementation report. Adding this path to the gate's
``testpaths`` would require editing ``pyproject.toml`` — outside the
authorized ``factory/control_plane/**`` boundary — and is left for a
separate owner authorization.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SUBTREE_ROOT = Path(__file__).resolve().parent.parent

if str(_SUBTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SUBTREE_ROOT))

REPOSITORY_ROOT = _SUBTREE_ROOT.parent.parent
SCHEMA_DIR = _SUBTREE_ROOT / "schema"
EXAMPLES = {
    "project": _SUBTREE_ROOT / "projects" / "demo_shop.json",
    "requirements": _SUBTREE_ROOT / "requirements" / "demo_shop_r1.json",
    "index": _SUBTREE_ROOT / "configurations" / "demo_shop_index.json",
    "version": _SUBTREE_ROOT / "configurations" / "demo_shop_c1.json",
}
