import pytest

from factory_artifact import build_container_image_canonical
from factory_artifact.container_image import ContainerImageError

CONFIG = {
    "entrypoint": [],
    "cmd": [],
    "working_dir": "",
}


def make_root(tmp_path):
    root = tmp_path / "root"
    layer = root / "layers" / "0"
    layer.mkdir(parents=True)
    (layer / "app.txt").write_bytes(b"app")
    return root


def valid_declaration():
    return {
        "layers": [
            {
                "ownership": "component",
                "entries": {
                    "app.txt": {
                        "owned": True,
                        "executable": False,
                    }
                },
            }
        ],
        "config": CONFIG.copy(),
    }


def test_extra_factory_declaration_key_fails_closed(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    declaration["unexpected"] = True

    with pytest.raises(ContainerImageError, match="extra"):
        build_container_image_canonical(root, declaration)


def test_extra_layer_declaration_key_fails_closed(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    declaration["layers"][0]["unexpected"] = True

    with pytest.raises(ContainerImageError, match="extra"):
        build_container_image_canonical(root, declaration)


def test_opaque_marker_uses_normative_name_and_representation(tmp_path):
    root = make_root(tmp_path)
    layer = root / "layers" / "0"
    (layer / ".wh..wh..opq").write_bytes(b"")

    declaration = valid_declaration()
    declaration["layers"][0]["entries"][".wh..wh..opq"] = {
        "owned": False,
        "executable": False,
    }

    canonical, _, _, _ = build_container_image_canonical(root, declaration)

    opaque = next(
        entry
        for entry in canonical["layers"][0]["entries"]
        if entry["path"] == ".wh..wh..opq"
    )

    assert opaque == {
        "path": ".wh..wh..opq",
        "type": "opaque",
        "owned": False,
        "executable": False,
        "content_digest": None,
        "symlink": None,
    }


def test_layer_order_changes_artifact_digest(tmp_path):
    root = tmp_path / "root"
    layer0 = root / "layers" / "0"
    layer1 = root / "layers" / "1"
    layer0.mkdir(parents=True)
    layer1.mkdir(parents=True)

    (layer0 / "base.txt").write_bytes(b"base")
    (layer1 / "app.txt").write_bytes(b"app")

    config = {
        "entrypoint": [],
        "cmd": [],
        "working_dir": "",
    }

    declaration = {
        "layers": [
            {
                "ownership": "base",
                "entries": {
                    "base.txt": {
                        "owned": False,
                        "executable": False,
                    }
                },
            },
            {
                "ownership": "component",
                "entries": {
                    "app.txt": {
                        "owned": True,
                        "executable": False,
                    }
                },
            },
        ],
        "config": config,
    }

    _, _, digest_forward, layer_digests_forward = build_container_image_canonical(
        root, declaration
    )

    reordered_root = tmp_path / "reordered"
    reordered_layer0 = reordered_root / "layers" / "0"
    reordered_layer1 = reordered_root / "layers" / "1"
    reordered_layer0.mkdir(parents=True)
    reordered_layer1.mkdir(parents=True)

    (reordered_layer0 / "app.txt").write_bytes(b"app")
    (reordered_layer1 / "base.txt").write_bytes(b"base")

    reordered_declaration = {
        "layers": [
            {
                "ownership": "component",
                "entries": {
                    "app.txt": {
                        "owned": True,
                        "executable": False,
                    }
                },
            },
            {
                "ownership": "base",
                "entries": {
                    "base.txt": {
                        "owned": False,
                        "executable": False,
                    }
                },
            },
        ],
        "config": config,
    }

    _, _, digest_reversed, layer_digests_reversed = build_container_image_canonical(
        reordered_root, reordered_declaration
    )

    assert layer_digests_forward == layer_digests_reversed[::-1]
    assert digest_forward != digest_reversed


def test_missing_factory_declaration_entry_fails_closed(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    del declaration["layers"][0]["entries"]["app.txt"]

    with pytest.raises(ContainerImageError, match="missing"):
        build_container_image_canonical(root, declaration)


def test_extra_factory_declaration_entry_fails_closed(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    declaration["layers"][0]["entries"]["extra.txt"] = {
        "owned": True,
        "executable": False,
    }

    with pytest.raises(ContainerImageError, match="extra"):
        build_container_image_canonical(root, declaration)


def test_invalid_factory_declaration_boolean_fails_closed(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    declaration["layers"][0]["entries"]["app.txt"]["owned"] = "true"

    with pytest.raises(ContainerImageError, match="owned must be bool"):
        build_container_image_canonical(root, declaration)


def test_physical_executable_with_false_declaration_fails_closed(tmp_path):
    root = make_root(tmp_path)
    layer = root / "layers" / "0"
    executable = layer / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    declaration = valid_declaration()
    declaration["layers"][0]["entries"]["run.sh"] = {
        "owned": True,
        "executable": False,
    }

    with pytest.raises(ContainerImageError, match="executable"):
        build_container_image_canonical(root, declaration)


def test_component_executable_requires_owned(tmp_path):
    root = make_root(tmp_path)
    layer = root / "layers" / "0"
    executable = layer / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    declaration = valid_declaration()
    declaration["layers"][0]["entries"]["run.sh"] = {
        "owned": False,
        "executable": True,
    }

    with pytest.raises(ContainerImageError, match="owned"):
        build_container_image_canonical(root, declaration)


def test_base_executable_may_be_unowned(tmp_path):
    root = make_root(tmp_path)
    layer = root / "layers" / "0"
    executable = layer / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    declaration = valid_declaration()
    declaration["layers"][0]["ownership"] = "base"
    declaration["layers"][0]["entries"]["run.sh"] = {
        "owned": False,
        "executable": True,
    }

    canonical, _, _, _ = build_container_image_canonical(root, declaration)

    run_entry = next(
        entry
        for entry in canonical["layers"][0]["entries"]
        if entry["path"] == "run.sh"
    )
    assert run_entry["owned"] is False
    assert run_entry["executable"] is True


def test_whiteout_removes_lower_entry(tmp_path):
    root = tmp_path / "root"
    base = root / "layers" / "0"
    component = root / "layers" / "1"
    base.mkdir(parents=True)
    component.mkdir(parents=True)

    (base / "old.txt").write_bytes(b"old")
    (component / ".wh.old.txt").write_bytes(b"")

    declaration = {
        "layers": [
            {
                "ownership": "base",
                "entries": {
                    "old.txt": {
                        "owned": False,
                        "executable": False,
                    }
                },
            },
            {
                "ownership": "component",
                "entries": {
                    ".wh.old.txt": {
                        "owned": False,
                        "executable": False,
                    }
                },
            },
        ],
        "config": CONFIG,
    }

    canonical, _, _, _ = build_container_image_canonical(root, declaration)

    final_paths = {entry["path"] for entry in canonical["layers"][1]["entries"]}
    assert "old.txt" not in final_paths


def test_opaque_hides_lower_directory_entries(tmp_path):
    root = tmp_path / "root"
    base = root / "layers" / "0"
    component = root / "layers" / "1"
    base.mkdir(parents=True)
    component.mkdir(parents=True)

    (base / "data").mkdir()
    (base / "data" / "old.txt").write_bytes(b"old")
    (base / "data" / "keep.txt").write_bytes(b"keep")
    (component / "data").mkdir()
    (component / "data" / ".wh..wh..opq").write_bytes(b"")
    (component / "data" / "new.txt").write_bytes(b"new")

    declaration = {
        "layers": [
            {
                "ownership": "base",
                "entries": {
                    "data": {
                        "owned": False,
                        "executable": False,
                    },
                    "data/old.txt": {
                        "owned": False,
                        "executable": False,
                    },
                    "data/keep.txt": {
                        "owned": False,
                        "executable": False,
                    },
                },
            },
            {
                "ownership": "component",
                "entries": {
                    "data": {
                        "owned": True,
                        "executable": False,
                    },
                    "data/.wh..wh..opq": {
                        "owned": False,
                        "executable": False,
                    },
                    "data/new.txt": {
                        "owned": True,
                        "executable": False,
                    },
                },
            },
        ],
        "config": CONFIG,
    }

    canonical, _, _, _ = build_container_image_canonical(root, declaration)

    final_paths = {entry["path"] for entry in canonical["layers"][1]["entries"]}
    assert "data/old.txt" not in final_paths
    assert "data/keep.txt" not in final_paths
    assert "data/new.txt" in final_paths


def test_dangling_symlink_fails_closed(tmp_path):
    root = make_root(tmp_path)
    layer = root / "layers" / "0"
    try:
        (layer / "link").symlink_to("missing.txt")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires additional privileges")
        raise

    declaration = valid_declaration()
    declaration["layers"][0]["entries"]["link"] = {
        "owned": True,
        "executable": False,
    }

    with pytest.raises(ContainerImageError, match="does not exist"):
        build_container_image_canonical(root, declaration)


def test_symlink_escape_fails_closed(tmp_path):
    root = make_root(tmp_path)
    layer = root / "layers" / "0"
    try:
        (layer / "link").symlink_to("../outside.txt")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires additional privileges")
        raise

    declaration = valid_declaration()
    declaration["layers"][0]["entries"]["link"] = {
        "owned": True,
        "executable": False,
    }

    with pytest.raises(ContainerImageError, match="escapes"):
        build_container_image_canonical(root, declaration)


def test_config_entrypoint_must_be_array_of_strings(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    declaration["config"]["entrypoint"] = "python"

    with pytest.raises(ContainerImageError, match="entrypoint"):
        build_container_image_canonical(root, declaration)


def test_config_cmd_must_be_array_of_strings(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    declaration["config"]["cmd"] = ["python", 123]

    with pytest.raises(ContainerImageError, match="cmd"):
        build_container_image_canonical(root, declaration)


def test_config_working_dir_must_be_canonical_relative_path(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()
    declaration["config"]["working_dir"] = "../app"

    with pytest.raises(ContainerImageError):
        build_container_image_canonical(root, declaration)


def test_declaration_mapping_order_does_not_change_artifact_digest(tmp_path):
    root_a = make_root(tmp_path / "a")
    root_b = make_root(tmp_path / "b")

    declaration_a = valid_declaration()
    declaration_b = {
        "config": {
            "working_dir": "",
            "cmd": [],
            "entrypoint": [],
        },
        "layers": [
            {
                "entries": {
                    "app.txt": {
                        "executable": False,
                        "owned": True,
                    }
                },
                "ownership": "component",
            }
        ],
    }

    _, _, digest_a, _ = build_container_image_canonical(root_a, declaration_a)
    _, _, digest_b, _ = build_container_image_canonical(root_b, declaration_b)

    assert digest_a == digest_b


def test_file_content_change_changes_artifact_digest(tmp_path):
    root_a = make_root(tmp_path / "a")
    root_b = make_root(tmp_path / "b")

    (root_b / "layers" / "0" / "app.txt").write_bytes(b"changed")

    declaration_a = valid_declaration()
    declaration_b = valid_declaration()

    _, _, digest_a, _ = build_container_image_canonical(root_a, declaration_a)
    _, _, digest_b, _ = build_container_image_canonical(root_b, declaration_b)

    assert digest_a != digest_b


def test_layer_digest_changes_with_layer_content(tmp_path):
    root_a = make_root(tmp_path / "a")
    root_b = make_root(tmp_path / "b")

    (root_b / "layers" / "0" / "app.txt").write_bytes(b"changed")

    declaration_a = valid_declaration()
    declaration_b = valid_declaration()

    _, _, _, layer_digests_a = build_container_image_canonical(root_a, declaration_a)
    _, _, _, layer_digests_b = build_container_image_canonical(root_b, declaration_b)

    assert layer_digests_a != layer_digests_b


def test_layer_digest_is_not_recursive(tmp_path):
    root = make_root(tmp_path)
    declaration = valid_declaration()

    canonical, _, _, layer_digests = build_container_image_canonical(root, declaration)

    layer = canonical["layers"][0]
    layer_without_digest = {
        "ownership": layer["ownership"],
        "entries": layer["entries"],
    }

    import hashlib
    import json

    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                layer_without_digest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )

    assert layer["digest"] == expected
    assert layer_digests[0] == expected


def test_whiteout_marker_must_be_a_file(tmp_path):
    root = make_root(tmp_path)
    layer = root / "layers" / "0"
    (layer / ".wh.deleted").mkdir()

    declaration = valid_declaration()
    declaration["layers"][0]["entries"][".wh.deleted"] = {
        "owned": False,
        "executable": False,
    }

    with pytest.raises(ContainerImageError):
        build_container_image_canonical(root, declaration)


def test_undeclared_physical_layer_fails_closed(tmp_path):
    root = make_root(tmp_path)
    extra_layer = root / "layers" / "1"
    extra_layer.mkdir()
    (extra_layer / "extra.txt").write_bytes(b"extra")

    declaration = valid_declaration()

    with pytest.raises(ContainerImageError):
        build_container_image_canonical(root, declaration)


def test_undeclared_physical_layer_entry_fails_closed(tmp_path):
    root = make_root(tmp_path)
    (root / "layers" / "unexpected").write_bytes(b"unexpected")

    declaration = valid_declaration()

    with pytest.raises(ContainerImageError):
        build_container_image_canonical(root, declaration)
