from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class ContainerImageError(ValueError):
    """Raised when a container image violates the Factory contract."""


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


def build_container_image_canonical(
    root: Path,
    factory_declaration: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, str, list[str]]:
    """Build the canonical representation for ``container_image/v1``."""
    root = Path(root)
    if not root.is_dir():
        raise ContainerImageError("sealed artifact root must be a directory")

    if not isinstance(factory_declaration, Mapping):
        raise ContainerImageError("factory declaration must be a mapping")

    extra_keys = set(factory_declaration) - {"layers", "config"}
    if extra_keys:
        raise ContainerImageError(
            f"factory declaration contains extra keys: {sorted(extra_keys)!r}"
        )

    layers = factory_declaration.get("layers")
    config = factory_declaration.get("config")
    if not isinstance(layers, list):
        raise ContainerImageError("layers must be a list")
    if not isinstance(config, Mapping):
        raise ContainerImageError("config must be a mapping")

    layer_results: list[dict[str, Any]] = []
    layer_digests: list[str] = []
    final_entries: dict[str, dict[str, Any]] = {}

    layers_root = root / "layers"
    if not layers_root.is_dir():
        raise ContainerImageError("physical layers directory is missing")

    physical_layers = {child.name for child in layers_root.iterdir()}
    expected_layers = {str(index) for index in range(len(layers))}
    if physical_layers != expected_layers:
        raise ContainerImageError(
            "physical layer entries do not match factory declaration"
        )
    if any(not (layers_root / layer_name).is_dir() for layer_name in expected_layers):
        raise ContainerImageError("declared physical layers must be directories")

    for index, layer_declaration in enumerate(layers):
        if not isinstance(layer_declaration, Mapping):
            raise ContainerImageError(f"layer {index}: declaration must be a mapping")

        extra_keys = set(layer_declaration) - {"ownership", "entries"}
        if extra_keys:
            raise ContainerImageError(
                f"layer {index}: declaration contains extra keys: "
                f"{sorted(extra_keys)!r}"
            )

        ownership = layer_declaration.get("ownership")
        if ownership not in {"base", "component"}:
            raise ContainerImageError(
                f"layer {index}: ownership must be base or component"
            )

        declarations = layer_declaration.get("entries")
        if not isinstance(declarations, Mapping):
            raise ContainerImageError(f"layer {index}: entries must be a mapping")

        layer_root = root / "layers" / str(index)
        if not layer_root.is_dir():
            raise ContainerImageError(
                f"layer {index}: physical layer directory is missing"
            )

        physical = _enumerate_layer(layer_root)
        _validate_layer_declaration(physical, declarations, index)
        entries = _build_layer_entries(
            physical,
            declarations,
            ownership,
            index,
        )

        layer_canonical = {
            "ownership": ownership,
            "entries": entries,
        }
        layer_bytes = _canonical_bytes(layer_canonical)
        layer_digest = _digest(layer_bytes)

        layer_results.append(
            {
                "digest": layer_digest,
                "ownership": ownership,
                "entries": entries,
            }
        )
        layer_digests.append(layer_digest)
        _apply_layer(final_entries, entries)

    _validate_final_filesystem(final_entries)
    canonical_config = _validate_config(config)

    canonical = {
        "form": "container_image/v1",
        "layers": layer_results,
        "config": canonical_config,
    }
    canonical_bytes = _canonical_bytes(canonical)
    artifact_digest = _digest(canonical_bytes)

    return canonical, canonical_bytes, artifact_digest, layer_digests


def _enumerate_layer(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def visit(directory: Path, prefix: str) -> None:
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise ContainerImageError(
                f"cannot enumerate directory {directory}"
            ) from exc

        for child in children:
            path = f"{prefix}/{child.name}" if prefix else child.name
            _validate_path(path)

            if path == "canonical.json":
                raise ContainerImageError(
                    "canonical.json must not be part of layer entries"
                )

            if path in result:
                raise ContainerImageError(f"duplicate path: {path}")

            if child.name == ".wh..wh..opq":
                if not child.is_file(follow_symlinks=False):
                    raise ContainerImageError(f"{path}: opaque marker must be a file")
                result[path] = {
                    "type": "opaque",
                    "content_digest": None,
                    "symlink": None,
                    "physical_executable": False,
                }
                continue

            if child.name.startswith(".wh."):
                if not child.is_file(follow_symlinks=False):
                    raise ContainerImageError(f"{path}: whiteout marker must be a file")
                target_name = child.name[4:]
                if not target_name:
                    raise ContainerImageError(f"{path}: invalid whiteout marker")
                target = f"{prefix}/{target_name}" if prefix else target_name
                result[path] = {
                    "type": "whiteout",
                    "target": target,
                    "content_digest": None,
                    "symlink": None,
                    "physical_executable": False,
                }
                continue

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
                raise ContainerImageError(f"unsupported filesystem entry: {path}")

    visit(root, "")
    _validate_collisions(result)
    return result


def _validate_layer_declaration(
    physical: Mapping[str, Mapping[str, Any]],
    declaration: Mapping[str, Any],
    index: int,
) -> None:
    if set(physical) != set(declaration):
        missing = sorted(set(physical) - set(declaration))
        extra = sorted(set(declaration) - set(physical))
        raise ContainerImageError(
            f"layer {index}: Factory declaration mismatch: "
            f"missing={missing!r}, extra={extra!r}"
        )

    for path, value in declaration.items():
        if not isinstance(value, Mapping):
            raise ContainerImageError(
                f"layer {index} {path}: declaration must be a mapping"
            )
        if set(value) != {"owned", "executable"}:
            raise ContainerImageError(
                f"layer {index} {path}: declaration must contain "
                "exactly owned and executable"
            )
        if type(value["owned"]) is not bool:
            raise ContainerImageError(f"layer {index} {path}: owned must be bool")
        if type(value["executable"]) is not bool:
            raise ContainerImageError(f"layer {index} {path}: executable must be bool")


def _build_layer_entries(
    physical: Mapping[str, Mapping[str, Any]],
    declaration: Mapping[str, Mapping[str, bool]],
    ownership: str,
    index: int,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    for path in sorted(physical, key=lambda value: value.encode("utf-8")):
        item = physical[path]
        declared = declaration[path]
        executable = declared["executable"]
        owned = declared["owned"]

        if item["type"] in {"whiteout", "opaque"}:
            if executable or owned:
                raise ContainerImageError(
                    f"layer {index} {path}: layer markers must have "
                    "owned=false and executable=false"
                )
        else:
            if item["type"] == "symlink" and executable:
                raise ContainerImageError(
                    f"layer {index} {path}: symlink entries must have executable=false"
                )

            if item["physical_executable"] and not executable:
                raise ContainerImageError(
                    f"layer {index} {path}: physical executable content "
                    "cannot be declared executable=false"
                )

            if executable and ownership == "component" and not owned:
                raise ContainerImageError(
                    f"layer {index} {path}: executable component content "
                    "must be component-owned"
                )

        entry = {
            "path": path,
            "type": item["type"],
            "executable": executable,
            "owned": owned,
            "content_digest": item["content_digest"],
            "symlink": item["symlink"],
        }
        if item["type"] == "whiteout":
            entry["target"] = item["target"]
        entries.append(entry)

    return entries


def _apply_layer(
    final_entries: dict[str, dict[str, Any]],
    entries: list[dict[str, Any]],
) -> None:
    for entry in entries:
        path = entry["path"]
        entry_type = entry["type"]

        if entry_type == "whiteout":
            target = entry["target"]
            _remove_path(final_entries, target)
            continue

        if entry_type == "opaque":
            prefix = path + "/"
            for existing in list(final_entries):
                if existing.startswith(prefix):
                    del final_entries[existing]
            continue

        _remove_conflicting_ancestors(final_entries, path, entry_type)
        if entry_type == "dir":
            _remove_descendants_if_replacing_file(final_entries, path)
        else:
            _remove_descendants_if_replacing_directory(final_entries, path)
        final_entries[path] = entry


def _remove_path(entries: dict[str, dict[str, Any]], path: str) -> None:
    entries.pop(path, None)
    prefix = path + "/"
    for existing in list(entries):
        if existing.startswith(prefix):
            del entries[existing]


def _remove_conflicting_ancestors(
    entries: dict[str, dict[str, Any]],
    path: str,
    entry_type: str,
) -> None:
    parts = path.split("/")
    for index in range(1, len(parts)):
        parent = "/".join(parts[:index])
        if parent in entries and entries[parent]["type"] != "dir":
            _remove_path(entries, parent)


def _remove_descendants_if_replacing_file(
    entries: dict[str, dict[str, Any]],
    path: str,
) -> None:
    entry = entries.get(path)
    if entry is not None and entry["type"] == "file":
        _remove_path(entries, path)


def _remove_descendants_if_replacing_directory(
    entries: dict[str, dict[str, Any]],
    path: str,
) -> None:
    entry = entries.get(path)
    if entry is not None and entry["type"] == "dir":
        prefix = path + "/"
        for existing in list(entries):
            if existing.startswith(prefix):
                del entries[existing]


def _validate_final_filesystem(
    entries: Mapping[str, Mapping[str, Any]],
) -> None:
    _validate_collisions(entries)
    _validate_symlinks(entries)

    for path, entry in entries.items():
        if entry["type"] == "whiteout" or entry["type"] == "opaque":
            raise ContainerImageError(
                f"{path}: layer marker cannot exist in final filesystem"
            )


def _validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if set(config) != {"entrypoint", "cmd", "working_dir"}:
        raise ContainerImageError(
            "config must contain exactly entrypoint, cmd, and working_dir"
        )

    entrypoint = config["entrypoint"]
    cmd = config["cmd"]
    working_dir = config["working_dir"]

    for name, value in (("entrypoint", entrypoint), ("cmd", cmd)):
        if not isinstance(value, list) or any(type(item) is not str for item in value):
            raise ContainerImageError(f"config {name} must be an array of strings")

    if not isinstance(working_dir, str):
        raise ContainerImageError("config working_dir must be a string")
    if working_dir:
        _validate_path(working_dir)
        if working_dir.startswith("./"):
            raise ContainerImageError(
                "config working_dir must be a canonical POSIX-relative path"
            )

    return {
        "entrypoint": list(entrypoint),
        "cmd": list(cmd),
        "working_dir": working_dir,
    }


def _validate_path(path: str) -> None:
    if not path or "\x00" in path:
        raise ContainerImageError(f"invalid path: {path!r}")
    if "\\" in path or "//" in path:
        raise ContainerImageError(f"invalid path: {path!r}")
    if path.startswith("/") or path in {".", ".."}:
        raise ContainerImageError(f"invalid path: {path!r}")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ContainerImageError(f"invalid path: {path!r}")
    if unicodedata.normalize("NFC", path) != path:
        raise ContainerImageError(f"path is not Unicode NFC: {path!r}")


def _validate_collisions(entries: Mapping[str, object]) -> None:
    paths = set(entries)
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in paths and entries[parent]["type"] != "dir":
                raise ContainerImageError(
                    f"file/directory collision: {parent!r} / {path!r}"
                )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContainerImageError(f"cannot read file: {path}") from exc
    return "sha256:" + digest.hexdigest()


def _physical_executable(path: Path, entry: os.DirEntry[str]) -> bool:
    try:
        mode = entry.stat(follow_symlinks=False).st_mode
        if mode & 0o111:
            return True
        with path.open("rb") as stream:
            prefix = stream.read(4)
    except OSError as exc:
        raise ContainerImageError(f"cannot inspect file: {path}") from exc

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
            raise ContainerImageError(f"{path}: symlink target must be POSIX-relative")

        target_path = _resolve_symlink(path, target)
        if target_path not in entries:
            raise ContainerImageError(f"{path}: dangling symlink")

        target_entry = entries[target_path]
        if target_entry["type"] not in {"file", "dir", "symlink"}:
            raise ContainerImageError(f"{path}: invalid symlink target")

    state: dict[str, int] = {}

    def visit(path: str) -> None:
        if entries[path]["type"] != "symlink":
            return
        current = state.get(path, 0)
        if current == 1:
            raise ContainerImageError(f"symlink cycle detected at {path}")
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
                raise ContainerImageError(
                    f"{path}: symlink target escapes artifact root"
                )
            parts.pop()
        else:
            parts.append(part)

    return "/".join(parts)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
