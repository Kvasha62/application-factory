"""Export the owner-side Layer R cell source from the host — read-only.

Run by the Running Platform owner **on the host** (migration step M1 of
``layer-r/README.md``).  It reads ``<RP_CELL_HOME>`` (for example
``/srv/running-platform/cell``) and writes a reviewable bundle elsewhere —
never into the cell root, never mutating a single byte of the host tree.

Fail-closed by policy: any file whose content looks like a secret aborts the
export before anything is copied.  Secrets, mutable runtime state and
generated/runtime data are out of scope of the bundle by construction (see the
classification in ``layer-r/README.md``).

Usage::

    python3 scripts/layer_r_export_bundle.py \\
        --cell-root /srv/running-platform/cell \\
        --out      /tmp/layer-r-export \\
        --exporter <handle>

Then: review the bundle, place the files under ``layer-r/source`` and
``layer-r/config``, run ``scripts/layer_r_manifest.py generate``, and open the
import PR.  ``--dry-run`` lists what would be exported without writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.layer_r_manifest import SECRET_PATTERNS

DEFAULT_INCLUDES = ("*.py", "*.json", "*.toml", "*.yaml", "*.yml", "*.md", "*.txt")
SUGGESTED_CLASS_BY_SUFFIX = {
    ".py": "source",
}
DEFAULT_SUGGESTED_CLASS = "deployable-configuration"

_SKIP_DIRS = {".git", "__pycache__", "state", "evidence"}
_SKIP_SUFFIXES = {".pyc", ".log"}
EXPORT_MANIFEST_NAME = "export-manifest.json"


class ExportRefused(Exception):
    """The export cannot proceed without violating the Layer R source policy."""


def _looks_like_secret(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def collect(cell_root: Path, includes: tuple[str, ...]) -> list[str]:
    """Relative POSIX paths of exportable files under ``cell_root``."""
    found: list[str] = []
    for path in sorted(cell_root.rglob("*")):
        relative = path.relative_to(cell_root)
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        if not path.is_file() or path.suffix in _SKIP_SUFFIXES:
            continue
        if any(path.match(pattern) for pattern in includes):
            found.append(relative.as_posix())
    return found


def scan_for_secrets(cell_root: Path, relatives: list[str]) -> list[str]:
    """Violation lines for every secret-looking or unreadable file (empty = clean)."""
    violations: list[str] = []
    for relative in relatives:
        target = cell_root / relative
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            violations.append(
                f"{relative}: not readable as UTF-8 text ({error.__class__.__name__})"
            )
            continue
        for finding in _looks_like_secret(text):
            violations.append(f"{relative}: secret-looking content ({finding})")
    return violations


def export(
    cell_root: Path,
    out_root: Path,
    *,
    includes: tuple[str, ...] = DEFAULT_INCLUDES,
    exporter: str = "unknown",
    dry_run: bool = False,
) -> dict:
    """Copy the exportable files into ``out_root`` and describe the bundle.

    Two passes: the whole tree is scanned first, and any violation aborts the
    export before any copy happens.  Nothing is ever written under
    ``cell_root``; ``out_root`` must not live inside it.
    """
    if not cell_root.is_dir():
        message = f"cell root {cell_root} is not a directory"
        raise ExportRefused(message)
    resolved_out = out_root.resolve()
    resolved_cell = cell_root.resolve()
    if resolved_out == resolved_cell or resolved_cell in resolved_out.parents:
        message = f"out {out_root} must not be inside the cell root {cell_root}"
        raise ExportRefused(message)

    relatives = collect(cell_root, includes)
    violations = scan_for_secrets(cell_root, relatives)
    if violations:
        raise ExportRefused(
            "export refused; resolve these first:\n" + "\n".join(violations)
        )

    files: list[dict] = []
    for relative in relatives:
        source = cell_root / relative
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        classification = SUGGESTED_CLASS_BY_SUFFIX.get(
            source.suffix, DEFAULT_SUGGESTED_CLASS
        )
        files.append(
            {
                "path": relative,
                "sha256": digest,
                "size": source.stat().st_size,
                "suggested_class": classification,
            }
        )
        if not dry_run:
            destination = out_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    bundle = {
        "exported_at": datetime.now(UTC).isoformat(),
        "cell_root": str(cell_root),
        "exporter": exporter,
        "includes": list(includes),
        "files": files,
    }
    if not dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / EXPORT_MANIFEST_NAME).write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cell-root", required=True, help="host cell root (read-only)")
    parser.add_argument(
        "--out", required=True, help="bundle destination (outside the cell)"
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help=f"file glob to export (repeatable; default: {' '.join(DEFAULT_INCLUDES)})",
    )
    parser.add_argument(
        "--exporter", default="unknown", help="handle recorded in the bundle"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list the bundle without writing"
    )
    arguments = parser.parse_args(argv)

    includes = tuple(arguments.include) or DEFAULT_INCLUDES
    try:
        bundle = export(
            Path(arguments.cell_root),
            Path(arguments.out),
            includes=includes,
            exporter=arguments.exporter,
            dry_run=arguments.dry_run,
        )
    except ExportRefused as error:
        sys.stderr.write(str(error) + "\n")
        return 1

    mode = "would export" if arguments.dry_run else "exported"
    sys.stdout.write(
        f"{mode} {len(bundle['files'])} files from {bundle['cell_root']}\n"
    )
    for entry in bundle["files"]:
        sys.stdout.write(f"  {entry['sha256'][:12]}  {entry['path']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
