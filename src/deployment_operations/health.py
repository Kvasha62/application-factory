"""Health/readiness — establishing and evaluating the platform's condition.

``ready`` is a **verified operational condition**, not a claim about a process
existing (ADR-0017 §33): merely starting artifacts, processes or containers is
not ``ready``, exactly as merely starting them is not ``deployed``
(ADR-0016 §10). This module evaluates the observations the running components
reported over their own published health and readiness surfaces, and it keeps
the two dimensions of an honest claim apart:

* :func:`evaluate_health` decides the **operational condition** — whether the
  platform is ``ready``;
* :func:`verify_identity` decides the **identity dimension** — whether the
  actual, observable Running Platform corresponds to the desired state of the
  specific Platform Instance, by identity, version and digest (ADR-0016 §8,
  §10; ADR-0017 §34).

Neither dimension may be dropped, and neither may be inferred from the other:
``healthcheck passed → deployed`` is not a lesser version of the claim, it is a
different and false one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from deployment_operations.verification import ComponentBinding

#: The status the published health surface of a component reports when it is
#: healthy, and the status a readiness surface reports.
HEALTHY_STATUS = "ok"
READY_STATUS = "ready"


@dataclass(frozen=True)
class ComponentObservation:
    """What one running component answered about itself."""

    component_id: str
    health: Mapping[str, Any] | None = None
    readiness: Mapping[str, Any] | None = None
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """True only when both published surfaces answered positively.

        A component that did not answer, answered with a non-object body, or
        reported anything other than its own healthy/ready status is not
        healthy: an unavailable runtime cannot be ``ready`` (ADR-0016 §19).
        """
        if self.error is not None:
            return False
        health = self.health
        readiness = self.readiness
        if not isinstance(health, Mapping) or not isinstance(readiness, Mapping):
            return False
        if health.get("status_code") != 200 or readiness.get("status_code") != 200:
            return False
        health_body = health.get("body")
        ready_body = readiness.get("body")
        if not isinstance(health_body, Mapping) or not isinstance(ready_body, Mapping):
            return False
        return (
            health_body.get("status") == HEALTHY_STATUS
            and ready_body.get("status") == READY_STATUS
        )

    def observed_identity(self) -> tuple[str | None, str | None, str | None]:
        """Return the component/version/platform identity it reported.

        Identity comes from the component's own health surface — what the
        running component says about itself — never from what was requested.
        """
        health = self.health
        body = health.get("body") if isinstance(health, Mapping) else None
        if not isinstance(body, Mapping):
            return (None, None, None)
        component_id = body.get("component_id")
        version = body.get("version")
        platform_id = body.get("platform_id")
        return (
            component_id if isinstance(component_id, str) else None,
            version if isinstance(version, str) else None,
            platform_id if isinstance(platform_id, str) else None,
        )

    def document(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "health": dict(self.health) if self.health else None,
            "readiness": dict(self.readiness) if self.readiness else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class HealthEvaluation:
    """The outcome of health/readiness evaluation for the whole platform."""

    observations: tuple[ComponentObservation, ...] = ()
    errors: tuple[str, ...] = ()
    extra_errors: tuple[str, ...] = field(default=())

    @property
    def ready(self) -> bool:
        """True when every runtime element of the instance is healthy."""
        if self.errors or self.extra_errors:
            return False
        return bool(self.observations) and all(
            observation.healthy for observation in self.observations
        )

    def observation(self, component_id: str) -> ComponentObservation | None:
        for entry in self.observations:
            if entry.component_id == component_id:
                return entry
        return None

    def unhealthy(self) -> tuple[str, ...]:
        return tuple(
            observation.component_id
            for observation in self.observations
            if not observation.healthy
        )

    def document(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "unhealthy_components": list(self.unhealthy()),
            "observations": [entry.document() for entry in self.observations],
            "errors": [*self.errors, *self.extra_errors],
        }


def evaluate_health(
    bindings: Sequence[ComponentBinding],
    observations: Sequence[ComponentObservation],
    errors: Sequence[str] = (),
) -> HealthEvaluation:
    """Evaluate the operational condition of the platform.

    Every component the instance declares must have answered. A component that
    was never reached, answered unusably or reported itself unhealthy makes the
    platform not ``ready``: the deployment operation stops here, and no
    ``realized`` / ``deployed`` claim is fixed (ADR-0016 §20; ADR-0017 §34).
    """
    evaluation_errors: list[str] = list(errors)
    by_id = {observation.component_id: observation for observation in observations}

    for binding in bindings:
        observation = by_id.get(binding.component_id)
        if observation is None:
            evaluation_errors.append(
                f"{binding.component_id}: no health/readiness observation was "
                "collected for this component"
            )
            continue
        if not observation.healthy:
            detail = observation.error or _summarize(observation)
            evaluation_errors.append(
                f"{binding.component_id}: the running component is not healthy "
                f"({detail})"
            )

    ordered = tuple(
        by_id[binding.component_id]
        for binding in bindings
        if binding.component_id in by_id
    )
    return HealthEvaluation(observations=ordered, errors=tuple(evaluation_errors))


def _summarize(observation: ComponentObservation) -> str:
    health = observation.health or {}
    readiness = observation.readiness or {}
    return (
        f"/health → {health.get('status_code')} {_body(health)}, "
        f"/ready → {readiness.get('status_code')} {_body(readiness)}"
    )


def _body(entry: Mapping[str, Any]) -> str:
    body = entry.get("body")
    if isinstance(body, Mapping):
        return json.dumps(dict(body), ensure_ascii=False, sort_keys=True)
    return "no JSON body"


def verify_identity(
    bindings: Sequence[ComponentBinding],
    observations: Sequence[ComponentObservation],
    *,
    platform_id: str | None,
    instance_digest: str | None,
) -> list[str]:
    """Verify the actual running platform against the desired state (§8, §10).

    This is the second dimension of an honest claim: every runtime element must
    report exactly the pinned component identity and version, and must serve
    exactly the platform instance the deployment was requested for. What is
    running is compared with what was pinned — never with what was convenient.
    """
    errors: list[str] = []
    by_id = {observation.component_id: observation for observation in observations}

    for binding in bindings:
        observation = by_id.get(binding.component_id)
        if observation is None:
            errors.append(
                f"{binding.component_id}: the running platform does not include "
                "this component of the instance"
            )
            continue
        observed_component, observed_version, observed_platform = (
            observation.observed_identity()
        )
        if observed_component is None or observed_version is None:
            errors.append(
                f"{binding.component_id}: the running component reported no "
                "identity; the actual state cannot be verified"
            )
            continue
        if observed_component != binding.component_id:
            errors.append(
                f"{binding.component_id}: the running component reports identity "
                f"{observed_component!r}; the instance pins "
                f"{binding.component_id!r}"
            )
        if observed_version != binding.component_version:
            errors.append(
                f"{binding.component_id}: the running component reports version "
                f"{observed_version!r}; the instance pins "
                f"{binding.component_version!r}"
            )
        if platform_id is not None and observed_platform != platform_id:
            errors.append(
                f"{binding.component_id}: the running component serves platform "
                f"{observed_platform!r}; the deployment was requested for "
                f"{platform_id!r}"
            )

    if instance_digest is None:
        errors.append(
            "the deployment input carries no instance digest; an honest "
            "deployed claim is always relative to a concrete instance"
        )

    return sorted(errors)


__all__ = [
    "HEALTHY_STATUS",
    "READY_STATUS",
    "ComponentObservation",
    "HealthEvaluation",
    "evaluate_health",
    "verify_identity",
]
