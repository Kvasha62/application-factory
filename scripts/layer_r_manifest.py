"""Generate and verify the committed Layer R source manifest.

The manifest (``layer-r/SOURCE_MANIFEST.json``) is the machine-readable link
between three things: the Layer R files committed to this repository, their
SHA-256 content identity, and the host path each file is deployed to on the
Running Platform host.  A release binds a deployed tree to one immutable Git
commit by definition: the manifest of that commit is the specification of the
deployed bytes, and :mod:`scripts.layer_r_drift_check` compares the host
against it without mutating anything.

Path policy (fail-closed).  ``entry.path`` must be a canonical relative POSIX
file path whose segments place it under exactly ``layer-r/source/`` or
``layer-r/config/`` — absolute paths, ``..`` / ``.`` segments, empty segments,
backslashes, trailing slashes, drive letters and any non-canonical spelling are
rejected, and prefix siblings such as ``layer-r/source-evil/`` do not match.
``entry.host_path`` is a canonical relative POSIX path under the host cell root
(empty = pending import).  Symlinks and other non-regular files are forbidden
anywhere in the tracked tree: they are never followed, and a resolved path must
remain inside the repository root.

Classification policy (see ``layer-r/README.md``): every tracked file carries
an explicit class —

* ``source``                     version-controlled implementation code
* ``deployable-configuration``   declared deployment inputs (never secrets)
* ``host-specific-template``     templates of host-bound files (unit files)

Secrets, mutable runtime state and generated/runtime data are forbidden in the
tree; this module refuses to record them and flags secret-looking content.
Verification is fail-closed: any unlisted file under the tracked roots, any
hash mismatch, any missing file and any unclassified file is an error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

MANIFEST_VERSION = 1
DEFAULT_MANIFEST_PATH = "layer-r/SOURCE_MANIFEST.json"
TRACKED_ROOTS = ("layer-r/source", "layer-r/config")
ALLOWED_CLASSES = ("source", "deployable-configuration", "host-specific-template")

#: Marker name under which ``symlinked_components`` reports a symlinked root.
ROOT_MARKER = "<root>"

#: Content that must never appear in any tracked file.  These patterns are a
#: last line of defense, not a substitute for review: export refuses the file
#: first (``scripts/layer_r_export_bundle.py``), this catches later edits.
SECRET_PATTERNS = (
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "credential-assignment",
        re.compile(
            r"(?i)(token|secret|password|passwd|api[_-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9+/_-]{8,}"
        ),
    ),
)

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SKIP_NAMES = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc"}


class ManifestError(Exception):
    """The manifest or the tree violates the Layer R source policy."""


def sha256_of(path: Path) -> str:
    """Content identity of one regular file, read-only."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secret_findings(text: str) -> list[str]:
    """Names of secret patterns found in ``text`` (empty when clean)."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def normalize_relative_path(value: object) -> str | None:
    """The canonical POSIX relative form of ``value``, or ``None`` if unsafe.

    Rejects: non-strings, empty strings, NUL, backslashes (a path separator on
    some hosts — ambiguous here), absolute paths, Windows drive letters, empty
    segments, ``.`` and ``..`` segments, trailing slashes, and any spelling
    that is not its own canonical form (``a//b``, ``a/./b``, …).  An accepted
    value therefore names exactly one location, which is what makes
    string-equality duplicate detection sound.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if "\\" in value:
        return None
    if value.startswith("/") or _WINDOWS_DRIVE_RE.match(value):
        return None
    if value.endswith("/"):
        return None
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    canonical = PurePosixPath(value).as_posix()
    if canonical != value:
        return None
    return canonical


def validate_tracked_path(value: object) -> str | None:
    """``value`` as a canonical manifest path under a tracked root, else ``None``.

    Segment-wise, not prefix-wise: ``layer-r/source-evil/file.py`` is rejected
    even though it shares the ``layer-r/source`` prefix.
    """
    canonical = normalize_relative_path(value)
    if canonical is None:
        return None
    parts = canonical.split("/")
    if len(parts) < 3:
        return None
    if parts[0] != "layer-r" or parts[1] not in ("source", "config"):
        return None
    return canonical


def scan_tracked(root: Path) -> tuple[list[str], list[str]]:
    """(regular files, unsafe paths) under the tracked roots.

    Nothing behind a symlink is ever followed: a symlink — file or directory —
    is reported as unsafe and the walk does not descend through it.  Non-regular
    entries (fifos, sockets, devices) are unsafe too.  A ``root`` reached
    through a symlink — the root itself or any ancestor — is refused before
    any walk and reported as ``ROOT_MARKER``.  Paths are root-relative POSIX
    strings.
    """
    files: list[str] = []
    unsafe: list[str] = []
    if symlinked_path_components(root):
        # The supplied root path must not be reached through a symlink — the
        # root itself or any ancestor of it.  Every path under it would be
        # reached through a symlinked directory.  Refuse before any walk —
        # ``os.walk`` and ``is_dir``/``is_symlink`` on children would follow
        # the link while resolving intermediate components.
        unsafe.append(ROOT_MARKER)
        return files, sorted(unsafe)
    layer_r = root / "layer-r"
    if layer_r.is_symlink():
        unsafe.append("layer-r")
        return files, sorted(unsafe)
    for tracked in TRACKED_ROOTS:
        base = root / tracked
        if base.is_symlink():
            unsafe.append(tracked)
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_NAMES]
            for name in list(dirnames):
                entry = Path(dirpath) / name
                if entry.is_symlink():
                    unsafe.append(entry.relative_to(root).as_posix())
                    dirnames.remove(name)
            for name in filenames:
                entry = Path(dirpath) / name
                relative = entry.relative_to(root).as_posix()
                if entry.suffix in _SKIP_SUFFIXES:
                    continue
                if entry.is_symlink() or not entry.is_file():
                    unsafe.append(relative)
                else:
                    files.append(relative)
    return sorted(files), sorted(unsafe)


def tree_files(root: Path) -> list[str]:
    """Every regular non-symlink file under the tracked roots (root-relative)."""
    return scan_tracked(root)[0]


def load_manifest(path: Path) -> dict:
    """Read the manifest document, refusing anything malformed."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        message = f"{path}: manifest is unreadable ({error.__class__.__name__})"
        raise ManifestError(message) from error
    if not isinstance(document, dict):
        message = f"{path}: manifest must be a JSON object"
        raise ManifestError(message)
    return document


def _entry_errors(entry: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["entry must be a JSON object"]
    path_value = entry.get("path")
    if not isinstance(path_value, str) or not path_value:
        return ["entry.path is required"]
    if validate_tracked_path(path_value) is None:
        errors.append(
            f"{path_value}: entry.path must be a canonical relative POSIX file "
            "path under 'layer-r/source/' or 'layer-r/config/'; absolute paths, "
            "'.', '..', empty segments, backslashes and non-canonical spellings "
            "are refused"
        )
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        errors.append(f"{path_value}: entry.sha256 must be 64 lowercase hex digits")
    classification = entry.get("class")
    if classification not in ALLOWED_CLASSES:
        errors.append(
            f"{path_value}: entry.class {classification!r} is not one of "
            f"{ALLOWED_CLASSES}; secrets, mutable state and generated data "
            "must never be tracked"
        )
    host_path = entry.get("host_path")
    if not isinstance(host_path, str):
        errors.append(f"{path_value}: entry.host_path must be a string")
    elif host_path and normalize_relative_path(host_path) is None:
        errors.append(
            f"{path_value}: entry.host_path must be a canonical relative POSIX "
            "path under the host cell root (or '' while pending import); "
            "absolute paths, '..', backslashes and non-canonical spellings "
            "are refused"
        )
    if not isinstance(entry.get("origin"), str) or not entry.get("origin"):
        errors.append(f"{path_value}: entry.origin is required")
    return errors


def _resolves_inside(target: Path, root: Path) -> bool:
    """True when ``target`` resolves to a location inside ``root``."""
    try:
        resolved = target.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or resolved.is_relative_to(base)


def symlinked_path_components(path: Path) -> list[str]:
    """Symlinked components of the supplied ``path`` — anchor through final.

    ``Path.is_symlink()`` inspects only the final component: a root supplied
    *through* a symlinked parent directory (``/tmp/link/cell`` where
    ``/tmp/link`` is a symlink) is reached via a symlink like any other path.
    Every component of the path **as spelled** is checked without following
    any of them.  For a relative path the process working directory is the
    anchor and is not itself inspected — the policy covers the spelling the
    caller supplied.  Inspection **stops at the first symlinked component**:
    checking any later component would have to resolve the link to reach it,
    which the policy forbids.  The returned list names that first offending
    component (at most one).
    """
    found: list[str] = []
    if path.is_absolute():
        current = Path(path.anchor)
        remaining = path.parts[1:]
    else:
        current = Path(".")
        remaining = path.parts
    for part in remaining:
        current = current / part
        if current.is_symlink():
            found.append(current.as_posix())
            break
    return found


def symlinked_components(target: Path, root: Path) -> list[str]:
    """Root-relative names of symlinked components on the way to ``target``.

    The supplied ``root`` itself is inspected first — together with its own
    ancestors, via :func:`symlinked_path_components` — and reported as
    ``ROOT_MARKER`` when the root path is reached through a symlink: every
    path under such a root is refused.  Otherwise **every** component
    below the root — the intermediate directories included — is checked without
    following any of them: a target reached through a symlinked directory is
    refused even when that directory resolves inside the trusted root.
    ``target.is_symlink()`` alone is not enough (it inspects only the final
    component) and ``resolve().is_relative_to()`` alone is not enough (an
    in-root redirect passes it).  The final component is included, so this
    subsumes a plain "target is a symlink" check.  Inspection stops at the
    first symlinked component — when the root path is itself reached through
    a symlink, nothing below it is inspected at all (reaching it would follow
    the link).
    """
    found: list[str] = []
    if symlinked_path_components(root):
        found.append(ROOT_MARKER)
        # Everything below the root can only be reached through that symlink;
        # not even link detection may follow it.
        return found
    try:
        relative = target.relative_to(root)
    except ValueError:
        return found + [str(target)]
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            found.append(current.relative_to(root).as_posix())
            break
    return found


def verify_tree(root: Path, manifest: dict) -> list[str]:
    """All policy violations of ``root`` against ``manifest`` (empty = conforming)."""
    errors: list[str] = []
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(
            f"manifest_version must be {MANIFEST_VERSION}, "
            f"found {manifest.get('manifest_version')!r}"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return errors + ["manifest.entries must be a list"]

    seen: set[str] = set()
    for entry in entries:
        errors.extend(_entry_errors(entry))
        if isinstance(entry, dict):
            path_value = entry.get("path")
            if isinstance(path_value, str):
                if path_value in seen:
                    errors.append(f"{path_value}: duplicate manifest entry")
                seen.add(path_value)

    files, unsafe = scan_tracked(root)
    for path_value in unsafe:
        if path_value not in seen:
            errors.append(
                f"{path_value}: symlinks and non-regular files are forbidden "
                "in the Layer R tree (nothing behind a symlink is followed)"
            )

    for path_value in sorted(seen):
        target = root / path_value
        entry = next(
            (e for e in entries if isinstance(e, dict) and e.get("path") == path_value),
            None,
        )
        linked = symlinked_components(target, root)
        if linked:
            errors.append(
                f"{path_value}: reached through symlinked component(s) "
                f"{', '.join(linked)}; symlinks are forbidden in the Layer R "
                "tree and nothing behind them is ever hashed or read"
            )
            continue
        if not target.exists():
            errors.append(f"{path_value}: listed in the manifest but missing")
            continue
        if not target.is_file():
            errors.append(f"{path_value}: not a regular file")
            continue
        if not _resolves_inside(target, root):
            errors.append(
                f"{path_value}: resolves outside the repository root and is refused"
            )
            continue
        if entry is None:
            continue
        expected = entry.get("sha256")
        if isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected):
            observed = sha256_of(target)
            if observed != expected:
                errors.append(
                    f"{path_value}: content drifted from the manifest "
                    f"({observed[:12]}… != {expected[:12]}…)"
                )
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(
                f"{path_value}: not readable as UTF-8 text "
                f"({error.__class__.__name__}); binary content is refused"
            )
            continue
        for finding in secret_findings(text):
            errors.append(f"{path_value}: secret-looking content ({finding})")

    for path_value in files:
        if path_value not in seen:
            errors.append(f"{path_value}: present in the tree but not in the manifest")
    return errors


def generate_manifest(
    root: Path,
    *,
    previous: dict | None = None,
    class_for: dict[str, str] | None = None,
    default_class: str | None = None,
    default_origin: str = "pending-owner-import",
) -> dict:
    """Build the manifest for ``root``; classification must be explicit.

    Class, ``host_path`` and ``origin`` of previously listed files are carried
    over.  A new file takes its class from ``class_for`` or ``default_class``;
    without either, generation fails closed rather than guessing.  Symlinks and
    non-regular files are refused outright — they have no safe semantics here.
    """
    class_for = class_for or {}
    known: dict[str, dict] = {}
    if previous is not None:
        for entry in previous.get("entries", []):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                known[entry["path"]] = entry

    paths, unsafe = scan_tracked(root)
    if unsafe:
        message = (
            "symlinks and non-regular files are forbidden in the Layer R tree: "
            + ", ".join(unsafe)
        )
        raise ManifestError(message)

    entries: list[dict] = []
    for path_value in paths:
        prior = known.get(path_value)
        if prior is not None:
            classification = str(prior.get("class"))
            host_path = str(prior.get("host_path", ""))
            origin = str(prior.get("origin", default_origin))
        else:
            classification = class_for.get(path_value) or default_class or ""
            if classification not in ALLOWED_CLASSES:
                message = (
                    f"{path_value}: new file has no explicit class; pass "
                    "--class-for PATH=CLASS or --default-class "
                    f"({', '.join(ALLOWED_CLASSES)})"
                )
                raise ManifestError(message)
            host_path = ""
            origin = default_origin
        entries.append(
            {
                "path": path_value,
                "sha256": sha256_of(root / path_value),
                "class": classification,
                "host_path": host_path,
                "origin": origin,
            }
        )
    entries.sort(key=lambda entry: entry["path"])
    return {
        "manifest_version": MANIFEST_VERSION,
        "layer_r_tree": "layer-r",
        "tracked_roots": list(TRACKED_ROOTS),
        "entries": entries,
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _parse_class_for(values: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for value in values:
        path_value, separator, classification = value.partition("=")
        if not separator or not path_value:
            message = f"--class-for expects PATH=CLASS, got {value!r}"
            raise ManifestError(message)
        mapping[path_value] = classification
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("verify", "generate"))
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST_PATH,
        help=f"manifest path relative to --root (default: {DEFAULT_MANIFEST_PATH})",
    )
    parser.add_argument(
        "--class-for",
        action="append",
        default=[],
        metavar="PATH=CLASS",
        help="explicit class of one new file (repeatable)",
    )
    parser.add_argument(
        "--default-class",
        default=None,
        choices=ALLOWED_CLASSES,
        help="class applied to every new file (explicit operator choice)",
    )
    arguments = parser.parse_args(argv)

    root = Path(arguments.root).resolve()
    manifest_path = root / arguments.manifest
    try:
        if arguments.command == "verify":
            if not manifest_path.is_file():
                message = f"{manifest_path}: manifest not found"
                raise ManifestError(message)
            errors = verify_tree(root, load_manifest(manifest_path))
            if errors:
                for error in errors:
                    sys.stderr.write(error + "\n")
                sys.stderr.write(
                    f"layer-r manifest verification FAILED ({len(errors)})\n"
                )
                return 1
            sys.stdout.write("layer-r manifest verification OK\n")
            return 0

        previous = load_manifest(manifest_path) if manifest_path.is_file() else None
        manifest = generate_manifest(
            root,
            previous=previous,
            class_for=_parse_class_for(arguments.class_for),
            default_class=arguments.default_class,
        )
        write_manifest(manifest_path, manifest)
        sys.stdout.write(
            f"{manifest_path}: manifest written ({len(manifest['entries'])} entries)\n"
        )
        return 0
    except ManifestError as error:
        sys.stderr.write(str(error) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
