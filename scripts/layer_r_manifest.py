"""Generate and verify the committed Layer R source manifest.

The manifest (``layer-r/SOURCE_MANIFEST.json``) is the machine-readable link
between three things: the Layer R files committed to this repository, their
SHA-256 content identity, and the host path each file is deployed to on the
Running Platform host.  A release binds a deployed tree to one immutable Git
commit by definition: the manifest of that commit is the specification of the
deployed bytes, and :mod:`scripts.layer_r_drift_check` compares the host
against it without mutating anything.

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
import re
import sys
from pathlib import Path

MANIFEST_VERSION = 1
DEFAULT_MANIFEST_PATH = "layer-r/SOURCE_MANIFEST.json"
TRACKED_ROOTS = ("layer-r/source", "layer-r/config")
ALLOWED_CLASSES = ("source", "deployable-configuration", "host-specific-template")

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

_SKIP_NAMES = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc"}


class ManifestError(Exception):
    """The manifest or the tree violates the Layer R source policy."""


def sha256_of(path: Path) -> str:
    """Content identity of one file, read-only."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secret_findings(text: str) -> list[str]:
    """Names of secret patterns found in ``text`` (empty when clean)."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def _is_tracked_candidate(path: Path) -> bool:
    if path.name in _SKIP_NAMES or path.suffix in _SKIP_SUFFIXES:
        return False
    return path.is_file()


def tree_files(root: Path) -> list[str]:
    """Every regular file under the tracked roots, as root-relative POSIX paths."""
    found: list[str] = []
    for tracked in TRACKED_ROOTS:
        base = root / tracked
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if _is_tracked_candidate(path):
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


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
    if not path_value.startswith(TRACKED_ROOTS):
        errors.append(
            f"{path_value}: entry.path must live under one of {TRACKED_ROOTS}"
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
    else:
        host = Path(host_path)
        if host.is_absolute() or ".." in host.parts:
            errors.append(
                f"{path_value}: entry.host_path must be relative to the host "
                "cell root and must not contain '..'"
            )
    if not isinstance(entry.get("origin"), str) or not entry.get("origin"):
        errors.append(f"{path_value}: entry.origin is required")
    return errors


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

    for path_value in sorted(seen):
        target = root / path_value
        entry = next(
            (e for e in entries if isinstance(e, dict) and e.get("path") == path_value),
            None,
        )
        if not target.is_file():
            errors.append(f"{path_value}: listed in the manifest but missing")
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

    for path_value in tree_files(root):
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
    without either, generation fails closed rather than guessing.
    """
    class_for = class_for or {}
    known: dict[str, dict] = {}
    if previous is not None:
        for entry in previous.get("entries", []):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                known[entry["path"]] = entry

    entries: list[dict] = []
    for path_value in tree_files(root):
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
