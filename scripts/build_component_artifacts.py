"""Build deterministic source-package artifacts for the nine registered components."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

from component_registry import load_registry
from factory_artifact import describe_source_package, registry_metadata
from factory_artifact.source_package import build_source_package_canonical

COMPONENT_ROOTS = {
    "authorization": Path("src/authorization_service"),
    "identity": Path("src/identity_service"),
    "tenant_authority": Path("src/tenant_authority"),
    "records": Path("src/records_service"),
    "learning": Path("src/learning_service"),
    "saga": Path("src/saga"),
    "idempotency": Path("src/idempotency"),
    "commerce": Path("src/commerce_service"),
    "booking": Path("src/booking_service"),
}

_EXECUTABLE_SUFFIXES = {
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
}
_MACH_O_MAGICS = {
    bytes.fromhex("feedfacf"),
    bytes.fromhex("feedface"),
    bytes.fromhex("cafebabe"),
}


def _physical_executable(path: Path) -> bool:
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        return True
    prefix = path.read_bytes()[:4]
    if prefix.startswith((b"\x7fELF", b"MZ", b"#!")):
        return True
    if prefix in _MACH_O_MAGICS:
        return True
    return path.suffix in _EXECUTABLE_SUFFIXES


def declaration_for(root: Path) -> dict[str, dict[str, bool]]:
    """Derive the complete Factory declaration for one sealed source root."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"component source root is missing: {root}")

    declaration: dict[str, dict[str, bool]] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            declaration[relative] = {"owned": True, "executable": False}
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            declaration[relative] = {
                "owned": True,
                "executable": _physical_executable(path),
            }
    return declaration


def build_all(repository_root: Path, output_root: Path) -> list[dict[str, Any]]:
    """Build canonical manifests for exactly the registered nine components."""
    repository_root = Path(repository_root)
    output_root = Path(output_root)
    registry = load_registry(root=repository_root)
    registered = set(registry.component_ids)
    expected = set(COMPONENT_ROOTS)
    if registered != expected:
        missing = sorted(expected - registered)
        extra = sorted(registered - expected)
        raise ValueError(f"component set mismatch: missing={missing!r}, extra={extra!r}")

    results: list[dict[str, Any]] = []
    for component_id in sorted(COMPONENT_ROOTS):
        root = repository_root / COMPONENT_ROOTS[component_id]
        declaration = declaration_for(root)
        descriptor = describe_source_package(root, declaration)
        metadata = registry_metadata(descriptor)
        canonical_dir = output_root / descriptor.digest / "canonical.json"
        canonical_dir.parent.mkdir(parents=True, exist_ok=True)

        canonical, canonical_bytes, recomputed = build_source_package_canonical(
            root, declaration
        )
        if recomputed != descriptor.digest:
            raise ValueError(
                f"{component_id}: descriptor digest differs from canonical digest"
            )
        canonical_dir.write_bytes(canonical_bytes)
        results.append(
            {
                "component_id": component_id,
                "component_version": registry.version(component_id),
                "artifact": metadata,
                "source_root": COMPONENT_ROOTS[component_id].as_posix(),
                "canonical": canonical,
            }
        )

    manifest_path = output_root / "publication-manifest.json"
    manifest_path.write_text(
        json.dumps(results, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("factory/artifacts"))
    args = parser.parse_args()
    build_all(args.root, args.root / args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
