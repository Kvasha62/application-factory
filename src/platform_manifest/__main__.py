"""CLI for Platform Manifest (Slice C).

Usage:

    python -m platform_manifest validate [path]
    python -m platform_manifest build-example [path]
    python -m platform_manifest digest [path]

The CLI is intentionally minimal and uses only the standard library plus the
Slice A/B factory tooling for reference validation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from component_registry import load_registry
from platform_manifest import (
    MANIFEST_DIR,
    ManifestNotFoundError,
    build_manifest_document,
    compute_manifest_digest,
    discover_root,
    load_manifest_document,
    render_manifest_document,
    validate_document,
)


def _cmd_validate(args: argparse.Namespace) -> int:
    root = discover_root()
    path = Path(args.path) if args.path else None
    if path is None:
        # Validate all example manifests if any exist, otherwise report.
        # For Slice C, we validate that the schema itself exists.
        from platform_manifest.schema import load_schema

        try:
            load_schema(root)
        except (FileNotFoundError, TypeError, ValueError, KeyError) as exc:
            print(f"schema load failed: {exc}", file=sys.stderr)
            return 1
        print("manifest schema is loadable and valid")
        return 0

    try:
        document = load_manifest_document(path)
    except ManifestNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors = validate_document(document, root=root)
    if errors:
        print(f"manifest is INVALID: {path}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"manifest is valid: {path}")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        document = load_manifest_document(path)
    except ManifestNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    digest = compute_manifest_digest(document)
    print(digest)
    return 0


def _cmd_build_example(args: argparse.Namespace) -> int:
    """Build a minimal example manifest from the current registry."""
    root = discover_root()
    registry = load_registry(root)

    components = []
    for entry in registry.entries:
        components.append(
            {
                "component_id": entry["component_id"],
                "component_version": entry["component_version"],
                "artifact": dict(entry["artifact"]),
            }
        )

    # Sort for reproducibility
    components = sorted(components, key=lambda e: e["component_id"])

    doc = build_manifest_document(
        manifest_id="example-platform",
        manifest_version="1.0.0",
        lifecycle_state="draft",
        components=components,
        predecessor=None,
    )

    # Validate before writing
    errors = validate_document(doc, root=root)
    if errors:
        print("built example is invalid:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    output = (
        Path(args.path) if args.path else root / MANIFEST_DIR / "example_manifest.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_manifest_document(doc), encoding="utf-8")
    print(f"example manifest written to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform_manifest")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate a manifest document")
    p_validate.add_argument("path", nargs="?", help="path to manifest JSON")
    p_validate.set_defaults(func=_cmd_validate)

    p_digest = sub.add_parser("digest", help="compute manifest digest")
    p_digest.add_argument("path", help="path to manifest JSON")
    p_digest.set_defaults(func=_cmd_digest)

    p_build = sub.add_parser(
        "build-example", help="build minimal example manifest from registry"
    )
    p_build.add_argument("path", nargs="?", help="output path")
    p_build.set_defaults(func=_cmd_build_example)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
