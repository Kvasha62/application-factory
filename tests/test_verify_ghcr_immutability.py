"""Non-network unit tests for the read-only GHCR immutability verifier.

Covers independent digest/size calculation, strict PASS / FAIL / ACCESS BLOCKED
classification, credential redaction, the oras read-only command surface, and
the least-privilege shape of the dispatch-only workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.verify_ghcr_immutability import (
    ACCESS_BLOCKED,
    CLASSIFICATIONS,
    EXPECTED_MANIFEST_DIGESTS,
    FAIL,
    PASS,
    ContentUnavailableError,
    OrasReader,
    main,
    overall_status,
    redact_secrets,
    render_markdown,
    repository_for,
    sha256_digest,
    verify_all,
    verify_component,
    write_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "verify-ghcr-immutability.yml"

EMPTY_CONFIG = b"{}"
CANONICAL_MT = "application/vnd.application-factory.canonical-manifest.v1+json"
SOURCE_MT = "application/vnd.application-factory.source-package.v1.tar+gzip"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class InMemoryReader:
    """Read-only in-memory registry; records every access, never mutates."""

    def __init__(self) -> None:
        self.manifests: dict[tuple[str, str], bytes] = {}
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.blocked: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, str, str]] = []

    def fetch_manifest(self, repository: str, digest: str) -> bytes:
        self.calls.append(("manifest", repository, digest))
        key = (repository, digest)
        if key in self.blocked or key not in self.manifests:
            raise ContentUnavailableError(f"manifest {repository}@{digest}: 404")
        return self.manifests[key]

    def fetch_blob(self, repository: str, digest: str) -> bytes:
        self.calls.append(("blob", repository, digest))
        key = (repository, digest)
        if key in self.blocked or key not in self.blobs:
            raise ContentUnavailableError(f"blob {repository}@{digest}: 404")
        return self.blobs[key]


def _publish(
    reader: InMemoryReader,
    component: str,
    *,
    layers: list[bytes] | None = None,
    config: bytes = EMPTY_CONFIG,
) -> tuple[str, dict[str, Any]]:
    """Register a well-formed artifact and return (manifest_digest, manifest)."""
    repository = repository_for(component)
    layer_blobs = (
        layers
        if layers is not None
        else [
            json.dumps({"component_id": component}).encode(),
            b"\x1f\x8b" + component.encode() * 5,
        ]
    )
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": "application/vnd.application-factory.component.v1",
        "config": {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": _digest(config),
            "size": len(config),
        },
        "layers": [
            {
                "mediaType": CANONICAL_MT if i == 0 else SOURCE_MT,
                "digest": _digest(blob),
                "size": len(blob),
            }
            for i, blob in enumerate(layer_blobs)
        ],
    }
    reader.blobs[(repository, _digest(config))] = config
    for blob in layer_blobs:
        reader.blobs[(repository, _digest(blob))] = blob
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    digest = _digest(raw)
    reader.manifests[(repository, digest)] = raw
    return digest, manifest


# ---------------------------------------------------------------------------
# Fixed reference set
# ---------------------------------------------------------------------------


def test_exactly_nine_digest_addressed_references_are_under_test() -> None:
    assert sorted(EXPECTED_MANIFEST_DIGESTS) == [
        "authorization",
        "booking",
        "commerce",
        "idempotency",
        "identity",
        "learning",
        "records",
        "saga",
        "tenant_authority",
    ]
    for digest in EXPECTED_MANIFEST_DIGESTS.values():
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert len(set(EXPECTED_MANIFEST_DIGESTS.values())) == 9
    assert repository_for("saga") == "ghcr.io/kvasha62/application-factory/saga"


def test_sha256_digest_is_independent_of_registry_claims() -> None:
    assert sha256_digest(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_digest(b"{}") == (
        "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_pass_requires_manifest_and_every_blob_fetched_and_verified() -> None:
    reader = InMemoryReader()
    digest, manifest = _publish(reader, "saga")
    result = verify_component(reader, "saga", digest)

    assert result.classification == PASS
    assert result.reasons == ()
    assert result.manifest_fetched is True
    assert result.manifest_computed_digest == digest
    assert result.artifact_type == manifest["artifactType"]
    assert [d.role for d in result.descriptors] == ["config", "layer[0]", "layer[1]"]
    assert all(d.verified for d in result.descriptors)
    assert [d.media_type for d in result.descriptors][1:] == [CANONICAL_MT, SOURCE_MT]
    # every referenced blob was actually fetched from the "registry"
    fetched = {d for kind, _, d in reader.calls if kind == "blob"}
    assert fetched == {manifest["config"]["digest"]} | {
        layer["digest"] for layer in manifest["layers"]
    }


def test_manifest_digest_mismatch_is_fail_not_pass() -> None:
    reader = InMemoryReader()
    digest, _ = _publish(reader, "saga")
    repository = repository_for("saga")
    tampered = reader.manifests[(repository, digest)] + b" "
    reader.manifests[(repository, digest)] = tampered  # registry lies about digest

    result = verify_component(reader, "saga", digest)
    assert result.classification == FAIL
    assert result.manifest_computed_digest == _digest(tampered)
    assert any("manifest digest mismatch" in r for r in result.reasons)


def test_blob_digest_mismatch_is_fail() -> None:
    reader = InMemoryReader()
    digest, manifest = _publish(reader, "booking")
    repository = repository_for("booking")
    layer_digest = manifest["layers"][1]["digest"]
    original = reader.blobs[(repository, layer_digest)]
    reader.blobs[(repository, layer_digest)] = b"X" + original[1:]  # same size

    result = verify_component(reader, "booking", digest)
    assert result.classification == FAIL
    layer = next(d for d in result.descriptors if d.role == "layer[1]")
    assert layer.result == "digest_mismatch"
    assert layer.fetched_size == layer.declared_size


def test_blob_size_mismatch_is_fail_even_when_digest_claims_match() -> None:
    reader = InMemoryReader()
    digest, _ = _publish(reader, "records")
    repository = repository_for("records")
    bad = json.loads(reader.manifests[(repository, digest)])
    bad["layers"][0]["size"] += 1
    raw = json.dumps(bad, separators=(",", ":")).encode()
    new_digest = _digest(raw)
    reader.manifests[(repository, new_digest)] = raw

    result = verify_component(reader, "records", new_digest)
    assert result.classification == FAIL
    layer = next(d for d in result.descriptors if d.role == "layer[0]")
    assert layer.result == "size_mismatch"
    assert layer.computed_digest == layer.declared_digest


def test_unreachable_manifest_is_access_blocked() -> None:
    reader = InMemoryReader()
    result = verify_component(reader, "identity", EXPECTED_MANIFEST_DIGESTS["identity"])
    assert result.classification == ACCESS_BLOCKED
    assert result.manifest_fetched is False
    assert result.descriptors == ()
    assert "404" in result.reasons[0]


def test_missing_referenced_blob_is_access_blocked_not_pass() -> None:
    reader = InMemoryReader()
    digest, manifest = _publish(reader, "learning")
    repository = repository_for("learning")
    reader.blocked.add((repository, manifest["layers"][1]["digest"]))

    result = verify_component(reader, "learning", digest)
    assert result.classification == ACCESS_BLOCKED
    assert next(d for d in result.descriptors if d.role == "layer[1]").result == (
        "unavailable"
    )
    # the other descriptors were still independently verified and recorded
    assert next(d for d in result.descriptors if d.role == "config").verified


def test_access_blocked_takes_precedence_over_pass_but_fail_is_recorded() -> None:
    reader = InMemoryReader()
    digest, manifest = _publish(reader, "commerce")
    repository = repository_for("commerce")
    reader.blocked.add((repository, manifest["layers"][0]["digest"]))
    layer1 = manifest["layers"][1]["digest"]
    reader.blobs[(repository, layer1)] = b"tampered-bytes-of-same-len"[
        : manifest["layers"][1]["size"]
    ]

    result = verify_component(reader, "commerce", digest)
    assert result.classification in (FAIL, ACCESS_BLOCKED)
    assert result.classification != PASS
    assert any("digest_mismatch" in r for r in result.reasons)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m.pop("config"),
        lambda m: m.pop("layers"),
        lambda m: m.__setitem__("layers", []),
        lambda m: m["layers"][0].pop("size"),
        lambda m: m["layers"][0].pop("mediaType"),
        lambda m: m["layers"][0].__setitem__("digest", "md5:abc"),
    ],
)
def test_malformed_manifest_descriptors_are_fail(mutation) -> None:
    reader = InMemoryReader()
    digest, _ = _publish(reader, "tenant_authority")
    repository = repository_for("tenant_authority")
    broken = json.loads(reader.manifests[(repository, digest)])
    mutation(broken)
    raw = json.dumps(broken, separators=(",", ":")).encode()
    new_digest = _digest(raw)
    reader.manifests[(repository, new_digest)] = raw

    result = verify_component(reader, "tenant_authority", new_digest)
    assert result.classification == FAIL


def test_non_json_manifest_is_fail() -> None:
    reader = InMemoryReader()
    repository = repository_for("saga")
    raw = b"not json"
    reader.manifests[(repository, _digest(raw))] = raw
    result = verify_component(reader, "saga", _digest(raw))
    assert result.classification == FAIL
    assert "not a JSON object" in result.reasons[0]


def test_invalid_expected_digest_never_touches_the_registry() -> None:
    reader = InMemoryReader()
    result = verify_component(reader, "saga", "latest")
    assert result.classification == FAIL
    assert reader.calls == []


def test_classification_vocabulary_is_closed() -> None:
    assert CLASSIFICATIONS == ("PASS", "FAIL", "ACCESS BLOCKED")


# ---------------------------------------------------------------------------
# Aggregate / evidence
# ---------------------------------------------------------------------------


def _full_pass_reader() -> tuple[InMemoryReader, dict[str, str]]:
    reader = InMemoryReader()
    expected = {}
    for component in EXPECTED_MANIFEST_DIGESTS:
        expected[component], _ = _publish(reader, component)
    return reader, expected


def test_overall_is_pass_only_when_all_nine_pass(tmp_path: Path) -> None:
    reader, expected = _full_pass_reader()
    results = verify_all(reader, expected)
    assert len(results) == 9
    assert overall_status(results) == PASS

    reader.blocked.add((repository_for("idempotency"), expected["idempotency"]))
    results = verify_all(reader, expected)
    assert [r.classification for r in results].count(PASS) == 8
    assert overall_status(results) == ACCESS_BLOCKED

    tampered = reader.manifests[(repository_for("saga"), expected["saga"])] + b"\n"
    reader.manifests[(repository_for("saga"), expected["saga"])] = tampered
    results = verify_all(reader, expected)
    assert overall_status(results) == FAIL  # FAIL dominates ACCESS BLOCKED

    assert overall_status([]) == FAIL


def test_evidence_is_machine_and_human_readable_and_credential_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_thisIsASecretToken0123456789abcdef")
    reader, expected = _full_pass_reader()
    results = verify_all(reader, expected)
    document = write_evidence(
        results,
        tmp_path,
        context={
            "note": "Authorization: Bearer ghs_thisIsASecretToken0123456789abcdef"
        },
    )

    data = json.loads((tmp_path / "verification-matrix.json").read_text())
    assert data["overall"] == PASS
    assert document["overall"] == PASS
    assert len(data["components"]) == 9
    row = data["components"][0]
    assert set(row) >= {
        "component",
        "remote_ref",
        "expected_manifest_digest",
        "manifest_computed_digest",
        "manifest_digest_verified",
        "descriptors",
        "classification",
    }
    assert row["manifest_digest_verified"] is True
    assert {d["role"] for d in row["descriptors"]} == {"config", "layer[0]", "layer[1]"}
    assert all(
        {
            "media_type",
            "declared_digest",
            "declared_size",
            "computed_digest",
            "fetched_size",
            "result",
        }
        <= set(d)
        for d in row["descriptors"]
    )

    text = (tmp_path / "verification-matrix.json").read_text()
    md = (tmp_path / "verification-matrix.md").read_text()
    assert "ghs_thisIsASecret" not in text
    assert "<REDACTED>" in text
    assert md.count("| **PASS** |") == 9
    assert "**Overall: PASS**" in md


def test_render_markdown_reports_blocked_rows_explicitly() -> None:
    reader = InMemoryReader()
    results = verify_all(reader, {"saga": EXPECTED_MANIFEST_DIGESTS["saga"]})
    md = render_markdown(results)
    assert "not fetched" in md
    assert "**ACCESS BLOCKED**" in md
    assert "### Reasons" in md


def test_main_exit_code_is_nonzero_unless_all_pass(tmp_path: Path) -> None:
    reader, expected = _full_pass_reader()
    # main() always verifies the fixed production set; publish under those digests
    for component, digest in EXPECTED_MANIFEST_DIGESTS.items():
        repo = repository_for(component)
        reader.manifests[(repo, digest)] = reader.manifests[(repo, expected[component])]
    # digest of the bytes will not match the production digest -> FAIL
    assert main(["--output", str(tmp_path / "a")], reader=reader) == 1
    assert (tmp_path / "a" / "verification-matrix.json").is_file()

    assert main(["--output", str(tmp_path / "b")], reader=InMemoryReader()) == 1
    doc = json.loads((tmp_path / "b" / "verification-matrix.json").read_text())
    assert doc["overall"] == ACCESS_BLOCKED
    assert {c["classification"] for c in doc["components"]} == {ACCESS_BLOCKED}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "error: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 rejected",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "password=hunter22 token: abc12345",
        "GET https://user:s3cr3tpw@ghcr.io/v2/ failed",
        "github_pat_11AAAAAAA0bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ],
)
def test_redact_secrets_scrubs_tokens_and_authorization_material(raw: str) -> None:
    out = redact_secrets(raw)
    for needle in (
        "ghp_ABCDEF",
        "eyJhbGci",
        "hunter22",
        "abc12345",
        "s3cr3tpw",
        "github_pat_11",
    ):
        assert needle not in out
    assert "<REDACTED>" in out


def test_redact_secrets_scrubs_environment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "plain-env-token-value")
    assert "plain-env-token-value" not in redact_secrets("x plain-env-token-value y")


# ---------------------------------------------------------------------------
# ORAS reader: read-only command surface
# ---------------------------------------------------------------------------


def test_oras_reader_only_issues_fetch_commands_and_reads_exact_bytes() -> None:
    commands: list[list[str]] = []
    payload = b'{"schemaVersion":2}'

    def fake_run(argv, capture_output, check):
        commands.append(list(argv))
        out = Path(argv[argv.index("--output") + 1])
        out.write_bytes(payload)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    reader = OrasReader(runner=fake_run)
    repo = repository_for("saga")
    digest = _digest(payload)
    assert reader.fetch_manifest(repo, digest) == payload
    assert reader.fetch_blob(repo, digest) == payload

    assert [c[:3] for c in commands] == [
        ["oras", "manifest", "fetch"],
        ["oras", "blob", "fetch"],
    ]
    for argv in commands:
        assert argv[-1] == f"{repo}@{digest}"  # digest-addressed, never a tag
        assert not {"push", "tag", "delete", "attach", "copy", "login"} & set(argv)


def test_oras_reader_failure_is_content_unavailable_and_redacted() -> None:
    def failing_run(argv, capture_output, check):
        return subprocess.CompletedProcess(
            argv,
            1,
            b"",
            b"Error: unauthorized: Bearer ghs_SECRETSECRETSECRETSECRET1234",
        )

    reader = OrasReader(runner=failing_run)
    with pytest.raises(ContentUnavailableError) as info:
        reader.fetch_manifest(repository_for("saga"), EXPECTED_MANIFEST_DIGESTS["saga"])
    assert "ghs_SECRET" not in str(info.value)
    assert "exited 1" in str(info.value)


def test_oras_reader_missing_binary_is_content_unavailable() -> None:
    def missing(argv, capture_output, check):
        raise FileNotFoundError("oras")

    with pytest.raises(ContentUnavailableError, match="could not be executed"):
        OrasReader(runner=missing).fetch_blob(
            repository_for("saga"), "sha256:" + "0" * 64
        )


def test_oras_reader_empty_output_file_is_content_unavailable() -> None:
    def no_file(argv, capture_output, check):
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with pytest.raises(ContentUnavailableError, match="wrote no content"):
        OrasReader(runner=no_file).fetch_blob(
            repository_for("saga"), "sha256:" + "0" * 64
        )


# ---------------------------------------------------------------------------
# Workflow shape (static, no YAML dependency)
# ---------------------------------------------------------------------------


def test_workflow_is_dispatch_only_least_privilege_and_read_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )

    on_block = re.search(r"^on:\n((?:[ \t]+.*\n?)+)", body, re.MULTILINE)
    assert on_block is not None
    triggers = re.findall(r"^[ \t]+([a-z_]+):", on_block.group(1), re.MULTILINE)
    assert triggers == ["workflow_dispatch"]

    perms = re.search(r"^permissions:\n((?:[ \t]+.*\n?)+)", body, re.MULTILINE)
    assert perms is not None
    granted = dict(
        re.findall(r"^[ \t]+([a-z-]+):[ \t]*([a-z]+)", perms.group(1), re.MULTILINE)
    )
    assert granted == {"contents": "read", "packages": "read"}
    assert "packages: write" not in body

    assert "runs-on: ubuntu-latest" in body
    assert "scripts/verify_ghcr_immutability.py" in body
    assert "oras-project/setup-oras@" in body
    assert "--password-stdin" in body
    assert "upload-artifact" in body
    for forbidden in (
        "oras push",
        "oras tag",
        "oras attach",
        "oras cp",
        "oras manifest delete",
        "oras blob delete",
        "build_component_artifacts.py",
        "--publish-ghcr",
        "--reconcile-registry",
    ):
        assert forbidden not in body
