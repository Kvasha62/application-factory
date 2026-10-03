"""Helpers for the rehearsal tests. REHEARSAL / NOT PRODUCTION.

``install_world`` stands in for the deployment that realized a platform: it is
the one place where a Platform Instance and the producer's runtime root meet,
and it runs before the producer is ever asked. It is test scaffolding, not part
of the producer or the adapter, and it is why a rehearsal cannot show that
actual and expected identity are independent of each other. What it can show is
how information flows across the adapter -> producer boundary.

The expected side is built through the real Composer, Platform Manifest and
Platform Instance surfaces, like every other Deployment & Operations test.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from _deployment_helpers import (
    COMPONENT_ID,
    COMPONENT_VERSION,
    PLATFORM_ID,
    ROOT,
    validated,
)

import deployment_operations
from composer import compose_request_document
from composer.request import build_request_document
from deployment_operations import DeploymentRequest, derive_deployment_id
from deployment_operations.platform_identity import (
    PlatformIdentityBinding,
    establish_identity_correspondence,
)
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from platform_manifest import validate_document
from running_platform_rehearsal.adapter import (
    BoundaryRequest,
    BoundaryResponse,
    BoundaryTimeout,
    RehearsalAdapter,
    Transport,
    run_producer_process,
)


def rich_manifest(root: Path = ROOT) -> dict[str, Any]:
    """A composed manifest whose instance carries configuration and branding."""
    request = build_request_document(
        manifest_id=PLATFORM_ID,
        manifest_version="1.0.0",
        components=[
            {"component_id": COMPONENT_ID, "component_version": COMPONENT_VERSION}
        ],
        configuration={COMPONENT_ID: {"platform_id": PLATFORM_ID}},
        branding={"display_name": "Rehearsal"},
    )
    document = validated(compose_request_document(request, root=root).document)
    assert validate_document(document, root=root) == []
    return document


# ---------------------------------------------------------------------------
# The rehearsal world: what the producer observes
# ---------------------------------------------------------------------------


def _stated(value: Any) -> dict[str, Any]:
    if value is None:
        return {"state": "ABSENT"}
    return {"state": "PRESENT", "value": copy.deepcopy(value)}


def _write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def install_world(root: Path, instance_document: Mapping[str, Any]) -> Path:
    """Realize ``instance_document`` as a producer-owned runtime root."""
    _write(
        root / "platform.json",
        {
            "platform_id": instance_document["platform_id"],
            "manifest": dict(instance_document["manifest"]),
            "manifest_state": instance_document["manifest_state"],
            "configuration": _stated(instance_document.get("configuration")),
            "branding": _stated(instance_document.get("branding")),
            "extensions": _stated(instance_document.get("extensions")),
            "golden_bundle": {
                **_stated(instance_document["golden_bundle"]),
                "inventory_complete": True,
            },
        },
    )
    for component in instance_document["components"]:
        artifact = component["artifact"]
        _write(
            root / "components" / component["component_id"] / "component.json",
            {
                "component_id": component["component_id"],
                "component_version": component["component_version"],
                "artifact": (
                    {"state": "ABSENT"}
                    if artifact["artifact_type"] == "none"
                    else _stated(dict(artifact))
                ),
            },
        )
    return root


def rewrite(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    _write(path, document)


def _component_path(root: Path) -> Path:
    return root / "components" / COMPONENT_ID / "component.json"


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(document: dict[str, Any]) -> None:
        document[key] = value

    return mutate


def drift_platform_id(root: Path) -> None:
    rewrite(root / "platform.json", _set("platform_id", "other-platform"))


def drift_membership(root: Path) -> None:
    (root / "components" / "unidentified").mkdir()


def drift_component_identity(root: Path) -> None:
    rewrite(_component_path(root), _set("component_version", "0.1.1"))


def drift_artifact_identity(root: Path) -> None:
    sealed = {
        "artifact_type": "source_package",
        "digest": "sha256:" + "ab" * 32,
        "pinned": True,
        "canonical_form": "source_package/v1",
    }
    rewrite(_component_path(root), _set("artifact", _stated(sealed)))


def drift_manifest(root: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        document["manifest"]["manifest_digest"] = "sha256:" + "cd" * 32

    rewrite(root / "platform.json", mutate)


def drift_configuration(root: Path) -> None:
    changed = _stated({COMPONENT_ID: {"platform_id": PLATFORM_ID, "drift": True}})
    rewrite(root / "platform.json", _set("configuration", changed))


def drift_golden_bundle(root: Path) -> None:
    incomplete = {**_stated(None), "inventory_complete": False}
    rewrite(root / "platform.json", _set("golden_bundle", incomplete))


def drift_branding(root: Path) -> None:
    rewrite(root / "platform.json", _set("branding", _stated({"display_name": "X"})))


def drift_extensions(root: Path) -> None:
    changed = _stated([{"extension_id": "rehearsal-extension"}])
    rewrite(root / "platform.json", _set("extensions", changed))


#: One mutation of the realized platform per identity surface.
DRIFTS: Mapping[str, Callable[[Path], None]] = {
    "platform_id": drift_platform_id,
    "membership": drift_membership,
    "component_identity": drift_component_identity,
    "artifact_identity": drift_artifact_identity,
    "manifest": drift_manifest,
    "configuration": drift_configuration,
    "golden_bundle": drift_golden_bundle,
    "branding": drift_branding,
    "extensions": drift_extensions,
}


# ---------------------------------------------------------------------------
# The boundary: record it completely, check it independently of the adapter
# ---------------------------------------------------------------------------


class RecordingTransport:
    """Wrap a transport and record every request and response that crosses."""

    def __init__(self, inner: Transport = run_producer_process) -> None:
        self._inner = inner
        self.requests: list[BoundaryRequest] = []
        self.responses: list[BoundaryResponse] = []

    def __call__(self, request: BoundaryRequest) -> BoundaryResponse:
        self.requests.append(request)
        response = self._inner(request)
        self.responses.append(response)
        return response


def producer_argv(root: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-P",
        "-m",
        "running_platform_rehearsal.producer",
        "--root",
        str(root),
    )


def counter_handles() -> Callable[[], str]:
    """Deterministic, unrelated-to-anything handles: 32 hex digits each."""
    state = {"next": 0}

    def issue() -> str:
        state["next"] += 1
        return f"{state['next']:032x}"

    return issue


def forbidden_values(
    instance_document: Mapping[str, Any],
    runtime_root: Path,
    environment_id: str,
    token: str,
) -> dict[str, str]:
    """Expected-state material that must never reach the producer."""
    digest = str(instance_document["instance_digest"])
    return {
        "binding token": token,
        "platform_id": str(instance_document["platform_id"]),
        "digest fragment": digest.removeprefix("sha256:")[:12],
        "full digest": digest,
        "environment_id": environment_id,
        "runtime root of the deployment": str(runtime_root),
        "manifest digest": str(instance_document["manifest"]["manifest_digest"]),
        "manifest id": str(instance_document["manifest"]["manifest_id"]),
    }


def _outbound(request: BoundaryRequest) -> bytes:
    environment = (f"{key}={value}" for key, value in request.env.items())
    text = "\0".join([*request.argv, request.cwd, *environment])
    return text.encode() + b"\0" + request.stdin


def boundary_violations(
    requests: Iterable[BoundaryRequest],
    responses: Iterable[BoundaryResponse],
    forbidden: Mapping[str, str],
    producer_root: Path,
) -> list[str]:
    """Blacklist every expected value and whitelist the whole request."""
    violations: list[str] = []
    for request in requests:
        blob = _outbound(request)
        violations.extend(
            f"{name} crossed the boundary"
            for name, value in forbidden.items()
            if value.encode() in blob
        )
        try:
            body = json.loads(request.stdin)
        except json.JSONDecodeError:
            body = None
        if not (
            isinstance(body, dict)
            and set(body) == {"op", "handle"}
            and body["op"] == "observe"
            and re.fullmatch(r"[0-9a-f]{32}", str(body["handle"]))
        ):
            violations.append("the request is not exactly op and a 32-hex handle")
        if request.argv != producer_argv(producer_root):
            violations.append("argv is not the fixed producer command")
        if set(request.env) - {"PYTHONPATH", "SystemRoot"}:
            violations.append("the environment carries more than an import path")
    # A producer that never held expected state cannot echo it.
    echoed = ("binding token", "digest fragment", "full digest")
    for response in responses:
        violations.extend(
            f"{name} came back from the producer"
            for name in echoed
            if forbidden[name].encode() in response.stdout + response.stderr
        )
    return violations


# ---------------------------------------------------------------------------
# Fault injection at the boundary (the producer itself has no fault modes)
# ---------------------------------------------------------------------------


def stalled(inner: Transport = run_producer_process) -> Transport:
    """Replace the producer with a process that never answers."""

    def transport(request: BoundaryRequest) -> BoundaryResponse:
        sleeper = (sys.executable, "-c", "import time; time.sleep(60)")
        return inner(
            BoundaryRequest(
                sleeper, request.env, request.cwd, request.stdin, request.timeout
            )
        )

    return transport


def rewritten(
    change: Callable[[dict[str, Any]], None], inner: Transport = run_producer_process
) -> Transport:
    """Let the real producer answer, then alter its answer."""

    def transport(request: BoundaryRequest) -> BoundaryResponse:
        response = inner(request)
        document = json.loads(response.stdout)
        change(document)
        return BoundaryResponse(
            json.dumps(document).encode(), response.stderr, response.returncode
        )

    return transport


def replaying(inner: Transport = run_producer_process) -> Transport:
    """Answer the first request for real and every later one with that answer."""
    first: list[BoundaryResponse] = []

    def transport(request: BoundaryRequest) -> BoundaryResponse:
        if not first:
            first.append(inner(request))
        return first[0]

    return transport


def raising_timeout(request: BoundaryRequest) -> BoundaryResponse:
    message = "no answer"
    raise BoundaryTimeout(message)


# ---------------------------------------------------------------------------
# Composition roots of the tests
# ---------------------------------------------------------------------------


def provider_for(adapter: RehearsalAdapter) -> OwnerSuppliedPlatformIdentityProvider:
    """The existing consumer-side wrapper, unchanged, around the adapter."""
    return OwnerSuppliedPlatformIdentityProvider(adapter)


def deploy_with_rehearsal(request: DeploymentRequest, adapter: RehearsalAdapter):
    """The one composition root: the real ``deploy`` with the adapter injected."""
    return deployment_operations.deploy(
        request, identity_provider=provider_for(adapter)
    )


def token_for(
    instance_document: Mapping[str, Any], environment_id: str, attempt: int = 1
) -> str:
    """The binding token ``deploy`` hands to the provider for this request."""
    return derive_deployment_id(
        instance_document.get("platform_id"),
        instance_document.get("instance_digest"),
        environment_id,
        attempt,
    )


def correspondence(
    instance_document: Mapping[str, Any], adapter: RehearsalAdapter, token: str
) -> str:
    """The acceptance seam, exercised through its public functions."""
    evidence = provider_for(adapter).observe_identity(PlatformIdentityBinding(token))
    return establish_identity_correspondence(instance_document, evidence)
