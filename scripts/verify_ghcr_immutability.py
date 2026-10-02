"""Read-only, independent verification of immutable OCI content in GHCR.

This verifier fetches *actual remote bytes* for a fixed, digest-addressed set
of component artifacts and checks them independently of the registry:

* the manifest is fetched by exact digest and its SHA-256 is recomputed;
* every descriptor referenced by the manifest (``config`` and each ``layers``
  entry) is fetched as a blob, SHA-256 is recomputed and the byte count is
  compared with the descriptor ``size``;
* ``mediaType``, digest, size and result are recorded for every descriptor.

Classification is strict and exactly one of:

``PASS``
    manifest **and** every referenced blob were fetched and independently
    digest/size verified;
``FAIL``
    remote content was fetched and at least one check failed;
``ACCESS BLOCKED``
    required remote content could not be obtained (network, auth, not found,
    missing client).

The verifier never pushes, tags, deletes or otherwise mutates registry state,
never rebuilds local artifacts as a substitute for remote content, and never
writes credentials into its evidence output. GitHub Packages metadata is not
consulted: only remote OCI bytes count as evidence.

Two expectation sources are supported:

``--mode derived`` (default): the expected OCI manifest digest for each
component is recomputed from THIS checkout by running the repository's own
deterministic build (``build_all``), and the fetched remote manifest must in
additionally carry exactly one canonical-manifest layer whose descriptor
digest equals the source-package lock in ``COMPONENT_VERSION_DIGESTS``. This
is what proves "the bytes in GHCR are the deterministic envelope of the
current sources", not merely "some immutable bytes exist at a pinned digest".

``--mode legacy``: validates the pinned 2026-10-01 ORAS-era envelope digests
(mirrored in ``scripts/ghcr_legacy_envelopes.json``) for explicit forensic
re-validation of historical evidence only. It is not the default gate and is
never a publication authority.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

REGISTRY = "ghcr.io"
NAMESPACE = "kvasha62/application-factory"

#: Historical ORAS-era envelope digests: the 2026-10-01 tag publication built
#: from commit c76cd983 by the removed publish_item_to_ghcr step. They are
#: retained ONLY as explicitly requested, non-default legacy evidence (immutable
#: content gate run 36975841017) and are never a publication authority. Floating
#: tags are never consulted in any mode.
LEGACY_EXPECTED_MANIFEST_DIGESTS: dict[str, str] = {
    "authorization": (
        "sha256:d3c4796fb1887e7113780a103b7352f6e36d16bc6e4bb7a3b9ddd656d84a4a0b"
    ),
    "booking": (
        "sha256:7394dbe2e94f336f9c2c9f27f6a06ac8cb33d9e427b2d8c7cea828bac80a5d67"
    ),
    "commerce": (
        "sha256:ecbff623cc06fa6174c87d5d79e1613de3125a6507fb065708c3e577aaadd981"
    ),
    "idempotency": (
        "sha256:de7a802961096cb81bde6197786d12f22f68e0e1f5607a0eb5c3fd16b5770a49"
    ),
    "identity": (
        "sha256:380c790df96f78d0ea53b0971af2e692d2341b46d34be42768b24d307d729351"
    ),
    "learning": (
        "sha256:b106c15d445cbe63c460648d5e4bbfa495482e4f2561f7df7bb2b6c41d96341b"
    ),
    "records": (
        "sha256:d494d49ee9e7a890995fb4a8bd7e644ab7b4ce8adc9c0e26b4df14493e5e5ed3"
    ),
    "saga": ("sha256:d8e093fc877ad1ab777ccae5ddabd8bbdceecea75d53d6d91193352a95b623ee"),
    "tenant_authority": (
        "sha256:b051749621e02f5ad01be08cc565c760cdb01a1756ebba0f1cf738e99876b1db"
    ),
}

#: Back-compat alias: the pinned legacy set, no longer the default gate.
EXPECTED_MANIFEST_DIGESTS = LEGACY_EXPECTED_MANIFEST_DIGESTS

#: Media type of the canonical source-package manifest layer inside the OCI
#: envelope; in derived mode its descriptor digest must equal the lock.
CANONICAL_LAYER_MEDIA_TYPE = (
    "application/vnd.application-factory.canonical-manifest.v1+json"
)

_LEGACY_FILE = Path(__file__).with_name("ghcr_legacy_envelopes.json")

PASS = "PASS"
FAIL = "FAIL"
ACCESS_BLOCKED = "ACCESS BLOCKED"
CLASSIFICATIONS = (PASS, FAIL, ACCESS_BLOCKED)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"((?i:authorization)\s*[:=]\s*)([^\s\"']+(?:\s+[^\s\"']+)?)"),
    re.compile(r"((?i:bearer)\s+)([A-Za-z0-9._\-+/=]{8,})"),
    re.compile(r"((?i:password|token|secret)\s*[:=]\s*)([^\s\"']+)"),
    re.compile(r"(https?://)([^/\s:@\"']+):([^@\s\"']+)@"),
)


class ContentUnavailableError(Exception):
    """Remote content could not be obtained (classified as ACCESS BLOCKED)."""


def redact_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    """Scrub credentials and tokens from any text that reaches the evidence."""
    redacted = str(text)
    env_secrets = [
        os.environ.get("GITHUB_TOKEN", ""),
        os.environ.get("GHCR_TOKEN", ""),
        os.environ.get("ACTIONS_RUNTIME_TOKEN", ""),
    ]
    for secret in (*secrets, *env_secrets):
        if isinstance(secret, str) and secret.strip():
            redacted = redacted.replace(secret.strip(), "<REDACTED>")
    redacted = _SECRET_PATTERNS[0].sub("<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[1].sub("<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[2].sub(r"\1<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[3].sub(r"\1<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[4].sub(r"\1<REDACTED>", redacted)
    redacted = _SECRET_PATTERNS[5].sub(r"\1<REDACTED>@", redacted)
    return redacted


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def repository_for(component: str) -> str:
    return f"{REGISTRY}/{NAMESPACE}/{component}"


class OCIReader(Protocol):
    """Read-only access to an OCI registry. No method may mutate state."""

    def fetch_manifest(self, repository: str, digest: str) -> bytes: ...

    def fetch_blob(self, repository: str, digest: str) -> bytes: ...


class OrasReader:
    """Read-only reader backed by the ``oras`` CLI (manifest/blob fetch only)."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        oras_binary: str = "oras",
    ) -> None:
        self._run = runner
        self._oras = oras_binary

    def _fetch(self, subcommand: str, repository: str, digest: str) -> bytes:
        """Run ``oras <subcommand> fetch --output <tmpfile> <ref>`` and read it.

        The content is always written to a file (never parsed from stdout) so
        the bytes are exactly what the registry served.
        """
        ref = f"{repository}@{digest}"
        what = f"{subcommand} {ref}"
        with tempfile.TemporaryDirectory(prefix="ghcr-verify-") as tmp:
            target = Path(tmp) / "content"
            argv = [self._oras, subcommand, "fetch", "--output", str(target), ref]
            try:
                proc = self._run(argv, capture_output=True, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ContentUnavailableError(
                    f"{what}: oras could not be executed: "
                    f"{redact_secrets(str(exc))}"
                ) from exc
            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
                raise ContentUnavailableError(
                    f"{what}: oras exited {proc.returncode}: "
                    f"{redact_secrets(stderr.strip())[:500]}"
                )
            if not target.is_file():
                raise ContentUnavailableError(
                    f"{what}: oras exited 0 but wrote no content file"
                )
            return target.read_bytes()

    def fetch_manifest(self, repository: str, digest: str) -> bytes:
        return self._fetch("manifest", repository, digest)

    def fetch_blob(self, repository: str, digest: str) -> bytes:
        return self._fetch("blob", repository, digest)


@dataclasses.dataclass(frozen=True)
class DescriptorResult:
    role: str  # "config" | "layer[<n>]"
    media_type: str | None
    declared_digest: str | None
    declared_size: int | None
    computed_digest: str | None
    fetched_size: int | None
    result: str  # "verified" | "digest_mismatch" | "size_mismatch" | ...
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.result == "verified"


@dataclasses.dataclass(frozen=True)
class ComponentResult:
    component: str
    remote_ref: str
    expected_manifest_digest: str
    manifest_fetched: bool
    manifest_computed_digest: str | None
    manifest_size: int | None
    manifest_media_type: str | None
    artifact_type: str | None
    descriptors: tuple[DescriptorResult, ...]
    classification: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "remote_ref": self.remote_ref,
            "expected_manifest_digest": self.expected_manifest_digest,
            "manifest_fetched": self.manifest_fetched,
            "manifest_computed_digest": self.manifest_computed_digest,
            "manifest_digest_verified": (
                self.manifest_computed_digest == self.expected_manifest_digest
            ),
            "manifest_size": self.manifest_size,
            "manifest_media_type": self.manifest_media_type,
            "artifact_type": self.artifact_type,
            "descriptors": [
                {
                    **dataclasses.asdict(d),
                    "detail": redact_secrets(d.detail),
                }
                for d in self.descriptors
            ],
            "classification": self.classification,
            "reasons": [redact_secrets(r) for r in self.reasons],
        }


def _descriptor_fields(
    descriptor: Any,
) -> tuple[str | None, str | None, int | None]:
    if not isinstance(descriptor, Mapping):
        return None, None, None
    media_type = descriptor.get("mediaType")
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    return (
        media_type if isinstance(media_type, str) else None,
        digest if isinstance(digest, str) else None,
        size if isinstance(size, int) and not isinstance(size, bool) else None,
    )


def _verify_descriptor(
    reader: OCIReader, repository: str, role: str, descriptor: Any
) -> DescriptorResult:
    media_type, digest, size = _descriptor_fields(descriptor)
    if digest is None or not _DIGEST_RE.match(digest):
        return DescriptorResult(
            role,
            media_type,
            digest,
            size,
            None,
            None,
            "invalid_descriptor",
            "descriptor digest missing or not a sha256 digest",
        )
    if size is None or size < 0:
        return DescriptorResult(
            role,
            media_type,
            digest,
            size,
            None,
            None,
            "invalid_descriptor",
            "descriptor size missing or invalid",
        )
    if media_type is None:
        return DescriptorResult(
            role,
            media_type,
            digest,
            size,
            None,
            None,
            "invalid_descriptor",
            "descriptor mediaType missing",
        )
    try:
        data = reader.fetch_blob(repository, digest)
    except ContentUnavailableError as exc:
        return DescriptorResult(
            role, media_type, digest, size, None, None, "unavailable", str(exc)
        )
    computed = sha256_digest(data)
    fetched_size = len(data)
    if computed != digest:
        return DescriptorResult(
            role,
            media_type,
            digest,
            size,
            computed,
            fetched_size,
            "digest_mismatch",
            f"computed {computed} != declared {digest}",
        )
    if fetched_size != size:
        return DescriptorResult(
            role,
            media_type,
            digest,
            size,
            computed,
            fetched_size,
            "size_mismatch",
            f"fetched {fetched_size} bytes != declared {size}",
        )
    return DescriptorResult(
        role, media_type, digest, size, computed, fetched_size, "verified"
    )


def verify_component(
    reader: OCIReader,
    component: str,
    expected_digest: str,
    *,
    expected_canonical_lock: str | None = None,
) -> ComponentResult:
    """Verify one digest-addressed artifact. Pure function of remote bytes.

    When ``expected_canonical_lock`` is provided, the fetched manifest must
    additionally carry exactly one canonical-manifest layer whose descriptor
    digest equals that source-package lock (derived mode's identity binding).
    """
    repository = repository_for(component)
    remote_ref = f"{repository}@{expected_digest}"
    reasons: list[str] = []

    def blocked(reason: str) -> ComponentResult:
        return ComponentResult(
            component,
            remote_ref,
            expected_digest,
            False,
            None,
            None,
            None,
            None,
            (),
            ACCESS_BLOCKED,
            (reason,),
        )

    if not _DIGEST_RE.match(expected_digest):
        return ComponentResult(
            component,
            remote_ref,
            expected_digest,
            False,
            None,
            None,
            None,
            None,
            (),
            FAIL,
            ("expected digest is not a valid sha256 digest",),
        )

    try:
        manifest_bytes = reader.fetch_manifest(repository, expected_digest)
    except ContentUnavailableError as exc:
        return blocked(str(exc))

    computed = sha256_digest(manifest_bytes)
    if computed != expected_digest:
        reasons.append(
            f"manifest digest mismatch: computed {computed}, "
            f"expected {expected_digest}"
        )

    try:
        manifest = json.loads(manifest_bytes)
    except ValueError:
        manifest = None
    if not isinstance(manifest, Mapping):
        reasons.append("manifest is not a JSON object")
        return ComponentResult(
            component,
            remote_ref,
            expected_digest,
            True,
            computed,
            len(manifest_bytes),
            None,
            None,
            (),
            FAIL,
            tuple(reasons),
        )

    media_type = manifest.get("mediaType")
    artifact_type = manifest.get("artifactType")
    descriptors: list[DescriptorResult] = []

    if "config" in manifest:
        descriptors.append(
            _verify_descriptor(reader, repository, "config", manifest["config"])
        )
    else:
        reasons.append("manifest has no config descriptor")

    layers = manifest.get("layers")
    if not isinstance(layers, list):
        reasons.append("manifest has no layers list")
        layers = []
    elif not layers:
        reasons.append("manifest layers list is empty")
    for index, layer in enumerate(layers):
        descriptors.append(
            _verify_descriptor(reader, repository, f"layer[{index}]", layer)
        )

    if expected_canonical_lock is not None:
        canonical_layers = [
            layer
            for layer in layers
            if isinstance(layer, Mapping)
            and layer.get("mediaType") == CANONICAL_LAYER_MEDIA_TYPE
        ]
        if len(canonical_layers) != 1:
            reasons.append(
                "expected exactly one canonical-manifest layer, found "
                f"{len(canonical_layers)}"
            )
        elif canonical_layers[0].get("digest") != expected_canonical_lock:
            reasons.append(
                f"canonical layer digest {canonical_layers[0].get('digest')!r} "
                "does not match the source-package lock "
                f"{expected_canonical_lock!r}"
            )

    unavailable = [d for d in descriptors if d.result == "unavailable"]
    failed = [d for d in descriptors if not d.verified and d.result != "unavailable"]
    for d in failed:
        reasons.append(f"{d.role}: {d.result} ({d.detail})")
    for d in unavailable:
        reasons.append(f"{d.role}: {d.detail}")

    if unavailable:
        classification = ACCESS_BLOCKED
    elif reasons:
        classification = FAIL
    else:
        classification = PASS

    return ComponentResult(
        component,
        remote_ref,
        expected_digest,
        True,
        computed,
        len(manifest_bytes),
        media_type if isinstance(media_type, str) else None,
        artifact_type if isinstance(artifact_type, str) else None,
        tuple(descriptors),
        classification,
        tuple(reasons),
    )


def verify_all(
    reader: OCIReader,
    expected: Mapping[str, str] | None = None,
    *,
    canonical_locks: Mapping[str, str] | None = None,
) -> list[ComponentResult]:
    expected_map = EXPECTED_MANIFEST_DIGESTS if expected is None else expected
    return [
        verify_component(
            reader,
            component,
            digest,
            expected_canonical_lock=(
                None if canonical_locks is None else canonical_locks.get(component)
            ),
        )
        for component, digest in sorted(expected_map.items())
    ]


def overall_status(results: Sequence[ComponentResult]) -> str:
    """Strict aggregate: PASS only if every component is PASS."""
    classes = {r.classification for r in results}
    if not results or classes - set(CLASSIFICATIONS):
        return FAIL
    if classes == {PASS}:
        return PASS
    if FAIL in classes:
        return FAIL
    return ACCESS_BLOCKED


def render_markdown(
    results: Sequence[ComponentResult], *, mode: str | None = None
) -> str:
    counts = {
        c: sum(1 for r in results if r.classification == c) for c in CLASSIFICATIONS
    }
    title = "## GHCR immutable-content verification"
    if mode:
        title += f" (mode: {mode})"
    lines = [
        title,
        "",
        (
            f"**Overall: {overall_status(results)}** — "
            f"PASS {counts[PASS]} · FAIL {counts[FAIL]} · "
            f"ACCESS BLOCKED {counts[ACCESS_BLOCKED]} (of {len(results)})"
        ),
        "",
        (
            "| component | remote ref | manifest digest (computed == expected) | "
            "config | layers | classification |"
        ),
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.manifest_computed_digest is None:
            manifest_cell = "not fetched"
        else:
            ok = r.manifest_computed_digest == r.expected_manifest_digest
            manifest_cell = (
                f"`{r.manifest_computed_digest}` ({'ok' if ok else 'MISMATCH'})"
            )
        config = [d for d in r.descriptors if d.role == "config"]
        layers = [d for d in r.descriptors if d.role.startswith("layer")]
        config_cell = (
            "; ".join(
                f"`{d.declared_digest}` {d.declared_size}B {d.media_type} → {d.result}"
                for d in config
            )
            or "n/a"
        )
        layers_cell = (
            "<br>".join(
                f"{d.role} `{d.declared_digest}` {d.declared_size}B "
                f"{d.media_type} → {d.result}"
                for d in layers
            )
            or "n/a"
        )
        lines.append(
            f"| {r.component} | `{r.remote_ref}` | {manifest_cell} | "
            f"{config_cell} | {layers_cell} | **{r.classification}** |"
        )
    failures = [r for r in results if r.reasons]
    if failures:
        lines += ["", "### Reasons", ""]
        for r in failures:
            for reason in r.reasons:
                lines.append(f"- **{r.component}**: {redact_secrets(reason)}")
    return "\n".join(lines) + "\n"


def write_evidence(
    results: Sequence[ComponentResult],
    output_dir: Path,
    *,
    context: Mapping[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": "ghcr-immutability-verification/1",
        "mode": mode,
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "registry": REGISTRY,
        "namespace": NAMESPACE,
        "overall": overall_status(results),
        "context": {k: redact_secrets(str(v)) for k, v in (context or {}).items()},
        "components": [r.to_dict() for r in results],
    }
    text = json.dumps(document, indent=2, sort_keys=True)
    (output_dir / "verification-matrix.json").write_text(
        redact_secrets(text) + "\n", encoding="utf-8"
    )
    (output_dir / "verification-matrix.md").write_text(
        render_markdown(results, mode=mode), encoding="utf-8"
    )
    return document


def _context_from_env() -> dict[str, str]:
    keys = (
        "GITHUB_REPOSITORY",
        "GITHUB_SHA",
        "GITHUB_REF",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_WORKFLOW",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "ImageOS",
        "ImageVersion",
    )
    return {k: os.environ[k] for k in keys if k in os.environ}


def derive_expected_manifest_digests(
    repository_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Recompute the deterministic expected OCI manifest digests and locks.

    Runs the repository's own deterministic ``build_all()`` into a temporary
    directory; the registry and the remote are never written. The expected
    remote reference for each component is the OCI manifest digest of the
    layout built from the checked-out sources; the lock is the content-
    addressed source-package digest pinned by ``COMPONENT_VERSION_DIGESTS``.
    """
    repository_root = Path(repository_root)
    for candidate in (repository_root, repository_root / "src"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    build_module = importlib.import_module("scripts.build_component_artifacts")
    with tempfile.TemporaryDirectory(prefix="ghcr-derive-") as tmp:
        results = build_module.build_all(repository_root, Path(tmp))
    expected = {
        str(item["component_id"]): str(item["oci"]["oci_manifest_digest"])
        for item in results
    }
    locks = {
        str(item["component_id"]): str(item["artifact"]["digest"]) for item in results
    }
    return expected, locks


def _load_legacy_expected() -> dict[str, str]:
    """Return the pinned legacy set, cross-checked against its JSON contract."""
    document = json.loads(_LEGACY_FILE.read_text(encoding="utf-8"))
    components = document.get("components") if isinstance(document, Mapping) else None
    if not isinstance(components, Mapping) or dict(components) != dict(
        LEGACY_EXPECTED_MANIFEST_DIGESTS
    ):
        msg = (
            "legacy envelope digests diverge from "
            f"{_LEGACY_FILE.name}; refusing legacy mode"
        )
        raise ValueError(msg)
    return dict(LEGACY_EXPECTED_MANIFEST_DIGESTS)


def main(argv: Sequence[str] | None = None, *, reader: OCIReader | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="directory for verification-matrix.json / .md (credential-free)",
    )
    parser.add_argument("--oras", default="oras", help="oras binary to use")
    parser.add_argument(
        "--mode",
        choices=("derived", "legacy"),
        default="derived",
        help=(
            "derived (default): expected OCI manifest digests and canonical-"
            "layer locks are recomputed from this checkout's deterministic "
            "build; legacy: pinned 2026-10-01 ORAS-era envelope digests for "
            "explicit historical re-validation only"
        ),
    )
    args = parser.parse_args(argv)

    active_reader: OCIReader = (
        reader if reader is not None else OrasReader(oras_binary=args.oras)
    )
    canonical_locks: Mapping[str, str] | None = None
    if args.mode == "derived":
        expected, canonical_locks = derive_expected_manifest_digests(
            Path(__file__).resolve().parents[1]
        )
    else:
        expected = _load_legacy_expected()
    results = verify_all(active_reader, expected, canonical_locks=canonical_locks)
    document = write_evidence(
        results,
        args.output,
        context={**_context_from_env(), "expected_digests_mode": args.mode},
        mode=args.mode,
    )
    sys.stdout.write(render_markdown(results, mode=args.mode))
    status = document["overall"]
    sys.stdout.write(f"\nOVERALL: {status}\n")
    return 0 if status == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
