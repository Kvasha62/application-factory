"""Layer R manifest / drift / export tooling — policy and read-only guarantees.

These tests are the CI gate for the Layer R source-control machinery
(``layer-r/``). They prove the fail-closed classification policy, the manifest
integrity rules, and that the drift check and export bundle never write to the
tree they inspect. No production behavior and no production evidence is
produced here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.layer_r_drift_check import CHANGED, EXTRA, IN_SYNC, MISSING, compare
from scripts.layer_r_export_bundle import ExportRefused, export
from scripts.layer_r_manifest import (
    ManifestError,
    generate_manifest,
    load_manifest,
    sha256_of,
    tree_files,
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

    def test_verify_rejects_path_outside_tracked_roots(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        manifest["entries"][0]["path"] = "elsewhere/cell.py"
        errors = verify_tree(tmp_path, manifest)
        assert any("must live under" in error for error in errors)

    def test_verify_rejects_absolute_host_path(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        manifest["entries"][0]["host_path"] = "/srv/running-platform/cell/cell.py"
        errors = verify_tree(tmp_path, manifest)
        assert any("relative to the host" in error for error in errors)

    def test_committed_manifest_verifies(self) -> None:
        root = Path(__file__).resolve().parent.parent
        manifest = load_manifest(root / "layer-r" / "SOURCE_MANIFEST.json")
        assert verify_tree(root, manifest) == []

    def test_tree_files_ignores_generated(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        (tmp_path / "layer-r" / "source" / "__pycache__").mkdir()
        (tmp_path / "layer-r" / "source" / "__pycache__" / "cell.pyc").write_bytes(b"x")
        assert tree_files(tmp_path) == ["layer-r/source/cell.py"]


class TestDriftCheck:
    def _pair(self, tmp_path: Path) -> tuple[Path, dict]:
        build_tree(tmp_path)
        host = tmp_path / "host-cell"
        (host / "layer-r" / "source").mkdir(parents=True)
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

    def test_drift_check_never_writes_the_host_tree(self, tmp_path: Path) -> None:
        host, manifest = self._pair(tmp_path)
        before = {
            path.relative_to(host).as_posix(): sha256_of(path)
            for path in sorted(host.rglob("*"))
            if path.is_file()
        }
        compare(host, manifest)
        after = {
            path.relative_to(host).as_posix(): sha256_of(path)
            for path in sorted(host.rglob("*"))
            if path.is_file()
        }
        assert before == after


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
        with pytest.raises(ExportRefused):
            export(cell, out)
        assert not out.exists()

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


class TestWriteManifest:
    def test_write_then_load_roundtrip(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        manifest = classed(tmp_path)
        target = tmp_path / "layer-r" / "SOURCE_MANIFEST.json"
        write_manifest(target, manifest)
        assert load_manifest(target)["entries"] == manifest["entries"]
