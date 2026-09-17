"""Test double: content that tries every route around the execution boundary.

The deployment binds this module — and, where a route needs a bound name, the
``helper`` module beside it — from the verified execution root. It then attempts
the routes a component could take to execute content the deployment never
verified: a workspace module of the same name, another finder ahead of the
boundary on ``sys.meta_path``, a hostile ``sys.path_hooks`` entry, content
injected into ``sys.modules``, compiled code labelled as component-owned
content, executable content read out of the workspace, a native library in the
workspace, a directory link, and child processes.

The route is chosen by the ``BOUNDARY_ROUTE`` operational input (an environment
secret the deployment environment injects at the boundary). Every attempt writes
what happened to ``import-outcome.txt`` in the process working directory, so a
test can prove which content ran rather than that it merely could have.

Deliberately, no route changes the identity, health or readiness this component
reports: what a runtime says about itself is never the evidence a deployment may
stand on (ADR-0016 §9, §10; ADR-0017 §43.10). The component's own deployment is
always the pinned component's, so ``/health`` and ``/ready`` answer exactly what
the accepted instance expects.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

from tenant_authority.deployment import build_deployment

#: Where a route records what happened, in the process working directory.
OUTCOME = "import-outcome.txt"


def _record(text: str) -> None:
    Path(OUTCOME).write_text(text, encoding="utf-8")


def _mark(name: str) -> None:
    Path(f"{name}.marker").write_text("ran\n", encoding="utf-8")


def _workspace() -> Path:
    """The process working directory: deployment-controlled, never trusted."""
    return Path.cwd()


def _bound_helper() -> Any:
    """A name this deployment bound, looked up by its literal module name."""
    return importlib.import_module("helper")


def _unbound_name() -> str:
    """The name of content that lives only outside the verified roots.

    Never a literal module name in this module's source: the engine's closure
    cannot bind what it cannot see, and the boundary decides at resolution time.
    """
    return "extra" + "_helper"


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------


def _route_bound() -> str:
    """Resolve a bound name and use it: the boundary serves the verified file."""
    helper = _bound_helper()
    helper.record("entrypoint")
    return f"loaded:{helper.__file__}"


def _route_meta_path() -> str:
    """Another finder, ahead of the boundary, claiming a bound name."""
    workspace = _workspace()
    bound = _bound_helper()
    del sys.modules["helper"]

    class HostileFinder:
        """Serves the workspace's copy of a name the deployment already bound."""

        def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
            if fullname == "helper":
                return importlib.util.spec_from_file_location(
                    "helper", workspace / "helper.py"
                )
            return None

    sys.meta_path.insert(0, HostileFinder())
    helper = importlib.import_module("helper")
    helper.record("entrypoint")
    return f"loaded:{helper.__file__} (bound was {bound.__file__})"


def _route_path_hooks() -> str:
    """The boundary's finder removed and a hostile path hook installed instead."""
    workspace = _workspace().resolve()
    sys.meta_path = [
        finder for finder in sys.meta_path if type(finder).__name__ != "_VebFinder"
    ]

    class WorkspaceHook:
        """Serves the workspace directory, and declines every other one.

        A path hook is asked about every entry on the search path. Claiming
        directories it does not own would break the interpreter's own
        resolution — the sabotage is pointed at the boundary, not at the
        runtime the component is standing on.
        """

        def __init__(self, path: str) -> None:
            candidate = Path(path)
            if candidate.resolve() != workspace:
                raise ImportError("not the workspace")
            self.path = candidate

        def find_spec(self, fullname: str, target: Any = None) -> Any:
            candidate = self.path / f"{fullname}.py"
            if candidate.is_file():
                return importlib.util.spec_from_file_location(fullname, candidate)
            return None

    sys.path_hooks.insert(0, WorkspaceHook)
    sys.path.insert(0, str(workspace))
    importlib.invalidate_caches()
    module = importlib.import_module(_unbound_name())
    module.record("entrypoint")
    return f"loaded:{module.__file__}"


def _route_sys_modules() -> str:
    """Content loaded by hand and preloaded into ``sys.modules`` under a bound name."""
    workspace = _workspace()
    spec = importlib.util.spec_from_file_location("helper", workspace / "helper.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return "no spec"
    module = importlib.util.module_from_spec(spec)
    sys.modules["helper"] = module
    spec.loader.exec_module(module)
    module.record("entrypoint")
    return f"loaded:{module.__file__}"


def _route_generated() -> str:
    """Compiled content labelled as component-owned content."""
    label = str(_workspace() / "helper.py")
    namespace: dict[str, Any] = {}
    exec(compile("MARKER = 1\n", label, "exec"), namespace)  # noqa: S102 - the double
    return f"executed generated code labelled {label}"


def _route_generated_read() -> str:
    """Executable content read out of the workspace to be run as generated code."""
    source = (_workspace() / "helper.py").read_text(encoding="utf-8")
    exec(source, {})  # noqa: S102 - the double
    return "read and executed workspace content"


def _route_generated_memory() -> str:
    """Generated code with no file provenance at all."""
    exec(  # noqa: S102 - the double
        compile(
            "from pathlib import Path\nPath('generated-in-memory.marker').write_text('ran')\n",
            "<string>",
            "exec",
        ),
        {},
    )
    return "executed generated code from a string"


def _route_reload() -> str:
    """A bound module reloaded after its file was replaced with other content.

    The bound content is loaded first — that is the content the deployment
    verified. The file is then replaced (a hard link to the workspace's copy,
    so nothing reads the workspace through Python), and the module is reloaded.
    Loading a bound name twice is loading the same content twice, or nothing.
    """
    helper = _bound_helper()
    helper.record("first")
    target = Path(helper.__file__)
    replacement = target.with_name("helper.swap")
    try:
        os.link(_workspace() / "helper.py", replacement)
        os.replace(replacement, target)
    except OSError as error:
        return f"swap failed: {error}"
    importlib.invalidate_caches()
    importlib.reload(helper)
    helper.record("second")
    return "reloaded"


def _route_write_bound() -> str:
    """Verified content rewritten from the process itself.

    The deployment bound this file and the boundary was established on those
    bytes; a process that rewrites them would make what executes differ from what
    was verified. The write is refused where it is attempted, and the refusal is
    evidence the deployment fails on.
    """
    helper = _bound_helper()
    helper.record("entrypoint")
    target = Path(helper.__file__)
    target.write_bytes(b"# replaced by the process\n")
    return "rewrote-bound-content"


def _route_marshalled_bound() -> str:
    """A code object built outside the boundary, executed under a bound name.

    The program is compiled under a label of its own, *renamed* to the bound file
    and round-tripped through ``marshal`` — no compile names verified content, so
    no compile is refused. The code object is still not one the boundary compiled
    from the bytes it verified, so it never runs under the verified name.
    """
    import marshal

    helper = _bound_helper()
    helper.record("entrypoint")
    target = Path(helper.__file__)
    code = compile(b"record = lambda where: None\n", "<route>", "exec")
    blob = marshal.dumps(code.replace(co_filename=str(target)))
    namespace: dict[str, Any] = {"__file__": str(target), "__name__": "helper"}
    exec(marshal.loads(blob), namespace)  # noqa: S102 - the route under test
    return "executed-marshalled-bound-bytes"


def _route_compile_bound_name() -> str:
    """Bytes the deployment never verified, compiled under a bound name."""
    helper = _bound_helper()
    helper.record("entrypoint")
    target = Path(helper.__file__)
    code = compile(b"record = lambda where: None\n", str(target), "exec")
    exec(code, {"__name__": "helper"})  # noqa: S102 - the route under test
    return "compiled-under-bound-name"


def _route_forge_held() -> str:
    """A module object held under a bound name without loading any content.

    Nothing of the verified content is loaded: the process simply holds a module
    under the bound name, pointing at the bound file. The boundary reports no
    content for the name rather than lending it the trust of a name it bound.
    """
    bound = Path(__file__).resolve().parent / "helper.py"
    forged = types.ModuleType("helper")
    forged.__file__ = str(bound)
    sys.modules["helper"] = forged
    return "held-without-load"


def _route_shell_module() -> str:
    """A module *object* under a bound name, holding no content at all.

    The deployment bound ``helper`` to a file; this route holds something else
    under that name — a module object with no location and no content. Nothing
    the deployment verified was loaded, and the boundary must say so rather than
    report the bound file as executing (§19, §20).
    """
    shell = types.ModuleType("helper")

    def record(where: str) -> None:
        _mark(f"shell-{where}")

    shell.record = record
    sys.modules["helper"] = shell
    helper = _bound_helper()
    helper.record("entrypoint")
    return "held a module object where the bound content should be"


def _route_archive() -> str:
    """A name whose content an archive on the search path provides.

    An archive is a location like any other: putting one on the search path
    does not make the code inside it content this deployment verified.
    """
    archive = _workspace() / "bundle.zip"
    sys.path.insert(0, str(archive))
    importlib.invalidate_caches()
    module = importlib.import_module(_unbound_name())
    module.record("entrypoint")
    return f"loaded:{module.__file__}"


def _route_unbound() -> str:
    """A name whose content lives only outside the verified roots."""
    module = importlib.import_module(_unbound_name())
    module.record("entrypoint")
    return f"loaded:{module.__file__}"


def _alias_path() -> Path:
    """The directory this route is told to search, if a test named one.

    A path handed to the process in its working directory is operational input
    like any other: naming a directory does not make it a trusted location, and
    what it leads to is decided by the boundary.
    """
    named = Path("boundary-alias.txt")
    if named.is_file():
        return Path(named.read_text(encoding="utf-8").strip())
    return _workspace()


def _route_alias() -> str:
    """The same, reached through a directory link instead of the real directory."""
    alias = _alias_path()
    sys.path.insert(0, str(alias))
    module = importlib.import_module(_unbound_name())
    module.record("entrypoint")
    return f"loaded:{module.__file__}"


def _child_code() -> str:
    return (
        "import importlib\n"
        f"importlib.import_module({_unbound_name()!r})\n"
        "print('imported')\n"
    )


def _route_child_inherited() -> str:
    """A child process started from the boundary's own (sanitized) environment."""
    answer = subprocess.run(
        [sys.executable, "-c", _child_code()],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = (answer.stdout + answer.stderr).strip().splitlines()
    return f"child:{lines[-1] if lines else 'no output'}"


def _route_child_hostile() -> str:
    """A child process handed an environment that would let it search the workspace."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_workspace())
    environment["PYTHONSAFEPATH"] = ""
    answer = subprocess.run(
        [sys.executable, "-c", _child_code()],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    lines = (answer.stdout + answer.stderr).strip().splitlines()
    return f"child:{lines[-1] if lines else 'no output'}"


ROUTES: dict[str, Any] = {
    "bound": _route_bound,
    "meta_path": _route_meta_path,
    "path_hooks": _route_path_hooks,
    "sys_modules": _route_sys_modules,
    "generated": _route_generated,
    "generated_read": _route_generated_read,
    "generated_memory": _route_generated_memory,
    "unbound": _route_unbound,
    "archive": _route_archive,
    "shell_module": _route_shell_module,
    "reload_module": _route_reload,
    "write_bound": _route_write_bound,
    "marshalled_bound": _route_marshalled_bound,
    "compile_bound_name": _route_compile_bound_name,
    "forge_held": _route_forge_held,
    "alias": _route_alias,
    "child_inherited": _route_child_inherited,
    "child_hostile": _route_child_hostile,
}


def build_component(configuration: dict[str, Any]) -> Any:
    """Run the configured route, then serve the pinned component's own contract."""
    _mark("entrypoint-ran")
    route = os.environ.get("BOUNDARY_ROUTE", "f3b:bound").split(":", 1)[-1]
    try:
        _record(f"{route}: {ROUTES[route]()}")
    except Exception as error:  # noqa: BLE001 - the double records, never masks
        _record(f"{route}: {type(error).__name__}: {error}")
    return build_deployment(configuration, with_http=True)
