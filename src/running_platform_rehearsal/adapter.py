"""Owner-side adapter for the rehearsal producer: an in-band S4 source.

REHEARSAL / NOT PRODUCTION. This is the in-process collector of the approved
M1-b / AG-2 decision (PR #138 comment 5965884785). It is not the evidence
producer. It implements ``RunningPlatformIdentitySource.observe`` and is meant
to be wrapped, unchanged, by the existing ``OwnerSuppliedPlatformIdentityProvider``;
the D&O acceptance seam is not touched.

What the adapter owns, because Deployment & Operations does not check it
in-band:

* **The evaluation handle.** It is created here, is unrelated to the binding
  and to any expected input, and is the only per-evaluation input the producer
  receives.
* **Correlation.** The evidence carries the binding token only because the
  adapter sets it, after the producer's answer has been tied to this handle.
* **Foreign and stale evidence.** An answer for an unknown handle is foreign;
  an answer for an earlier handle is stale. Both are refused.
* **The bounded wait.** The producer has a hard deadline; an answer that comes
  after it, or never, is refused. A timeout never yields acceptance.
* **Provenance.** The snapshot can carry one class. Surfaces that report
  different classes are refused rather than flattened.

Everything that crosses the adapter -> producer boundary passes through one
transport callable, so a test can record it completely.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualIdentityUnavailable,
    EvidenceProvenance,
    IdentityField,
    PlatformIdentityBinding,
)
from deployment_operations.platform_identity_source import ActualPlatformSnapshot
from running_platform_rehearsal import REHEARSAL_LABEL
from running_platform_rehearsal.producer import PROTOCOL, SURFACES

#: A rehearsal default only. The production bound is an owner input (RF-4).
DEFAULT_TIMEOUT_SECONDS = 5.0

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_NORMATIVE = frozenset(
    {
        EvidenceProvenance.MEASURED,
        EvidenceProvenance.TRANSITIVE,
        EvidenceProvenance.ATTESTED,
    }
)
_NO_ARTIFACT: Mapping[str, Any] = {
    "artifact_type": "none",
    "digest": None,
    "pinned": False,
    "canonical_form": None,
}


@dataclass(frozen=True)
class BoundaryRequest:
    """Everything that crosses the adapter -> producer boundary (B2)."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    stdin: bytes
    timeout: float


@dataclass(frozen=True)
class BoundaryResponse:
    """Everything that comes back across the boundary."""

    stdout: bytes
    stderr: bytes
    returncode: int


class BoundaryTimeout(Exception):
    """The producer did not answer within the deadline."""


Transport = Callable[[BoundaryRequest], BoundaryResponse]


def run_producer_process(request: BoundaryRequest) -> BoundaryResponse:
    """The default transport: one producer process, one request, a hard deadline.

    ``subprocess.run`` kills the process when the deadline passes, so a stalled
    producer cannot outlive the evaluation.
    """
    try:
        completed = subprocess.run(
            list(request.argv),
            input=request.stdin,
            capture_output=True,
            timeout=request.timeout,
            env=dict(request.env),
            cwd=request.cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        message = "the producer process did not answer in time"
        raise BoundaryTimeout(message) from error
    return BoundaryResponse(completed.stdout, completed.stderr, completed.returncode)


def _unavailable(message: str) -> ActualIdentityUnavailable:
    return ActualIdentityUnavailable(message)


def _random_handle() -> str:
    return secrets.token_hex(16)


def _child_environment() -> dict[str, str]:
    """The producer's whole environment: an import path and nothing else."""
    environment = {"PYTHONPATH": str(_SOURCE_ROOT)}
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
    if system_root:
        environment["SystemRoot"] = system_root
    return environment


def _decode(response: BoundaryResponse) -> dict[str, Any]:
    if response.returncode != 0:
        raise _unavailable("the rehearsal producer exited abnormally")
    try:
        observation = json.loads(response.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise _unavailable("the rehearsal producer answered invalid JSON") from error
    if not isinstance(observation, dict) or observation.get("protocol") != PROTOCOL:
        raise _unavailable("the rehearsal producer answered an unknown protocol")
    if observation.get("rehearsal") is not True:
        raise _unavailable("the evidence is not marked as rehearsal evidence")
    if observation.get("label") != REHEARSAL_LABEL:
        raise _unavailable("the evidence does not carry the rehearsal label")
    if observation.get("status") != "ok":
        raise _unavailable("the rehearsal producer refused the observation")
    return observation


def _uniform_provenance(surfaces: Mapping[str, Any]) -> str:
    classes = set()
    for name in SURFACES:
        surface = surfaces[name]
        basis = surface.get("basis") if isinstance(surface, dict) else None
        if not isinstance(basis, str) or not basis:
            raise _unavailable(f"{name} reports no explicit basis")
        classes.add(surface.get("provenance"))
    if len(classes) != 1 or not classes <= _NORMATIVE:
        raise _unavailable(
            "mixed or non-normative provenance cannot be carried by one snapshot"
        )
    return next(iter(classes))


def _field(surface: Mapping[str, Any]) -> IdentityField:
    if surface["state"] == "PRESENT":
        return IdentityField.present(surface["value"])
    if surface["state"] == "ABSENT":
        return IdentityField.absent()
    raise _unavailable("a surface is neither PRESENT nor ABSENT")


def _artifact(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    if entry["state"] == "ABSENT":
        return dict(_NO_ARTIFACT)
    if entry["state"] == "PRESENT" and isinstance(entry["value"], dict):
        return entry["value"]
    raise _unavailable("an artifact state is neither PRESENT nor ABSENT")


def _components(surfaces: Mapping[str, Any]) -> tuple[ActualComponentIdentity, ...]:
    names = surfaces["membership"]["value"]["components"]
    identities = surfaces["component_identity"]["value"]
    artifacts = surfaces["artifact_identity"]["value"]
    if not len(names) == len(identities) == len(artifacts):
        raise _unavailable("the per-member surfaces describe different members")
    if [item["component_id"] for item in identities] != names:
        raise _unavailable("the per-member surfaces describe different members")
    return tuple(
        ActualComponentIdentity(
            item["component_id"], item["component_version"], _artifact(artifact)
        )
        for item, artifact in zip(identities, artifacts, strict=True)
    )


class RehearsalAdapter:
    """In-process collector for the rehearsal producer (S4). NOT PRODUCTION."""

    def __init__(
        self,
        producer_root: Path,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport = run_producer_process,
        handle_factory: Callable[[], str] = _random_handle,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not timeout > 0:
            message = "the bounded wait must be a positive number of seconds"
            raise ValueError(message)
        self._root = Path(producer_root)
        self._timeout = timeout
        self._transport = transport
        self._new_handle = handle_factory
        self._clock = clock
        self._issued: set[str] = set()
        self._record: dict[str, Any] | None = None

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        handle: str | None = None
        observation: dict[str, Any] | None = None
        try:
            handle = self._issue()
            observation = self._collect(handle)
            snapshot = self._snapshot(observation, binding)
        except ActualIdentityUnavailable as error:
            self._settle(handle, None, f"refused: {error}")
            raise
        self._settle(handle, observation, "accepted")
        return snapshot

    def evidence_record(self) -> dict[str, Any]:
        """The labelled record of the latest evaluation, accepted or refused."""
        if self._record is None:
            raise _unavailable("no evaluation has been made")
        return copy.deepcopy(self._record)

    def _issue(self) -> str:
        handle = self._new_handle()
        if not isinstance(handle, str) or not handle or handle in self._issued:
            raise _unavailable("the evaluation handle is not fresh")
        self._issued.add(handle)
        return handle

    def _request(self, handle: str) -> BoundaryRequest:
        return BoundaryRequest(
            argv=(
                sys.executable,
                "-P",
                "-m",
                "running_platform_rehearsal.producer",
                "--root",
                str(self._root),
            ),
            env=_child_environment(),
            cwd=str(self._root),
            stdin=(json.dumps({"op": "observe", "handle": handle}) + "\n").encode(),
            timeout=self._timeout,
        )

    def _collect(self, handle: str) -> dict[str, Any]:
        started = self._clock()
        try:
            response = self._transport(self._request(handle))
        except BoundaryTimeout as error:
            message = f"the rehearsal producer did not answer within {self._timeout} s"
            raise _unavailable(message) from error
        if self._clock() - started > self._timeout:
            raise _unavailable("the answer came after the deadline and is refused")
        observation = _decode(response)
        echoed = observation.get("handle")
        if echoed != handle:
            if isinstance(echoed, str) and echoed in self._issued:
                raise _unavailable("stale evidence: it answers an earlier evaluation")
            raise _unavailable("foreign evidence: it does not answer this evaluation")
        return observation

    @staticmethod
    def _snapshot(
        observation: Mapping[str, Any], binding: PlatformIdentityBinding
    ) -> ActualPlatformSnapshot:
        surfaces = observation.get("surfaces")
        if not isinstance(surfaces, dict) or set(surfaces) != set(SURFACES):
            raise _unavailable("the observation must cover exactly the nine surfaces")
        provenance = _uniform_provenance(surfaces)
        try:
            manifest = surfaces["manifest"]["value"]
            return ActualPlatformSnapshot(
                platform_id=surfaces["platform_id"]["value"],
                manifest={
                    "manifest_id": manifest["manifest_id"],
                    "manifest_version": manifest["manifest_version"],
                    "manifest_digest": manifest["manifest_digest"],
                },
                manifest_state=manifest["manifest_state"],
                components=_components(surfaces),
                membership_established=(
                    surfaces["membership"]["value"]["complete"] is True
                ),
                configuration=_field(surfaces["configuration"]),
                golden_bundle=_field(surfaces["golden_bundle"]),
                golden_bundle_inventory_established=(
                    surfaces["golden_bundle"]["inventory_complete"] is True
                ),
                extensions=_field(surfaces["extensions"]),
                branding=_field(surfaces["branding"]),
                provenance=provenance,
                correlation_token=binding.token,
                freshness_current=True,
            )
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise _unavailable("the observation is malformed") from error

    def _settle(
        self, handle: str | None, observation: Mapping[str, Any] | None, outcome: str
    ) -> None:
        rows = []
        if observation is not None:
            rows = [
                {
                    "surface": name,
                    "provenance": observation["surfaces"][name]["provenance"],
                    "basis": observation["surfaces"][name]["basis"],
                }
                for name in SURFACES
            ]
        self._record = {
            "label": REHEARSAL_LABEL,
            "rehearsal": True,
            "production": False,
            "boundary": {
                "transport": "one producer process per evaluation, stdio JSON lines",
                "request_fields": ["op", "handle"],
            },
            "handle": handle,
            "outcome": outcome,
            "surfaces": rows,
        }
