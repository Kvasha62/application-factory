"""Owner-side producer / D&O-side consumer contract over a real boundary.

These tests exercise the local (non-production) Running Platform stack of
``local/``: the owner-side producer runs as its own OS process, the D&O side
reaches it across a real loopback transport, and the acceptance/refusal logic
that runs afterwards is the repository's own (``deploy`` and the shipped
identity seam). Nothing here is production evidence: the producer is a local
stand-in whose authoritative state is seeded by the owner-side preparation tool,
and every result is ``LOCAL DOCKER / NON-PRODUCTION``.

The Docker launch path is covered separately (``tests/test_local_docker_stack``)
and is opt-in, because these tests must not depend on a container engine being
present or on the user's own Docker state.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_LOCAL_DIR = Path(__file__).resolve().parents[1] / "local"
if str(_LOCAL_DIR) not in sys.path:
    # The local stack is a set of scripts, not an installed package.
    sys.path.insert(0, str(_LOCAL_DIR))

from tools.prepare_local_platform import prepare

from deployment_operations import (
    DeploymentRequest,
    DeploymentStateStore,
    IdentityVerificationFailed,
    InstanceReference,
    deploy,
    derive_deployment_id,
    load_environment,
    load_record,
)
from deployment_operations.platform_identity import (
    ActualIdentityUnavailable,
    BindingEvaluationContext,
    IdentityCorrespondenceResult,
    PlatformIdentityBinding,
    establish_identity_correspondence,
    require_correlated_evidence,
)
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from running_platform.http_owner_state import (
    BINDING_TOKEN_KEY,
    HttpRunningPlatformOwnerStateReader,
)
from running_platform.owner_state import (
    OwnerStateSnapshotSource,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AMBIENT_IDENTITY_FILENAME = "running_platform_identity.json"
#: Strings that must never cross the boundary as identity material.
_FORBIDDEN_MARKERS = (
    "instance_digest",
    "manifest_digest",
    "expected_instance",
    "golden_bundle",
    "membership_established",
    "provenance",
    "artifact_type",
    "canonical_form",
)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(_REPO_ROOT / "src")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _request(url: str, method: str, body: bytes | None = None) -> tuple[int, bytes]:
    _, _, rest = url.partition("://")
    authority, _, path = rest.partition("/")
    hostname, _, port = authority.partition(":")
    connection = http.client.HTTPConnection(hostname, int(port or "80"), timeout=5.0)
    try:
        connection.request(
            method,
            "/" + path,
            body=body,
            headers={"Content-Type": "application/json"} if body else {},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class LocalProducer:
    """The owner-side producer as its own process, with its own audit log."""

    def __init__(self, work_dir: Path, scenario: str, delay_seconds: float) -> None:
        self.audit_path = work_dir / "rp" / f"audit-test-{scenario}.jsonl"
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
        threading.Thread(target=self._collect, daemon=True).start()

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
            if self.endpoint:
                status, _ = _request(
                    self.endpoint.replace("/observe", "/healthz"), "GET"
                )
                if status == 200:
                    return
            if self._process.poll() is not None:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                pytest.fail(f"producer exited early: {stderr}")
            time.sleep(0.05)
        pytest.fail("producer did not become ready")

    def audit_entries(self) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - hostile producer
                self._process.kill()
                self._process.wait(timeout=10)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One prepared local platform definition and owner-side state."""
    work_dir = tmp_path_factory.mktemp("local-running-platform")
    return prepare(work_dir)


@pytest.fixture
def producer_factory(prepared: dict[str, Any], tmp_path: Path) -> Iterator[Any]:
    """Start one producer per call and stop every started producer at the end."""

    work_dir = Path(str(prepared["work_dir"]))
    started: list[LocalProducer] = []

    def start(scenario: str, *, delay_seconds: float = 0.0) -> LocalProducer:
        producer = LocalProducer(work_dir, scenario, delay_seconds)
        started.append(producer)
        producer.wait_ready()
        return producer

    yield start
    for producer in started:
        producer.stop()


def _reader(producer: LocalProducer, *, timeout_seconds: float = 5.0):
    return HttpRunningPlatformOwnerStateReader(
        producer.endpoint, timeout_seconds=timeout_seconds
    )


def _provider(reader: HttpRunningPlatformOwnerStateReader):
    return OwnerSuppliedPlatformIdentityProvider(OwnerStateSnapshotSource(reader))


def _binding(
    prepared: dict[str, Any],
    token: str = "opaque-binding-a1",
    *,
    target: str | None = None,
    scope: str | None = None,
    sequence: int = 1,
) -> PlatformIdentityBinding:
    """The evaluation binding these tests establish for the local platform.

    The handle states its own position in the binding space (the local owner
    side reads it out of the handle); the context states the binding semantics
    the evaluation established. It carries no expected identity.
    """

    return PlatformIdentityBinding(
        token=token,
        context=BindingEvaluationContext(
            authority="tests/deployment-operations",
            basis="tests/deployment-record",
            target=str(prepared["platform_id"]) if target is None else target,
            scope=str(prepared["environment_id"]) if scope is None else scope,
            sequence=sequence,
            established_at="2026-09-16T00:00:00Z",
        ),
    )


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


def test_owner_surface_crosses_the_boundary_and_is_read(prepared, producer_factory):
    producer = producer_factory("correct")
    reader = _reader(producer)
    state = reader.read(_binding(prepared))

    assert state.platform_id == prepared["platform_id"]
    assert [observation["request_keys"] for observation in reader.observations] == [
        [BINDING_TOKEN_KEY]
    ]
    # The owner states, as its own fact, the binding identity it answered under.
    assert state.correlation.token == "opaque-binding-a1"
    assert state.correlation.scope == prepared["environment_id"]
    assert state.correlation.sequence == 1
    assert state.correlation.target == prepared["platform_id"]


def test_the_only_bytes_crossing_the_boundary_are_the_opaque_binding(
    prepared, producer_factory
):
    producer = producer_factory("correct")
    reader = _reader(producer)
    body = reader.request_body(PlatformIdentityBinding("opaque-binding-1"))

    assert json.loads(body) == {BINDING_TOKEN_KEY: "opaque-binding-1"}
    text = body.decode("utf-8")
    for marker in _FORBIDDEN_MARKERS:
        assert marker not in text
    assert str(prepared["instance_digest"]) not in text
    assert str(prepared["manifest_digest"]) not in text


def test_binding_token_value_is_the_derived_deployment_id(prepared, producer_factory):
    """Characterisation recorded for O4-C D5 (i): the token is not opaque.

    The shipped call path binds the provider to ``deployment_id``, whose value
    is ``dep-<platform>-<environment>-<digest prefix>-a<attempt>`` — it carries
    the platform name, the environment id, a 12-hex-character prefix of the
    expected instance digest and the attempt number in plaintext. This test
    pins today's behaviour so a change is visible; it endorses nothing.
    """
    producer = producer_factory("correct")
    reader = _reader(producer)
    instance = json.loads(Path(str(prepared["documents"]["instance"])).read_text())
    expected_token = derive_deployment_id(
        instance["platform_id"],
        instance["instance_digest"],
        str(prepared["environment_id"]),
        1,
    )
    body = reader.request_body(PlatformIdentityBinding(expected_token))
    token = json.loads(body)[BINDING_TOKEN_KEY]

    assert token == expected_token
    assert str(prepared["platform_id"]) in token
    assert str(prepared["environment_id"]) in token
    assert str(instance["instance_digest"]).removeprefix("sha256:")[:12] in token
    assert token.endswith("-a1")


def test_the_boundary_refuses_anything_but_the_opaque_binding(
    prepared, producer_factory
):
    producer = producer_factory("correct")
    payload = json.dumps(
        {
            BINDING_TOKEN_KEY: "opaque-binding-1",
            "instance_digest": str(prepared["instance_digest"]),
        }
    ).encode("utf-8")
    status, body = _request(producer.endpoint, "POST", payload)

    assert status == 400
    assert b"only the opaque binding" in body
    refusals = [
        entry
        for entry in producer.audit_entries()
        if entry.get("event") == "observation_received" and entry["accepted"] is False
    ]
    assert refusals and refusals[0]["extra_keys"] == ["instance_digest"]


# ---------------------------------------------------------------------------
# Acceptance and refusal at the consumer
# ---------------------------------------------------------------------------


def test_correct_owner_identity_corresponds_to_the_expected_instance(
    prepared, producer_factory
):
    producer = producer_factory("correct")
    reader = _reader(producer)
    provider = _provider(reader)
    instance = json.loads(Path(str(prepared["documents"]["instance"])).read_text())
    binding = _binding(prepared)

    evidence = provider.observe_identity(binding)

    # The producer's own statement of the binding it observed, as evidence.
    assert evidence.correlation.token == binding.token
    assert evidence.correlation.target == prepared["platform_id"]
    assert (
        establish_identity_correspondence(instance, evidence, binding=binding)
        is IdentityCorrespondenceResult.MATCH
    )
    # Equal content without the correlation rule certifies nothing: the same
    # evidence evaluated without a producer-established binding is UNAVAILABLE.
    assert (
        establish_identity_correspondence(instance, evidence)
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_another_platforms_identity_is_not_evidence_about_this_one(
    prepared, producer_factory
):
    producer = producer_factory("foreign")
    reader = _reader(producer)
    provider = _provider(reader)
    instance = json.loads(Path(str(prepared["documents"]["instance"])).read_text())
    binding = _binding(prepared)

    evidence = provider.observe_identity(binding)

    # The observation is real, and it is an observation of another platform: the
    # producer's own statement says so, so it is not evidence about this one.
    assert evidence.value.platform_id == prepared["foreign_platform_id"]
    assert evidence.correlation.target == prepared["foreign_platform_id"]
    with pytest.raises(
        ActualIdentityUnavailable, match="another identity-bearing binding"
    ):
        require_correlated_evidence(binding, evidence)
    assert (
        establish_identity_correspondence(instance, evidence, binding=binding)
        is IdentityCorrespondenceResult.UNAVAILABLE
    )


def test_stale_owner_surface_is_refused(prepared, producer_factory):
    reader = _reader(producer_factory("stale"))

    with pytest.raises(ActualIdentityUnavailable, match="stale"):
        reader.read(_binding(prepared))


def test_wrong_correlation_is_refused(prepared, producer_factory):
    reader = _reader(producer_factory("wrong-correlation"))

    with pytest.raises(ActualIdentityUnavailable, match="foreign correlation"):
        reader.read(_binding(prepared))


def test_contradictory_identity_surface_is_refused(prepared, producer_factory):
    provider = _provider(_reader(producer_factory("contradictory")))

    with pytest.raises(
        ActualIdentityUnavailable, match="contradicts actual platform_id"
    ):
        provider.observe_identity(_binding(prepared))


def test_unavailable_owner_side_is_refused(prepared, producer_factory):
    reader = _reader(producer_factory("unavailable"), timeout_seconds=5.0)

    with pytest.raises(ActualIdentityUnavailable, match="HTTP 503"):
        reader.read(_binding(prepared))


def test_the_owner_side_refuses_a_handle_that_states_no_binding_position(
    prepared, producer_factory
):
    """A handle that states no position cannot be answered with evidence."""

    reader = _reader(producer_factory("correct"))

    with pytest.raises(ActualIdentityUnavailable, match="HTTP 503"):
        reader.read(PlatformIdentityBinding("opaque-binding"))


def test_declared_bound_ends_the_wait_and_a_late_answer_cannot_be_accepted(
    prepared, producer_factory
):
    producer = producer_factory("delay", delay_seconds=3.0)
    reader = _reader(producer, timeout_seconds=0.75)

    started = time.monotonic()
    with pytest.raises(ActualIdentityUnavailable, match="TimeoutError"):
        reader.read(_binding(prepared))
    waited = time.monotonic() - started

    # The wait ended by the declared bound, not by the answer.
    assert waited < 3.0
    assert waited <= 1.5
    assert reader.observations[0]["outcome"].startswith("refused")
    assert reader.observations[0]["declared_bound_seconds"] == 0.75
    # The producer answers anyway, later, and its own transcript says so.
    deadline = time.monotonic() + 6.0
    answered: list[dict[str, Any]] = []
    while time.monotonic() < deadline and not answered:
        answered = [
            entry
            for entry in producer.audit_entries()
            if entry.get("event") == "observation_answered"
        ]
        time.sleep(0.1)
    assert answered, "the producer never recorded its (late) answer"
    assert answered[0]["elapsed_seconds"] >= 3.0


# ---------------------------------------------------------------------------
# The shipped deployment operation, over the real boundary
# ---------------------------------------------------------------------------


def _deployment_inputs(
    prepared: dict[str, Any], tmp_path: Path
) -> tuple[Any, Any, Any]:
    documents = prepared["documents"]
    instance = json.loads(Path(str(documents["instance"])).read_text())
    manifest = json.loads(Path(str(documents["manifest"])).read_text())
    environment_document = json.loads(
        Path(str(documents["environment"])).read_text(encoding="utf-8")
    )
    environment_document["runtime_root"] = str(tmp_path / "runtime")
    environment = load_environment(environment_document, base_dir=tmp_path)
    return instance, manifest, environment


def _request_for(instance, manifest, environment, attempt: int) -> DeploymentRequest:
    return DeploymentRequest(
        instance=InstanceReference.from_document(instance),
        instance_document=instance,
        manifest_document=manifest,
        environment=environment,
        attempt=attempt,
    )


def test_real_deployment_accepts_identity_observed_across_the_boundary(
    prepared, producer_factory, tmp_path
):
    instance, manifest, environment = _deployment_inputs(prepared, tmp_path)
    reader = _reader(producer_factory("correct"))
    deployment = deploy(
        _request_for(instance, manifest, environment, attempt=1),
        identity_provider=_provider(reader),
    )

    assert deployment.record.deployed is True
    assert deployment.record.identity_verified is True
    assert deployment.record.instance_digest == instance["instance_digest"]
    # Identity came across the boundary; no ambient owner file exists.
    assert not (environment.runtime_root / _AMBIENT_IDENTITY_FILENAME).exists()
    assert len(reader.observations) == 1
    assert reader.observations[0]["outcome"] == "accepted"


def test_real_deployment_refuses_a_foreign_identity_across_the_boundary(
    prepared, producer_factory, tmp_path
):
    instance, manifest, environment = _deployment_inputs(prepared, tmp_path)
    reader = _reader(producer_factory("foreign"))
    request = _request_for(instance, manifest, environment, attempt=1)

    with pytest.raises(IdentityVerificationFailed):
        deploy(request, identity_provider=_provider(reader))

    record = load_record(
        DeploymentStateStore.path_for(
            environment.operations_dir,
            derive_deployment_id(
                instance["platform_id"],
                instance["instance_digest"],
                environment.environment_id,
                1,
            ),
        )
    )
    assert record.deployed is False
    assert record.identity_verified is False
    assert record.failure is not None
    assert record.failure.stage == "ready"
