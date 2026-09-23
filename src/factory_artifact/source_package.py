from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class SourcePackageError(ValueError):
    """Raised when a source package violates the Factory contract."""


_EXECUTABLE_SUFFIXES = (
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
)

_MACH_O_MAGICS = {
    bytes.fromhex("feedfacf"),
    bytes.fromhex("feedface"),
    bytes.fromhex("cafebabe"),
}


def build_source_package_canonical(
    root: Path,
    factory_declaration: Mapping[str, Mapping[str, bool]],
) -> tuple[dict[str, Any], bytes, str]:
    """Build the canonical representation for ``source_package/v1``."""
    root = Path(root)
    if not root.is_dir():
        raise SourcePackageError("sealed artifact root must be a directory")

    physical = _enumerate(root)
    _validate_declaration(physical, factory_declaration)

    entries = []
    for path in sorted(physical, key=lambda value: value.encode("utf-8")):
        item = physical[path]
        declaration = factory_declaration[path]
        executable = bool(declaration["executable"])
        owned = bool(declaration["owned"])

        if item["type"] == "symlink" and executable:
            raise SourcePackageError(
                f"{path}: symlink entries must have executable=false"
            )

        physical_executable = item["physical_executable"]
        if physical_executable and not executable:
            raise SourcePackageError(
                f"{path}: physical executable content cannot be declared "
                "executable=false"
            )
        if executable and not owned:
            raise SourcePackageError(
                f"{path}: executable content must be component-owned"
            )

        entry = {
            "path": path,
            "type": item["type"],
            "executable": executable,
            "owned": owned,
            "content_digest": item["content_digest"],
            "symlink": item["symlink"],
        }
        entries.append(entry)

    _validate_symlinks(physical)

    canonical: dict[str, Any] = {
        "form": "source_package/v1",
        "entries": entries,
    }
    canonical_bytes = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    artifact_digest = "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()
    return canonical, canonical_bytes, artifact_digest


def _enumerate(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def visit(directory: Path, prefix: str) -> None:
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise SourcePackageError(f"cannot enumerate directory {directory}") from exc

        for child in children:
            path = f"{prefix}/{child.name}" if prefix else child.name
            _validate_path(path)

            if path == "canonical.json":
                raise SourcePackageError(
                    "canonical.json must not be part of sealed artifact entries"
                )

            if path in result:
                raise SourcePackageError(f"duplicate path: {path}")

            if child.is_symlink():
                target = os.readlink(child.path)
                result[path] = {
                    "type": "symlink",
                    "content_digest": None,
                    "symlink": target,
                    "physical_executable": False,
                }
            elif child.is_dir(follow_symlinks=False):
                result[path] = {
                    "type": "dir",
                    "content_digest": None,
                    "symlink": None,
                    "physical_executable": False,
                }
                visit(Path(child.path), path)
            elif child.is_file(follow_symlinks=False):
                result[path] = {
                    "type": "file",
                    "content_digest": _file_digest(Path(child.path)),
                    "symlink": None,
                    "physical_executable": _physical_executable(
                        Path(child.path), child
                    ),
                }
            else:
                raise SourcePackageError(f"unsupported filesystem entry: {path}")

    visit(root, "")
    _validate_collisions(result)
    return result


def _validate_path(path: str) -> None:
    if not path or "\x00" in path:
        raise SourcePackageError(f"invalid path: {path!r}")
    if "\\" in path or "//" in path:
        raise SourcePackageError(f"invalid path: {path!r}")
    if path.startswith("/") or path in {".", ".."}:
        raise SourcePackageError(f"invalid path: {path!r}")
    if any(part in {".", ".."} for part in path.split("/")):
        raise SourcePackageError(f"invalid path: {path!r}")
    if unicodedata.normalize("NFC", path) != path:
        raise SourcePackageError(f"path is not Unicode NFC: {path!r}")


def _validate_collisions(entries: Mapping[str, object]) -> None:
    paths = set(entries)
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in paths and entries[parent]["type"] == "file":
                raise SourcePackageError(
                    f"file/directory collision: {parent!r} / {path!r}"
                )


def _validate_declaration(
    physical: Mapping[str, object],
    declaration: Mapping[str, Mapping[str, bool]],
) -> None:
    if set(physical) != set(declaration):
        missing = sorted(set(physical) - set(declaration))
        extra = sorted(set(declaration) - set(physical))
        raise SourcePackageError(
            f"Factory declaration mismatch: missing={missing!r}, extra={extra!r}"
        )

    for path, value in declaration.items():
        if not isinstance(value, Mapping):
            raise SourcePackageError(f"{path}: declaration must be a mapping")
        if set(value) != {"owned", "executable"}:
            raise SourcePackageError(
                f"{path}: declaration must contain exactly owned and executable"
            )
        if type(value["owned"]) is not bool:
            raise SourcePackageError(f"{path}: owned must be bool")
        if type(value["executable"]) is not bool:
            raise SourcePackageError(f"{path}: executable must be bool")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SourcePackageError(f"cannot read file: {path}") from exc
    return "sha256:" + digest.hexdigest()


def _physical_executable(path: Path, entry: os.DirEntry[str]) -> bool:
    try:
        mode = entry.stat(follow_symlinks=False).st_mode
        if mode & 0o111:
            return True
        with path.open("rb") as stream:
            prefix = stream.read(4)
    except OSError as exc:
        raise SourcePackageError(f"cannot inspect file: {path}") from exc

    if prefix.startswith((b"\x7fELF", b"MZ")):
        return True
    if prefix in _MACH_O_MAGICS:
        return True
    if prefix.startswith(b"#!"):
        return True
    return path.suffix in _EXECUTABLE_SUFFIXES


def _validate_symlinks(entries: Mapping[str, Mapping[str, Any]]) -> None:
    for path, entry in entries.items():
        if entry["type"] != "symlink":
            continue

        target = entry["symlink"]
        if not target or target.startswith("/") or "\\" in target:
            raise SourcePackageError(f"{path}: symlink target must be POSIX-relative")

        parent = PurePosixPath(path).parent
        resolved = PurePosixPath(parent, target)

        parts: list[str] = []
        for part in resolved.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    raise SourcePackageError(
                        f"{path}: symlink target escapes artifact root"
                    )
                parts.pop()
            else:
                parts.append(part)

        target_path = "/".join(parts)
        if target_path not in entries:
            raise SourcePackageError(f"{path}: dangling symlink")

        target_entry = entries[target_path]
        if target_entry["type"] not in {"file", "dir", "symlink"}:
            raise SourcePackageError(f"{path}: invalid symlink target")

    state: dict[str, int] = {}

    def visit(path: str) -> None:
        if entries[path]["type"] != "symlink":
            return
        current = state.get(path, 0)
        if current == 1:
            raise SourcePackageError(f"symlink cycle detected at {path}")
        if current == 2:
            return

        state[path] = 1
        target = _resolve_symlink(path, entries[path]["symlink"])
        if entries[target]["type"] == "symlink":
            visit(target)
        state[path] = 2

    for path in entries:
        visit(path)


def _resolve_symlink(path: str, target: str) -> str:
    parent = PurePosixPath(path).parent
    resolved = PurePosixPath(parent, target)
    parts: list[str] = []
    for part in resolved.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise SourcePackageError(
                    f"{path}: symlink target escapes artifact root"
                )
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)
