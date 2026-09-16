"""CLI for the Composer (Slice E).

Usage:

    python -m composer validate [path]
    python -m composer compose <path> [--out PATH]
    python -m composer build-example [path]

``validate`` without a path checks that the normative composition request
schema is loadable. With a path it reports the complete deterministic list of
request and composition violations. ``compose`` produces the draft Platform
Manifest and its composition report. ``build-example`` regenerates the
deterministic example request from the authoritative Component Registry — the
example is derived, never a second source of truth.

The CLI uses the standard library plus the factory tooling of Slice A/D (the
authoritative registry, published contracts and the Golden Bundle document
reader). It never touches component internals, a component database or
business data.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from component_registry import load_registry
from composer import (
    EXAMPLE_REQUEST_PATH,
    ComposerError,
    Composition,
    CompositionRejectedError,
    build_request_document,
    compose_diagnostics,
    compose_request_path,
    discover_root,
    load_request_document,
    render_request_document,
    resolve_closure,
)
from composer.resolution import Resolution
from composer.schema import load_schema
from composer.verification import contract_path, read_published_contract

#: Identity of the generated example composition.
EXAMPLE_MANIFEST_ID = "example-platform"
EXAMPLE_MANIFEST_VERSION = "1.0.0"

#: Values the example states for the configuration keys components require. A
#: required key with no value here is a loud failure, never a guessed value.
EXAMPLE_CONFIGURATION_VALUES = {
    "platform_id": "example-platform",
    "current_platform_id": "example-platform",
}


def _cmd_validate(args: argparse.Namespace) -> int:
    root = discover_root()
    if not args.path:
        try:
            load_schema(root)
        except (FileNotFoundError, TypeError, ValueError, KeyError) as exc:
            print(f"schema load failed: {exc}", file=sys.stderr)
            return 1
        print("composition request schema is loadable and valid")
        return 0

    try:
        document = load_request_document(Path(args.path))
    except ComposerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors = compose_diagnostics(document, root=root)
    if errors:
        print(f"composition request is INVALID: {args.path}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"composition request is valid and composable: {args.path}")
    return 0


def _composition_report(composition: Composition) -> str:
    lines = [
        f"platform manifest: {composition.manifest_id} {composition.manifest_version}",
        f"manifest digest: {composition.manifest_digest}",
        f"lifecycle: {composition.lifecycle_state}",
        f"components ({len(composition.component_ids)}):",
    ]
    for decision in composition.decisions:
        required_by = ",".join(decision["required_by"]) or "-"
        lines.append(
            f"  {decision['component_id']} {decision['component_version']} "
            f"[{decision['selected_as']} {decision['constraint'] or '-'}] "
            f"required_by={required_by}"
        )
    if composition.golden_bundle:
        bundle = composition.golden_bundle
        lines.append(
            f"golden bundle: {bundle['bundle_id']} {bundle['bundle_version']} "
            f"{bundle['bundle_digest']} (certified)"
        )
    else:
        lines.append(
            "golden bundle: none (explicitly uncertified, ARCHITECTURE.md §31)"
        )
    return "\n".join(lines)


def _cmd_compose(args: argparse.Namespace) -> int:
    root = discover_root()
    try:
        composition = compose_request_path(Path(args.path), root=root)
    except CompositionRejectedError as exc:
        print("composition rejected:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    except ComposerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(_composition_report(composition))

    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(composition.render(), encoding="utf-8")
        print(f"draft platform manifest written to {output}")

    return 0


def _example_configuration(
    root: Path, registry: Any, resolution: Resolution
) -> dict[str, dict[str, object]]:
    """State a value for every configuration key the composed set requires."""
    configuration: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for component in resolution.selected:
        entry = registry.entry(component.component_id)
        contract, failure = read_published_contract(root, contract_path(entry))
        if contract is None:
            raise ComposerError(
                f"cannot build the example request: {component.component_id} — {failure}"
            )
        schema = contract.get("configuration_schema")
        required = schema.get("required") if isinstance(schema, dict) else None
        values: dict[str, object] = {}
        for key in required or []:
            if key not in EXAMPLE_CONFIGURATION_VALUES:
                missing.append(f"{component.component_id}.{key}")
                continue
            values[key] = EXAMPLE_CONFIGURATION_VALUES[key]
        if values:
            configuration[component.component_id] = values
    if missing:
        raise ComposerError(
            "cannot build the example request: no example value is defined for "
            f"{', '.join(sorted(missing))}"
        )
    return configuration


def _cmd_build_example(args: argparse.Namespace) -> int:
    """Build the deterministic example request from the registry.

    The example explicitly requests the registered Business Systems and lets the
    Composer resolve the declared dependency closure (ARCHITECTURE.md §17), so it
    demonstrates deterministic resolution rather than a hand-written component
    list.
    """
    root = discover_root()
    registry = load_registry(root)

    business_systems = sorted(
        entry["component_id"]
        for entry in registry.entries
        if entry.get("class") == "business_system"
        and isinstance(entry.get("component_id"), str)
    )
    if not business_systems:
        print("no Business System is registered", file=sys.stderr)
        return 1

    components = [
        {
            "component_id": component_id,
            "component_version": registry.version(component_id),
        }
        for component_id in business_systems
    ]
    provisional = build_request_document(
        manifest_id=EXAMPLE_MANIFEST_ID,
        manifest_version=EXAMPLE_MANIFEST_VERSION,
        components=components,
    )

    try:
        resolution, resolution_errors = resolve_closure(
            registry, provisional["components"]
        )
        if resolution_errors:
            print("cannot resolve the example composition:", file=sys.stderr)
            for error in resolution_errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        configuration = _example_configuration(root, registry, resolution)
    except ComposerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    document = build_request_document(
        manifest_id=EXAMPLE_MANIFEST_ID,
        manifest_version=EXAMPLE_MANIFEST_VERSION,
        components=components,
        configuration=configuration or None,
    )

    problems = compose_diagnostics(document, root=root)
    if problems:
        print("built example request cannot be composed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    output = Path(args.path) if args.path else root / EXAMPLE_REQUEST_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_request_document(document), encoding="utf-8")
    print(f"example composition request written to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="composer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser(
        "validate", help="validate a composition request (or the request schema)"
    )
    p_validate.add_argument("path", nargs="?", help="path to composition request JSON")
    p_validate.set_defaults(func=_cmd_validate)

    p_compose = sub.add_parser("compose", help="compose a draft Platform Manifest")
    p_compose.add_argument("path", help="path to composition request JSON")
    p_compose.add_argument("--out", help="write the produced manifest to this path")
    p_compose.set_defaults(func=_cmd_compose)

    p_build = sub.add_parser(
        "build-example", help="build the example request from the registry"
    )
    p_build.add_argument("path", nargs="?", help="output path")
    p_build.set_defaults(func=_cmd_build_example)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
