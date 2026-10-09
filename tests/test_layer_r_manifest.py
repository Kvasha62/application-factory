"""Layer R manifest / drift / export tooling — policy and read-only guarantees.

These tests are the CI gate for the Layer R source-control machinery
(``layer-r/``). They call the real tool functions (``verify_tree``,
``generate_manifest``, ``compare``, ``export``, ``main``) and prove the
fail-closed classification and path policy, the symlink non-following rules,
the manifest integrity rules, and that the drift check and export bundle never
write to the tree they inspect. No production behavior and no production
evidence is produced here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.layer_r_drift_check import (
    CHANGED,
    EXTRA,
    IN_SYNC,
    MISSING,
    UNSAFE,
    DriftCheckError,
    compare,
)
from scripts.layer_r_export_bundle import ExportRefused, collect, export
from scripts.layer_r_manifest import (
    ManifestError,
    generate_manifest,
    load_manifest,
    main,
    normalize_relative_path,
    sha256_of,
    tree_files,
    validate_tracked_path,
    verify_tree,
    write_manifest,
)

CLEAN = "value = 1\n"


def build_tree(root: Path) -> Path:
    """A minimal layer-r tree with one clean source file."""
    (root / "layer-r" / "source").mkdir(parents=True)
    (root / "layer-r" / "source" / "cell.py").write_text(CLEAN, encoding="utf-8")
    return root


def classed(root: Path) -> dict:
    return generate_manifest(root, class_for={"layer-r/source/cell.py": "source"})


def statuses(report: dict) -> dict[str, str]:
    return {row["host_path"] or row["path"]: row["status"] for row in report["results"]}


def tree_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_of(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def bad_path_manifest(path_value: str) -> dict:
    return {
        "manifest_version": 1,
        "entries": [
            {
                "path": path_value,
                "sha256": "1" * 64,
                "class": "source",
                "host_path": "cell.py",
                "origin": "tests",
            }
        ],
    }


class TestPathPolicy:
    @pytest.mark.parametrize(
        "unsafe",
        [
            "/etc/passwd",
            "layer-r/source/../config/x.py",
            "layer-r/source/../../etc/passwd",
            "layer-r/source/./x.py",
            "layer-r/source//x.py",
            "layer-r/source/x.py/",
            "layer-r\\source\\x.py",
            "C:/layer-r/source/x.py",
            "layer-r/source-evil/file.py",
            "layer-r/sourceevil/file.py",
            "layer-r/other/file.py",
            "layer-r/source",
            "elsewhere/cell.py",
            "",
        ],
    )
    def test_unsafe_manifest_paths_are_refused(self, unsafe: str) -> None:
        assert validate_tracked_path(unsafe) is None

    def test_canonical_tracked_path_is_accepted(self) -> None:
        assert (
            validate_tracked_path("layer-r/source/cell.py") == "layer-r/source/cell.py"
        )
        assert validate_tracked_path("layer-r/config/environment.json") == (
            "layer-r/config/environment.json"
        )
        assert validate_tracked_path("layer-r/source/pkg/mod.py") == (
            "layer-r/source/pkg/mod.py"
        )

    @pytest.mark.parametrize(
        "unsafe",
        [
            "/abs",
            "a/../b",
            "a/./b",
            "a//b",
            "a/b/",
            "a\\b",
            "C:/a",
            "..",
            "",
        ],
    )
    def test_normalize_rejects_ambiguous_or_escaping_variants(
        self, unsafe: str
    ) -> None:
        assert normalize_relative_path(unsafe) is None

    def test_prefix_sibling_entry_is_rejected_in_the_manifest(self) -> None:
        root = Path(__file__).resolve().parent.parent
        errors = verify_tree(root, bad_path_manifest("layer-r/source-evil/file.py"))
        assert any("canonical relative POSIX" in error for error in errors)

    @pytest.mark.parametrize(
        "unsafe",
        [
            "/etc/passwd",
            "layer-r/source/../config/x.py",
            "layer-r\\source\\x.py",
            "layer-r/source/./x.py",
            "layer-r/source/x.py/",
        ],
    )
    def test_unsafe_entries_are_rejected_by_verify(self, unsafe: str) -> None:
        root = Path(__file__).resolve().parent.parent
        errors = verify_tree(root, bad_path_manifest(unsafe))
        assert any("canonical relative POSIX" in error for error in errors)

    def test_host_path_rejects_parent_traversal(self) -> None:
        root = Path(__file__).resolve().parent.parent
        manifest = bad_path_manifest("layer-r/source/x.py")
        manifest["entries"][0]["host_path"] = "../outside/cell.py"
        errors = verify_tree(root, manifest)
        assert any("host_path" in error and "refused" in error for error in errors)

    def test_duplicate_entries_are_refused(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        manifest["entries"].append(dict(manifest["entries"][0]))
        errors = verify_tree(tmp_path, manifest)
        assert any("duplicate" in error for error in errors)


class TestManifestPolicy:
    def test_generate_requires_explicit_classification(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        with pytest.raises(ManifestError):
            generate_manifest(tmp_path)

    def test_generate_and_verify_roundtrip(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        assert manifest["entries"][0]["sha256"] == sha256_of(
            tmp_path / "layer-r" / "source" / "cell.py"
        )
        assert verify_tree(tmp_path, manifest) == []

    def test_verify_detects_content_drift(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        (tmp_path / "layer-r" / "source" / "cell.py").write_text(
            "value = 2\n", encoding="utf-8"
        )
        errors = verify_tree(tmp_path, manifest)
        assert any("drifted" in error for error in errors)

    def test_verify_detects_missing_file(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        (tmp_path / "layer-r" / "source" / "cell.py").unlink()
        errors = verify_tree(tmp_path, manifest)
        assert any("missing" in error for error in errors)

    def test_verify_detects_unlisted_file(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        (tmp_path / "layer-r" / "source" / "rogue.py").write_text(
            CLEAN, encoding="utf-8"
        )
        errors = verify_tree(tmp_path, manifest)
        assert any("not in the manifest" in error for error in errors)

    def test_verify_rejects_forbidden_class(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        manifest["entries"][0]["class"] = "secret"
        errors = verify_tree(tmp_path, manifest)
        assert any("secret" in error for error in errors)

    def test_verify_rejects_secret_looking_content(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        (tmp_path / "layer-r" / "source" / "cell.py").write_text(
            'TENANT_AUTHORITY_SERVICE_TOKEN = "abcdef0123456789"\n', encoding="utf-8"
        )
        manifest["entries"][0]["sha256"] = sha256_of(
            tmp_path / "layer-r" / "source" / "cell.py"
        )
        errors = verify_tree(tmp_path, manifest)
        assert any("secret-looking" in error for error in errors)

    def test_verify_rejects_absolute_host_path(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        manifest["entries"][0]["host_path"] = "/srv/running-platform/cell/cell.py"
        errors = verify_tree(tmp_path, manifest)
        assert any("host_path" in error and "refused" in error for error in errors)

    def test_committed_manifest_verifies(self) -> None:
        root = Path(__file__).resolve().parent.parent
        manifest = load_manifest(root / "layer-r" / "SOURCE_MANIFEST.json")
        assert verify_tree(root, manifest) == []

    def test_tree_files_ignores_generated(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        (tmp_path / "layer-r" / "source" / "__pycache__").mkdir()
        (tmp_path / "layer-r" / "source" / "__pycache__" / "cell.pyc").write_bytes(b"x")
        assert tree_files(tmp_path) == ["layer-r/source/cell.py"]


class TestSymlinkPolicy:
    def test_verify_rejects_symlink_escaping_the_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        build_tree(repo)
        manifest = classed(repo)  # generated while the tree is clean
        outside = tmp_path / "outside-target.txt"
        outside.write_text('SERVICE_TOKEN = "abcdef0123456789"\n', encoding="utf-8")
        (repo / "layer-r" / "source" / "cell.py").unlink()
        (repo / "layer-r" / "source" / "cell.py").symlink_to(outside)
        errors = verify_tree(repo, manifest)
        assert any("symlink" in error for error in errors)
        # The symlink was not hashed through: no secret finding on the target.
        assert not any("secret-looking" in error for error in errors)

    def test_generate_refuses_symlinks(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        outside = tmp_path / "outside-target.txt"
        outside.write_text(CLEAN, encoding="utf-8")
        (tmp_path / "layer-r" / "source" / "link.py").symlink_to(outside)
        with pytest.raises(ManifestError, match="symlinks"):
            generate_manifest(tmp_path, class_for={"layer-r/source/link.py": "source"})

    def test_verify_rejects_symlinked_tracked_root(self, tmp_path: Path) -> None:
        real = tmp_path / "real-source"
        real.mkdir()
        (real / "cell.py").write_text(CLEAN, encoding="utf-8")
        (tmp_path / "layer-r").mkdir()
        (tmp_path / "layer-r" / "source").symlink_to(real, target_is_directory=True)
        manifest = bad_path_manifest("layer-r/source/cell.py")
        errors = verify_tree(tmp_path, manifest)
        assert any("symlink" in error for error in errors)

    def test_in_tree_symlink_to_sibling_is_still_refused(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)  # generated while the tree is clean
        (tmp_path / "layer-r" / "source" / "other.py").write_text(
            CLEAN, encoding="utf-8"
        )
        (tmp_path / "layer-r" / "source" / "cell.py").unlink()
        (tmp_path / "layer-r" / "source" / "cell.py").symlink_to("other.py")
        errors = verify_tree(tmp_path, manifest)
        assert any("symlink" in error for error in errors)


class TestDriftCheck:
    def _pair(self, tmp_path: Path) -> tuple[Path, dict]:
        build_tree(tmp_path)
        host = tmp_path / "host-cell"
        host.mkdir()
        manifest = classed(tmp_path)
        entry = manifest["entries"][0]
        entry["host_path"] = "cell.py"
        (host / "cell.py").write_text(CLEAN, encoding="utf-8")
        return host, manifest

    def test_in_sync(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        report = compare(host, manifest)
        assert report["in_sync"] is True
        assert statuses(report)["cell.py"] == IN_SYNC

    def test_changed_missing_and_extra_are_drift(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        (host / "cell.py").write_text("value = 9\n", encoding="utf-8")
        (host / "leftover.log").write_text("log\n", encoding="utf-8")
        (host / "orphan.py").write_text(CLEAN, encoding="utf-8")
        report = compare(host, manifest)
        assert report["in_sync"] is False
        observed = statuses(report)
        assert observed["cell.py"] == CHANGED
        assert observed["orphan.py"] == EXTRA
        assert not any(
            ".log" in name for name in observed
        )  # generated logs are skipped

    def test_missing(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        (host / "cell.py").unlink()
        report = compare(host, manifest)
        assert statuses(report)["cell.py"] == MISSING

    def test_pending_import_entry_is_not_drift(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        (host / "cell.py").unlink()
        manifest["entries"][0]["host_path"] = ""
        report = compare(host, manifest)
        assert report["in_sync"] is True

    def test_symlinked_target_is_unsafe_and_never_followed(
        self, tmp_path: Path
    ) -> None:
        host, manifest = self._pair(tmp_path)
        outside = tmp_path / "outside-target.txt"
        outside.write_text(CLEAN, encoding="utf-8")
        (host / "cell.py").unlink()
        (host / "cell.py").symlink_to(outside)
        before = tree_digests(tmp_path)
        report = compare(host, manifest)
        assert report["in_sync"] is False
        assert statuses(report)["cell.py"] == UNSAFE
        assert tree_digests(tmp_path) == before  # nothing read wrote anything

    def test_unsafe_host_path_value_is_refused(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        manifest["entries"][0]["host_path"] = "../escape/cell.py"
        with pytest.raises(DriftCheckError):
            compare(host, manifest)

    def test_symlink_outside_manifest_is_reported_unsafe(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        outside = tmp_path / "outside-target.txt"
        outside.write_text(CLEAN, encoding="utf-8")
        (host / "rogue-link").symlink_to(outside)
        report = compare(host, manifest)
        assert statuses(report)["rogue-link"] == UNSAFE

    def test_drift_check_never_writes_the_host_tree(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        before = tree_digests(host)
        compare(host, manifest)
        assert tree_digests(host) == before


class TestExportBundle:
    def test_export_copies_hashes_and_suggests_classes(self, tmp_path: Path) -> None:
        cell = tmp_path / "cell"
        cell.mkdir()
        (cell / "producer.py").write_text(CLEAN, encoding="utf-8")
        (cell / "environment.json").write_text("{}\n", encoding="utf-8")
        out = tmp_path / "bundle"
        bundle = export(cell, out, exporter="test-handle")
        classes = {entry["path"]: entry["suggested_class"] for entry in bundle["files"]}
        assert classes == {
            "environment.json": "deployable-configuration",
            "producer.py": "source",
        }
        assert (out / "producer.py").read_text(encoding="utf-8") == CLEAN
        recorded = json.loads(
            (out / "export-manifest.json").read_text(encoding="utf-8")
        )
        assert recorded["exporter"] == "test-handle"

    def test_secret_file_aborts_before_any_copy(self, tmp_path: Path) -> None:
        cell = tmp_path / "cell"
        cell.mkdir()
        (cell / "clean.py").write_text(CLEAN, encoding="utf-8")
        (cell / "leaky.py").write_text(
            'SERVICE_TOKEN = "abcdef0123456789"\n', encoding="utf-8"
        )
        out = tmp_path / "bundle"
        before = tree_digests(cell)
        with pytest.raises(ExportRefused):
            export(cell, out)
        assert not out.exists()
        assert tree_digests(cell) == before  # refusal copies nothing, mutates nothing

    def test_symlink_payload_aborts_before_any_copy(self, tmp_path: Path) -> None:
        cell = tmp_path / "cell"
        cell.mkdir()
        (cell / "clean.py").write_text(CLEAN, encoding="utf-8")
        outside = tmp_path / "outside-target.txt"
        outside.write_text(CLEAN, encoding="utf-8")
        (cell / "evil.py").symlink_to(outside)
        out = tmp_path / "bundle"
        before = tree_digests(cell)
        with pytest.raises(ExportRefused, match="symlink"):
            export(cell, out)
        assert not out.exists()
        assert tree_digests(cell) == before

    def test_collect_flags_escaping_symlink_as_unsafe(self, tmp_path: Path) -> None:
        cell = tmp_path / "cell"
        (cell / "sub").mkdir(parents=True)
        outside = tmp_path / "outside-target.txt"
        outside.write_text(CLEAN, encoding="utf-8")
        (cell / "sub" / "escape.py").symlink_to(outside)
        relatives, unsafe = collect(cell, ("*.py",))
        assert relatives == []
        assert unsafe == ["sub/escape.py"]
        out = tmp_path / "bundle"
        before = tree_digests(tmp_path)
        with pytest.raises(ExportRefused):
            export(cell, out, includes=("*.py",))
        assert not out.exists()
        assert tree_digests(tmp_path) == before

    def test_out_must_not_be_inside_the_cell(self, tmp_path: Path) -> None:
        cell = tmp_path / "cell"
        cell.mkdir()
        with pytest.raises(ExportRefused):
            export(cell, cell / "inside")

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        cell = tmp_path / "cell"
        cell.mkdir()
        (cell / "producer.py").write_text(CLEAN, encoding="utf-8")
        out = tmp_path / "bundle"
        bundle = export(cell, out, dry_run=True)
        assert bundle["files"][0]["path"] == "producer.py"
        assert not out.exists()

    def test_generated_and_state_files_are_skipped(self, tmp_path: Path) -> None:
        cell = tmp_path / "cell"
        (cell / "state").mkdir(parents=True)
        (cell / "__pycache__").mkdir()
        (cell / "producer.py").write_text(CLEAN, encoding="utf-8")
        (cell / "state" / "platform.json").write_text("{}", encoding="utf-8")
        (cell / "__pycache__" / "producer.pyc").write_bytes(b"x")
        (cell / "runtime.log").write_text("log\n", encoding="utf-8")
        bundle = export(cell, tmp_path / "bundle", dry_run=True)
        assert [entry["path"] for entry in bundle["files"]] == ["producer.py"]


class TestMainEntryPoint:
    def test_verify_exit_codes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        build_tree(tmp_path)
        manifest_path = tmp_path / "layer-r" / "SOURCE_MANIFEST.json"
        write_manifest(manifest_path, classed(tmp_path))
        assert main(["verify", "--root", str(tmp_path)]) == 0

        (tmp_path / "layer-r" / "source" / "cell.py").write_text(
            "value = 2\n", encoding="utf-8"
        )
        assert main(["verify", "--root", str(tmp_path)]) == 1
        assert "FAILED" in capsys.readouterr().err

        manifest_path.unlink()
        assert main(["verify", "--root", str(tmp_path)]) == 2

    def test_generate_refuses_symlink_via_cli(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        outside = tmp_path / "outside-target.txt"
        outside.write_text(CLEAN, encoding="utf-8")
        (tmp_path / "layer-r" / "source" / "link.py").symlink_to(outside)
        code = main(
            [
                "generate",
                "--root",
                str(tmp_path),
                "--default-class",
                "source",
            ]
        )
        assert code == 2


class TestWriteManifest:
    def test_write_then_load_roundtrip(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        target = tmp_path / "layer-r" / "SOURCE_MANIFEST.json"
        write_manifest(target, manifest)
        assert load_manifest(target)["entries"] == manifest["entries"]
