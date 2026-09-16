"""CLI for Platform Instance assembly (Slice F).

Usage:

    python -m platform_instance validate <instance-path> <manifest-path>
    python -m platform_instance digest <instance-path>
    python -m platform_instance assemble <manifest-path> --platform-id <id>

The CLI is intentionally minimal and uses only the standard library plus the
factory tooling of Slices C (Platform Manifest) and A (Component Registry,
behind manifest validation).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from platform_instance import (
    InstanceNotFoundError,
    PlatformInstanceError,
    assemble_manifest_path,
    discover_root,
    load_instance_document,
    validate_instance_document,
)
from platform_manifest import load_manifest_document


def _cmd_validate(args: argparse.Namespace) -> int:
    root = discover_root()
    try:
        instance = load_instance_document(Path(args.instance))
        manifest = load_manifest_document(Path(args.manifest))
    except (InstanceNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors = validate_instance_document(instance, manifest, root=root)
    if errors:
        print(f"platform instance is INVALID: {args.instance}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"platform instance is valid: {args.instance}")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    try:
        document = load_instance_document(Path(args.instance))
    except InstanceNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from platform_instance import compute_instance_digest

    print(compute_instance_digest(document))
    return 0


def _cmd_assemble(args: argparse.Namespace) -> int:
    try:
        instance = assemble_manifest_path(
            Path(args.manifest), platform_id=args.platform_id
        )
    except PlatformInstanceError as exc:
        print(f"assembly rejected:\n{exc}", file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(instance.render(), encoding="utf-8")
        print(f"assembled platform instance written: {args.output}")
    else:
        sys.stdout.write(instance.render())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform_instance",
        description="Platform Instance assembly (Slice F)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate an instance document")
    validate.add_argument("instance")
    validate.add_argument("manifest")
    validate.set_defaults(func=_cmd_validate)

    digest = sub.add_parser("digest", help="print the instance content digest")
    digest.add_argument("instance")
    digest.set_defaults(func=_cmd_digest)

    assemble = sub.add_parser("assemble", help="assemble an instance")
    assemble.add_argument("manifest")
    assemble.add_argument("--platform-id", required=True)
    assemble.add_argument("--output", default=None)
    assemble.set_defaults(func=_cmd_assemble)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
