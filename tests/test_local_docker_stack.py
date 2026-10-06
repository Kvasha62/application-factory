"""The Docker launch path of the local Running Platform stack (opt-in).

LOCAL DOCKER / NON-PRODUCTION. The launch path needs a container engine, so the
end-to-end test is skipped — never faked — unless ``AF_LOCAL_DOCKER=1`` is set
and a container engine is on PATH: the normal test suite must not depend on the
user's own Docker state. The structural test always runs: it pins the two sides
of the topology and the absence of secrets/credentials from the stack.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

_LOCAL_DIR = Path(__file__).resolve().parents[1] / "local"
_REPO_ROOT = _LOCAL_DIR.parent
_COMPOSE_FILE = _LOCAL_DIR / "compose.yaml"
_OPT_IN = os.environ.get("AF_LOCAL_DOCKER") == "1"
_HAVE_ENGINE = shutil.which("docker") is not None

needs_docker = pytest.mark.skipif(
    not _OPT_IN,
    reason="set AF_LOCAL_DOCKER=1 to exercise the Docker launch path",
)
needs_engine = pytest.mark.skipif(
    not _HAVE_ENGINE, reason="no container engine on PATH"
)


def _run(
    args: Sequence[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(_REPO_ROOT),
        env=env or os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
        timeout=1200,
    )


def test_compose_declares_two_separated_sides_and_no_credentials() -> None:
    text = _COMPOSE_FILE.read_text(encoding="utf-8")

    assert "running-platform:" in text
    assert "dno:" in text
    assert "local/Dockerfile.running-platform" in text
    assert "local/Dockerfile.dno" in text
    # The stack carries no credentials and no GitHub Environment material:
    # no compose secrets, no token variables, no credential keys.
    assert "secrets:" not in text
    assert "GITHUB_TOKEN" not in text
    assert "credentials:" not in text
    assert "password" not in text


@needs_docker
@needs_engine
def test_docker_stack_accepts_identity_across_the_container_boundary(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    out_dir = tmp_path / "evidence"
    prepare = _run(
        [
            sys.executable,
            "local/tools/prepare_local_platform.py",
            "--work-dir",
            str(work_dir),
            # The D&O container sees the work directory at /work.
            "--runtime-root",
            "/work/dno/runtime",
        ]
    )
    assert prepare.returncode == 0, prepare.stderr
    run = _run(
        [
            sys.executable,
            "local/run_local_evidence.py",
            "--mode",
            "docker",
            "--work-dir",
            str(work_dir),
            "--out",
            str(out_dir),
            "--scenarios",
            "correct",
        ]
    )
    assert run.returncode == 0, run.stdout + run.stderr
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    checks = summary["scenarios"][0]
    assert checks["scenario"] == "correct"
    assert checks["observed_result"] == "accepted"
    assert checks["non_forwarding"]["verdict"] == "one-opaque-key-only"
