"""The component runtime process of the first deployment slice.

One runtime element of a platform is one component, running in its own OS
process, started by Deployment & Operations from the runtime spec of a
deployment operation. This module is the entrypoint of that process — and also
of the component's transient **migration session**: the same component code, the
same pinned spec, but a session that runs the required migrations and ends
instead of becoming part of the Running Platform. Both roles are the *same*
protocol; which one a process is playing is decided by the engine that started
it, and only ``start`` makes it a runtime element of the platform (§36–§37)::

    python -m deployment_operations.runtime_worker <spec.json>

It speaks one JSON request per line over stdin and answers one JSON response
per line on stdout, so the deployment engine can start a component, orchestrate
its component-defined migrations and ask it for its health, readiness and
identity without touching anything of the component's own.

Division of labour, and the architectural rules behind it:

* the component constructs itself, through its **own** composition-internal
  deployment entrypoint named in the runtime spec — this module knows no
  component by name and contains no per-component behavior;
* migrations are **component-owned code**: this module only executes the
  declarations the component publishes, in the declared order, forward only
  (LAW-08). A declaration that carries a reverse/downgrade path is refused, so
  a migration can never rewrite history (ADR-0016 §14);
* health/readiness are the **component's own published surface**: the checks
  executed here are the component's own ``/health`` and ``/ready`` routes,
  invoked through its published ASGI application. This module never invents a
  health verdict, and the deployment engine — not this process — decides what
  the observations mean (ADR-0016 §18, §19).

Nothing here reads or writes business data: every store, journal and schema
belongs to the component and is only ever reached through the component's own
code. Secrets are never passed in the spec; they arrive through the process
environment, which the runtime adapter sets at start time (ADR-0016 §12).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import sysconfig
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Keys a forward migration declaration may carry. Anything else — in
#: particular a reverse or downgrade path — makes the declaration unusable:
#: schema develops forward only (LAW-08; ADR-0016 §14).
FORWARD_MIGRATION_KEYS = frozenset({"migration_id", "forward"})

#: Operation names of the deployment engine ↔ component process protocol.
OP_START = "start"
OP_MIGRATE = "migrate"
OP_PROBE = "probe"
OP_STOP = "stop"


class RuntimeWorkerError(Exception):
    """A refusal or failure inside the component runtime process."""


def _load_spec(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimeWorkerError("the runtime spec is not a JSON object")
    return document


def _trusted_prefixes() -> tuple[Path, ...]:
    """The trusted runtime environment: T, and only T.

    T is the interpreter's own facilities (standard library, installed
    dependencies) and the deployment engine's code — never the component's
    content, never the workspace, never a path an environment variable named.
    Being reachable from this process does not make a file trusted (§3).
    """
    prefixes: list[Path] = []
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            found = sysconfig.get_path(key)
        except KeyError:  # pragma: no cover - depends on the interpreter build
            found = None
        if found:
            prefixes.append(Path(found))
    prefixes.extend((Path(sys.prefix), Path(sys.base_prefix)))
    prefixes.append(Path(__file__).resolve().parent.parent)
    resolved: list[Path] = []
    for prefix in prefixes:
        try:
            candidate = prefix.resolve()
        except OSError:  # pragma: no cover - unreadable interpreter paths
            continue
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _inside_apparent(root: Path, path: Path) -> bool:
    """True when a path *appears* to lie inside a root, without following links.

    Path identity is never a string prefix test — that is what lets a symlink
    out of a trusted-looking directory carry unbound content. It is, however,
    the other half of the question: a path that appears inside a root the
    boundary does not trust is component-owned provenance however its links
    resolve, so containment is asked both of the path as written and of the
    object it denotes (§7).
    """
    try:
        apparent_root = Path(os.path.abspath(root))
        apparent_path = Path(os.path.abspath(path))
    except OSError:  # pragma: no cover - unreadable paths are not trusted
        return False
    return apparent_path == apparent_root or apparent_root in apparent_path.parents


def _inside(root: Path, path: Path) -> bool:
    """True when a path lies inside a root, after resolving both."""
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
    except OSError:  # pragma: no cover - unreadable paths are not trusted
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def _sanitize_search_path(trusted: Sequence[Path]) -> None:
    """Leave only trusted runtime facilities on the import search path.

    Component content is not found by searching; it is loaded from the boundary.
    Anything the environment put on the search path — the workspace, a
    declared import root, a directory named by ``PYTHONPATH`` — is removed
    before any component code can run, so it can never provide content (§17).
    """
    kept: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        if any(_inside(prefix, Path(entry)) for prefix in trusted):
            kept.append(entry)
    sys.path[:] = kept


class ExecutionBoundaryError(RuntimeWorkerError):
    """Content the Verified Execution Boundary refuses to load or to trust."""


@dataclass(frozen=True)
class _BoundContent:
    """One module of the boundary: the file it must be loaded from, and its shape.

    The boundary carries the engine-computed digest as an enforcement invariant,
    not as runtime self-report. The worker compares the bytes it reads against
    that digest before any component code can execute; later execution evidence
    remains a separate observation checked by the engine (ADR-0016 §9–§10).
    """

    module: str
    path: Path
    digest: str
    is_package: bool


class _BoundLoader(importlib.abc.Loader):
    """Loads component content from exactly the file the engine bound.

    The bytes are re-digested at the moment of loading: content that is not the
    content the engine verified is refused before it can execute, so there is no
    window between verification and execution (§8).
    """

    def __init__(self, boundary: _ExecutionBoundary, content: _BoundContent) -> None:
        self._boundary = boundary
        self._content = content

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        content = self._content
        # Nothing a component installed may stand ahead of the boundary at the
        # moment its content runs (§13).
        self._boundary.assert_first()
        try:
            source = content.path.read_bytes()
        except OSError as error:
            message = (
                f"{content.module}: the bound content {content.path} cannot be "
                f"read ({error.__class__.__name__}: {error})"
            )
            self._boundary.record_refusal(message)
            raise ExecutionBoundaryError(message) from error
        # The digest is taken from the bytes that are about to execute, so the
        # evidence describes the content that ran, not whatever the file holds
        # when the process is later asked about itself (§8).
        known = self._boundary.loaded_digest(content.module)
        fresh = f"sha256:{hashlib.sha256(source).hexdigest()}"
        if known is not None and known != fresh:
            # This process has already loaded this module, and the content at
            # the bound path is no longer the content it loaded. A module that
            # is loaded twice under one bound name is the same content twice, or
            # it is not loaded at all (§8, §12; verified at t0, executed at t1).
            message = (
                f"{content.module}: this process already loaded this module from "
                f"{content.path} and the content there has changed since; the "
                "content about to run is not the content that was verified"
            )
            self._boundary.record_refusal(message)
            raise ExecutionBoundaryError(message)
        if content.digest != fresh:
            message = (
                f"{content.module}: the content at {content.path} does not match "
                f"the digest fixed by the engine ({content.digest}); refusing to "
                "execute content different from the verified binding"
            )
            self._boundary.record_refusal(message)
            raise ExecutionBoundaryError(message)
        established = self._boundary.established_digest(content.module)
        if established is not None and established != fresh:
            # What this boundary was established on is the only content it
            # executes: bytes that differ from them are not verified bytes,
            # however they reached the bound path (§8).
            message = (
                f"{content.module}: the content at {content.path} is not the "
                "content this boundary was established on; the boundary executes "
                "only the bytes it was established on"
            )
            self._boundary.record_refusal(message)
            raise ExecutionBoundaryError(message)
        self._boundary.record_load(content.module, source)
        if content.path.suffix == ".py":
            code = self._boundary.compile_bound(source, content.path)
            module.__file__ = str(content.path)
            exec(code, module.__dict__)  # noqa: S102 - bound, verified content
            return
        # Native component-owned content is bound like any other content, and
        # loaded by the machinery for the interpreter's extension modules —
        # never from an unverified location (§14).
        spec = importlib.util.spec_from_file_location(content.module, content.path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            message = (
                f"{content.module}: the bound content {content.path} is not loadable"
            )
            self._boundary.record_refusal(message)
            raise ExecutionBoundaryError(message)
        spec.loader.exec_module(module)


#: Content this boundary governs because it can execute: source, bytecode and
#: native libraries. Data a component keeps beside itself is not in this set.
EXECUTABLE_CONTENT_SUFFIXES = (
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
)

#: Audit events by which a process can rewrite content on the file system. The
#: boundary refuses every one of them inside the content it verified: what was
#: verified is what executes, and the process never rewrites it (§8, §13).
_MUTATION_EVENTS = frozenset(
    {
        "os.chmod",
        "os.chown",
        "os.link",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.symlink",
        "os.truncate",
        "os.utime",
    }
)


#: The interpreter's own type for code objects. Verified bytes are compiled by
#: the boundary itself, so the boundary can walk the nested code of that compile
#: without importing beyond the surface this capability is allowed to touch.
_CODE_TYPE = type(_load_spec.__code__)


def _writes_content(mode: Any, flags: Any) -> bool:
    """True when an ``open`` event describes a write, however it was named.

    ``open`` reports the mode as a string and the flags as an integer;
    ``os.open`` reports an integer mode. A change is a change whichever of them
    carries the write bit (§13).
    """
    for value in (mode, flags):
        if isinstance(value, str):
            if any(character in value for character in "wax+"):
                return True
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            return True
    return False


def _apparent_path(path: Path) -> Path:
    """The location a path names as written, before any link is followed.

    Path identity is never a string prefix test: a link is judged by what it
    leads to. It is, however, half of the question — a path that *appears* to
    lie inside a location the deployment does not trust is component-owned
    provenance however its links resolve (§7).
    """
    try:
        return Path(os.path.abspath(path))
    except OSError:  # pragma: no cover - a path that cannot be named is not judged
        return path


def _is_pseudo_filename(filename: str) -> bool:
    """True for interpreter labels such as ``<string>`` or ``<frozen os>``.

    A label carries no provenance: nothing can be verified about it, so nothing
    is ever trusted from it (§19). Only a real or real-looking path can name
    content this boundary governs.
    """
    return filename.startswith("<") and filename.endswith(">")


class _ExecutionAudit:
    """Refuses content the boundary did not bind, however the process reaches it.

    The boundary governs resolution; this governs execution. The interpreter
    reports every code object about to run, every source about to be compiled,
    every governed file about to be opened and every native library about to be
    loaded — so component-owned content cannot execute merely because a
    component found a route around the finder (``sys.meta_path``,
    ``sys.path_hooks``, ``sys.modules``, a hand-rolled loader, ``exec`` of
    compiled source). Content with no provenance at all — code compiled from a
    string — is never trusted and never reported, and only an OS sandbox could
    forbid it: there is none here, and none is claimed (§13, §19).
    """

    def __init__(self, boundary: _ExecutionBoundary) -> None:
        self._boundary = boundary

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        try:
            if event == "exec":
                self._executed(args[0])
            elif event == "compile":
                self._compiled(
                    args[0] if args else None, args[1] if len(args) > 1 else None
                )
            elif event == "open":
                self._opened(
                    args[0],
                    args[1] if len(args) > 1 else None,
                    args[2] if len(args) > 2 else None,
                )
            elif event == "ctypes.dlopen":
                self._dlopened(args[0])
            elif event == "subprocess.Popen":
                self._spawned(args[0], args[2], args[3])
            elif event in _MUTATION_EVENTS:
                self._mutated(event, args)
        except ExecutionBoundaryError:
            raise
        except Exception:  # noqa: BLE001 - audit bookkeeping never breaks a process
            return

    # -- the four routes ---------------------------------------------------
    def _executed(self, code: Any) -> None:
        filename = getattr(code, "co_filename", None)
        if not isinstance(filename, str):
            return
        self._refuse(filename, "a code object")
        if not self._boundary.is_bound_code(code) and self._boundary.inside_verified(
            Path(filename)
        ):
            # The boundary compiles the bytes it verified and knows those code
            # objects. A code object it did not compile is not the code the
            # deployment verified, whatever its filename claims (§8, §13).
            self._refuse_with(
                f"a code object at {filename}: this content is verified execution "
                "content, and the code object about to run was not compiled by "
                "the boundary from the bytes it verified; unverified code objects "
                "never execute under verified content"
            )

    def _compiled(self, source: Any, filename: Any) -> None:
        if isinstance(filename, bytes):
            filename = filename.decode("utf-8", "replace")
        if not isinstance(filename, str):
            return
        self._refuse(filename, "compiled source")
        if self._boundary.inside_verified(Path(filename)) and (
            not self._boundary.is_bound_source(source, filename)
        ):
            # Only the bytes the boundary verified may be compiled under a
            # verified name: any other source is building a code object out of
            # content the deployment never verified (§8, §13).
            self._refuse_with(
                f"compiled source at {filename}: this content is verified "
                "execution content, and only the bytes this boundary verified are "
                "compiled under its name; a compile from any other source or "
                "route is refused"
            )

    def _opened(self, path: Any, mode: Any = None, flags: Any = None) -> None:
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        if not isinstance(path, str):
            return
        if _writes_content(mode, flags) and self._boundary.inside_verified(Path(path)):
            # Verified content is read by the boundary and by nothing else here:
            # a process rewrite would make what executes differ from what was
            # verified (§8, §13).
            self._refuse_with(
                f"a write to {path}: verified execution content is immutable "
                "inside the boundary; the boundary executes the content it "
                "verified and never rewrites it"
            )
        if Path(path).suffix in EXECUTABLE_CONTENT_SUFFIXES:
            self._refuse(path, "executable content opened")

    def _dlopened(self, name: Any) -> None:
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if not isinstance(name, str) or ("/" not in name and os.sep not in name):
            # A bare library name is resolved by the platform loader, which is
            # trusted runtime machinery; only a path names content here.
            return
        self._refuse(name, "a native library")

    def _spawned(self, executable: Any, cwd: Any, environment: Any) -> None:
        """A child process may not be handed content this deployment never bound.

        A child is a separate process: nothing here can verify what it executes,
        so its execution is never part of this deployment's claim. What the
        boundary does control is what it hands over — a child that *inherits*
        this process's environment inherits the sanitized one and cannot resolve
        unbound component content; a child handed an environment that would let
        it do so is refused, not trusted (§18).
        """
        if not isinstance(environment, Mapping):
            # No explicit environment: the child inherits this process's
            # environment, which carries only trusted facilities.
            return
        for entry in str(environment.get("PYTHONPATH", "")).split(os.pathsep):
            if entry:
                self._refuse(entry, "a child process handed this import path")
        if str(environment.get("PYTHONSAFEPATH", "")).strip() not in ("", "0"):
            return
        name = (
            Path(executable).name if isinstance(executable, str) and executable else ""
        )
        if not name.startswith("python"):
            return
        working = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
        self._refuse(
            str(working), "a child interpreter that would search this directory"
        )

    def _mutated(self, event: str, args: tuple[Any, ...]) -> None:
        for candidate in args:
            if isinstance(candidate, bytes):
                candidate = candidate.decode("utf-8", "replace")
            if not isinstance(candidate, str) or not candidate:
                continue
            if self._boundary.inside_verified(Path(candidate)):
                self._refuse_with(
                    f"a file system change ({event}) of {candidate}: verified "
                    "execution content is immutable inside the boundary; the "
                    "boundary executes the content it verified and never "
                    "rewrites it"
                )

    # -- the decision ------------------------------------------------------
    def _refuse(self, filename: str, what: str) -> None:
        if not filename or _is_pseudo_filename(filename):
            return
        path = Path(filename)
        if not path.is_absolute() and not path.exists():
            # A relative label that names no file is a label, like ``<string>``:
            # it carries no provenance to refuse and no content to trust.
            return
        verdict = self._boundary.foreign_content(path)
        if verdict is None:
            return
        self._refuse_with(f"{what} at {filename}: {verdict}")

    def _refuse_with(self, message: str) -> None:
        self._boundary.record_refusal(message)
        raise ExecutionBoundaryError(message)


class _VebFinder(importlib.abc.MetaPathFinder):
    """Resolves imports against the boundary, and refuses everything else.

    Two rules, in order: a name the boundary bound is loaded from its bound
    file; a name that would resolve to unbound component-owned content — inside
    a verified root, inside the workspace, or already present in ``sys.modules``
    from such a location — is refused. Names that are neither are left to the
    trusted runtime machinery, which by then searches only trusted facilities.
    """

    def __init__(self, boundary: _ExecutionBoundary) -> None:
        self._boundary = boundary

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        content = self._boundary.bound(fullname)
        if content is not None:
            return importlib.util.spec_from_loader(
                fullname,
                _BoundLoader(self._boundary, content),
                origin=str(content.path),
                is_package=content.is_package,
            )
        refusal = self._boundary.refusal_for(fullname)
        if refusal is not None:
            # The refusal is evidence: a component that catches the error and
            # carries on must still not look like a verified execution (§19).
            self._boundary.record_refusal(refusal)
            raise ExecutionBoundaryError(refusal)
        located = self._boundary.unverified_resolution(fullname, path)
        if located is not None:
            # Nothing here is found by searching: a name is served from the
            # content this deployment bound or from a trusted runtime facility,
            # and a name that would resolve anywhere else is refused before it
            # can load, whatever put that location on the search path (§5, §13).
            self._boundary.record_refusal(located)
            raise ExecutionBoundaryError(located)
        return None


class _ExecutionBoundary:
    """The engine's binding, enforced while the process runs (§2, §5, §19).

    The boundary knows the closed set of component-owned content the engine
    verified. It resolves component-owned names against that set alone, refuses
    unbound component-owned content wherever it is found, and reports what it
    actually loaded — evidence the engine compares with its own binding.
    """

    def __init__(self, binding: Mapping[str, Any]) -> None:
        self._binding = binding
        self.bound_modules: dict[str, _BoundContent] = {}
        for entry in binding["modules"]:
            module = str(entry["module"])
            path = Path(str(entry["path"]))
            digest = entry.get("digest")
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise ExecutionBoundaryError(
                    f"{module}: the launch binding carries no valid content digest"
                )
            hex_digest = digest.removeprefix("sha256:")
            if len(hex_digest) != 64 or any(
                character not in "0123456789abcdef" for character in hex_digest
            ):
                raise ExecutionBoundaryError(
                    f"{module}: the launch binding carries an invalid content digest"
                )
            self.bound_modules[module] = _BoundContent(
                module=module,
                path=path,
                digest=digest,
                is_package=path.name == "__init__.py",
            )
        self.bound_modules_order = [
            str(entry["module"]) for entry in binding["modules"]
        ]
        self._order = list(self.bound_modules_order)
        self._roots = tuple(Path(str(root)) for root in binding.get("roots", ()))
        self._untrusted = tuple(
            Path(str(path)) for path in binding.get("untrusted", ())
        )
        self._load_digests: dict[str, str] = {}
        #: The digests of the content the boundary was established on — its own
        #: reading, taken before any component content can run, never received
        #: from the engine (§8).
        self._established: dict[str, str] = {}
        #: Every code object this boundary compiled from verified bytes; the
        #: only code objects that may execute under verified content (§8).
        self._bound_code: set[Any] = set()
        #: The digest of the exact bytes this boundary compiled for a path, so a
        #: compile of anything else under that name is refused (§8, §13).
        self._bound_source: dict[str, str] = {}
        self._refusals: list[str] = []
        self._finder = _VebFinder(self)
        #: T — the trusted runtime environment. Its content is not component
        #: content, so the boundary never calls it foreign (§3).
        self._trusted = _trusted_prefixes()

    def loaded_digest(self, module: str) -> str | None:
        """The digest of the bytes this process loaded for a module, if any.

        The value is this process's own record of what it executed, taken at
        load time; it is never received from the engine, so reporting it can
        never be a matter of echoing an expected value (§8).
        """
        return self._load_digests.get(module)

    def record_load(self, module: str, content: bytes) -> None:
        """Remember the digest of the bytes this process loaded for a module."""
        self._load_digests[module] = f"sha256:{hashlib.sha256(content).hexdigest()}"

    def established_digest(self, module: str) -> str | None:
        """The digest of the content this boundary was established on, if known."""
        return self._established.get(module)

    def compile_bound(self, source: bytes, path: Path) -> Any:
        """Compile verified bytes — the only compilation the boundary authorises."""
        self._bound_source[str(path)] = f"sha256:{hashlib.sha256(source).hexdigest()}"
        code = compile(source, str(path), "exec")
        self.register_code(code)
        return code

    def is_bound_source(self, source: Any, filename: str) -> bool:
        """True when ``source`` is exactly the bytes this boundary compiled."""
        recorded = self._bound_source.get(filename)
        if recorded is None:
            return False
        if isinstance(source, bytes):
            observed = f"sha256:{hashlib.sha256(source).hexdigest()}"
        elif isinstance(source, str):
            observed = f"sha256:{hashlib.sha256(source.encode('utf-8', 'replace')).hexdigest()}"
        else:
            return False
        return observed == recorded

    def register_code(self, code: Any) -> None:
        """Remember a code object compiled from the bytes this boundary verified.

        Nested code objects — function and class bodies, comprehensions — are
        part of the same module content: the code of verified bytes is verified
        code (§8).
        """
        pending = [code]
        while pending:
            current = pending.pop()
            self._bound_code.add(current)
            for const in getattr(current, "co_consts", ()):
                if isinstance(const, _CODE_TYPE):
                    pending.append(const)

    def is_bound_code(self, code: Any) -> bool:
        """True when this boundary compiled the code object from verified bytes."""
        return code in self._bound_code

    def _bound_paths(self) -> set[Path]:
        paths: set[Path] = set()
        for content in self.bound_modules.values():
            try:
                paths.add(content.path.resolve())
            except OSError:  # pragma: no cover - unreadable paths are not trusted
                continue
        return paths

    def inside_verified(self, path: Path) -> bool:
        """True when a path is verified content, or lies inside a verified root."""
        if _is_pseudo_filename(str(path)):
            return False
        if not path.is_absolute() and not path.exists():
            return False
        try:
            resolved = path.resolve()
        except OSError:  # pragma: no cover - unreadable paths are not trusted
            return False
        if resolved in self._bound_paths():
            return True
        return any(
            _inside(root, path) or _inside_apparent(root, path) for root in self._roots
        )

    def record_refusal(self, message: str) -> None:
        """Remember a boundary refusal, whether or not the caller caught it.

        A refusal is not an internal detail of one import: it is the process
        telling the engine that content it did not bind was asked for. Hiding it
        would let a component swallow the error and keep reporting a state that
        no verified execution stands behind (§19).
        """
        if message not in self._refusals:
            self._refusals.append(message)

    # -- installation ------------------------------------------------------
    def assert_first(self) -> None:
        """Put the boundary ahead of every other finder on the import path.

        A component may install finders of its own; none of them may be
        consulted before the boundary that decides what this process executes.
        This cannot seal the process (there is no OS sandbox here), but no
        import may resolve past the boundary: content that still arrives through
        another route is reported to the engine and refused there (§13, §20).
        """
        try:
            sys.meta_path.remove(self._finder)
        except ValueError:  # pragma: no cover - the boundary installed it
            pass
        sys.meta_path.insert(0, self._finder)

    def establish(self) -> None:
        """Verify the launch content against the engine's immutable binding before execution.

        The engine computes each digest before launching the process and carries
        that value in the launch binding. This process may not establish its own
        desired identity: it must first prove that the bytes present at the bound
        path are exactly the bytes the engine verified. A mismatch aborts the
        process before component code can run (§8–§10).
        """
        errors: list[str] = []
        for name, content in self.bound_modules.items():
            try:
                observed = _content_digest(content.path)
            except OSError as error:
                observed = ""
                errors.append(
                    f"{name}: the bound content {content.path} cannot be read "
                    f"before execution ({error.__class__.__name__}: {error})"
                )
            self._established[name] = observed
            if observed != content.digest:
                errors.append(
                    f"{name}: launch content digest mismatch: engine verified "
                    f"{content.digest}, but the process found {observed or 'unreadable'} "
                    f"at {content.path}; refusing to execute substituted content"
                )
        if errors:
            for message in errors:
                self.record_refusal(message)
            raise ExecutionBoundaryError("; ".join(errors))

    def install(self) -> None:
        """Establish the boundary before any component content can be loaded."""
        self.establish()
        _sanitize_search_path(self._trusted)
        # Resolution is not enough on its own: the process also refuses to
        # execute, compile, load or open component-owned content it did not
        # bind, whichever route a component takes to it.
        sys.addaudithook(_ExecutionAudit(self))
        for name, content in self.bound_modules.items():
            existing = sys.modules.get(name)
            origin = getattr(existing, "__file__", None)
            held = (
                Path(origin).resolve() if isinstance(origin, str) and origin else None
            )
            if held is not None and held != content.path.resolve():
                self._refusals.append(
                    f"{name}: the process already holds content from {origin}; "
                    f"the boundary loads component content only from {content.path}"
                )
        self.assert_first()

    # -- resolution --------------------------------------------------------
    def bound(self, fullname: str) -> _BoundContent | None:
        return self.bound_modules.get(fullname)

    def refusal_for(self, fullname: str) -> str | None:
        """Why this name may not be resolved, or ``None`` when it is not ours.

        A name is component-owned when it resolves inside a verified root or
        inside an untrusted location. Either way it is refused unless the
        boundary bound it: component-owned content is never searched for.
        """
        existing = sys.modules.get(fullname)
        origin = getattr(existing, "__file__", None)
        if isinstance(origin, str) and origin:
            verdict = self._location_verdict(Path(origin))
            if verdict is not None:
                return verdict
        for root in self._untrusted:
            candidate = _resolve_under(fullname, root)
            if candidate is None:
                continue
            if candidate.suffix != ".py":
                return (
                    f"{fullname}: native content {candidate} under {root} has "
                    "no provenance in this deployment; non-Python component "
                    "content executes only when the engine bound it as verified "
                    "content, and this deployment bound none"
                )
            return (
                f"{fullname}: content for this name is present under "
                f"{root}, which is never a source of trusted component "
                "content; the boundary refuses to load it"
            )
        for root in self._roots:
            if _resolve_under(fullname, root) is not None:
                return (
                    f"{fullname}: this name resolves to component-owned content "
                    "inside a verified execution root that the boundary did not "
                    "bind; unbound component content is never executed"
                )
        return None

    def governed(self, path: Path) -> bool:
        """True when the boundary judges the content a path names.

        The boundary judges component-owned content — whatever lies inside a
        verified execution root, whatever lies in a location that is never a
        source of trusted content, and whatever lies anywhere else the
        deployment did not verify. It does not judge the trusted runtime
        environment, whose facilities are T (§3, §5).
        """
        for root in (*self._untrusted, *self._roots):
            if _inside(root, path) or _inside_apparent(root, path):
                return True
        return not any(_inside(prefix, path) for prefix in self._trusted)

    def foreign_content(self, path: Path) -> str | None:
        """Why the content at ``path`` may not be executed or opened, or None.

        Trusted runtime facilities and the content this deployment bound are the
        only content a process may run; everything else is refused here, before
        it can execute — not merely noticed afterwards (§5, §13). The question is
        asked of the path as it was named and of the object it denotes, because
        a path through a link is component-owned provenance however its links
        resolve.
        """
        if not self.governed(path):
            return None
        return self._location_verdict(path) or self._location_verdict(
            _apparent_path(path)
        )

    def _location_verdict(self, path: Path) -> str | None:
        try:
            resolved = path.resolve()
        except OSError:  # pragma: no cover - unreadable paths are not trusted
            return None
        bound_paths = {
            content.path.resolve() for content in self.bound_modules.values()
        }
        for root in self._untrusted:
            if _inside(root, path) or _inside_apparent(root, path):
                return (
                    f"{path}: content from {root} is never trusted component "
                    "content, whatever the process holds in memory"
                )
        if any(
            _inside(prefix, path) or _inside_apparent(prefix, path)
            for prefix in self._trusted
        ):
            # Trusted runtime facilities — the interpreter's own libraries,
            # installed dependencies and the engine's code — are T, not
            # component content. Containment is asked both of the path as the
            # interpreter names it and of the object it denotes: a standard
            # library that its distributor links elsewhere (a packaged
            # ``sitecustomize`` pointing into ``/etc``) is still the same
            # facility the interpreter itself loaded.
            return None
        for root in self._roots:
            if (_inside(root, path) or _inside_apparent(root, path)) and (
                resolved not in bound_paths
            ):
                return (
                    f"{path}: content inside a verified execution root that the "
                    "boundary did not bind is never executed"
                )
        if resolved not in bound_paths:
            # The whole of the rule, for every location the deployment does not
            # know by name: what executes in this process is the content the
            # deployment verified or a trusted runtime facility, and nothing
            # else — wherever it lies and however it was reached (§3, §5).
            return (
                f"{path}: content at a location outside every verified "
                "execution root and outside the trusted runtime environment is "
                "never executed; whatever the process holds in memory, only "
                "verified content and trusted facilities run"
            )
        return None

    # -- resolution --------------------------------------------------------
    def unverified_resolution(self, fullname: str, path: Any) -> str | None:
        """Why this name may not resolve at all, or ``None`` when T provides it.

        Resolution is decided, not merely observed: the boundary asks where this
        name would load from *now* — through this process's own machinery, with
        the search path as it stands at this moment — and refuses anything that
        is not content this deployment bound or content of the trusted runtime
        environment. A directory a component adds to ``sys.path`` at runtime, a
        directory handed in as the parent ``path`` of a submodule, a namespace
        portion outside T: none of them may serve a name, so ``Resolve(x) => x ∈
        V(I) ∪ T`` holds however the name is reached (§5, §13).
        """
        if not fullname or not all(part.isidentifier() for part in fullname.split(".")):
            return None
        search: Any = sys.path if path is None else path
        try:
            spec = importlib.machinery.PathFinder.find_spec(fullname, search)
        except (AttributeError, ImportError, TypeError, ValueError):
            # A search path this process cannot even search provides nothing;
            # whether the name resolves at all is then the interpreter's answer,
            # and no content of ours is at stake.
            return None
        if spec is None:
            return None
        bound_paths = {
            content.path.resolve() for content in self.bound_modules.values()
        }
        locations: list[Path] = []
        origin = getattr(spec, "origin", None)
        if isinstance(origin, str) and origin and not _is_pseudo_filename(origin):
            locations.append(Path(origin))
        for portion in tuple(spec.submodule_search_locations or ()):
            locations.append(Path(portion))
        for location in locations:
            resolved = None
            try:
                resolved = location.resolve()
            except OSError:  # pragma: no cover - an unreadable location is not T
                resolved = None
            if resolved is not None and resolved in bound_paths:
                continue
            if any(_inside(prefix, location) for prefix in self._trusted):
                continue
            return (
                f"{fullname}: this name resolves to {location}, which is neither "
                "content this deployment bound nor a trusted runtime facility; "
                "the boundary executes only content it verified"
            )
        return None

    # -- loading -----------------------------------------------------------
    def load_module(self, module: str) -> Any:
        """Load one bound module, or refuse; never search for the name."""
        content = self.bound_modules.get(module)
        if content is None:
            message = (
                f"{module}: the boundary bound no content for this name; "
                "unbound content is never imported"
            )
            self.record_refusal(message)
            raise ExecutionBoundaryError(message)
        self.assert_first()
        existing = sys.modules.get(module)
        if existing is not None:
            origin = getattr(existing, "__file__", None)
            if (
                not isinstance(origin, str)
                or not origin
                or Path(origin).resolve() != content.path.resolve()
            ):
                # Something is held under a name this deployment bound, and it
                # is not the content the deployment bound: a module object with
                # no location at all, or content from somewhere else. Neither is
                # the verified content, and neither may be run as it (§19).
                message = (
                    f"{module}: the process holds content for this name that is "
                    f"not the content this deployment bound ({content.path}); "
                    "refusing to run either"
                )
                self.record_refusal(message)
                raise ExecutionBoundaryError(message)
            if module not in self._load_digests:
                # Held content is not loaded content: a module object under a
                # bound name that this boundary never loaded is no evidence of
                # the bound content, and is never treated as it (§19).
                message = (
                    f"{module}: the process holds content for this name that this "
                    "boundary never loaded; a name is never trusted by its label"
                )
                self.record_refusal(message)
                raise ExecutionBoundaryError(message)
            return existing
        loaded = importlib.import_module(module)
        origin = getattr(loaded, "__file__", None)
        if (
            not isinstance(origin, str)
            or Path(origin).resolve() != content.path.resolve()
        ):
            raise ExecutionBoundaryError(
                f"{module}: the loaded content is not the bound content "
                f"({origin!r})"
            )
        if module not in self._load_digests:
            message = (
                f"{module}: the content loaded for this name was not loaded by this "
                "boundary; a name is never trusted by its label"
            )
            self.record_refusal(message)
            raise ExecutionBoundaryError(message)
        return loaded

    # -- evidence ----------------------------------------------------------
    def evidence(self) -> dict[str, Any]:
        """The boundary's account of what is executing (§19, §20).

        Every bound module is reported with the digest of the content this
        process loaded for it, and with any unbound component-owned content the
        process holds: content the engine must refuse, because a deployment may
        not claim what its boundary does not cover.
        """
        foreign = list(self._refusals)
        for name, module in list(sys.modules.items()):
            if name in self.bound_modules:
                continue
            origin = getattr(module, "__file__", None)
            if not isinstance(origin, str) or not origin:
                continue
            verdict = self._location_verdict(Path(origin))
            if verdict is not None and verdict not in foreign:
                foreign.append(verdict)
        modules: list[dict[str, Any]] = []
        for module in self._order:
            content = self.bound_modules[module]
            held = sys.modules.get(module)
            origin = getattr(held, "__file__", None) if held is not None else None
            actual = Path(origin) if isinstance(origin, str) and origin else None
            if held is None:
                # The name was never imported: the process did not run this
                # content. The file the deployment bound is reported with the
                # digest it holds — evidence of the content, not of execution.
                modules.append(
                    {
                        "module": module,
                        "path": str(content.path),
                        "digest": self._load_digests.get(module)
                        or _content_digest(content.path),
                        "loaded": False,
                    }
                )
                continue
            if actual is None or actual.resolve() != content.path.resolve():
                # A bound name whose content is not the content this deployment
                # bound — somewhere else, or a module object with no location at
                # all. The deployment must see that the name holds no verified
                # content, never the name it was supposed to hold (§19, §20).
                report = (
                    f"{actual if actual is not None else 'content held without a location'}: "
                    f"content for the bound name {module!r} is not the content this "
                    "deployment bound; a boundary never accepts content it did not "
                    "bind"
                )
                if report not in foreign:
                    foreign.append(report)
                modules.append(
                    {
                        "module": module,
                        "path": str(actual) if actual is not None else "",
                        "digest": (
                            _optional_digest(actual) if actual is not None else ""
                        ),
                        "loaded": False,
                    }
                )
                continue
            if module not in self._load_digests:
                # Something holds a module under a bound name that this boundary
                # never loaded. Its content is not evidence of the bound content,
                # so the boundary reports no content for the name rather than
                # lending the name its trust (§19).
                report = (
                    f"{actual}: the process holds content for the bound name "
                    f"{module!r} that this boundary never loaded; a boundary "
                    "reports only what it loaded"
                )
                if report not in foreign:
                    foreign.append(report)
                modules.append(
                    {"module": module, "path": "", "digest": "", "loaded": False}
                )
                continue
            modules.append(
                {
                    "module": module,
                    "path": str(actual),
                    "digest": self._load_digests[module],
                    "loaded": True,
                }
            )
        return {
            "root": str(self._roots[0]) if self._roots else "",
            "roots": [str(root) for root in self._roots],
            "modules": modules,
            "foreign": foreign,
        }


def _optional_digest(path: Path) -> str:
    """The digest of the content at ``path``, or ``""`` when it cannot be read.

    Content the boundary refuses — anything with component-owned provenance it
    did not bind — is never read, not even to describe it: a refusal is not an
    exception to the rule it enforces. The engine is told where the content was
    found and refuses the claim on that alone; an unreadable file is reported as
    no evidence at all, never as an invented value.
    """
    try:
        return _content_digest(path)
    except (OSError, ExecutionBoundaryError):
        return ""


def _resolve_under(module: str, root: Path) -> Path | None:
    """The file this module name would be loaded from under one root, if any."""
    parts = module.split(".")
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    base = root.joinpath(*parts)
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    if base.parent.is_dir():
        native = sorted(
            candidate
            for pattern in (f"{parts[-1]}.so", f"{parts[-1]}.*.so")
            for candidate in base.parent.glob(pattern)
            if candidate.is_file()
        )
        if native:
            return native[0]
    return None


def _load_binding(raw: str) -> dict[str, Any]:
    """Parse the launch binding the engine started this process with.

    The binding is the engine's determination of the content this process
    executes: one execution root and the file each component-owned module is
    loaded from. It is required — a process started without it would have to
    find the component's code by name, which is exactly what cannot be
    verified, so it refuses to run instead (ADR-0016 §9, §10).
    """
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeWorkerError(f"the launch binding is not valid JSON: {error}")
    if not isinstance(document, Mapping):
        raise RuntimeWorkerError("the launch binding is not an object")
    root = document.get("root")
    modules = document.get("modules")
    if not isinstance(root, str) or not root:
        raise RuntimeWorkerError("the launch binding names no execution root")
    if not isinstance(modules, list) or not modules:
        raise RuntimeWorkerError("the launch binding names no executable content")

    def _paths(key: str) -> list[str]:
        raw = document.get(key, [])
        if not isinstance(raw, list):
            raise RuntimeWorkerError(f"the launch binding's {key} is not a list")
        return [item for item in raw if isinstance(item, str) and item]

    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in modules:
        if not isinstance(entry, Mapping):
            raise RuntimeWorkerError("the launch binding carries an unusable entry")
        module = entry.get("module")
        path = entry.get("path")
        digest = entry.get("digest")
        if not isinstance(module, str) or not module:
            raise RuntimeWorkerError("the launch binding names no module")
        if not isinstance(path, str) or not path:
            raise RuntimeWorkerError(
                f"{module}: the launch binding names no content for this module"
            )
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise RuntimeWorkerError(
                f"{module}: the launch binding names no content digest"
            )
        hex_digest = digest.removeprefix("sha256:")
        if len(hex_digest) != 64 or any(
            character not in "0123456789abcdef" for character in hex_digest
        ):
            raise RuntimeWorkerError(
                f"{module}: the launch binding carries an invalid content digest"
            )
        if module in seen:
            raise RuntimeWorkerError(f"{module}: the launch binding repeats a module")
        seen.add(module)
        parsed.append({"module": module, "path": path, "digest": digest})
    return {
        "root": Path(root),
        "roots": _paths("roots"),
        "untrusted": _paths("untrusted"),
        "modules": parsed,
    }


def _content_digest(path: Path) -> str:
    """``sha256:<hex>`` of the bytes this process reads from a bound file."""
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _build_deployment(spec: Mapping[str, Any], runtime: ComponentRuntime) -> Any:
    """Construct the component's own deployment from its pinned configuration.

    The entrypoint is the one the deployment environment bound for this
    component in this environment, loaded from the content the engine bound to
    this process; the configuration is exactly what the accepted Platform
    Instance declared, plus the environment-specific values bound to it
    (ADR-0016 §13). Construction failure is a fail-closed startup failure —
    there is no fallback composition.
    """
    entry = runtime.entry_module()
    factory_name = (spec.get("runtime") or {}).get("deployment_factory")
    if not isinstance(factory_name, str):
        raise RuntimeWorkerError(
            "the runtime spec names no component deployment entrypoint"
        )
    module = runtime.load_module(entry)
    factory = getattr(module, factory_name, None)
    if factory is None or not callable(factory):
        raise RuntimeWorkerError(
            f"{entry}:{factory_name} is not a callable deployment entrypoint"
        )
    configuration = spec.get("configuration")
    if not isinstance(configuration, Mapping):
        configuration = {}
    return factory(dict(configuration))


def _contract_app(deployment: Any) -> Any:
    """The component's published ASGI contract, as the component exposes it."""
    app_factory = getattr(deployment, "contract_app", None)
    if app_factory is None or not callable(app_factory):
        raise RuntimeWorkerError(
            "the component deployment exposes no published contract application"
        )
    return app_factory()


def _normalize_migrations(
    declared: object,
    *,
    location: str,
) -> list[tuple[str, Callable[[Any], Any]]]:
    """Normalize a component's forward migration declaration.

    A declaration is a sequence whose entries are either callables or mappings
    with exactly ``migration_id`` and ``forward``. Order is the declared order
    and is never re-sorted: the component's contract decides the sequence in
    which its own migrations may run (ADR-0016 §14).
    """
    if not isinstance(declared, Sequence) or isinstance(declared, str | bytes):
        if callable(declared):
            return [("migration-1", declared)]
        raise RuntimeWorkerError(
            f"{location} does not declare a sequence of forward migrations"
        )

    migrations: list[tuple[str, Callable[[Any], Any]]] = []
    for index, entry in enumerate(declared):
        if callable(entry):
            migrations.append((f"migration-{index + 1}", entry))
            continue
        if not isinstance(entry, Mapping):
            raise RuntimeWorkerError(
                f"{location}[{index}] is neither a migration callable nor a declaration"
            )
        extra = sorted(set(entry) - FORWARD_MIGRATION_KEYS)
        if extra:
            raise RuntimeWorkerError(
                f"{location}[{index}] declares {extra!r}; migrations are forward "
                "only and a declaration may carry no reverse or downgrade path"
            )
        forward = entry.get("forward")
        if not callable(forward):
            raise RuntimeWorkerError(
                f"{location}[{index}] declares no forward migration callable"
            )
        migration_id = entry.get("migration_id")
        if not isinstance(migration_id, str) or not migration_id:
            raise RuntimeWorkerError(
                f"{location}[{index}] declares no concrete migration identity"
            )
        migrations.append((migration_id, forward))
    return migrations


class ComponentRuntime:
    """One component, running in one process, answering the engine's requests.

    The process knows only the content the engine bound to it: every
    component-owned module — the entrypoint, the migration content and
    everything they import — is loaded through the Verified Execution Boundary
    from the file the engine verified, and the engine's requests are answered
    with the digests of the files this process loaded and with any unbound
    component-owned content it holds.
    """

    def __init__(self, spec: Mapping[str, Any], boundary: _ExecutionBoundary) -> None:
        self._spec = spec
        self._boundary = boundary
        self._deployment: Any = None
        self._app: Any = None
        declared = (spec.get("runtime") or {}).get("deployment_module")
        if declared != self.entry_module():
            raise RuntimeWorkerError(
                f"the runtime spec names {declared!r} as the component's "
                f"entrypoint while the launch binding names "
                f"{self.entry_module()!r}; refusing to run either"
            )

    def entry_module(self) -> str:
        """The component's deployment entrypoint, as the engine bound it."""
        return self._boundary.bound_modules_order[0]

    def load_module(self, module: str) -> Any:
        """Load one bound module through the boundary, or refuse."""
        return self._boundary.load_module(module)

    def execution_evidence(self) -> dict[str, Any]:
        """What this process loaded, module by module (§9, §10, §19)."""
        return self._boundary.evidence()

    def start(self) -> dict[str, Any]:
        self._ensure_built()
        return {"status": "ok"}

    def _ensure_built(self) -> None:
        if self._deployment is None:
            self._deployment = _build_deployment(self._spec, self)
        if self._app is None:
            self._app = _contract_app(self._deployment)

    def migrate(self) -> dict[str, Any]:
        """Execute the component's own required migrations, forward only."""
        migrations_binding = (self._spec.get("runtime") or {}).get("migrations")
        if not isinstance(migrations_binding, Mapping):
            return {"status": "ok", "declared": False, "executed": [], "required": []}

        module_name = migrations_binding.get("module")
        attribute = migrations_binding.get("attribute")
        if not isinstance(module_name, str) or not isinstance(attribute, str):
            raise RuntimeWorkerError(
                "the runtime spec names no component migration declaration"
            )
        module = self.load_module(module_name)
        if not hasattr(module, attribute):
            raise RuntimeWorkerError(
                f"{module_name}.{attribute} is not declared by the component"
            )

        self._ensure_built()
        declared = getattr(module, attribute)
        location = f"{module_name}.{attribute}"
        if callable(declared) and not isinstance(declared, Sequence):
            declared = declared(self._deployment)
        migrations = _normalize_migrations(declared, location=location)

        executed: list[str] = []
        for migration_id, forward in migrations:
            try:
                forward(self._deployment)
            except Exception as error:  # noqa: BLE001 - fail closed, never mask
                return {
                    "status": "failed",
                    "declared": True,
                    "required": [entry[0] for entry in migrations],
                    "executed": executed,
                    "migration_id": migration_id,
                    "error": f"{error.__class__.__name__}: {error}",
                }
            executed.append(migration_id)

        return {
            "status": "ok",
            "declared": True,
            "required": [entry[0] for entry in migrations],
            "executed": executed,
        }

    def probe(self) -> dict[str, Any]:
        """Execute the component's own health and readiness routes.

        The observations are returned verbatim: this process reports what the
        component's published surface answered, and the deployment engine
        evaluates it.
        """
        self._ensure_built()
        from fastapi.testclient import TestClient

        client = TestClient(self._app)
        return {
            "status": "ok",
            "health": _call(client, "/health"),
            "ready": _call(client, "/ready"),
        }

    def stop(self) -> dict[str, Any]:
        self._app = None
        self._deployment = None
        return {"status": "ok"}


def _call(client: Any, path: str) -> dict[str, Any]:
    response = client.get(path)
    try:
        body = response.json()
    except ValueError:
        body = None
    return {"path": path, "status_code": response.status_code, "body": body}


def _respond(response: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(response), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _answered(runtime: ComponentRuntime, response: Mapping[str, Any]) -> dict[str, Any]:
    """One protocol answer, carrying what this process loaded (§9, §10).

    The evidence rides every answer the engine can attach a claim to. It never
    changes an answer's status: a runtime that cannot report the content it
    loaded reports nothing, and the engine's verification fails closed on the
    missing evidence rather than being told a reassuring value.
    """
    answer = dict(response)
    try:
        answer["execution"] = runtime.execution_evidence()
    except RuntimeWorkerError as error:
        answer["execution_error"] = str(error)
    return answer


def run(spec_path: Path, binding: Mapping[str, Any]) -> int:
    """Serve one component process until the engine asks it to stop."""
    spec = _load_spec(spec_path)
    boundary = _ExecutionBoundary(binding)
    # The boundary is established before any component-owned content can be
    # loaded, and before any import resolution can reach beyond it (§2).
    boundary.install()
    runtime = ComponentRuntime(spec, boundary)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            _respond({"status": "error", "error": f"invalid request: {error}"})
            continue
        operation = request.get("op")
        try:
            if operation == OP_START:
                _respond(_answered(runtime, runtime.start()))
            elif operation == OP_MIGRATE:
                _respond(_answered(runtime, runtime.migrate()))
            elif operation == OP_PROBE:
                _respond(_answered(runtime, runtime.probe()))
            elif operation == OP_STOP:
                _respond(runtime.stop())
                return 0
            else:
                _respond(
                    {"status": "error", "error": f"unknown operation {operation!r}"}
                )
        except RuntimeWorkerError as error:
            _respond(_answered(runtime, {"status": "error", "error": str(error)}))
        except Exception as error:  # noqa: BLE001 - report, never leak a traceback
            _respond(
                _answered(
                    runtime,
                    {
                        "status": "error",
                        "error": f"{error.__class__.__name__}: {error}",
                    },
                )
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3 or arguments[1] != "--binding":
        sys.stderr.write(
            "usage: python -m deployment_operations.runtime_worker SPEC "
            "--binding BINDING\n"
        )
        return 2
    try:
        binding = _load_binding(arguments[2])
    except RuntimeWorkerError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    return run(Path(arguments[0]), binding)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FORWARD_MIGRATION_KEYS",
    "ComponentRuntime",
    "RuntimeWorkerError",
    "main",
    "run",
]
