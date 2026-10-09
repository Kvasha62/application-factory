"""Read-only drift check: the deployed host tree vs a committed manifest.

The only evidence that the running host is synchronized with Git is a host-run
comparison of the deployed bytes against the manifest of one specific commit.
CI proves the committed tree is consistent; this tool proves — without writing
a single byte to the host — whether ``<RP_CELL_HOME>`` actually serves those
bytes.

Usage (on the Running Platform host, by its owner)::

    git -C ~/application-factory show <commit>:layer-r/SOURCE_MANIFEST.json \\
        > /tmp/manifest.json
    python3 scripts/layer_r_drift_check.py \\
        --host-root /srv/running-platform/cell --manifest /tmp/manifest.json

Path and symlink policy.  ``entry.host_path`` must be a canonical relative
POSIX path (see :func:`scripts.layer_r_manifest.normalize_relative_path`);
absolute paths, ``..`` and ambiguous spellings are refused.  A symlinked
target is **never followed or hashed** — it is reported as ``unsafe`` and
counts as drift, as does any entry that resolves outside the host root and any
non-regular file.  Symlinked directories are not descended into.

Exit codes: ``0`` in sync · ``1`` drift (changed / missing / extra / unsafe) ·
``2`` usage or read error.  Output is stdout only (``--json`` for machine
form); nothing is ever created, modified or removed under ``--host-root``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from scripts.layer_r_manifest import normalize_relative_path, symlinked_components

IN_SYNC = "in-sync"
CHANGED = "changed"
MISSING = "missing"
EXTRA = "extra"
UNSAFE = "unsafe"

_SKIP_DIRS = {".git", "__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".log"}


class DriftCheckError(Exception):
    """The check cannot be performed (usage or read failure)."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_path(host_root: Path, host_path: str) -> Path:
    canonical = normalize_relative_path(host_path)
    if canonical is None:
        message = (
            f"manifest host_path {host_path!r} must be a canonical relative POSIX "
            "path under the host cell root; absolute paths, '..', backslashes and "
            "non-canonical spellings are refused"
        )
        raise DriftCheckError(message)
    return host_root / canonical


def _walk_host(host_root: Path) -> tuple[set[str], set[str]]:
    """(regular files, unsafe paths) under ``host_root``; symlinks never followed."""
    found: set[str] = set()
    unsafe: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(host_root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in list(dirnames):
            entry = Path(dirpath) / name
            if entry.is_symlink():
                unsafe.add(entry.relative_to(host_root).as_posix())
                dirnames.remove(name)
        for name in filenames:
            entry = Path(dirpath) / name
            relative = entry.relative_to(host_root).as_posix()
            if entry.is_symlink() or not entry.is_file():
                unsafe.add(relative)
            elif entry.suffix in _SKIP_SUFFIXES:
                continue
            else:
                found.add(relative)
    return found, unsafe


def _resolves_inside(target: Path, host_root: Path) -> bool:
    try:
        resolved = target.resolve()
        base = host_root.resolve()
    except OSError:
        return False
    return resolved == base or resolved.is_relative_to(base)


def compare(host_root: Path, manifest: dict) -> dict:
    """Compare ``host_root`` against ``manifest``; read-only by construction."""
    if host_root.is_symlink():
        # ``is_dir`` would follow the root link and the walk below would read
        # and hash through it.  The supplied root itself must not be a symlink.
        message = (
            f"host root {host_root} is a symlink; nothing behind it "
            "is ever walked, read or hashed"
        )
        raise DriftCheckError(message)
    if not host_root.is_dir():
        message = f"host root {host_root} is not a directory"
        raise DriftCheckError(message)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        message = "manifest.entries must be a list"
        raise DriftCheckError(message)

    results: list[dict] = []
    expected: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            message = "every manifest entry must be a JSON object"
            raise DriftCheckError(message)
        host_path = entry.get("host_path")
        digest = entry.get("sha256")
        repo_path = entry.get("path", "")
        if not isinstance(host_path, str) or not host_path:
            # Not yet imported: nothing is deployed from this entry.
            results.append(
                {
                    "path": repo_path,
                    "host_path": "",
                    "status": IN_SYNC,
                    "note": "pending-import",
                }
            )
            continue
        target = _host_path(host_root, host_path)
        expected.add(Path(host_path).as_posix())
        linked = symlinked_components(target, host_root)
        if linked:
            results.append(
                {
                    "path": repo_path,
                    "host_path": host_path,
                    "status": UNSAFE,
                    "note": (
                        "reached through symlinked component(s) "
                        f"{', '.join(linked)}; never followed or hashed"
                    ),
                }
            )
            continue
        if not target.exists():
            results.append(
                {"path": repo_path, "host_path": host_path, "status": MISSING}
            )
            continue
        if not target.is_file():
            results.append(
                {
                    "path": repo_path,
                    "host_path": host_path,
                    "status": UNSAFE,
                    "note": "not a regular file",
                }
            )
            continue
        if not _resolves_inside(target, host_root):
            results.append(
                {
                    "path": repo_path,
                    "host_path": host_path,
                    "status": UNSAFE,
                    "note": "resolves outside the host root; never read",
                }
            )
            continue
        observed = sha256_of(target)
        status = IN_SYNC if observed == digest else CHANGED
        results.append(
            {
                "path": repo_path,
                "host_path": host_path,
                "status": status,
                "manifest_sha256": digest,
                "observed_sha256": observed,
            }
        )

    present, unsafe = _walk_host(host_root)
    for extra in sorted(present - expected):
        results.append({"path": "", "host_path": extra, "status": EXTRA})
    for path_value in sorted(unsafe - expected):
        results.append(
            {
                "path": "",
                "host_path": path_value,
                "status": UNSAFE,
                "note": "symlink or non-regular file on host",
            }
        )

    drifted = [row for row in results if row["status"] != IN_SYNC]
    return {
        "host_root": str(host_root),
        "in_sync": not drifted,
        "results": results,
        "drifted": drifted,
    }


def render(report: dict) -> str:
    lines = [f"host root: {report['host_root']}"]
    for row in report["results"]:
        note = row.get("note")
        suffix = f" ({note})" if note else ""
        lines.append(f"  {row['status']:>7}  {row['host_path'] or row['path']}{suffix}")
    verdict = (
        "IN SYNC" if report["in_sync"] else f"DRIFT ({len(report['drifted'])} files)"
    )
    lines.append(f"verdict: {verdict}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host-root", required=True, help="deployed cell root on this host"
    )
    parser.add_argument(
        "--manifest", required=True, help="SOURCE_MANIFEST.json of one commit"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    arguments = parser.parse_args(argv)

    host_root = Path(arguments.host_root)
    try:
        manifest = json.loads(Path(arguments.manifest).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            message = "manifest must be a JSON object"
            raise DriftCheckError(message)
        report = compare(host_root, manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, DriftCheckError) as error:
        sys.stderr.write(f"drift check refused: {error.__class__.__name__}: {error}\n")
        return 2

    if arguments.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render(report))
    return 0 if report["in_sync"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
