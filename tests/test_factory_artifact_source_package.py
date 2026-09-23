import hashlib
import json
import os

import pytest

from factory_artifact.source_package import (
    SourcePackageError,
    build_source_package_canonical,
)


def declaration(*paths, owned=True, executable=False):
    return {path: {"owned": owned, "executable": executable} for path in paths}


def make_symlink(target, link):
    try:
        os.symlink(target, link)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise


def test_empty_directory_produces_empty_entries(tmp_path):
    canonical, canonical_bytes, artifact_digest = build_source_package_canonical(
        tmp_path, {}
    )

    assert canonical == {"form": "source_package/v1", "entries": []}
    assert canonical_bytes == b'{"entries":[],"form":"source_package/v1"}'
    assert artifact_digest == "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()


def test_same_content_has_same_identity(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    (root_a / "app.txt").write_bytes(b"hello")
    (root_b / "app.txt").write_bytes(b"hello")

    declaration_a = declaration("app.txt", owned=True)
    declaration_b = declaration("app.txt", owned=True)

    result_a = build_source_package_canonical(root_a, declaration_a)
    result_b = build_source_package_canonical(root_b, declaration_b)

    assert result_a[1] == result_b[1]
    assert result_a[2] == result_b[2]


def test_content_change_changes_identity(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    path = root / "app.txt"
    path.write_bytes(b"hello")

    declaration_data = declaration("app.txt", owned=True)

    first = build_source_package_canonical(root, declaration_data)

    path.write_bytes(b"changed")

    second = build_source_package_canonical(root, declaration_data)

    assert first[2] != second[2]
    assert first[0]["entries"][0]["content_digest"] != (
        second[0]["entries"][0]["content_digest"]
    )


def test_entries_are_sorted_by_utf8_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    (root / "z").write_bytes(b"z")
    (root / "a").write_bytes(b"a")
    (root / "dir").mkdir()
    (root / "dir" / "b").write_bytes(b"b")

    declarations = declaration(
        "z",
        "a",
        "dir",
        "dir/b",
        owned=False,
    )

    canonical, _, _ = build_source_package_canonical(root, declarations)

    assert [entry["path"] for entry in canonical["entries"]] == [
        "a",
        "dir",
        "dir/b",
        "z",
    ]


def test_nested_directories_are_explicit(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "b").mkdir()
    (root / "a" / "b" / "file").write_bytes(b"x")

    declarations = declaration(
        "a",
        "a/b",
        "a/b/file",
        owned=False,
    )

    canonical, _, _ = build_source_package_canonical(root, declarations)

    entries = {entry["path"]: entry for entry in canonical["entries"]}

    assert entries["a"]["type"] == "dir"
    assert entries["a/b"]["type"] == "dir"
    assert entries["a/b/file"]["type"] == "file"


def test_missing_factory_declaration_fails_closed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"x")

    with pytest.raises(SourcePackageError, match="missing"):
        build_source_package_canonical(root, {})


def test_extra_factory_declaration_fails_closed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(SourcePackageError, match="extra"):
        build_source_package_canonical(
            root,
            declaration("ghost", owned=False),
        )


def test_executable_must_be_owned(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "run.py").write_bytes(b"print('x')")

    with pytest.raises(SourcePackageError, match="component-owned"):
        build_source_package_canonical(
            root,
            declaration("run.py", owned=False, executable=True),
        )


def test_physical_executable_cannot_be_hidden(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "run.py").write_bytes(b"print('x')")

    with pytest.raises(SourcePackageError, match="executable=false"):
        build_source_package_canonical(
            root,
            declaration("run.py", owned=True, executable=False),
        )


def test_canonical_json_is_not_part_of_sealed_artifact(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "canonical.json").write_bytes(b"{}")

    with pytest.raises(SourcePackageError, match="canonical.json"):
        build_source_package_canonical(
            root,
            declaration("canonical.json", owned=False),
        )


def test_symlink_is_not_executable(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "target").write_bytes(b"x")
    make_symlink("target", root / "link")

    with pytest.raises(SourcePackageError, match="symlink.*executable=false"):
        build_source_package_canonical(
            root,
            declaration(
                "target",
                "link",
                owned=False,
                executable=True,
            ),
        )


def test_symlink_target_must_exist(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    make_symlink("missing", root / "link")

    with pytest.raises(SourcePackageError, match="dangling"):
        build_source_package_canonical(
            root,
            declaration("link", owned=False),
        )


def test_symlink_cycle_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    make_symlink("b", root / "a")
    make_symlink("a", root / "b")

    with pytest.raises(SourcePackageError, match="cycle"):
        build_source_package_canonical(
            root,
            declaration("a", "b", owned=False),
        )


def test_non_nfc_path_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    non_nfc = "e\u0301.txt"
    (root / non_nfc).write_bytes(b"x")

    with pytest.raises(SourcePackageError, match="NFC"):
        build_source_package_canonical(
            root,
            declaration(non_nfc, owned=False),
        )


def test_canonical_json_uses_required_serialization(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"x")

    canonical, canonical_bytes, digest = build_source_package_canonical(
        root,
        declaration("file", owned=False),
    )

    expected = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    assert canonical_bytes == expected
    assert digest == "sha256:" + hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("script.py", b"print('x')"),
        ("script.pyc", b"x"),
        ("script.pyo", b"x"),
        ("module.pyd", b"x"),
        ("module.so", b"x"),
        ("module.dll", b"x"),
        ("module.dylib", b"x"),
        ("program.exe", b"x"),
        ("script", b"#!/usr/bin/env python\n"),
        ("elf", b"\x7fELF" + b"\x00" * 20),
        ("pe", b"MZ" + b"\x00" * 20),
        ("macho32", bytes.fromhex("feedface") + b"\x00" * 20),
        ("macho64", bytes.fromhex("feedfacf") + b"\x00" * 20),
        ("macho_fat", bytes.fromhex("cafebabe") + b"\x00" * 20),
    ],
)
def test_physical_executable_signs_are_detected(tmp_path, filename, content):
    root = tmp_path / "root"
    root.mkdir()

    (root / filename).write_bytes(content)

    canonical, _, _ = build_source_package_canonical(
        root,
        declaration(filename, owned=True, executable=True),
    )

    assert canonical["entries"][0]["executable"] is True


def test_posix_executable_bit_is_detected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    path = root / "program"
    path.write_bytes(b"plain")
    path.chmod(path.stat().st_mode | 0o111)

    canonical, _, _ = build_source_package_canonical(
        root,
        declaration("program", owned=True, executable=True),
    )

    assert canonical["entries"][0]["executable"] is True


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"owned": True},
        {"executable": False},
        {"owned": True, "executable": False, "extra": False},
        {"owned": 1, "executable": False},
        {"owned": False, "executable": 0},
    ],
)
def test_factory_declaration_shape_is_strict(tmp_path, value):
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"x")

    with pytest.raises(SourcePackageError, match="declaration|must be bool"):
        build_source_package_canonical(
            root,
            {"file": value},
        )


@pytest.mark.parametrize(
    "target",
    [
        "/absolute",
        "../outside",
    ],
)
def test_symlink_target_must_stay_inside_root(tmp_path, target):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").parent.mkdir(exist_ok=True)
    make_symlink(target, root / "link")

    with pytest.raises(SourcePackageError, match="POSIX-relative|escapes"):
        build_source_package_canonical(
            root,
            declaration("link", owned=False),
        )


def test_symlink_relative_parent_may_stay_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "dir").mkdir()
    (root / "target").write_bytes(b"x")
    make_symlink("../target", root / "dir" / "link")

    canonical, _, _ = build_source_package_canonical(
        root,
        declaration("dir", "dir/link", "target", owned=False),
    )

    entries = {entry["path"]: entry for entry in canonical["entries"]}
    assert entries["dir/link"]["type"] == "symlink"
    assert entries["dir/link"]["symlink"] == "../target"


def test_symlink_to_directory_is_allowed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "target").mkdir()
    (root / "target" / "file").write_bytes(b"x")
    make_symlink("target", root / "link")

    canonical, _, _ = build_source_package_canonical(
        root,
        declaration("target", "target/file", "link", owned=False),
    )

    entries = {entry["path"]: entry for entry in canonical["entries"]}
    assert entries["link"]["type"] == "symlink"
    assert entries["link"]["symlink"] == "target"


def test_empty_file_has_empty_sha256_digest(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "empty").write_bytes(b"")

    canonical, _, _ = build_source_package_canonical(
        root,
        declaration("empty", owned=False),
    )

    assert canonical["entries"][0]["content_digest"] == (
        "sha256:" + hashlib.sha256(b"").hexdigest()
    )


def test_hardlinked_files_share_content_digest_but_keep_ownership_per_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    first = root / "first"
    second = root / "second"
    first.write_bytes(b"same-content")

    try:
        os.link(first, second)
    except OSError as exc:
        if getattr(exc, "winerror", None) is not None:
            pytest.skip("Windows hard-link creation is unavailable")
        raise

    canonical, _, _ = build_source_package_canonical(
        root,
        {
            "first": {"owned": True, "executable": False},
            "second": {"owned": False, "executable": False},
        },
    )

    entries = {entry["path"]: entry for entry in canonical["entries"]}

    assert entries["first"]["content_digest"] == entries["second"]["content_digest"]
    assert entries["first"]["owned"] is True
    assert entries["second"]["owned"] is False


def test_non_executable_content_may_be_non_owned(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "foreign.txt").write_bytes(b"environment-owned")

    canonical, _, _ = build_source_package_canonical(
        root,
        declaration("foreign.txt", owned=False, executable=False),
    )

    entry = canonical["entries"][0]
    assert entry["owned"] is False
    assert entry["executable"] is False
    assert entry["content_digest"] == (
        "sha256:" + hashlib.sha256(b"environment-owned").hexdigest()
    )
