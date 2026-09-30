"""Architectural tests for the D&O reconciliation / drift-detection slice.

These tests verify architecture rules, not merely successful construction
(ADR-0015 §16; ADR-0016 §30). Reconciliation answers exactly one question —
«does the actual, observable Running Platform still correspond to the exact
Platform Instance this deployment record pins?» — and the tests hold it to that
question and to nothing more:

* AC A — desired state is the exact identity/version/digest already fixed in an
  authoritative deployment record; a name-only, floating or unverified subject
  is refused before any observation;
* AC B — actual state is obtained through the existing Running Platform
  observation boundary, and the expected identity is never treated as actual
  evidence;
* AC C — any proven identity/version/digest divergence is an operational
  failure signal, and missing, invalid or stale evidence is fail-closed rather
  than healthy;
* AC D — reconciliation never falsely upgrades `deployed`, `ready` or
  `realized`, and existing authoritative state semantics are untouched;
* AC E — outcomes are append-only, correlatable, secret-free and free of
  business/tenant data;
* AC F/G — there is no remediation path of any kind, and repeating the
  operation against unchanged evidence has no deployment or runtime side
  effects; a failed publication leaves no contradictory state or signal.

The synthetic fixtures below pin the *contract* at its edges (drift, staleness,
publication failure); the final class runs the same operation against a
genuinely deployed platform of this repository, realized through the real
Factory surfaces and the real runtime.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _deployment_helpers import (
    ROOT,
    environment_for,
    instance_for,
    manifest_for,
    request_for,
)

from deployment_operations import (
    LIFECYCLE_FAILED,
    LIFECYCLE_REALIZED,
    RECONCILIATION_DRIFT,
    RECONCILIATION_IN_CORRESPONDENCE,
    RECONCILIATION_OUTCOMES,
    RECONCILIATION_UNVERIFIABLE,
    ActualComponentIdentity,
    ActualEvidence,
    ActualPlatformSnapshot,
    ComponentRecord,
    Deployment,
    DeploymentInputRejected,
    DeploymentStateError,
    DeploymentStateStore,
    EventJournal,
    EvidenceProvenance,
    IdentityField,
    InvalidDeploymentStateTransition,
    OwnerSuppliedPlatformIdentityProvider,
    PlatformIdentityBinding,
    ReconciliationDriftDetected,
    ReconciliationEvidenceUnavailable,
    ReconciliationRequest,
    SecretLeakRefused,
    derive_reconciliation_id,
    reconcile,
)
from deployment_operations import deployment as deployment_module
from deployment_operations import reconciliation as reconciliation_module
from deployment_operations.platform_identity import ActualIdentityUnavailable
from platform_instance import compute_instance_digest


@contextmanager
def refusal(
    error_type: type[Exception], fragment: str
) -> Iterator[pytest.ExceptionInfo]:
    """Assert a fail-closed refusal that names its own reason (ADR-0016 §20)."""
    with pytest.raises(error_type) as caught:
        yield caught
    diagnostics = [*getattr(caught.value, "errors", ()), str(caught.value)]
    assert any(
        re.search(fragment, entry) for entry in diagnostics
    ), f"expected {fragment!r} among the refusal diagnostics: {diagnostics}"


# ---------------------------------------------------------------------------
# Synthetic fixtures: one exact instance, observed or diverged
# ---------------------------------------------------------------------------

MODULE_PATH = ROOT / "src" / "deployment_operations" / "reconciliation.py"

#: The Manifest identity the synthetic desired state is bound to.
MANIFEST_BINDING: dict[str, Any] = {
    "manifest_id": "reconciliation-manifest",
    "manifest_version": "1.0.0",
    "manifest_digest": "sha256:" + "a" * 64,
}

#: Explicit absence of an artifact: the component needs none (ADR-0018).
NO_ARTIFACT: dict[str, Any] = {
    "artifact_type": "none",
    "digest": None,
    "pinned": False,
    "canonical_form": None,
}

#: The instance the synthetic Running Platform is expected to be.
OBSERVED_INSTANCE: dict[str, Any] = {
    "platform_id": "reconciliation-platform",
    "manifest": dict(MANIFEST_BINDING),
    "manifest_state": "published",
    "components": [
        {
            "component_id": "alpha",
            "component_version": "1.0.0",
            "artifact": dict(NO_ARTIFACT),
        }
    ],
    "golden_bundle": None,
}

#: Its digest — the pinned instance identity of the synthetic record.
OBSERVED_DIGEST = compute_instance_digest(OBSERVED_INSTANCE)

AT = "2026-09-30T00:00:00Z"


def _components(
    *,
    component_id: str = "alpha",
    component_version: str = "1.0.0",
    artifact_type: str = "none",
    artifact_digest: str | None = None,
) -> tuple[ComponentRecord, ...]:
    return (
        ComponentRecord(
            component_id=component_id,
            component_version=component_version,
            artifact_type=artifact_type,
            artifact_digest=artifact_digest,
        ),
    )


def _record(
    *,
    realized: bool = True,
    running: bool = True,
    identity_verified: bool = True,
    components: tuple[ComponentRecord, ...] | None = None,
    platform_id: str | None = "reconciliation-platform",
    instance_digest: str | None = OBSERVED_DIGEST,
    environment_id: str = "local",
    deployment_id: str = "dep-reconciliation-platform-local-a1",
) -> Any:
    """One authoritative deployment record of the synthetic instance.

    ``realized=False`` leaves the record before any verification; the other
    keywords express records that could only exist if state were tampered with,
    which is exactly what several tests must refuse.
    """
    record = deployment_module.DeploymentRecord.initial(
        deployment_id=deployment_id,
        environment_id=environment_id,
        attempt=1,
        platform_id=platform_id,
        manifest_id=MANIFEST_BINDING["manifest_id"],
        manifest_version=MANIFEST_BINDING["manifest_version"],
        manifest_digest=MANIFEST_BINDING["manifest_digest"],
        manifest_state="published",
        instance_digest=instance_digest,
        at=AT,
    )
    record = record.with_components(
        _components() if components is None else components, at=AT
    )
    if not realized:
        return record
    record = record.mark_running(at=AT, running=True)
    record = record.mark_ready(at=AT)
    if identity_verified:
        record = record.mark_identity_verified(at=AT)
    else:
        record = replace(record, identity_verified=False)
    record = replace(record, lifecycle=LIFECYCLE_REALIZED)
    if not running:
        record = replace(record, running=False, ready=False)
    return record


def _deployment(
    operations_dir: Path,
    record: Any,
    *,
    secrets: Sequence[str] = (),
) -> Deployment:
    """A deployment handle whose state file genuinely holds ``record``."""
    state_path = DeploymentStateStore.path_for(operations_dir, record.deployment_id)
    store = DeploymentStateStore(state_path)
    store.write(record)
    journal = EventJournal(EventJournal.path_for(operations_dir, record.deployment_id))
    return Deployment(
        record=record,
        verification=SimpleNamespace(),
        state_path=state_path,
        events_path=journal.path,
        _journal=journal,
        _store=store,
        _secrets=tuple(secrets),
    )


def _snapshot(**overrides: Any) -> ActualPlatformSnapshot:
    """One owner-supplied actual identity snapshot of the expected platform."""
    values: dict[str, Any] = {
        "platform_id": "reconciliation-platform",
        "manifest": dict(MANIFEST_BINDING),
        "manifest_state": "published",
        "components": (ActualComponentIdentity("alpha", "1.0.0", dict(NO_ARTIFACT)),),
        "membership_established": True,
        "configuration": IdentityField.absent(),
        "golden_bundle": IdentityField.absent(),
        "golden_bundle_inventory_established": True,
        "extensions": IdentityField.absent(),
        "branding": IdentityField.absent(),
        "provenance": EvidenceProvenance.MEASURED,
        "correlation_token": None,
        "freshness_current": True,
    }
    values.update(overrides)
    return ActualPlatformSnapshot(**values)


class _OwnerSource:
    """Test double of the Running Platform owner's observation surface.

    It answers for whatever evaluation the boundary bound it to, and records
    the bindings it received so the tests can prove what the observer was — and
    was not — told.
    """

    def __init__(self, build: Callable[[PlatformIdentityBinding], Any]) -> None:
        self.build = build
        self.bindings: list[PlatformIdentityBinding] = []

    def observe(self, binding: PlatformIdentityBinding) -> Any:
        self.bindings.append(binding)
        return self.build(binding)


def _owner(**overrides: Any) -> _OwnerSource:
    """An owner surface that reports the expected platform, with overrides."""

    def build(binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        return _snapshot(correlation_token=binding.token, **overrides)

    return _OwnerSource(build)


def _failing_owner(error: Exception) -> _OwnerSource:
    def build(binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        raise error

    return _OwnerSource(build)


def _provider(source: _OwnerSource) -> OwnerSuppliedPlatformIdentityProvider:
    return OwnerSuppliedPlatformIdentityProvider(source)


def _request(deployment: Deployment) -> ReconciliationRequest:
    return ReconciliationRequest(
        deployment=deployment, reconciliation_id=derive_reconciliation_id(deployment)
    )


def _summarize(record: Any) -> dict[str, Any]:
    """Every authoritative state field reconciliation must not touch."""
    return {
        "lifecycle": record.lifecycle,
        "ready": record.ready,
        "running": record.running,
        "identity_verified": record.identity_verified,
        "deployed": record.deployed,
        "failure": record.failure,
        "stages": record.stages,
        "components": record.components,
        "migrations": record.migrations,
        "operational_actions": record.operational_actions,
    }


def _journal_lines(deployment: Deployment) -> list[dict[str, Any]]:
    path = Path(deployment.events_path)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _digests(root: Path) -> dict[str, str]:
    """Content digests of a runtime tree, for «nothing was touched» checks."""
    digests: dict[str, str] = {}
    if not root.is_dir():
        return digests
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


def _binding_token(record: Any, sequence: int = 1) -> str:
    """The binding of one observation, as the boundary binds it."""
    return f"{record.deployment_id}#observation:{sequence}"


# ---------------------------------------------------------------------------
# AC A — desired state is the exact recorded Platform Instance
# ---------------------------------------------------------------------------


class TestExactDesiredTarget:
    """Reconciliation has one exact subject: never a name, never a selector."""

    def test_reconciliation_id_names_the_exact_recorded_instance(self, tmp_path: Path):
        record = _record()
        deployment = _deployment(tmp_path, record)
        identity = derive_reconciliation_id(deployment)
        assert record.deployment_id in identity
        assert record.instance_digest in identity
        assert record.environment_id in identity

    def test_a_retargeted_reconciliation_id_is_refused(self, tmp_path: Path):
        record = _record()
        deployment = _deployment(tmp_path, record)
        source = _owner()
        before = json.loads(Path(deployment.state_path).read_text(encoding="utf-8"))

        with refusal(DeploymentInputRejected, "reconciliation_id"):
            reconcile(
                ReconciliationRequest(
                    deployment=deployment,
                    reconciliation_id="recon:some-other-platform@latest",
                ),
                identity_provider=_provider(source),
            )
        assert source.bindings == [], "a refused request must not observe anything"
        assert (
            json.loads(Path(deployment.state_path).read_text(encoding="utf-8"))
            == before
        )

    @pytest.mark.parametrize(
        ("build", "fragment"),
        [
            (
                lambda: replace(_record(), instance_digest=""),
                "concrete instance digest",
            ),
            (
                lambda: replace(_record(), instance_digest="latest"),
                "concrete instance digest",
            ),
            (lambda: replace(_record(), platform_id=""), "Platform Instance identity"),
            (
                lambda: replace(_record(), platform_id=None),
                "Platform Instance identity",
            ),
            (lambda: replace(_record(), components=()), "component identity"),
            (
                lambda: _record(identity_verified=False),
                "verified identity",
            ),
        ],
        ids=[
            "empty-digest",
            "floating-digest",
            "empty-platform",
            "absent-platform",
            "no-components",
            "unverified-identity",
        ],
    )
    def test_an_inexact_subject_is_refused_before_any_observation(
        self, tmp_path: Path, build: Callable[[], Any], fragment: str
    ):
        deployment = _deployment(tmp_path, build())
        source = _owner()

        with refusal(DeploymentInputRejected, fragment):
            reconcile(_request(deployment), identity_provider=_provider(source))
        assert source.bindings == [], "nothing may be observed for an inexact subject"

    @pytest.mark.parametrize("realized", [True, False])
    def test_a_record_that_never_realized_the_instance_is_refused(
        self, tmp_path: Path, realized: bool
    ):
        record = _record(realized=False)
        if realized:
            record = record.mark_failed(
                stage="deploying",
                reason="the instance was not realized",
                errors=["deployment execution failed"],
                at=AT,
            )
        deployment = _deployment(tmp_path, record)
        source = _owner()
        with refusal(DeploymentInputRejected, "not a realized deployment"):
            reconcile(_request(deployment), identity_provider=_provider(source))
        assert source.bindings == []

    def test_a_record_without_a_running_platform_is_refused(self, tmp_path: Path):
        deployment = _deployment(tmp_path, _record(running=False))
        source = _owner()
        with refusal(DeploymentInputRejected, "no Running Platform to observe"):
            reconcile(_request(deployment), identity_provider=_provider(source))
        assert source.bindings == []

    def test_tampered_persisted_state_is_refused(self, tmp_path: Path):
        deployment = _deployment(tmp_path, _record())
        persisted = json.loads(Path(deployment.state_path).read_text(encoding="utf-8"))
        persisted["platform_instance"]["instance_digest"] = "sha256:" + "b" * 64
        Path(deployment.state_path).write_text(json.dumps(persisted), encoding="utf-8")
        source = _owner()
        with pytest.raises(InvalidDeploymentStateTransition):
            reconcile(_request(deployment), identity_provider=_provider(source))
        assert source.bindings == []

    def test_missing_persisted_state_is_refused(self, tmp_path: Path):
        deployment = _deployment(tmp_path, _record())
        Path(deployment.state_path).unlink()
        with pytest.raises(InvalidDeploymentStateTransition):
            reconcile(_request(deployment), identity_provider=_provider(_owner()))


# ---------------------------------------------------------------------------
# AC B — actual state is independently observed
# ---------------------------------------------------------------------------


class TestIndependentActualObservation:
    """The expected identity is never offered to the observer, or as evidence."""

    def test_the_observer_receives_only_an_opaque_binding(self, tmp_path: Path):
        record = _record()
        deployment = _deployment(tmp_path, record)
        source = _owner()
        reconcile(_request(deployment), identity_provider=_provider(source))

        assert len(source.bindings) == 1
        binding = source.bindings[0]
        assert isinstance(binding, PlatformIdentityBinding)
        assert binding.token == _binding_token(record)
        assert binding.token != record.instance_digest
        assert not hasattr(binding, "instance_digest")

    def test_projection_consumes_observed_evidence_not_desired_state(
        self, tmp_path: Path, monkeypatch
    ):
        """The actual projection is built from what was observed, not from what was wanted."""
        deployment = _deployment(tmp_path, _record())
        seen: dict[str, Any] = {}
        original = reconciliation_module.project_actual_identity

        def spy(evidence: Any) -> dict[str, Any]:
            seen["evidence"] = evidence
            return original(evidence)

        monkeypatch.setattr(reconciliation_module, "project_actual_identity", spy)
        with pytest.raises(ReconciliationDriftDetected):
            reconcile(
                _request(deployment),
                identity_provider=_provider(_owner(platform_id="another-platform")),
            )

        evidence = seen["evidence"]
        assert isinstance(evidence, ActualEvidence)
        assert evidence.value.platform_id == "another-platform"
        assert evidence.value.platform_id != deployment.record.platform_id

    def test_unavailable_actual_state_is_never_replaced_by_desired_state(
        self, tmp_path: Path
    ):
        deployment = _deployment(tmp_path, _record())
        with pytest.raises(ReconciliationEvidenceUnavailable):
            reconcile(
                _request(deployment),
                identity_provider=_provider(_failing_owner(RuntimeError("offline"))),
            )
        record = deployment.record
        assert record.in_correspondence is None, "unproven is not healthy"
        assert record.drifted is False
        assert record.deployed is True, "the record's own claim is untouched"
        assert record.reconciliations[-1].outcome == RECONCILIATION_UNVERIFIABLE

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"freshness_current": False}, "stale"),
            ({"provenance": "DERIVED"}, "provenance"),
            ({"membership_established": False}, "membership"),
            ({"configuration": IdentityField.unknown()}, "UNKNOWN"),
            ({"components": ()}, "empty"),
            ({"manifest_state": "not-a-lifecycle-state"}, "manifest_state"),
        ],
    )
    def test_invalid_or_stale_evidence_is_unverifiable(
        self, tmp_path: Path, overrides: Mapping[str, Any], fragment: str
    ):
        deployment = _deployment(tmp_path, _record())
        with pytest.raises(ReconciliationEvidenceUnavailable) as failure:
            reconcile(
                _request(deployment),
                identity_provider=_provider(_owner(**overrides)),
            )
        assert fragment in "\n".join(failure.value.errors)
        observation = deployment.record.reconciliations[-1]
        assert observation.outcome == RECONCILIATION_UNVERIFIABLE
        assert observation.actual is None
        assert deployment.record.in_correspondence is None

    def test_a_malformed_owner_surface_is_unverifiable(self, tmp_path: Path):
        deployment = _deployment(tmp_path, _record())
        with pytest.raises(ReconciliationEvidenceUnavailable):
            reconcile(
                _request(deployment),
                identity_provider=_provider(_OwnerSource(lambda binding: None)),
            )
        assert deployment.record.reconciliations[-1].outcome == (
            RECONCILIATION_UNVERIFIABLE
        )

    def test_deployment_time_evidence_cannot_be_replayed(self, tmp_path: Path):
        """The owner surface of the deployment itself is not this observation."""
        runtime_root = tmp_path / "runtime"
        record = _record()
        deployment = _deployment(runtime_root / "operations", record)
        surface = runtime_root / "running_platform_identity.json"
        surface.write_text(
            json.dumps(
                {
                    "platform_id": OBSERVED_INSTANCE["platform_id"],
                    "manifest": dict(MANIFEST_BINDING),
                    "manifest_state": "published",
                    "components": [
                        {
                            "component_id": "alpha",
                            "component_version": "1.0.0",
                            "artifact_identity": dict(NO_ARTIFACT),
                        }
                    ],
                    "membership_established": True,
                    "configuration": {"state": "ABSENT"},
                    "golden_bundle": {"state": "ABSENT"},
                    "golden_bundle_inventory_established": True,
                    "extensions": {"state": "ABSENT"},
                    "branding": {"state": "ABSENT"},
                    "provenance": EvidenceProvenance.MEASURED,
                    "correlation_token": record.deployment_id,
                    "freshness_current": True,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ReconciliationEvidenceUnavailable) as failure:
            reconcile(_request(deployment))
        assert "correlation" in "\n".join(failure.value.errors)

    def test_the_environment_owner_surface_is_read_for_this_observation(
        self, tmp_path: Path
    ):
        """The default source is the owner-published surface of the environment."""
        runtime_root = tmp_path / "runtime"
        record = _record()
        deployment = _deployment(runtime_root / "operations", record)
        (runtime_root / "running_platform_identity.json").write_text(
            json.dumps(
                {
                    "platform_id": OBSERVED_INSTANCE["platform_id"],
                    "manifest": dict(MANIFEST_BINDING),
                    "manifest_state": "published",
                    "components": [
                        {
                            "component_id": "alpha",
                            "component_version": "1.0.0",
                            "artifact_identity": dict(NO_ARTIFACT),
                        }
                    ],
                    "membership_established": True,
                    "configuration": {"state": "ABSENT"},
                    "golden_bundle": {"state": "ABSENT"},
                    "golden_bundle_inventory_established": True,
                    "extensions": {"state": "ABSENT"},
                    "branding": {"state": "ABSENT"},
                    "provenance": EvidenceProvenance.MEASURED,
                    "correlation_token": _binding_token(record),
                    "freshness_current": True,
                }
            ),
            encoding="utf-8",
        )
        result = reconcile(_request(deployment))
        assert result.in_correspondence


# ---------------------------------------------------------------------------
# AC C — drift and unverifiability are surfaced, never smoothed over
# ---------------------------------------------------------------------------


class TestDriftDetection:
    """Any proven divergence is a failure signal; no proof means no health."""

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"platform_id": "another-platform"}, ["platform_id", "instance_digest"]),
            (
                {"manifest": {**MANIFEST_BINDING, "manifest_version": "2.0.0"}},
                ["manifest.manifest_version", "instance_digest"],
            ),
            (
                {
                    "manifest": {
                        **MANIFEST_BINDING,
                        "manifest_digest": "sha256:" + "b" * 64,
                    }
                },
                ["manifest.manifest_digest", "instance_digest"],
            ),
            ({"manifest_state": "approved"}, ["manifest_state", "instance_digest"]),
            (
                {
                    "components": (
                        ActualComponentIdentity("alpha", "9.9.9", dict(NO_ARTIFACT)),
                    )
                },
                ["instance_digest", "components[alpha].component_version"],
            ),
            (
                {
                    "components": (
                        ActualComponentIdentity(
                            "alpha",
                            "1.0.0",
                            {
                                **NO_ARTIFACT,
                                "artifact_type": "source_package",
                                "digest": "sha256:" + "c" * 64,
                                "pinned": True,
                                "canonical_form": "source_package/v1",
                            },
                        ),
                    )
                },
                ["instance_digest", "components[alpha].artifact_digest"],
            ),
            (
                {
                    "components": (
                        ActualComponentIdentity("beta", "1.0.0", dict(NO_ARTIFACT)),
                    )
                },
                [
                    "instance_digest",
                    "components[alpha].absent",
                    "components[beta].unexpected",
                ],
            ),
            (
                {
                    "components": (
                        ActualComponentIdentity("alpha", "1.0.0", dict(NO_ARTIFACT)),
                        ActualComponentIdentity("beta", "1.0.0", dict(NO_ARTIFACT)),
                    )
                },
                ["instance_digest", "components[beta].unexpected"],
            ),
        ],
    )
    def test_every_identity_version_digest_divergence_is_drift(
        self, tmp_path: Path, overrides: Mapping[str, Any], expected: Sequence[str]
    ):
        record = _record()
        deployment = _deployment(tmp_path, record)
        with pytest.raises(ReconciliationDriftDetected) as failure:
            reconcile(
                _request(deployment),
                identity_provider=_provider(_owner(**overrides)),
            )
        assert list(failure.value.errors) == list(expected)
        assert failure.value.instance_digest == record.instance_digest
        assert failure.value.actual_instance_digest != record.instance_digest
        observation = deployment.record.reconciliations[-1]
        assert observation.outcome == RECONCILIATION_DRIFT
        assert list(observation.differences) == list(expected)
        assert deployment.record.drifted is True
        assert deployment.record.in_correspondence is False

    def test_a_divergence_the_field_comparison_cannot_name_is_still_drift(
        self, tmp_path: Path
    ):
        """The canonical digest decides: content outside the named fields counts."""
        deployment = _deployment(tmp_path, _record())
        with pytest.raises(ReconciliationDriftDetected) as failure:
            reconcile(
                _request(deployment),
                identity_provider=_provider(
                    _owner(
                        configuration=IdentityField.present(
                            {"alpha": {"platform_id": OBSERVED_INSTANCE["platform_id"]}}
                        )
                    )
                ),
            )
        assert list(failure.value.errors) == ["instance_digest"]
        assert deployment.record.reconciliations[-1].outcome == RECONCILIATION_DRIFT

    def test_in_correspondence_returns_without_a_failure_signal(self, tmp_path: Path):
        deployment = _deployment(tmp_path, _record())
        result = reconcile(_request(deployment), identity_provider=_provider(_owner()))
        assert result.outcome == RECONCILIATION_IN_CORRESPONDENCE
        assert result.in_correspondence is True
        assert result.drifted is False
        assert deployment.record.drifted is False
        assert deployment.record.in_correspondence is True

    def test_an_unverifiable_observation_does_not_erase_a_proven_drift(
        self, tmp_path: Path
    ):
        deployment = _deployment(tmp_path, _record())
        with pytest.raises(ReconciliationDriftDetected):
            reconcile(
                _request(deployment),
                identity_provider=_provider(_owner(platform_id="another-platform")),
            )
        with pytest.raises(ReconciliationEvidenceUnavailable):
            reconcile(
                _request(deployment),
                identity_provider=_provider(_failing_owner(RuntimeError("offline"))),
            )
        assert deployment.record.drifted is True
        assert [entry.outcome for entry in deployment.record.reconciliations] == [
            RECONCILIATION_DRIFT,
            RECONCILIATION_UNVERIFIABLE,
        ]

    def test_evidence_that_becomes_unavailable_is_not_a_match(self, tmp_path: Path):
        deployment = _deployment(tmp_path, _record())
        for _ in range(2):
            with pytest.raises(ReconciliationEvidenceUnavailable):
                reconcile(
                    _request(deployment),
                    identity_provider=_provider(
                        _failing_owner(ActualIdentityUnavailable("gone"))
                    ),
                )
        assert deployment.record.in_correspondence is None
        assert deployment.record.drifted is False


# ---------------------------------------------------------------------------
# AC D — state honesty: nothing is upgraded, nothing is rewritten
# ---------------------------------------------------------------------------


class TestStateHonesty:
    """Reconciliation records; it does not revise what the operation did."""

    @pytest.mark.parametrize(
        "provider_factory",
        [
            lambda: _provider(_owner()),
            lambda: _provider(_owner(platform_id="another-platform")),
            lambda: _provider(_failing_owner(RuntimeError("offline"))),
        ],
    )
    def test_no_authoritative_state_field_is_changed(
        self, tmp_path: Path, provider_factory: Callable[[], Any]
    ):
        deployment = _deployment(tmp_path, _record())
        before = _summarize(deployment.record)
        try:
            reconcile(_request(deployment), identity_provider=provider_factory())
        except (ReconciliationDriftDetected, ReconciliationEvidenceUnavailable):
            pass
        assert _summarize(deployment.record) == before
        assert len(deployment.record.reconciliations) == 1

    def test_reconciliation_never_claims_realized_or_deployed(self, tmp_path: Path):
        for record in (_record(realized=False), _record(running=False)):
            deployment = _deployment(tmp_path, record)
            before = _summarize(deployment.record)
            with pytest.raises(DeploymentInputRejected):
                reconcile(_request(deployment), identity_provider=_provider(_owner()))
            assert _summarize(deployment.record) == before
            assert deployment.record.deployed is False
            assert deployment.record.reconciliations == ()

    def test_a_failed_record_keeps_stating_its_failure(self, tmp_path: Path):
        failed = _record(realized=False).mark_failed(
            stage="deploying",
            reason="deployment execution failed",
            errors=["the instance was not realized"],
            at=AT,
        )
        deployment = _deployment(tmp_path, failed)
        with refusal(DeploymentInputRejected, "states a failure"):
            reconcile(_request(deployment), identity_provider=_provider(_owner()))
        assert deployment.record.lifecycle == LIFECYCLE_FAILED
        assert deployment.record.failure == failed.failure

    def test_the_record_round_trips_with_its_reconciliation_history(
        self, tmp_path: Path
    ):
        deployment = _deployment(tmp_path, _record())
        reconcile(_request(deployment), identity_provider=_provider(_owner()))
        reread = DeploymentStateStore(deployment.state_path).read()
        assert reread == deployment.record
        assert reread.reconciliations == deployment.record.reconciliations

    def test_a_record_without_reconciliation_history_still_reads(self, tmp_path: Path):
        """Existing deployment state documents keep their exact meaning."""
        record = _record()
        deployment = _deployment(tmp_path, record)
        document = json.loads(Path(deployment.state_path).read_text(encoding="utf-8"))
        document.pop("reconciliation", None)
        Path(deployment.state_path).write_text(json.dumps(document), encoding="utf-8")
        reread = DeploymentStateStore(deployment.state_path).read()
        assert reread == record
        assert reread.reconciliations == ()
        assert reread.in_correspondence is None
        assert reread.drifted is False
        assert reread.last_reconciliation is None

    def test_only_a_recorded_outcome_can_be_appended(self):
        record = _record()
        with pytest.raises(DeploymentStateError):
            record.with_reconciliation(SimpleNamespace(outcome="probably-fine"), at=AT)
        assert set(RECONCILIATION_OUTCOMES) == {
            RECONCILIATION_IN_CORRESPONDENCE,
            RECONCILIATION_DRIFT,
            RECONCILIATION_UNVERIFIABLE,
        }


# ---------------------------------------------------------------------------
# AC E — observability: append-only, correlated, secret-free
# ---------------------------------------------------------------------------


class TestObservability:
    """Every observation is one correlatable signal, and signals never lie."""

    def test_one_observation_publishes_one_correlated_signal(self, tmp_path: Path):
        record = _record()
        deployment = _deployment(tmp_path, record)
        reconcile(_request(deployment), identity_provider=_provider(_owner()))
        lines = _journal_lines(deployment)
        assert len(lines) == 1
        event = lines[0]
        assert event["sequence"] == 1
        assert event["event"] == "reconciliation_in_correspondence"
        assert event["deployment_id"] == record.deployment_id
        assert event["environment_id"] == record.environment_id
        assert event["platform_instance"] == {
            "platform_id": record.platform_id,
            "instance_digest": record.instance_digest,
        }
        assert event["manifest"]["manifest_digest"] == record.manifest_digest
        assert event["stage"] == "reconciliation"
        assert event["detail"]["reconciliation_id"] == derive_reconciliation_id(
            deployment
        )
        assert event["detail"]["reconciliation_sequence"] == 1
        assert event["detail"]["outcome"] == RECONCILIATION_IN_CORRESPONDENCE
        assert event["detail"]["actual_instance_digest"] == OBSERVED_DIGEST
        assert event["detail"]["evidence_provenance"] == EvidenceProvenance.MEASURED
        assert event["detail"]["differences"] == []

    @pytest.mark.parametrize(
        ("provider_factory", "outcome", "event"),
        [
            (
                lambda: _provider(_owner()),
                RECONCILIATION_IN_CORRESPONDENCE,
                "reconciliation_in_correspondence",
            ),
            (
                lambda: _provider(_owner(platform_id="another-platform")),
                RECONCILIATION_DRIFT,
                "reconciliation_drift_detected",
            ),
            (
                lambda: _provider(_failing_owner(RuntimeError("offline"))),
                RECONCILIATION_UNVERIFIABLE,
                "reconciliation_unverifiable",
            ),
        ],
    )
    def test_state_and_signal_agree_on_every_outcome(
        self,
        tmp_path: Path,
        provider_factory: Callable[[], Any],
        outcome: str,
        event: str,
    ):
        deployment = _deployment(tmp_path, _record())
        try:
            reconcile(_request(deployment), identity_provider=provider_factory())
        except (ReconciliationDriftDetected, ReconciliationEvidenceUnavailable):
            pass
        observation = deployment.record.reconciliations[-1]
        assert observation.outcome == outcome
        signal = _journal_lines(deployment)[-1]
        assert signal["event"] == event
        assert signal["detail"]["outcome"] == observation.outcome
        if outcome == RECONCILIATION_UNVERIFIABLE:
            assert signal["detail"]["actual_instance_digest"] is None
            assert signal["detail"]["errors"]

    def test_repeated_observations_append_in_order(self, tmp_path: Path):
        deployment = _deployment(tmp_path, _record())
        for _ in range(3):
            reconcile(_request(deployment), identity_provider=_provider(_owner()))
        assert [entry.sequence for entry in deployment.record.reconciliations] == [
            1,
            2,
            3,
        ]
        assert [line["sequence"] for line in _journal_lines(deployment)] == [1, 2, 3]
        assert [
            line["detail"]["reconciliation_sequence"]
            for line in _journal_lines(deployment)
        ] == [1, 2, 3]

    def test_the_journal_lines_up_with_the_deployment_operation(self, tmp_path: Path):
        record = _record()
        deployment = _deployment(tmp_path, record)
        journal = EventJournal(deployment.events_path)
        journal.append(
            deployment_module.DeploymentEvent(
                sequence=journal.next_sequence(),
                event="deployment_realized",
                occurred_at=AT,
                deployment_id=record.deployment_id,
                environment_id=record.environment_id,
                platform_id=record.platform_id,
                instance_digest=record.instance_digest,
            )
        )
        reconcile(_request(deployment), identity_provider=_provider(_owner()))
        lines = _journal_lines(deployment)
        assert [line["event"] for line in lines] == [
            "deployment_realized",
            "reconciliation_in_correspondence",
        ]
        assert [line["sequence"] for line in lines] == [1, 2]

    def test_a_secret_in_the_published_surface_is_refused_not_persisted(
        self, tmp_path: Path
    ):
        secret = "correct-horse-battery-staple"
        record = _record()
        deployment = _deployment(tmp_path, record, secrets=(secret,))
        before = Path(deployment.state_path).read_text(encoding="utf-8")
        with pytest.raises(SecretLeakRefused):
            reconcile(
                _request(deployment),
                identity_provider=_provider(_owner(platform_id=secret)),
            )
        assert Path(deployment.state_path).read_text(encoding="utf-8") == before
        assert _journal_lines(deployment) == []
        assert secret not in Path(deployment.state_path).read_text(encoding="utf-8")
        assert deployment.record == record

    def test_no_business_or_tenant_data_enters_the_record(self, tmp_path: Path):
        business_marker = "tenant-42-private-record"
        deployment = _deployment(tmp_path, _record())
        with pytest.raises(ReconciliationDriftDetected):
            reconcile(
                _request(deployment),
                identity_provider=_provider(
                    _owner(
                        configuration=IdentityField.present(
                            {
                                "alpha": {
                                    "platform_id": OBSERVED_INSTANCE["platform_id"],
                                    "business_setting": business_marker,
                                }
                            }
                        )
                    )
                ),
            )
        rendered = Path(deployment.state_path).read_text(encoding="utf-8")
        assert business_marker not in rendered
        assert business_marker not in json.dumps(_journal_lines(deployment))
        assert set(deployment.record.reconciliations[-1].document()) == {
            "reconciliation_id",
            "sequence",
            "outcome",
            "occurred_at",
            "desired",
            "actual",
            "differences",
            "evidence",
            "errors",
        }


# ---------------------------------------------------------------------------
# AC F/G — no remediation, idempotent repetition, fail-closed publication
# ---------------------------------------------------------------------------


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    return called


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


class TestNoRemediation:
    """There is no path from a drift signal to a corrective action."""

    def test_the_module_calls_no_operational_verb(self):
        forbidden = {
            "deploy",
            "rollback",
            "upgrade",
            "stop",
            "start",
            "restart",
            "redeploy",
            "repair",
            "provision",
            "build_elements",
            "materialize",
        }
        assert not (_called_names(MODULE_PATH) & forbidden)

    def test_the_module_imports_no_runtime_or_orchestration_surface(self):
        forbidden = {
            "subprocess",
            "os",
            "shutil",
            "socket",
            "deployment_operations.runtime",
            "deployment_operations.runtime_worker",
            "deployment_operations.provisioning",
            "deployment_operations.health",
            "deployment_operations.upgrade",
            "deployment_operations.rollback",
            "deployment_operations.environment",
        }
        assert not (_imported_modules(MODULE_PATH) & forbidden)
        imported_names = {
            name.name
            for node in ast.walk(ast.parse(MODULE_PATH.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
            for name in node.names
        }
        assert not (
            imported_names
            & {
                "deploy",
                "rollback",
                "upgrade",
                "LocalProcessRuntime",
                "RuntimeAdapter",
                "build_elements",
                "provision",
            }
        )

    def test_repeating_the_operation_has_no_deployment_or_runtime_side_effects(
        self, tmp_path: Path
    ):
        runtime_root = tmp_path / "runtime"
        record = _record()
        deployment = _deployment(runtime_root / "operations", record)
        workspace = runtime_root / "deployments" / record.deployment_id
        (workspace / "components").mkdir(parents=True)
        (workspace / "components" / "runtime.json").write_text("{}\n", encoding="utf-8")
        before = _digests(runtime_root / "deployments")
        operations_before = sorted(
            path.name for path in (runtime_root / "operations").glob("*.json")
        )

        for _ in range(2):
            reconcile(_request(deployment), identity_provider=_provider(_owner()))

        assert _digests(runtime_root / "deployments") == before
        assert (
            sorted(path.name for path in (runtime_root / "operations").glob("*.json"))
            == operations_before
        )
        assert deployment.record.running is True
        assert deployment.record.operational_actions == ()
        assert deployment.deployed is True
        assert len(deployment.record.reconciliations) == 2

    def test_a_failed_state_write_records_and_publishes_nothing(self, tmp_path: Path):
        record = _record()
        deployment = _deployment(tmp_path, record)

        class _FailingStore:
            def write(self, state: object, *, secrets: Sequence[str] = ()) -> None:
                raise DeploymentStateError("deployment state could not be written")

        deployment._store = _FailingStore()  # type: ignore[assignment]
        with pytest.raises(DeploymentStateError):
            reconcile(_request(deployment), identity_provider=_provider(_owner()))
        assert deployment.record == record
        assert _journal_lines(deployment) == []
        assert DeploymentStateStore(deployment.state_path).read() == record

    def test_a_failed_publication_leaves_no_contradictory_state_or_signal(
        self, tmp_path: Path
    ):
        record = _record()
        deployment = _deployment(tmp_path, record)

        class _FailingJournal:
            def next_sequence(self) -> int:
                return 1

            def append(self, event: object, *, secrets: Sequence[str] = ()) -> None:
                raise DeploymentStateError("the operational journal is unwritable")

        deployment._journal = _FailingJournal()  # type: ignore[assignment]
        with pytest.raises(DeploymentStateError):
            reconcile(_request(deployment), identity_provider=_provider(_owner()))

        observation = deployment.record.reconciliations[-1]
        assert observation.outcome == RECONCILIATION_IN_CORRESPONDENCE
        assert DeploymentStateStore(deployment.state_path).read() == deployment.record
        assert _journal_lines(deployment) == []

        deployment._journal = EventJournal(deployment.events_path)
        with pytest.raises(ReconciliationDriftDetected):
            reconcile(
                _request(deployment),
                identity_provider=_provider(_owner(platform_id="another-platform")),
            )
        assert [entry.sequence for entry in deployment.record.reconciliations] == [1, 2]
        signals = _journal_lines(deployment)
        assert [line["event"] for line in signals] == ["reconciliation_drift_detected"]
        assert signals[0]["detail"]["reconciliation_sequence"] == 2
        assert deployment.record.drifted is True


# ---------------------------------------------------------------------------
# The same operation against a genuinely deployed platform
# ---------------------------------------------------------------------------


class _RunningPlatformOwner:
    """Owner-side surface of a Running Platform realized from ``instance``.

    It reports the platform that is actually running — the same composition the
    deployment realized — and can be told to report a divergence or to be
    unavailable, so the slice is exercised against a real deployment rather than
    against a hand-written record.
    """

    def __init__(
        self,
        instance: Any,
        manifest: Mapping[str, Any],
        *,
        component_version: str | None = None,
        platform_id: str | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.instance = instance
        self.manifest = manifest
        self.component_version = component_version
        self.platform_id = platform_id
        self.failure = failure

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        if self.failure is not None:
            raise self.failure
        components = []
        for component in self.manifest.get("components", []):
            artifact = component.get("artifact")
            components.append(
                ActualComponentIdentity(
                    component["component_id"],
                    self.component_version or component["component_version"],
                    artifact if isinstance(artifact, Mapping) else None,
                )
            )
        configuration = self.instance.document.get("configuration")
        return ActualPlatformSnapshot(
            platform_id=self.platform_id or self.instance.document["platform_id"],
            manifest={
                "manifest_id": self.manifest["manifest_id"],
                "manifest_version": self.manifest["manifest_version"],
                "manifest_digest": self.manifest["manifest_digest"],
            },
            manifest_state=self.manifest["lifecycle"]["state"],
            components=tuple(components),
            membership_established=True,
            configuration=(
                IdentityField.present(configuration)
                if configuration is not None
                else IdentityField.absent()
            ),
            golden_bundle=IdentityField.absent(),
            golden_bundle_inventory_established=True,
            extensions=IdentityField.absent(),
            branding=IdentityField.absent(),
            provenance=EvidenceProvenance.MEASURED,
            correlation_token=binding.token,
            freshness_current=True,
        )


@pytest.fixture(scope="module")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="module")
def manifest(root: Path) -> dict:
    return manifest_for(root=root)


@pytest.fixture(scope="module")
def instance(manifest: dict, root: Path) -> Any:
    return instance_for(manifest, root=root)


class TestReconciliationAgainstARealDeployment:
    """A real deployment, observed freshly, diverged, and then unavailable."""

    def test_a_real_platform_is_observed_reconciled_and_brought_to_drift(
        self, tmp_path: Path, instance: Any, manifest: dict
    ) -> None:
        runtime_root = tmp_path / "runtime"
        request = request_for(instance, manifest, environment_for(runtime_root))
        deployment = deployment_module.deploy(
            request,
            identity_provider=OwnerSuppliedPlatformIdentityProvider(
                _RunningPlatformOwner(instance, manifest)
            ),
        )
        try:
            assert deployment.deployed is True

            # -- a fresh, independent observation corresponds ---------------
            reconciled = reconcile(
                _request(deployment),
                identity_provider=OwnerSuppliedPlatformIdentityProvider(
                    _RunningPlatformOwner(instance, manifest)
                ),
            )
            assert reconciled.in_correspondence is True
            assert deployment.record.drifted is False
            assert deployment.deployed is True, (
                "reconciliation confirms or denies correspondence; it never "
                "re-claims or withdraws the operation's own verification"
            )

            # -- drift after a successful deployment ------------------------
            with pytest.raises(ReconciliationDriftDetected) as failure:
                reconcile(
                    _request(deployment),
                    identity_provider=OwnerSuppliedPlatformIdentityProvider(
                        _RunningPlatformOwner(
                            instance, manifest, component_version="9.9.9"
                        )
                    ),
                )
            assert any("component_version" in entry for entry in failure.value.errors)
            assert deployment.record.drifted is True
            assert deployment.record.in_correspondence is False
            assert deployment.record.lifecycle == LIFECYCLE_REALIZED
            assert deployment.deployed is True, "no false claim was manufactured"
            assert deployment.record.operational_actions == ()

            # -- evidence that disappears is fail-closed --------------------
            with pytest.raises(ReconciliationEvidenceUnavailable):
                reconcile(
                    _request(deployment),
                    identity_provider=OwnerSuppliedPlatformIdentityProvider(
                        _RunningPlatformOwner(
                            instance, manifest, failure=RuntimeError("owner offline")
                        )
                    ),
                )
            assert (
                deployment.record.drifted is True
            ), "unverifiable evidence does not erase a proven divergence"
            assert deployment.record.running is True

            # -- every outcome is in the operation's own journal -----------
            names = [event.event for event in deployment.events()]
            assert names[-3:] == [
                "reconciliation_in_correspondence",
                "reconciliation_drift_detected",
                "reconciliation_unverifiable",
            ]
            assert names.count("deployment_realized") == 1
            stored = DeploymentStateStore(deployment.state_path).read()
            assert stored == deployment.record
        finally:
            deployment.stop()
