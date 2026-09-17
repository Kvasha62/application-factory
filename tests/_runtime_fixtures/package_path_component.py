"""Test double: a bound package whose ``__path__`` is pointed at the workspace.

The deployment binds this component and the ``pkg`` package beside it, both
from the verified execution root. The component then rewrites the bound
package's search path so that a name it never bound — a module the workspace
holds — would be found *through a package the deployment verified*. Trust does
not travel that way: a name served from the workspace is workspace content
whatever package's ``__path__`` pointed at it, and the boundary refuses it.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

# The package this deployment binds from the verified execution root. The
# component reaches for a *submodule* of it whose name is not in this source.
import pkg

from tenant_authority.deployment import build_deployment


def _submodule_name() -> str:
    """The name of content the workspace holds: never a literal in this source."""
    return "pkg." + "extra" + "_helper"


def build_component(configuration: dict[str, Any]) -> Any:
    """Point the bound package at the working directory, then reach for a name."""
    Path("entrypoint-ran.marker").write_text("A\n", encoding="utf-8")
    try:
        pkg.__path__ = [str(Path.cwd())]
        module = importlib.import_module(_submodule_name())
        module.record("entrypoint")
        outcome = f"loaded:{module.__file__}"
    except Exception as error:  # noqa: BLE001 - the double records, never masks
        outcome = f"{type(error).__name__}: {error}"
    Path("import-outcome.txt").write_text(outcome, encoding="utf-8")
    return build_deployment(configuration, with_http=True)
