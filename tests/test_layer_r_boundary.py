"""Layer R boundary conformance — RuntimeAdapter and identity-producer rules.

This suite is the CI gate that the future Layer R source payload (migration
step M1, see ``layer-r/README.md``) will be pointed at.  Today it runs against
clearly labelled in-test fixtures: a compliant owner-side pair proves the
checks accept correct semantics, deliberately violating fixtures prove the
checks *catch* the forbidden ones.  It is not production code, not production
evidence, and asserts nothing about any host.

Semantics under test come from the shipped contracts and the S4 runbook, not
from invention:

* ``RuntimeAdapter`` six operations, ``stop`` detach-only, ``attach`` only for
  already-supervised elements (runbook §7 / A11 / A12; ``runtime.py``);
* the producer receives exactly ``{"op": "observe", "handle": <H>}`` and no
  expected-derived value (runbook §6 / §10 step 11);
* contamination, foreign and stale evidence are refused fail-closed by the
  shipped rules (``running_platform.owner_state``,
  ``deployment_operations.platform_identity``).

After M1 the same checks run against the real owner-side factory when
``LAYER_R_CONFORMANCE_TARGET=<absolute-path.py>:<factory>`` is set (the D5
declaration form); with the variable unset the hook tests skip.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualEvidence,
    ActualIdentityUnavailable,
    BindingEvaluationContext,
    EvidenceCorrelation,
    EvidenceFreshness,
    EvidenceProvenance,
    IdentityField,
    PlatformIdentityBinding,
    PlatformIdentitySurface,
    require_correlated_evidence,
    validate_actual_evidence,
)
from running_platform.owner_state import read_owner_state_surface

HANDLE = "ev-" + "0" * 32
FORBIDDEN_INPUT_KEYS = ("instance_digest", "expected_instance")
PRODUCER_OPS = ("observe",)

# ---------------------------------------------------------------------------
# Conformance checks (the suite itself)
# ---------------------------------------------------------------------------


def check_runtime_adapter_surface(adapter: Any) -> list[str]:
    """The six documented RuntimeAdapter operations must all be callable."""
    violations: list[str] = []
    for name in ("materialize", "migrate", "start", "attach", "request", "stop"):
        if not callable(getattr(adapter, name, None)):
            violations.append(f"runtime adapter has no callable {name!r}")
    return violations


def check_stop_is_detach_only(adapter: Any, world: Any) -> list[str]:
    """Stopping a handle must detach, never terminate a supervised member."""
    handle = adapter.start(object())
    adapter.stop(handle)
    if not world.alive(handle.member):
        return ["stop terminated a supervised member; stop must be detach-only"]
    return []


def check_attach_binds_only_supervised(adapter: Any) -> list[str]:
    """Attach must refuse an element the cell does not supervise."""
    try:
        adapter.attach(object())
    except Refused:
        return []
    return ["attach answered for an unsupervised element instead of refusing"]


def check_producer_input_transcript(frames: list[str]) -> list[str]:
    """Every frame to the producer must be exactly {"op","handle"}; nothing else."""
    violations: list[str] = []
    for index, frame in enumerate(frames):
        for key in FORBIDDEN_INPUT_KEYS:
            if key in frame:
                violations.append(
                    f"frame {index} forwards expected-derived value {key!r}"
                )
        try:
            document = json.loads(frame)
        except json.JSONDecodeError:
            violations.append(f"frame {index} is not one JSON object")
            continue
        if not isinstance(document, dict):
            violations.append(f"frame {index} is not one JSON object")
            continue
        if set(document) != {"op", "handle"}:
            violations.append(
                f"frame {index} carries {sorted(document)}, expected ['handle', 'op']"
            )
            continue
        if document["op"] not in PRODUCER_OPS:
            violations.append(f"frame {index} has unknown op {document['op']!r}")
        if not isinstance(document["handle"], str) or not document["handle"]:
            violations.append(f"frame {index} carries no opaque handle")
    return violations


# ---------------------------------------------------------------------------
# Labelled test fixtures — NOT PRODUCTION, NOT AN IMPLEMENTATION
# ---------------------------------------------------------------------------


class Refused(Exception):
    """A compliant owner-side component refuses, it does not comply silently."""


@dataclass
class SimulatedCellWorld:
    """A toy cell registry: which members the cell supervises and keeps alive."""

    members: dict[str, bool] = field(default_factory=dict)

    def supervise(self, name: str) -> str:
        self.members[name] = True
        return name

    def alive(self, name: str) -> bool:
        return self.members.get(name, False)


@dataclass
class FixtureHandle:
    member: str


class CompliantDetachAdapter:
    """TEST FIXTURE. Detach-only stop; attach only for supervised members."""

    def __init__(self, world: SimulatedCellWorld) -> None:
        self._world = world
        self._counter = 0

    def materialize(self, element: Any) -> dict:
        return {"materialized": True}

    def migrate(self, element: Any) -> dict:
        return {"executed": []}

    def start(self, element: Any) -> FixtureHandle:
        self._counter += 1
        return FixtureHandle(self._world.supervise(f"member-{self._counter}"))

    def attach(self, element: Any) -> FixtureHandle:
        raise Refused("no supervised member for this element")

    def request(self, handle: Any, op: Any, timeout: Any = None) -> dict:
        return {"op": op, "status": "ok"}

    def stop(self, handle: Any) -> dict:
        return {"detached": True}


class KillingStopAdapter(CompliantDetachAdapter):
    """TEST FIXTURE VIOLATION: stop kills the member it should only detach."""

    def stop(self, handle: FixtureHandle) -> dict:
        self._world.members[handle.member] = False
        return {"killed": True}


class DetachWorldAdapter(CompliantDetachAdapter):
    """TEST FIXTURE VIOLATION: attach for a member the world never supervised."""

    def attach(self, element: Any) -> FixtureHandle:
        return FixtureHandle("ghost")


def producer_request(handle: str = HANDLE) -> str:
    return json.dumps({"op": "observe", "handle": handle})


def binding(token: str = HANDLE) -> PlatformIdentityBinding:
    return PlatformIdentityBinding(
        token,
        BindingEvaluationContext(
            authority="tests/evaluation",
            basis="tests/deployment-record",
            target="example-platform",
            scope="test-environment",
            sequence=1,
            established_at="2026-10-09T00:00:00Z",
        ),
    )


def evidence_correlation(**overrides: object) -> EvidenceCorrelation:
    values: dict[str, object] = {
        "token": HANDLE,
        "scope": "test-environment",
        "sequence": 1,
        "target": "example-platform",
        "authority": "tests/owner-side-producer",
        "basis": "tests/owner-observation/1",
        "established_at": "2026-10-09T00:00:00Z",
    }
    values.update(overrides)
    return EvidenceCorrelation(**values)


def evidence(
    correlation: EvidenceCorrelation, *, current: bool = True
) -> ActualEvidence:
    """One complete, self-consistent evidence envelope for the given correlation."""
    component = ActualComponentIdentity(
        component_id="tenant_authority",
        component_version="0.1.0",
        artifact_identity={
            "artifact_type": "none",
            "digest": None,
            "pinned": False,
            "canonical_form": None,
        },
    )
    freshness = EvidenceFreshness(current)
    surface = PlatformIdentitySurface(
        platform_id="example-platform",
        manifest={
            "manifest_id": "example-manifest",
            "manifest_version": "1.0.0",
            "manifest_digest": "sha256:" + "1" * 64,
        },
        manifest_state="published",
        components=(
            ActualEvidence(
                value=component,
                provenance=EvidenceProvenance.ATTESTED,
                correlation=correlation,
                freshness=freshness,
            ),
        ),
        membership_established=True,
        configuration=IdentityField.present(
            {"tenant_authority": {"platform_id": "example-platform"}}
        ),
        golden_bundle=IdentityField.absent(),
        golden_bundle_inventory_established=True,
        extensions=IdentityField.absent(),
        branding=IdentityField.absent(),
    )
    return ActualEvidence(
        value=surface,
        provenance=EvidenceProvenance.ATTESTED,
        correlation=correlation,
        freshness=freshness,
    )


# ---------------------------------------------------------------------------
# The suite against the fixtures: accept compliant, catch violating
# ---------------------------------------------------------------------------


class TestRuntimeAdapterConformance:
    def test_compliant_fixture_passes_every_check(self) -> None:
        world = SimulatedCellWorld()
        adapter = CompliantDetachAdapter(world)
        assert check_runtime_adapter_surface(adapter) == []
        assert check_stop_is_detach_only(adapter, world) == []
        assert check_attach_binds_only_supervised(adapter) == []

    def test_killing_stop_is_flagged(self) -> None:
        world = SimulatedCellWorld()
        adapter = KillingStopAdapter(world)
        violations = check_stop_is_detach_only(adapter, world)
        assert any("detach-only" in violation for violation in violations)

    def test_unsupervised_attach_is_flagged(self) -> None:
        world = SimulatedCellWorld()
        adapter = DetachWorldAdapter(world)
        violations = check_attach_binds_only_supervised(adapter)
        assert any("instead of refusing" in violation for violation in violations)

    def test_missing_operation_is_flagged(self) -> None:
        violations = check_runtime_adapter_surface(object())
        assert len(violations) == 6


class TestProducerInputBoundary:
    def test_compliant_frames_pass(self) -> None:
        assert check_producer_input_transcript([producer_request()]) == []

    def test_forwarded_expected_state_is_flagged(self) -> None:
        frame = json.dumps(
            {"op": "observe", "handle": HANDLE, "instance_digest": "sha256:" + "1" * 64}
        )
        violations = check_producer_input_transcript([frame])
        assert any("instance_digest" in violation for violation in violations)

    def test_extra_field_is_flagged(self) -> None:
        frame = json.dumps({"op": "observe", "handle": HANDLE, "attempt": 1})
        violations = check_producer_input_transcript([frame])
        assert any(
            "expected" in violation or "attempt" in violation
            for violation in violations
        )

    def test_missing_handle_is_flagged(self) -> None:
        violations = check_producer_input_transcript([json.dumps({"op": "observe"})])
        assert violations


class TestShippedRefusalRules:
    """The D&O-side fail-closed rules the owner-side boundary must survive."""

    def test_matching_correlation_is_accepted(self) -> None:
        established = require_correlated_evidence(
            binding(), evidence(evidence_correlation())
        )
        assert established.token == HANDLE

    def test_foreign_handle_is_refused(self) -> None:
        foreign = evidence(evidence_correlation(token="ev-" + "1" * 32))
        with pytest.raises(ActualIdentityUnavailable):
            require_correlated_evidence(binding(), foreign)

    def test_bare_handle_agreement_establishes_nothing(self) -> None:
        bare = evidence(
            evidence_correlation(scope="", target="", authority="", basis="")
        )
        with pytest.raises(ActualIdentityUnavailable):
            require_correlated_evidence(binding(), bare)

    def test_stale_evidence_is_refused(self) -> None:
        with pytest.raises(ActualIdentityUnavailable, match="stale"):
            validate_actual_evidence(evidence(evidence_correlation(), current=False))

    def test_contaminated_owner_surface_is_refused(self) -> None:
        text = json.dumps({"instance_digest": "sha256:" + "1" * 64})
        with pytest.raises(ActualIdentityUnavailable):
            read_owner_state_surface(text, binding())

    def test_malformed_owner_surface_is_refused(self) -> None:
        with pytest.raises(ActualIdentityUnavailable):
            read_owner_state_surface("{not json", binding())


class TestDeclaredTargetHook:
    """After M1 the suite is pointed at the real payload via the env hook."""

    def test_hook_is_optional(self) -> None:
        target = os.environ.get("LAYER_R_CONFORMANCE_TARGET")
        if not target:
            pytest.skip(
                "no Layer R payload declared (LAYER_R_CONFORMANCE_TARGET unset)"
            )
        module_path, _, factory_name = target.partition(":")
        spec = importlib.util.spec_from_file_location("layer_r_target", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        factory: Callable[[], dict] = getattr(module, factory_name)
        wiring = factory()
        assert isinstance(wiring, dict)
        assert check_runtime_adapter_surface(wiring.get("runtime_adapter")) == []
        assert set(wiring) <= {
            "runtime_adapter",
            "identity_provider",
            "identity_source",
        }
