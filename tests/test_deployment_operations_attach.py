"""F-1 — re-binding an already-standing Running Platform: ``attach``.

The Running Platform outlives the deployment operation that realized it, so a
replaced Deployment & Operations process must be able to restore its
**reference** to the runtime elements it realized — while creating, starting,
stopping, restarting and migrating nothing, and inventing no claim
(ADR-0016 §18; the approved Level B F-1 slice).

The tests model the two owners explicitly:

* :class:`StandingLayerR` is the runtime layer. It supervises the elements it
  started and keeps its own registry of them; it outlives any Layer O process.
* :class:`LayerOSeam` is one Deployment & Operations process's view of the seam.
  A *replaced* process gets a fresh seam over the same standing layer, and every
  call it makes is recorded, so "attach touches nothing else" is provable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _deployment_helpers import (
    environment_for,
    instance_for,
    manifest_for,
    request_for,
    tampered_instance,
)

from deployment_operations import (
    RESTART_COMPLETED,
    Deployment,
    DeploymentRequest,
    DeploymentStateStore,
    EventJournal,
    InstanceReference,
    OwnerSuppliedPlatformIdentityProvider,
    attach,
    deploy,
    load_record,
    restart,
    verify_instance,
)
from deployment_operations.errors import (
    DeploymentInputRejected,
    DeploymentStateError,
    IdentityVerificationFailed,
    InvalidDeploymentStateTransition,
)
from deployment_operations.events import EVENT_PLATFORM_ATTACHED
from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualIdentityUnavailable,
    EvidenceCorrelation,
    IdentityField,
    PlatformIdentityBinding,
)
from deployment_operations.platform_identity_source import ActualPlatformSnapshot
from deployment_operations.restart import RestartRequest, derive_restart_id
from deployment_operations.runtime import (
    LocalProcessRuntime,
    RuntimeHandle,
    RuntimeProcessError,
)
from deployment_operations.state import LIFECYCLE_REALIZED
from platform_instance import Instance, discover_root

COMPONENT_ID = "tenant_authority"


# ---------------------------------------------------------------------------
# Fixtures: one genuinely accepted Platform Instance, through the real Factory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root() -> Path:
    return discover_root()


@pytest.fixture(scope="module")
def manifest(root: Path) -> dict[str, Any]:
    return manifest_for(root=root)


@pytest.fixture(scope="module")
def instance(manifest: dict[str, Any], root: Path) -> Instance:
    return instance_for(manifest, root=root)


# ---------------------------------------------------------------------------
# The actual identity source of the platform that is actually running
# ---------------------------------------------------------------------------


class CanonicalSource:
    """The owner-side source of the canonical platform's actual identity.

    It answers for the binding it was given and states, as its own facts, the
    platform identity it observed, the binding space of this request's
    environment and this attempt's position in it. The expected Platform
    Instance is never part of that statement.
    """

    def __init__(
        self,
        request: Any,
        *,
        platform_id: str | None = None,
        freshness_current: bool = True,
        correlation_handle: object | None = None,
        correlation_target: str | None = None,
    ) -> None:
        self.request = request
        self._platform_id = platform_id
        self._freshness_current = freshness_current
        self._correlation_handle = correlation_handle
        self._correlation_target = correlation_target

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        components = []
        for component in self.request.manifest_document.get("components", []):
            artifact = component.get("artifact")
            components.append(
                ActualComponentIdentity(
                    component["component_id"],
                    component["component_version"],
                    artifact if isinstance(artifact, Mapping) else None,
                )
            )
        configuration = self.request.instance_document.get("configuration")
        declared = (
            self._platform_id
            if self._platform_id is not None
            else self.request.instance_document["platform_id"]
        )
        token = (
            binding.token
            if self._correlation_handle is None
            else self._correlation_handle
        )
        observed = self._correlation_target or declared
        correlation = EvidenceCorrelation(
            token=token,
            scope=self.request.environment.environment_id,
            sequence=self.request.attempt,
            target=str(observed),
            authority="tests/owner-side-identity-source",
            basis="tests/owner-observation/1",
            established_at="2026-09-16T00:00:00Z",
        )
        return ActualPlatformSnapshot(
            platform_id=declared,
            manifest={
                "manifest_id": self.request.manifest_document["manifest_id"],
                "manifest_version": self.request.manifest_document["manifest_version"],
                "manifest_digest": self.request.manifest_document["manifest_digest"],
            },
            manifest_state=self.request.manifest_document["lifecycle"]["state"],
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
            provenance="MEASURED",
            correlation=correlation,
            freshness_current=self._freshness_current,
        )


def provider_for(request: Any, **source: Any) -> Any:
    """The existing S4 seam over the owner-side source."""
    return OwnerSuppliedPlatformIdentityProvider(CanonicalSource(request, **source))


class CorrelationEnforcingProvider:
    """The owner-side correlation rule the S4 seam mandates.

    The D&O side deliberately performs no in-band comparison of the evidence
    correlation against the evaluation binding, so the owner-side adapter is the
    place a foreign correlation is stopped. This is that adapter, written only
    with the published contract.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def observe_identity(self, binding: PlatformIdentityBinding) -> Any:
        evidence = self._inner.observe_identity(binding)
        if not evidence.correlation.echoes(binding.token):
            raise ActualIdentityUnavailable(
                "the actual evidence is correlated to another evaluation"
            )
        return evidence


# ---------------------------------------------------------------------------
# Layer R and Layer O's view of the seam
# ---------------------------------------------------------------------------


class StandingLayerR:
    """The standing runtime layer: it supervises the elements it started.

    Its registry is its own state, not the deployment operation's, which is
    exactly why a replaced D&O process can be re-bound to it.
    """

    def __init__(self) -> None:
        self._inner = LocalProcessRuntime()
        self._elements: dict[str, Any] = {}

    def supervised(self) -> tuple[str, ...]:
        return tuple(sorted(self._elements))

    def handle_of(self, component_id: str) -> Any:
        return self._elements[component_id]

    def retire(self, component_id: str) -> None:
        """Layer R's own decision: the element is no longer supervised."""
        self._elements.pop(component_id, None)

    def materialize(self, element: Any) -> Any:
        return self._inner.materialize(element)

    def migrate(self, element: Any) -> Any:
        return self._inner.migrate(element)

    def start(self, element: Any) -> Any:
        handle = self._inner.start(element)
        self._elements[element.component.component_id] = handle
        return handle

    def request(self, handle: Any, operation: str, *, timeout: float | None = None):
        return self._inner.request(handle, operation, timeout=timeout)

    def stop(self, handle: Any) -> Any:
        self._elements.pop(handle.component_id, None)
        return self._inner.stop(handle)

    def attach(self, element: Any) -> Any:
        handle = self._elements.get(element.component.component_id)
        if handle is None:
            raise RuntimeProcessError(
                f"{element.component.component_id}: this runtime layer "
                "supervises no such element; attach binds an existing runtime "
                "and creates none"
            )
        return handle


class LayerOSeam:
    """One D&O process's view of the seam, recording every call it makes."""

    def __init__(self, layer_r: StandingLayerR) -> None:
        self._layer_r = layer_r
        self.calls: list[str] = []

    def _component(self, element: Any) -> str:
        return element.component.component_id

    def materialize(self, element: Any) -> Any:
        self.calls.append(f"materialize:{self._component(element)}")
        return self._layer_r.materialize(element)

    def migrate(self, element: Any) -> Any:
        self.calls.append(f"migrate:{self._component(element)}")
        return self._layer_r.migrate(element)

    def start(self, element: Any) -> Any:
        self.calls.append(f"start:{self._component(element)}")
        return self._layer_r.start(element)

    def request(self, handle: Any, operation: str, *, timeout: float | None = None):
        self.calls.append(f"request:{operation}")
        return self._layer_r.request(handle, operation, timeout=timeout)

    def stop(self, handle: Any) -> Any:
        self.calls.append(f"stop:{handle.component_id}")
        return self._layer_r.stop(handle)

    def attach(self, element: Any) -> Any:
        self.calls.append(f"attach:{self._component(element)}")
        return self._layer_r.attach(element)


@pytest.fixture()
def standing_platform(tmp_path: Path, instance: Instance, manifest: dict[str, Any]):
    """One realized deployment over a standing Layer R, and its own request."""
    layer_r = StandingLayerR()
    environment = environment_for(tmp_path / "runtime")
    request = request_for(instance, manifest, environment)
    seam = LayerOSeam(layer_r)
    deployment = deploy(request, runtime=seam, identity_provider=provider_for(request))
    assert deployment.deployed is True
    yield layer_r, environment, request, deployment
    for component_id in layer_r.supervised():
        layer_r.stop(layer_r.handle_of(component_id))


# ---------------------------------------------------------------------------
# attach binds an already-standing runtime — and nothing else
# ---------------------------------------------------------------------------


def test_attach_binds_the_already_standing_runtime(standing_platform) -> None:
    layer_r, _environment, request, deployed = standing_platform
    before = layer_r.handle_of(COMPONENT_ID)
    assert before.process.poll() is None

    fresh = LayerOSeam(layer_r)  # a replaced D&O process
    attached = attach(request, runtime=fresh, identity_provider=provider_for(request))

    assert attached.record.deployment_id == deployed.record.deployment_id
    assert attached.record.lifecycle == LIFECYCLE_REALIZED
    assert attached.deployed is True
    assert attached._handles == (before,)
    assert fresh.calls == [f"attach:{COMPONENT_ID}"]

    actions = [action.name for action in attached.record.operational_actions]
    assert "platform_attached" in actions
    persisted = load_record(attached.state_path)
    assert "platform_attached" in [a.name for a in persisted.operational_actions]
    assert persisted.lifecycle == LIFECYCLE_REALIZED
    assert persisted.running is True
    assert [event.event for event in attached.events()][-1] == EVENT_PLATFORM_ATTACHED
    attached.stop()


def test_attach_creates_starts_stops_and_migrates_nothing(standing_platform) -> None:
    layer_r, _environment, request, _deployed = standing_platform
    before = layer_r.handle_of(COMPONENT_ID)
    supervised_before = layer_r.supervised()

    fresh = LayerOSeam(layer_r)
    attached = attach(request, runtime=fresh, identity_provider=provider_for(request))

    assert [call for call in fresh.calls if not call.startswith("attach:")] == []
    assert layer_r.supervised() == supervised_before
    assert layer_r.handle_of(COMPONENT_ID) is before
    assert before.process.poll() is None
    assert attached.record.running is True
    attached.stop()


def test_attach_refuses_when_the_layer_supervises_no_element(standing_platform) -> None:
    layer_r, _environment, request, _deployed = standing_platform
    layer_r.retire(COMPONENT_ID)

    fresh = LayerOSeam(layer_r)
    with pytest.raises(RuntimeProcessError, match="supervises no such element"):
        attach(request, runtime=fresh, identity_provider=provider_for(request))
    assert fresh.calls == [f"attach:{COMPONENT_ID}"]


def test_attach_refuses_a_record_that_claims_no_running_platform(
    standing_platform,
) -> None:
    layer_r, _environment, request, deployed = standing_platform
    deployed.stop()  # an honest, in-process stop: the operation is bound

    fresh = LayerOSeam(layer_r)
    with pytest.raises(InvalidDeploymentStateTransition) as excinfo:
        attach(request, runtime=fresh, identity_provider=provider_for(request))
    assert "claims no running platform" in excinfo.value.diagnostics()
    assert fresh.calls == []


def test_attach_refuses_when_there_is_no_authoritative_record(
    tmp_path: Path, instance: Instance, manifest: dict[str, Any]
) -> None:
    environment = environment_for(tmp_path / "runtime")
    request = request_for(instance, manifest, environment)
    fresh = LayerOSeam(StandingLayerR())
    with pytest.raises(
        DeploymentStateError, match="no authoritative deployment record"
    ):
        attach(request, runtime=fresh, identity_provider=provider_for(request))
    assert fresh.calls == []


def test_attach_refuses_an_element_of_another_deployment(standing_platform) -> None:
    """Layer R must not be able to hand over another operation's runtime."""
    layer_r, _environment, request, deployed = standing_platform
    original = layer_r.handle_of(COMPONENT_ID)

    class CrossBound(LayerOSeam):
        def attach(self, element: Any) -> Any:
            self.calls.append(f"attach:{element.component.component_id}")
            foreign = replace(original.element, deployment_id="another-deployment")
            return RuntimeHandle(element=foreign, process=original.process)

    seam = CrossBound(layer_r)
    with pytest.raises(InvalidDeploymentStateTransition) as excinfo:
        attach(request, runtime=seam, identity_provider=provider_for(request))
    assert "an element of deployment" in excinfo.value.diagnostics()
    assert load_record(deployed.state_path).running is True
    deployed.stop()


def test_attach_refuses_an_adapter_without_the_attach_operation(
    standing_platform,
) -> None:
    layer_r, _environment, request, _deployed = standing_platform

    class NoAttach:
        def materialize(self, element: Any) -> Any:
            return layer_r.materialize(element)

    with pytest.raises(DeploymentInputRejected) as excinfo:
        attach(
            request,
            runtime=NoAttach(),  # type: ignore[arg-type]
            identity_provider=provider_for(request),
        )
    assert "provides no attach operation" in excinfo.value.diagnostics()


# ---------------------------------------------------------------------------
# attach re-verifies: identity, correlation and the authoritative record
# ---------------------------------------------------------------------------


def test_attach_refuses_an_actual_identity_that_does_not_match(
    standing_platform,
) -> None:
    layer_r, _environment, request, deployed = standing_platform
    fresh = LayerOSeam(layer_r)
    drifted = provider_for(request, platform_id="another-platform")
    with pytest.raises(IdentityVerificationFailed):
        attach(request, runtime=fresh, identity_provider=drifted)
    # A refused attach changed neither the record nor the runtime.
    assert "platform_attached" not in [
        action.name for action in load_record(deployed.state_path).operational_actions
    ]
    assert layer_r.handle_of(COMPONENT_ID).process.poll() is None
    deployed.stop()


def test_attach_refuses_stale_actual_evidence(standing_platform) -> None:
    layer_r, _environment, request, deployed = standing_platform
    fresh = LayerOSeam(layer_r)
    stale = provider_for(request, freshness_current=False)
    with pytest.raises(IdentityVerificationFailed) as excinfo:
        attach(request, runtime=fresh, identity_provider=stale)
    assert "identity evidence is stale" in excinfo.value.diagnostics()
    assert layer_r.handle_of(COMPONENT_ID).process.poll() is None
    deployed.stop()


def test_attach_refuses_a_foreign_correlation(standing_platform) -> None:
    layer_r, _environment, request, deployed = standing_platform
    fresh = LayerOSeam(layer_r)
    foreign = CorrelationEnforcingProvider(
        provider_for(request, correlation_handle="a-token-this-evaluation-never-issued")
    )
    with pytest.raises(IdentityVerificationFailed) as excinfo:
        attach(request, runtime=fresh, identity_provider=foreign)
    assert "another evaluation" in excinfo.value.diagnostics()
    assert "platform_attached" not in [
        action.name for action in load_record(deployed.state_path).operational_actions
    ]
    deployed.stop()


def test_attach_refuses_a_tampered_authoritative_record(standing_platform) -> None:
    layer_r, _environment, request, deployed = standing_platform
    document = json.loads(deployed.state_path.read_text(encoding="utf-8"))
    document["platform_instance"]["instance_digest"] = "sha256:" + "b" * 64
    deployed.state_path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    fresh = LayerOSeam(layer_r)
    with pytest.raises(InvalidDeploymentStateTransition) as excinfo:
        attach(request, runtime=fresh, identity_provider=provider_for(request))
    diagnostics = excinfo.value.diagnostics()
    assert "does not name itself" in diagnostics
    assert "altered after it was written" in diagnostics
    assert fresh.calls == []


def test_attach_refuses_a_record_pinning_another_component_version(
    standing_platform,
) -> None:
    layer_r, _environment, request, deployed = standing_platform
    document = json.loads(deployed.state_path.read_text(encoding="utf-8"))
    document["components"][0]["component_version"] = "9.9.9"
    deployed.state_path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    fresh = LayerOSeam(layer_r)
    with pytest.raises(InvalidDeploymentStateTransition) as excinfo:
        attach(request, runtime=fresh, identity_provider=provider_for(request))
    assert "the record pins version" in excinfo.value.diagnostics()
    assert fresh.calls == []


def test_attach_refuses_another_instance_than_the_one_realized(
    standing_platform, manifest: dict[str, Any], instance: Instance
) -> None:
    """A digest-level mismatch: another instance names another operation."""
    layer_r, environment, _request, deployed = standing_platform
    other = tampered_instance(instance, platform_id="another-platform")
    request = DeploymentRequest(
        instance=InstanceReference.from_document(other),
        instance_document=other,
        manifest_document=manifest,
        environment=environment,
    )

    fresh = LayerOSeam(layer_r)
    with pytest.raises(
        DeploymentStateError, match="no authoritative deployment record"
    ):
        attach(request, runtime=fresh, identity_provider=provider_for(request))
    assert fresh.calls == []
    assert load_record(deployed.state_path).running is True
    deployed.stop()


# ---------------------------------------------------------------------------
# The cold stop guard
# ---------------------------------------------------------------------------


def test_cold_stop_fails_closed_and_changes_nothing(standing_platform) -> None:
    layer_r, environment, request, deployed = standing_platform
    state_path = deployed.state_path
    persisted_before = load_record(state_path)
    assert persisted_before.running is True

    # Exactly what a replaced D&O process can rebuild from disk: the record,
    # the verification, the store and the journal — and no runtime element.
    cold = Deployment(
        record=persisted_before,
        verification=verify_instance(
            request.instance_document,
            request.manifest_document,
            environment_id=environment.environment_id,
        ),
        state_path=state_path,
        events_path=EventJournal.path_for(
            environment.operations_dir, persisted_before.deployment_id
        ),
        _runtime=LayerOSeam(layer_r),
        _handles=(),
        _store=DeploymentStateStore(state_path),
    )
    with pytest.raises(DeploymentStateError, match="holds no runtime element"):
        cold.stop()

    after = load_record(state_path)
    assert after.running is True
    assert after.deployed is True
    assert after.updated_at == persisted_before.updated_at
    assert layer_r.handle_of(COMPONENT_ID).process.poll() is None
    deployed.stop()


def test_cold_stop_stays_idempotent_for_an_already_stopped_record(
    standing_platform,
) -> None:
    _layer_r, environment, request, deployed = standing_platform
    deployed.stop()
    cold = Deployment(
        record=load_record(deployed.state_path),
        verification=verify_instance(
            request.instance_document,
            request.manifest_document,
            environment_id=environment.environment_id,
        ),
        state_path=deployed.state_path,
        events_path=deployed.events_path,
        _handles=(),
        _store=DeploymentStateStore(deployed.state_path),
    )
    # Nothing is running and the operation reaches nothing: there is no false
    # claim to make, so stopping again records nothing twice.
    assert cold.stop().running is False


# ---------------------------------------------------------------------------
# A re-bound operation is a working operation
# ---------------------------------------------------------------------------


def test_restart_after_attach_uses_the_restored_handle(standing_platform) -> None:
    layer_r, _environment, request, _deployed = standing_platform
    fresh = LayerOSeam(layer_r)
    attached = attach(request, runtime=fresh, identity_provider=provider_for(request))
    assert fresh.calls == [f"attach:{COMPONENT_ID}"]

    fresh.calls.clear()
    result = restart(
        RestartRequest(deployment=attached, restart_id=derive_restart_id(attached)),
        identity_provider=provider_for(request),
    )
    assert result.outcome == RESTART_COMPLETED
    assert result.completed is True
    assert result.record.deployed is True
    # The restart — not the attach — is what touched the runtime.
    assert any(call.startswith("stop:") for call in fresh.calls)
    assert any(call.startswith("start:") for call in fresh.calls)
    assert not any(call.startswith("attach:") for call in fresh.calls)
    result.deployment.stop()
