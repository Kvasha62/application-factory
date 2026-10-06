#!/usr/bin/env python3
"""Run the local acceptance/refusal evidence scenarios.

LOCAL DOCKER / NON-PRODUCTION. Runs the six producer scenarios of the local
Running Platform stack against the repository's own deployment operation and
writes the transcripts under ``--out``:

* ``correct``           — acceptance: producer emits actual identity, D&O
                          receives it, expected and actual correspond
* ``foreign``           — another platform's identity is refused
* ``stale``             — owner-side freshness is not current: refused
* ``wrong-correlation`` — an answer addressed to another evaluation: refused
* ``contradictory``     — configuration contradicts the served platform id
* ``delay``             — the producer stalls: D&O refuses within its bound
* ``unavailable``       — the owner side answers that it is unavailable

Two modes:

* ``--mode process`` (default) — the Running Platform producer and the D&O
  runner are separate OS processes on loopback; this is the mode that runs
  anywhere, including environments without a container engine;
* ``--mode docker`` — the same producer and the same D&O runner as containers
  of ``local/compose.yaml``, reaching each other over the compose network.

Either way the transport boundary is a real process/container boundary, never
an in-process stub: the boundary is crossed by an HTTP request carrying exactly
one opaque evaluation binding.

Usage::

    python local/run_local_evidence.py --mode process --out local/evidence
    python local/run_local_evidence.py --mode docker  --out local/evidence
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_LOCAL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _LOCAL_DIR.parent
_WORK_DEFAULT = _LOCAL_DIR / "work"
_OUT_DEFAULT = _LOCAL_DIR / "evidence"

CLASSIFICATION = "LOCAL DOCKER / NON-PRODUCTION"
COMPOSE_FILE = _LOCAL_DIR / "compose.yaml"
#: Where the D&O container sees the work directory (see local/compose.yaml).
DOCKER_RUNTIME_ROOT = "/work/dno/runtime"

#: Scenario -> (expected result, refusal reason fragment expected in the logs).
SCENARIOS: dict[str, tuple[str, str]] = {
    "correct": ("accepted", ""),
    # Another platform's observation is not evidence about this one: the
    # correlation rule refuses it, and D&O fails closed with the generic
    # unavailable message (never MATCH, never MISMATCH).
    "foreign": ("refused", "running platform identity evidence is unavailable"),
    "stale": ("refused", "stale"),
    "wrong-correlation": ("refused", "foreign correlation"),
    "contradictory": ("refused", "contradicts actual platform_id"),
    "mismatch": ("refused", "does not match the requested instance"),
    "delay": ("refused", "TimeoutError"),
    "unavailable": ("refused", "HTTP 503"),
}

#: Strings that must never cross the boundary as identity material. The token
#: value is scanned separately (it is the one opaque handle that may cross).
FORBIDDEN_MARKERS = (
    "instance_digest",
    "manifest_digest",
    "expected_instance",
    "identity_provider",
    "golden_bundle",
    "membership_established",
    "provenance",
    "extensions",
    "branding",
    "artifact_type",
    "canonical_form",
    "DeploymentRecord",
)


class HarnessError(Exception):
    """The harness cannot run a scenario."""


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(_REPO_ROOT / "src")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    _, _, rest = url.partition("://")
    authority, _, path = rest.partition("/")
    hostname, _, port_text = authority.partition(":")
    connection = http.client.HTTPConnection(
        hostname, int(port_text or "80"), timeout=timeout
    )
    try:
        connection.request("GET", "/" + path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _http_post(url: str, body: bytes, timeout: float = 2.0) -> tuple[int, bytes]:
    _, _, rest = url.partition("://")
    authority, _, path = rest.partition("/")
    hostname, _, port_text = authority.partition(":")
    connection = http.client.HTTPConnection(
        hostname, int(port_text or "80"), timeout=timeout
    )
    try:
        connection.request(
            "POST",
            "/" + path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class ProducerProcess:
    """The owner-side producer as its own OS process (process mode)."""

    def __init__(self, work_dir: Path, scenario: str, delay_seconds: float) -> None:
        self.audit_path = work_dir / "rp" / f"audit-{scenario}.jsonl"
        if self.audit_path.is_file():
            self.audit_path.unlink()
        self.stdout_lines: list[str] = []
        self.endpoint = ""
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "running_platform.local_identity_service",
                "--state",
                str(work_dir / "rp" / "identity.json"),
                "--foreign-state",
                str(work_dir / "rp" / "identity-foreign.json"),
                "--mismatch-state",
                str(work_dir / "rp" / "identity-mismatch.json"),
                "--scenario",
                scenario,
                "--delay-seconds",
                str(delay_seconds),
                "--port",
                "0",
                "--audit",
                str(self.audit_path),
            ],
            cwd=str(_REPO_ROOT),
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._reader = threading.Thread(target=self._collect, daemon=True)
        self._reader.start()

    def _collect(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self.stdout_lines.append(line.rstrip("\n"))
            if line.startswith("PORT=") and not self.endpoint:
                port = line.strip().split("=", 1)[1]
                self.endpoint = f"http://127.0.0.1:{port}/observe"

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                message = f"producer exited early: {self.stderr_text()}"
                raise HarnessError(message)
            if self.endpoint:
                try:
                    status, _ = _http_get(self.endpoint.replace("/observe", "/healthz"))
                    if status == 200:
                        return
                except OSError:
                    pass
            time.sleep(0.05)
        message = "producer did not become ready"
        raise HarnessError(message)

    def stop(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - hostile producer
            self._process.kill()
            self._process.wait(timeout=10)

    def stderr_text(self) -> str:
        if self._process.stderr is None:
            return ""
        try:
            return self._process.stderr.read()
        except OSError:  # pragma: no cover - closed pipe
            return ""


class DockerStack:
    """The same two sides as containers of ``local/compose.yaml``."""

    def __init__(self, scenario: str, delay_seconds: float, work_dir: Path) -> None:
        self._scenario = scenario
        self._delay = delay_seconds
        self._env = dict(os.environ)
        self._env["RP_SCENARIO"] = scenario
        self._env["RP_DELAY_SECONDS"] = str(delay_seconds)
        # The host work directory is mounted at /work inside both containers.
        self._env["AF_WORK_DIR"] = str(work_dir)

    def _compose(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
            cwd=str(_REPO_ROOT),
            env=self._env,
            text=True,
            capture_output=True,
            check=False,
            **kwargs,
        )

    def start(self) -> None:
        up = self._compose("up", "-d", "--force-recreate", "running-platform")
        if up.returncode != 0:
            raise HarnessError(f"compose up failed: {up.stderr.strip()}")
        deadline = time.monotonic() + 60.0
        probe = (
            "import http.client;c=http.client.HTTPConnection('127.0.0.1',8080,"
            "timeout=2);c.request('GET','/healthz');"
            "print(c.getresponse().status)"
        )
        while time.monotonic() < deadline:
            ready = self._compose(
                "exec", "-T", "running-platform", "python", "-c", probe
            )
            if ready.returncode == 0 and "200" in ready.stdout:
                return
            time.sleep(1.0)
        raise HarnessError("running-platform container did not become healthy")

    def endpoint(self) -> str:
        return "http://running-platform:8080/observe"

    def run_dno(
        self, work_dir: Path, timeout: float, result_out: Path
    ) -> tuple[str, str]:
        run = self._compose(
            "run",
            "--rm",
            "--no-deps",
            "dno",
            "python",
            "local/dno/run_deployment.py",
            "--instance",
            "/work/dno/platform-instance.json",
            "--manifest",
            "/work/dno/platform-manifest.json",
            "--environment",
            "/work/dno/environment.json",
            "--identity-endpoint",
            self.endpoint(),
            "--identity-timeout",
            str(timeout),
            "--result-out",
            "/work/dno/result.json",
        )
        return run.stdout, run.stderr

    def audit(self) -> str:
        logs = self._compose("logs", "--no-log-prefix", "running-platform")
        return logs.stdout + logs.stderr

    def stop(self) -> None:
        self._compose("down", "-v", "--remove-orphans")


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        message = f"{path}: a JSON object is required"
        raise HarnessError(message)
    return document


def non_forwarding_report(
    result: dict[str, Any], preparation: dict[str, Any]
) -> dict[str, Any]:
    """Analyse what actually crossed the boundary in this run."""
    observations = result.get("identity", {}).get("observations", [])
    report: dict[str, Any] = {
        "observations": len(observations),
        "request_keys": sorted(
            {
                key
                for observation in observations
                for key in observation.get("request_keys", [])
            }
        ),
        "forbidden_markers_in_body": [],
        "token_analysis": [],
        "verdict": "no-observation",
    }
    forbidden = list(FORBIDDEN_MARKERS) + [
        str(preparation.get("platform_id", "")),
        str(preparation.get("foreign_platform_id", "")),
        str(preparation.get("environment_id", "")),
        str(preparation.get("instance_digest", "")),
        str(preparation.get("manifest_digest", "")),
    ]
    for observation in observations:
        body = str(observation.get("request_body", ""))
        token = ""
        try:
            token = str(json.loads(body).get("binding_token", ""))
        except json.JSONDecodeError:  # pragma: no cover - our own body
            token = ""
        residual = body.replace(token, "<opaque-binding>") if token else body
        hits = [
            marker for marker in sorted(set(forbidden)) if marker and marker in residual
        ]
        report["forbidden_markers_in_body"].extend(hits)
        report["token_analysis"].append(
            {
                "token": token,
                "contains_platform_id": bool(
                    preparation.get("platform_id")
                    and preparation["platform_id"] in token
                ),
                "contains_environment_id": bool(
                    preparation.get("environment_id")
                    and preparation["environment_id"] in token
                ),
                "contains_instance_digest_prefix": bool(
                    preparation.get("instance_digest")
                    and observation.get("request_body", "").find(
                        str(preparation["instance_digest"]).removeprefix("sha256:")[:12]
                    )
                    >= 0
                ),
                "contains_attempt_suffix": token.endswith("-a1"),
            }
        )
    if observations:
        keys_ok = report["request_keys"] == ["binding_token"]
        content_ok = not report["forbidden_markers_in_body"]
        report["verdict"] = (
            "one-opaque-key-only" if keys_ok and content_ok else "check-failed"
        )
    return report


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_scenario(
    *,
    scenario: str,
    mode: str,
    work_dir: Path,
    out_dir: Path,
    preparation: dict[str, Any],
    identity_timeout: float,
    delay_seconds: float,
) -> dict[str, Any]:
    """Run one scenario and write its transcripts."""
    expected, fragment = SCENARIOS[scenario]
    scenario_dir = out_dir / f"{scenario}"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    effective_delay = delay_seconds if scenario == "delay" else 0.0
    started = time.monotonic()
    producer: ProducerProcess | None = None
    stack: DockerStack | None = None
    if mode == "process":
        producer = ProducerProcess(work_dir, scenario, effective_delay)
        producer.wait_ready()
        endpoint = producer.endpoint
        audit_text = ""
    else:
        stack = DockerStack(scenario, effective_delay, work_dir)
        stack.start()
        endpoint = stack.endpoint()
    result_out = work_dir / "dno" / f"result-{scenario}.json"
    dno_stdout = ""
    dno_stderr = ""
    audit_text = ""
    try:
        if mode == "process":
            completed = subprocess.run(
                [
                    sys.executable,
                    "local/dno/run_deployment.py",
                    "--instance",
                    str(work_dir / "dno" / "platform-instance.json"),
                    "--manifest",
                    str(work_dir / "dno" / "platform-manifest.json"),
                    "--environment",
                    str(work_dir / "dno" / "environment.json"),
                    "--identity-endpoint",
                    endpoint,
                    "--identity-timeout",
                    str(identity_timeout),
                    "--result-out",
                    str(result_out),
                ],
                cwd=str(_REPO_ROOT),
                env=_child_env(),
                text=True,
                capture_output=True,
                check=False,
            )
            dno_stdout, dno_stderr = completed.stdout, completed.stderr
            dno_exit = completed.returncode
        else:
            assert stack is not None
            dno_stdout, dno_stderr = stack.run_dno(
                work_dir, identity_timeout, result_out
            )
            dno_exit = 0 if '"result": "accepted"' in dno_stdout else 3
            audit_text = stack.audit()
    finally:
        if producer is not None and scenario == "delay":
            # Let the deliberately late answer arrive and be recorded: the
            # producer's own transcript then shows it answered after D&O had
            # already refused, and that the refusal came from the bound.
            time.sleep(min(effective_delay + 2.0, 15.0))
        if producer is not None:
            producer.stop()
        if stack is not None:
            stack.stop()
    if producer is not None:
        # Read the owner-side transcript only after the producer stopped, so a
        # deliberately late answer and its delivery outcome are included. The
        # audit file is the transcript; stdout is only its live copy (plus the
        # harness bookkeeping line, which is not evidence).
        if producer.audit_path.is_file():
            audit_text = producer.audit_path.read_text(encoding="utf-8")
        else:  # pragma: no cover - the harness always passes --audit
            audit_text = "\n".join(
                line for line in producer.stdout_lines if not line.startswith("PORT=")
            )
    elapsed = round(time.monotonic() - started, 3)
    result = _load_json(result_out) if result_out.is_file() else {}
    observed = str(result.get("result", "missing"))
    errors = list(result.get("errors", []))
    matched_reason = any(fragment in error for error in errors) if fragment else True
    non_forwarding = non_forwarding_report(result, preparation)
    checks = {
        "scenario": scenario,
        "expected_result": expected,
        "observed_result": observed,
        "outcome_ok": observed == expected,
        "expected_reason_fragment": fragment,
        "errors": errors,
        "reason_ok": matched_reason,
        "harness_elapsed_seconds": elapsed,
        "identity_declared_bound_seconds": identity_timeout,
        "producer_delay_seconds": effective_delay,
        "dno_exit_code": dno_exit,
        "non_forwarding": non_forwarding,
    }
    if scenario == "delay":
        waited = [
            observation.get("waited_seconds")
            for observation in result.get("identity", {}).get("observations", [])
        ]
        checks["observation_waited_seconds"] = waited
        checks["bound_held"] = bool(waited) and all(
            value is not None and value <= identity_timeout + 0.5 for value in waited
        )
        checks["answer_was_late"] = effective_delay > identity_timeout
    _write(scenario_dir / "producer-audit.jsonl", audit_text)
    _write(scenario_dir / "dno-stdout.txt", dno_stdout)
    _write(scenario_dir / "dno-stderr.txt", dno_stderr)
    _write(
        scenario_dir / "checks.json",
        json.dumps(checks, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    return checks


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("process", "docker"), default="process")
    parser.add_argument("--work-dir", type=Path, default=_WORK_DEFAULT)
    parser.add_argument("--out", type=Path, default=_OUT_DEFAULT)
    parser.add_argument("--identity-timeout", type=float, default=2.0)
    parser.add_argument("--delay-seconds", type=float, default=6.0)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    args = parser.parse_args(argv)

    work_dir = args.work_dir.resolve()
    out_dir = args.out.resolve()
    preparation_path = work_dir / "preparation.json"
    expected_runtime_root = (
        DOCKER_RUNTIME_ROOT
        if args.mode == "docker"
        else str(work_dir / "dno" / "runtime")
    )
    current_runtime_root = ""
    if preparation_path.is_file():
        current_runtime_root = str(_load_json(preparation_path).get("runtime_root", ""))
    if current_runtime_root != expected_runtime_root:
        prepared = subprocess.run(
            [
                sys.executable,
                "local/tools/prepare_local_platform.py",
                "--work-dir",
                str(work_dir),
                "--runtime-root",
                expected_runtime_root,
            ],
            cwd=str(_REPO_ROOT),
            env=_child_env(),
            text=True,
            capture_output=True,
            check=False,
        )
        if prepared.returncode != 0:
            message = f"preparation failed: {prepared.stderr.strip()}"
            raise SystemExit(message)
    preparation = _load_json(preparation_path)
    selected = [name.strip() for name in args.scenarios.split(",") if name.strip()]
    summary: list[dict[str, Any]] = []
    for scenario in selected:
        if scenario not in SCENARIOS:
            message = f"unknown scenario: {scenario}"
            raise SystemExit(message)
        checks = run_scenario(
            scenario=scenario,
            mode=args.mode,
            work_dir=work_dir,
            out_dir=out_dir,
            preparation=preparation,
            identity_timeout=args.identity_timeout,
            delay_seconds=args.delay_seconds,
        )
        summary.append(checks)
        status = "OK" if checks["outcome_ok"] and checks["reason_ok"] else "CHECK"
        sys.stdout.write(
            f"[{status}] {scenario}: expected={checks['expected_result']} "
            f"observed={checks['observed_result']} "
            f"non-forwarding={checks['non_forwarding']['verdict']}\n"
        )
    _write(
        out_dir / "summary.json",
        json.dumps(
            {
                "classification": CLASSIFICATION,
                "mode": args.mode,
                "preparation": preparation,
                "scenarios": summary,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
    )
    failed = [
        checks["scenario"]
        for checks in summary
        if not checks["outcome_ok"] or not checks["reason_ok"]
    ]
    if failed:
        sys.stderr.write(f"scenarios not as expected: {', '.join(failed)}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
