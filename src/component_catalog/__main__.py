"""Command line entry point for the Component Catalog.

``python -m component_catalog build`` regenerates the canonical catalog
document from the canonical registry and the published contracts.

``python -m component_catalog validate`` verifies that the stored catalog
still is the deterministic derived view of its canonical sources.
"""

from __future__ import annotations

from component_catalog.catalog import load_catalog
from component_catalog.derive import write_catalog
from component_catalog.errors import CatalogError
from component_registry import load_registry
from component_registry.errors import RegistryError
from component_registry.registry import discover_root


def _build() -> int:
    root = discover_root()
    registry = load_registry(root)
    path = write_catalog(registry, root)
    print(
        f"component catalog written: {path} ({len(registry.component_ids)} components)"
    )
    return 0


def _validate() -> int:
    try:
        catalog = load_catalog(root=None, strict=False)
    except (CatalogError, RegistryError) as error:
        print(f"component catalog: {error.__class__.__name__}: {error}")
        return 1
    errors = catalog.validate()
    if errors:
        print(f"invalid component catalog ({len(errors)} errors):")
        for item in errors:
            print(f"  - {item}")
        return 1
    print(
        f"component catalog: valid "
        f"({len(catalog.component_ids)} components, derived from the canonical registry)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the catalog command line tool; return the process exit code."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="component_catalog",
        description="Build and validate the derived Component Catalog (Slice B).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "build",
        help="regenerate the canonical catalog from the canonical registry",
    )
    subparsers.add_parser(
        "validate",
        help="validate the canonical catalog against its canonical sources",
    )
    arguments = parser.parse_args(argv)

    if arguments.command == "build":
        return _build()
    return _validate()


if __name__ == "__main__":
    raise SystemExit(main())
