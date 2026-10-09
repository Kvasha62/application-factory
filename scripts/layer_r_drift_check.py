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

Exit codes: ``0`` in sync · ``1`` drift (changed / missing / extra files) ·
``2`` usage or read error.  Output is stdout only (``--json`` for machine
form); nothing is ever created, modified or removed under ``--host-root``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

IN_SYNC = "in-sync"
CHANGED = "changed"
MISSING = "missing"
EXTRA = "extra"

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
    relative = Path(host_path)
    if relative.is_absolute() or ".." in relative.parts:
        message = (
            f"manifest host_path {host_path!r} must be relative to the host "
            "cell root and must not contain '..'"
        )
        raise DriftCheckError(message)
    return host_root / relative


def _walk_host_files(host_root: Path) -> set[str]:
    found: set[str] = set()
    for path in host_root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(host_root).parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        found.add(path.relative_to(host_root).as_posix())
    return found


def compare(host_root: Path, manifest: dict) -> dict:
    """Compare ``host_root`` against ``manifest``; read-only by construction."""
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
        if not target.is_file():
            results.append(
                {"path": repo_path, "host_path": host_path, "status": MISSING}
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

    for extra in sorted(_walk_host_files(host_root) - expected):
        results.append({"path": "", "host_path": extra, "status": EXTRA})

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
