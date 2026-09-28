from __future__ import annotations

import pytest

from deployment_operations import deployment
from deployment_operations.errors import IdentityVerificationFailed
from deployment_operations.platform_identity import (
    ActualEvidence,
    ActualIdentityUnavailable,
    IdentityCorrespondenceResult,
    PlatformIdentityBinding,
)

EXPECTED = {"instance_digest": "sha256:" + "a" * 64}


class RecordingProvider:
    def __init__(self, result: object = None) -> None:
        self.bindings: list[PlatformIdentityBinding] = []
        self.result = result

    def observe_identity(self, binding: PlatformIdentityBinding) -> ActualEvidence:
        self.bindings.append(binding)
        return self.result  # type: ignore[return-value]


def test_identity_adapter_receives_only_an_opaque_binding(monkeypatch) -> None:
    provider = RecordingProvider()
    observed: dict[str, object] = {}

    def fake_correspondence(expected: object, evidence: object) -> str:
        observed["expected"] = expected
        observed["evidence"] = evidence
        return IdentityCorrespondenceResult.MATCH

    monkeypatch.setattr(
        deployment,
        "establish_identity_correspondence",
        fake_correspondence,
    )

    token = object()
    evidence = object()
    provider.result = evidence

    deployment._verify_running_platform_identity(
        provider,
        EXPECTED,
        binding_token=token,
    )

    assert provider.bindings == [PlatformIdentityBinding(token)]
    assert provider.bindings[0].token is token
    assert provider.bindings[0].token is not EXPECTED["instance_digest"]
    assert observed["expected"] is EXPECTED
    assert observed["evidence"] is evidence


def test_identity_adapter_does_not_forward_expected_digest_in_binding(
    monkeypatch,
) -> None:
    class HostileProvider:
        def __init__(self) -> None:
            self.received: PlatformIdentityBinding | None = None

        def observe_identity(
            self, binding: PlatformIdentityBinding
        ) -> ActualEvidence:
            self.received = binding
            if binding.token == EXPECTED["instance_digest"]:
                raise AssertionError("expected identity leaked into the binding")
            return object()  # type: ignore[return-value]

    provider = HostileProvider()
    monkeypatch.setattr(
        deployment,
        "establish_identity_correspondence",
        lambda expected, evidence: IdentityCorrespondenceResult.MATCH,
    )

    deployment._verify_running_platform_identity(
        provider,
        EXPECTED,
        binding_token="opaque-deployment-evaluation",
    )

    assert provider.received is not None
    assert provider.received.token == "opaque-deployment-evaluation"


def (
    test_identity_adapter_converts_unavailable_evidence_to_deployment_failure
) -> None:
    class UnavailableProvider:
        def observe_identity(
            self, binding: PlatformIdentityBinding
        ) -> ActualEvidence:
            raise ActualIdentityUnavailable("source is unavailable")

    with pytest.raises(
        IdentityVerificationFailed, match="evidence is unavailable"
    ):
        deployment._verify_running_platform_identity(
            UnavailableProvider(),
            EXPECTED,
            binding_token=object(),
        )


def test_identity_adapter_converts_provider_crash_to_deployment_failure() -> None:
    class BrokenProvider:
        def observe_identity(
            self, binding: PlatformIdentityBinding
        ) -> ActualEvidence:
            raise RuntimeError("unexpected owner-side failure")

    with pytest.raises(IdentityVerificationFailed, match="failed closed"):
        deployment._verify_running_platform_identity(
            BrokenProvider(),
            EXPECTED,
            binding_token=object(),
        )


@pytest.mark.parametrize(
    "result",
    [
        IdentityCorrespondenceResult.MISMATCH,
        IdentityCorrespondenceResult.UNAVAILABLE,
        "unexpected-result",
    ],
)
def test_identity_adapter_never_treats_non_match_as_success(result: str) -> None:
    provider = RecordingProvider(object())
    original = deployment.establish_identity_correspondence
    try:
        deployment.establish_identity_correspondence = (
            lambda expected, evidence: result
        )
        with pytest.raises(IdentityVerificationFailed):
            deployment._verify_running_platform_identity(
                provider,
                EXPECTED,
                binding_token=object(),
            )
    finally:
        deployment.establish_identity_correspondence = original
