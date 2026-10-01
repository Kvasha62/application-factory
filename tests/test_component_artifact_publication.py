from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_component_artifacts import (
    COMPONENT_ROOTS,
    build_all,
    declaration_for,
)

EXPECTED_COMPONENTS = {
    "authorization",
    "identity",
    "tenant_authority",
    "records",
    "learning",
    "saga",
    "idempotency",
    "commerce",
    "booking",
}


def test_component_set_is_exactly_the_approved_nine() -> None:
    assert set(COMPONENT_ROOTS) == EXPECTED_COMPONENTS


@pytest.mark.parametrize("component_id", sorted(EXPECTED_COMPONENTS))
def test_component_source_root_exists(component_id: str) -> None:
    root = COMPONENT_ROOTS[component_id]
    assert root.is_dir(), root


@pytest.mark.parametrize("component_id", sorted(EXPECTED_COMPONENTS))
def test_component_declaration_covers_every_physical_entry(component_id: str) -> None:
    root = COMPONENT_ROOTS[component_id]
    declaration = declaration_for(root)

    physical = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
    }
    assert set(declaration) == physical


def test_build_all_creates_content_addressed_manifests(tmp_path: Path) -> None:
    repository_root = Path.cwd()
    output = tmp_path / "artifacts"

    results = build_all(repository_root, output)

    assert {item["component_id"] for item in results} == EXPECTED_COMPONENTS
    for item in results:
        digest = item["artifact"]["digest"]
        assert digest.startswith("sha256:")
        manifest = output / digest / "canonical.json"
        assert manifest.is_file()
        assert manifest.read_bytes()
        assert item["artifact"]["pinned"] is True
        assert item["artifact"]["canonical_form"] == "source_package/v1"
        assert item["artifact"]["canonical_manifest"] == (
            f"factory/artifacts/{digest}/canonical.json"
        )


def test_build_all_is_deterministic(tmp_path: Path) -> None:
    repository_root = Path.cwd()
    first = tmp_path / "first"
    second = tmp_path / "second"

    build_all(repository_root, first)
    build_all(repository_root, second)

    def snapshot(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert snapshot(first) == snapshot(second)


def test_publication_manifest_contains_no_lifecycle_claims(tmp_path: Path) -> None:
    results = build_all(Path.cwd(), tmp_path / "artifacts")
    for item in results:
        assert "deployable" not in item["artifact"]
        assert "publishable" not in item["artifact"]
        assert "publishability_blockers" not in item["artifact"]


def test_unknown_component_set_is_not_silently_accepted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setitem(COMPONENT_ROOTS, "unexpected", Path("src"))
    with pytest.raises(ValueError, match="component set mismatch"):
        build_all(Path(".").resolve(), tmp_path / "artifacts")
