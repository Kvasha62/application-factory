"""CLI for Golden Bundle (Slice D).

Usage:

    python -m golden_bundle validate [path]
    python -m golden_bundle digest [path]
    python -m golden_bundle build-example [path]

The CLI is intentionally minimal and uses only the standard library plus the
Slice A factory tooling for authoritative reference checks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from component_registry import load_registry
from component_registry.validation import DEPENDENCY_KINDS as REGISTRY_DEPENDENCY_KINDS
from golden_bundle import (
    BUNDLE_DIR,
    GoldenBundleNotFoundError,
    build_bundle_document,
    compute_bundle_digest,
    discover_root,
    load_bundle_document,
    render_bundle_document,
    validate_document,
)


def _cmd_validate(args: argparse.Namespace) -> int:
    root = discover_root()
    path = Path(args.path) if args.path else None
    if path is None:
        from golden_bundle.schema import load_schema

        try:
            load_schema(root)
        except (FileNotFoundError, TypeError, ValueError, KeyError) as exc:
            print(f"schema load failed: {exc}", file=sys.stderr)
            return 1
        print("golden bundle schema is loadable and valid")
        return 0

    try:
        document = load_bundle_document(path)
    except GoldenBundleNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors = validate_document(document, root=root)
    if errors:
        print(f"golden bundle is INVALID: {path}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"golden bundle is valid: {path}")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        document = load_bundle_document(path)
    except GoldenBundleNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    digest = compute_bundle_digest(document)
    print(digest)
    return 0


def _cmd_build_example(args: argparse.Namespace) -> int:
    """Build a minimal example bundle pinning the current registry inventory.

    The bundle is a *composition* artifact, not a registry: it is derived from
    the authoritative Component Registry at build time, but never stored as a
    second source of truth — regenerate it with this command or author a
    bundle explicitly by hand.
    """
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
    components = sorted(components, key=lambda e: e["component_id"])

    # Propagate the authoritative declared dependencies into the explicit
    # compatibility matrix: every dependency of a pinned component over a
    # pinned target becomes a validates-compatible pair. Nothing is resolved,
    # discovered or invented here — only declared edges are propagated.
    pinned_ids = {entry["component_id"] for entry in components}
    pairs = []
    for entry in registry.entries:
        for dependency in entry["dependencies"]:
            if dependency.get("component_id") in pinned_ids and (
                dependency.get("kind") in REGISTRY_DEPENDENCY_KINDS
            ):
                pairs.append(
                    {
                        "from": entry["component_id"],
                        "to": dependency["component_id"],
                        "mechanism": dependency["kind"],
                        "compatible": True,
                    }
                )

    doc = build_bundle_document(
        bundle_id="example-golden-bundle",
        bundle_version="1.0.0",
        lifecycle_state="candidate",
        components=components,
        compatibility_pairs=pairs,
        certification=None,
    )

    errors = validate_document(doc, root=root)
    if errors:
        print("built example is invalid:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    output = Path(args.path) if args.path else root / BUNDLE_DIR / "example_bundle.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_bundle_document(doc), encoding="utf-8")
    print(f"example golden bundle written to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="golden_bundle")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate a bundle document")
    p_validate.add_argument("path", nargs="?", help="path to bundle JSON")
    p_validate.set_defaults(func=_cmd_validate)

    p_digest = sub.add_parser("digest", help="compute bundle digest")
    p_digest.add_argument("path", help="path to bundle JSON")
    p_digest.set_defaults(func=_cmd_digest)

    p_build = sub.add_parser(
        "build-example", help="build minimal example bundle from the registry"
    )
    p_build.add_argument("path", nargs="?", help="output path")
    p_build.set_defaults(func=_cmd_build_example)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
