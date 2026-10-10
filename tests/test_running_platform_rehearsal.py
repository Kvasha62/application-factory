"""Stage-1 rehearsal of the approved in-band identity contract.

REHEARSAL / NOT PRODUCTION. These tests exercise the approved decision
(M1-b: the strict expected-state non-forwarding invariant sits at the producer
boundary; an in-band S4 owner-side adapter is a collector; AG-2: the adapter
enforces correlation) against a stand-in Running Platform that lives in this
repository. The expected side is built through the real Composer and Platform
Instance surfaces and accepted by the real ``deploy``; the Deployment &
Operations acceptance seam is not changed.

Named per acceptance check of the stage:

* A1 the real-boundary non-forwarding check passes — TestRealBoundaryNonForwarding
* A2 foreign and stale evidence is refused — TestForeignAndStaleEvidence
* A3 a timeout can never yield acceptance — TestBoundedWait
* A4 nine surfaces, explicit provenance — TestNineSurfaces
* A5 evidence is labelled rehearsal — TestRehearsalLabelling
* the seam is preserved — TestSeamPreservation

What a rehearsal cannot show: that actual and expected identity are independent
(the world is installed from the instance by the harness), anything about a
real environment, owner, producer or credential, or any production sign-off.
Those stay open (Issue #128: RF-5, RF-10, D1, D2, D8).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _deployment_helpers import (
    COMPONENT_ID,
    PLATFORM_ID,
    environment_for,
    instance_for,
    only_operation,
    request_for,
)
from _rehearsal_helpers import (
    DRIFTS,
    RecordingTransport,
    boundary_violations,
    correspondence,
    counter_handles,
    deploy_with_rehearsal,
    forbidden_values,
    install_world,
    producer_argv,
    raising_timeout,
    replaying,
    rewrite,
    rewritten,
    rich_manifest,
    stalled,
    token_for,
)

import running_platform_rehearsal
from deployment_operations import LIFECYCLE_FAILED, IdentityVerificationFailed
from deployment_operations.platform_identity import (
    ActualIdentityUnavailable,
    EvidenceProvenance,
    IdentityCorrespondenceResult,
    PlatformIdentityBinding,
)
from deployment_operations.platform_identity_source import (
    ActualPlatformSnapshot,
    OwnerSuppliedPlatformIdentityProvider,
)
from platform_instance import discover_root
from running_platform_rehearsal import REHEARSAL_LABEL, producer
from running_platform_rehearsal.adapter import (
    BoundaryRequest,
    BoundaryResponse,
    RehearsalAdapter,
    run_producer_process,
)

SOURCE = Path(running_platform_rehearsal.__file__).parent
NORMATIVE = {
    EvidenceProvenance.MEASURED,
    EvidenceProvenance.TRANSITIVE,
    EvidenceProvenance.ATTESTED,
}
ENVIRONMENT_ID = "local-test"
BINDING = PlatformIdentityBinding("an-opaque-evaluation-token")


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return rich_manifest(discover_root())


@pytest.fixture(scope="module")
def instance(manifest: dict[str, Any]):
    return instance_for(manifest, root=discover_root())


@pytest.fixture
def document(instance) -> dict[str, Any]:
    return dict(instance.document)


@pytest.fixture
def world(tmp_path: Path, document: dict[str, Any]) -> Path:
    return install_world(tmp_path / "rehearsal-world", document)


@pytest.fixture
def token(document: dict[str, Any]) -> str:
    return token_for(document, ENVIRONMENT_ID)


def rich_environment(runtime_root: Path):
    """The instance pins ``platform_id``; the overlay must not repeat it."""
    return environment_for(
        runtime_root, overlay={COMPONENT_ID: {"environment": "test"}}
    )


def outcome(document: dict[str, Any], adapter: RehearsalAdapter, token: str) -> str:
    """MATCH, MISMATCH or UNAVAILABLE, exactly as the acceptance seam sees it."""
    try:
        return correspondence(document, adapter, token)
    except ActualIdentityUnavailable:
        return IdentityCorrespondenceResult.UNAVAILABLE


def failure_reasons(caught: pytest.ExceptionInfo[IdentityVerificationFailed]) -> str:
    return " | ".join([*getattr(caught.value, "errors", ()), str(caught.value)])


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# A1 — the real-boundary non-forwarding check
# ---------------------------------------------------------------------------


class TestRealBoundaryNonForwarding:
    """The producer receives an adapter-made handle and nothing else."""

    def test_deploy_accepts_through_the_real_boundary_and_nothing_leaks(
        self, tmp_path: Path, instance, manifest, world: Path
    ):
        environment = rich_environment(tmp_path / "runtime")
        request = request_for(instance, manifest, environment)
        recording = RecordingTransport()
        seen: list[object] = []

        class Spy(RehearsalAdapter):
            def observe(self, binding: PlatformIdentityBinding):
                seen.append(binding.token)
                return super().observe(binding)

        adapter = Spy(world, transport=recording)
        with deploy_with_rehearsal(request, adapter) as deployment:
            assert deployment.deployed is True

        token = token_for(instance.document, environment.environment_id)
        assert seen == [token], "the check below uses the token D&O really passed"
        assert len(recording.requests) == 1, "one evaluation, one request"
        forbidden = forbidden_values(
            instance.document,
            environment.runtime_root,
            environment.environment_id,
            token,
        )
        assert (
            boundary_violations(
                recording.requests, recording.responses, forbidden, world
            )
            == []
        )

    def test_the_request_is_exactly_an_operation_and_a_handle(
        self, document, world: Path, token: str
    ):
        recording = RecordingTransport()
        adapter = RehearsalAdapter(
            world, transport=recording, handle_factory=counter_handles()
        )
        assert outcome(document, adapter, token) == "MATCH"

        (request,) = recording.requests
        assert json.loads(request.stdin) == {"op": "observe", "handle": "0" * 31 + "1"}
        assert request.argv == producer_argv(world)
        assert set(request.env) <= {"PYTHONPATH", "SystemRoot"}

    def test_the_handle_is_made_by_the_adapter_and_unrelated_to_the_binding(
        self, document, world: Path, token: str
    ):
        recording = RecordingTransport()
        adapter = RehearsalAdapter(world, transport=recording)
        assert outcome(document, adapter, token) == "MATCH"
        assert outcome(document, adapter, token) == "MATCH"

        handles = [json.loads(call.stdin)["handle"] for call in recording.requests]
        assert len(set(handles)) == len(handles), "every evaluation has a new handle"
        for handle in handles:
            assert len(handle) == 32
            assert handle not in token
            assert token not in handle

    def test_a_forwarding_adapter_is_caught_at_the_boundary_while_dno_cannot_tell(
        self, document, world: Path, token: str
    ):
        """The reason the boundary check exists: D&O accepts the leak."""

        class Leaky(RehearsalAdapter):
            def observe(self, binding: PlatformIdentityBinding):
                self.leak = binding.token
                return super().observe(binding)

            def _request(self, handle: str) -> BoundaryRequest:
                request = super()._request(handle)
                body = json.loads(request.stdin)
                body["token"] = self.leak
                return replace(request, stdin=(json.dumps(body) + "\n").encode())

        def tolerant(inner):
            """A producer that ignores what it should have refused."""

            def transport(request: BoundaryRequest) -> BoundaryResponse:
                body = json.loads(request.stdin)
                body.pop("token", None)
                return inner(replace(request, stdin=(json.dumps(body) + "\n").encode()))

            return transport

        recording = RecordingTransport(tolerant(run_producer_process))
        adapter = Leaky(world, transport=recording)

        assert outcome(document, adapter, token) == "MATCH", "D&O cannot see a leak"
        forbidden = forbidden_values(
            document, Path("/nonexistent/runtime"), ENVIRONMENT_ID, token
        )
        violations = boundary_violations(
            recording.requests, recording.responses, forbidden, world
        )
        assert "binding token crossed the boundary" in violations
        assert "the request is not exactly op and a 32-hex handle" in violations

    def test_the_producer_refuses_a_request_that_carries_anything_else(
        self, world: Path, token: str
    ):
        for extra in ({"token": token}, {"platform_id": PLATFORM_ID}, {"attempt": 1}):
            line = json.dumps({"op": "observe", "handle": "h", **extra})
            answer = producer.answer(world, line)
            assert answer["status"] == "refused"
            assert "surfaces" not in answer
        refused = producer.answer(world, json.dumps({"op": "observe"}))
        assert refused["status"] == "refused"

    def test_the_producer_loads_no_other_first_party_module(self):
        first_party = sorted(
            entry.name
            for entry in SOURCE.parent.iterdir()
            if (entry / "__init__.py").is_file()
        )
        code = (
            "import sys, running_platform_rehearsal.producer;"
            f"names = {first_party!r};"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in names))"
        )
        done = subprocess.run(
            [sys.executable, "-P", "-c", code],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(SOURCE.parent)},
            check=True,
        )
        loaded = ast.literal_eval(done.stdout.strip())
        assert loaded == [
            "running_platform_rehearsal",
            "running_platform_rehearsal.producer",
        ], loaded


# ---------------------------------------------------------------------------
# A2 — foreign and stale evidence is refused
# ---------------------------------------------------------------------------


def _foreign(document: dict[str, Any]) -> None:
    document["handle"] = "f" * 32


class TestForeignAndStaleEvidence:
    """D&O does not compare correlation in-band, so the adapter must."""

    def test_an_answer_for_an_unknown_handle_is_foreign(
        self, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world, transport=rewritten(_foreign))
        with pytest.raises(ActualIdentityUnavailable, match="foreign evidence"):
            correspondence(document, adapter, token)
        assert adapter.evidence_record()["outcome"].startswith("refused: foreign")

    def test_a_replayed_answer_is_stale(self, document, world: Path, token: str):
        adapter = RehearsalAdapter(
            world, transport=replaying(), handle_factory=counter_handles()
        )
        assert outcome(document, adapter, token) == "MATCH"
        with pytest.raises(ActualIdentityUnavailable, match="stale evidence"):
            correspondence(document, adapter, token)

    def test_a_cached_answer_cannot_hide_drift(self, document, world: Path, token: str):
        adapter = RehearsalAdapter(world, handle_factory=counter_handles())
        assert outcome(document, adapter, token) == "MATCH"
        DRIFTS["component_identity"](world)
        assert outcome(document, adapter, token) == "MISMATCH"

    def test_deploy_fails_closed_on_foreign_evidence(
        self, tmp_path: Path, instance, manifest, world: Path
    ):
        environment = rich_environment(tmp_path / "runtime")
        request = request_for(instance, manifest, environment)
        adapter = RehearsalAdapter(world, transport=rewritten(_foreign))
        with pytest.raises(IdentityVerificationFailed) as caught:
            deploy_with_rehearsal(request, adapter)
        assert "foreign evidence" in failure_reasons(caught)

        record = only_operation(environment)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    @pytest.mark.parametrize(
        ("label", "damage"),
        [
            ("no protocol", lambda d: d.pop("protocol")),
            ("refused status", lambda d: d.update(status="refused")),
            ("no handle", lambda d: d.pop("handle")),
            ("a surface is missing", lambda d: d["surfaces"].pop("branding")),
            ("a surface is added", lambda d: d["surfaces"].update(extra={})),
            ("state is not stated", lambda d: d["surfaces"]["branding"].pop("state")),
            (
                "PRESENT without a value",
                lambda d: d["surfaces"]["branding"].update(state="PRESENT", value=None),
            ),
        ],
    )
    def test_a_damaged_answer_is_refused(
        self, label: str, damage, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world, transport=rewritten(damage))
        assert outcome(document, adapter, token) == "UNAVAILABLE", label

    @pytest.mark.parametrize(
        "response",
        [
            BoundaryResponse(b"", b"", 0),
            BoundaryResponse(b"not json", b"", 0),
            BoundaryResponse(b"[]", b"", 0),
            BoundaryResponse(b"{}", b"", 1),
        ],
    )
    def test_an_unusable_answer_is_refused(
        self, response: BoundaryResponse, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world, transport=lambda _request: response)
        assert outcome(document, adapter, token) == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# A3 — the bounded wait
# ---------------------------------------------------------------------------


class TestBoundedWait:
    """A timeout ends in refusal, never in acceptance."""

    def test_a_stalled_producer_is_refused_within_the_bound(
        self, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world, timeout=0.5, transport=stalled())
        started = time.monotonic()
        with pytest.raises(ActualIdentityUnavailable, match="did not answer within"):
            correspondence(document, adapter, token)
        elapsed = time.monotonic() - started
        assert elapsed < 10, "the stalled process was killed, not waited for"
        assert adapter.evidence_record()["outcome"].startswith("refused")

    def test_a_transport_timeout_is_refused(self, document, world: Path, token: str):
        adapter = RehearsalAdapter(world, transport=raising_timeout)
        with pytest.raises(ActualIdentityUnavailable, match="did not answer within"):
            correspondence(document, adapter, token)

    def test_a_late_answer_is_refused_even_when_the_transport_returns_it(
        self, document, world: Path, token: str
    ):
        """The deadline belongs to the adapter, not to the transport."""
        clock = _FakeClock()

        def slow(request: BoundaryRequest) -> BoundaryResponse:
            clock.now += 10.0
            return run_producer_process(request)

        adapter = RehearsalAdapter(world, timeout=1.0, transport=slow, clock=clock)
        with pytest.raises(ActualIdentityUnavailable, match="after the deadline"):
            correspondence(document, adapter, token)

    def test_deploy_fails_closed_when_the_producer_stalls(
        self, tmp_path: Path, instance, manifest, world: Path
    ):
        environment = rich_environment(tmp_path / "runtime")
        request = request_for(instance, manifest, environment)
        adapter = RehearsalAdapter(world, timeout=0.5, transport=stalled())
        with pytest.raises(IdentityVerificationFailed) as caught:
            deploy_with_rehearsal(request, adapter)
        assert "did not answer within" in failure_reasons(caught)

        record = only_operation(environment)
        assert record.lifecycle == LIFECYCLE_FAILED
        assert record.deployed is False

    @pytest.mark.parametrize("timeout", [0, -1.0])
    def test_the_bound_must_be_positive(self, timeout: float, world: Path):
        with pytest.raises(ValueError, match="positive"):
            RehearsalAdapter(world, timeout=timeout)


# ---------------------------------------------------------------------------
# A4 — nine surfaces, explicit provenance
# ---------------------------------------------------------------------------


class TestNineSurfaces:
    """Every surface is observed, labelled with a basis, and load-bearing."""

    def test_all_nine_surfaces_are_reported_with_explicit_provenance(
        self, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world)
        assert outcome(document, adapter, token) == "MATCH"

        rows = adapter.evidence_record()["surfaces"]
        assert [row["surface"] for row in rows] == list(producer.SURFACES)
        assert len(rows) == 9
        for row in rows:
            assert row["provenance"] in NORMATIVE
            assert row["basis"], f"{row['surface']} has no stated basis"

    def test_the_instance_exercises_present_and_absent_facts(self, document):
        assert document["configuration"] and document["branding"]
        assert document.get("extensions") is None
        assert document["golden_bundle"] is None

    @pytest.mark.parametrize("surface", list(producer.SURFACES))
    def test_each_surface_changes_the_verdict(
        self, surface: str, document, world: Path, token: str
    ):
        assert outcome(document, RehearsalAdapter(world), token) == "MATCH"
        DRIFTS[surface](world)
        verdict = outcome(document, RehearsalAdapter(world), token)
        assert verdict in {"MISMATCH", "UNAVAILABLE"}, surface

    def test_different_provenance_classes_are_refused_not_flattened(
        self, document, world: Path, token: str
    ):
        def mix(answer: dict[str, Any]) -> None:
            answer["surfaces"]["manifest"]["provenance"] = "ATTESTED"

        adapter = RehearsalAdapter(world, transport=rewritten(mix))
        with pytest.raises(ActualIdentityUnavailable, match="mixed or non-normative"):
            correspondence(document, adapter, token)

    def test_a_surface_without_a_basis_is_refused(
        self, document, world: Path, token: str
    ):
        def strip(answer: dict[str, Any]) -> None:
            del answer["surfaces"]["branding"]["basis"]

        adapter = RehearsalAdapter(world, transport=rewritten(strip))
        with pytest.raises(ActualIdentityUnavailable, match="no explicit basis"):
            correspondence(document, adapter, token)

    def test_multiplicity_is_preserved_until_validation(
        self, document, world: Path, token: str
    ):
        copy_directory = world / "components" / "duplicate"
        copy_directory.mkdir()
        original = world / "components" / "tenant_authority" / "component.json"
        (copy_directory / "component.json").write_bytes(original.read_bytes())

        assert outcome(document, RehearsalAdapter(world), token) == "UNAVAILABLE"

    def test_silence_is_not_absence(self, document, world: Path, token: str):
        rewrite(world / "platform.json", lambda platform: platform.pop("branding"))
        adapter = RehearsalAdapter(world)
        with pytest.raises(ActualIdentityUnavailable, match="refused the observation"):
            correspondence(document, adapter, token)

    def test_membership_is_scoped_to_the_runtime_root(
        self, tmp_path: Path, document, world: Path, token: str
    ):
        decoy = install_world(tmp_path / "decoy", document)
        rewrite(
            decoy / "components" / "tenant_authority" / "component.json",
            lambda member: member.update(component_id="decoy_component"),
        )
        recording = RecordingTransport()
        adapter = RehearsalAdapter(world, transport=recording)
        assert outcome(document, adapter, token) == "MATCH"

        answer = json.loads(recording.responses[0].stdout)
        members = answer["surfaces"]["membership"]["value"]["components"]
        assert members == ["tenant_authority"], "no host-wide discovery"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
    def test_a_link_inside_the_scope_is_an_unidentified_member(
        self, tmp_path: Path, document, world: Path, token: str
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        (world / "components" / "link").symlink_to(outside, target_is_directory=True)
        assert outcome(document, RehearsalAdapter(world), token) == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# A5 — the evidence is labelled rehearsal, not production
# ---------------------------------------------------------------------------


class TestRehearsalLabelling:
    def test_the_label_is_the_agreed_text(self):
        assert REHEARSAL_LABEL == "REHEARSAL / NOT PRODUCTION"
        assert "NOT PRODUCTION" in (running_platform_rehearsal.__doc__ or "")

    def test_the_evidence_record_is_labelled_and_never_claims_production(
        self, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world)
        assert outcome(document, adapter, token) == "MATCH"
        record = adapter.evidence_record()
        assert record["label"] == REHEARSAL_LABEL
        assert record["rehearsal"] is True
        assert record["production"] is False
        assert record["outcome"] == "accepted"

    def test_a_refused_evaluation_is_labelled_too(
        self, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world, transport=rewritten(_foreign))
        assert outcome(document, adapter, token) == "UNAVAILABLE"
        record = adapter.evidence_record()
        assert record["label"] == REHEARSAL_LABEL
        assert record["production"] is False
        assert record["surfaces"] == []

    def test_every_producer_answer_carries_the_label(self, world: Path):
        accepted = producer.answer(world, json.dumps({"op": "observe", "handle": "h"}))
        refused = producer.answer(world, "not json")
        for answer in (accepted, refused):
            assert answer["label"] == REHEARSAL_LABEL
            assert answer["rehearsal"] is True

    @pytest.mark.parametrize(
        "unlabel",
        [
            lambda d: d.pop("label"),
            lambda d: d.update(label="PRODUCTION"),
            lambda d: d.update(rehearsal=False),
            lambda d: d.pop("rehearsal"),
        ],
    )
    def test_evidence_that_is_not_labelled_is_refused(
        self, unlabel, document, world: Path, token: str
    ):
        adapter = RehearsalAdapter(world, transport=rewritten(unlabel))
        assert outcome(document, adapter, token) == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# The Deployment & Operations seam is preserved
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


class TestSeamPreservation:
    def test_the_adapter_is_an_unchanged_s4_source_behind_the_existing_wrapper(
        self, world: Path, document, token: str
    ):
        adapter = RehearsalAdapter(world)
        snapshot = adapter.observe(PlatformIdentityBinding(token))
        assert isinstance(snapshot, ActualPlatformSnapshot)
        assert snapshot.correlation_token == token
        assert snapshot.freshness_current is True
        assert snapshot.provenance in NORMATIVE
        evidence = OwnerSuppliedPlatformIdentityProvider(adapter).observe_identity(
            PlatformIdentityBinding(token)
        )
        assert evidence.correlation.token == token

    def test_the_adapter_uses_only_the_published_identity_contracts(self):
        used = {
            name
            for name in _imports(SOURCE / "adapter.py")
            if name.startswith("deployment_operations")
        }
        assert used == {
            "deployment_operations.platform_identity.ActualComponentIdentity",
            "deployment_operations.platform_identity.ActualIdentityUnavailable",
            "deployment_operations.platform_identity.EvidenceProvenance",
            "deployment_operations.platform_identity.IdentityField",
            "deployment_operations.platform_identity.PlatformIdentityBinding",
            "deployment_operations.platform_identity_source.ActualPlatformSnapshot",
        }

    def test_there_is_no_second_canonicalization_and_no_digest(self):
        for path in sorted(SOURCE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = _imports(path)
            assert not {"hashlib", "hmac", "zlib"} & imported, path.name
            calls = {
                node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Attribute, ast.Name))
            }
            assert not [
                c for c in calls if "digest" in c or "canonical" in c
            ], path.name

    def test_the_package_never_reads_the_deployment_runtime_root(self):
        for path in sorted(SOURCE.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            assert "runtime_root" not in text, path.name
            assert "operations_dir" not in text, path.name
