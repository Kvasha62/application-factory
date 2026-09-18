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
from pathlib import Path
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
    #: What the running process reported about the content it loaded. Evidence,
    #: never authority: the engine compares it with the content it bound to that
    #: process before launch (§9, §10).
    execution: Mapping[str, Any] | None = None

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

    def observed_execution(self) -> tuple[tuple[str, ...], dict[str, tuple[str, str]]]:
        """Return the execution content the process reported loading.

        The result is the reported execution root and, per module the process
        loaded, the path it was loaded from and the digest of the bytes it read.
        What the runtime says is never taken as the source of truth for content:
        it is the evidence the engine checks its own launch binding against.
        """
        execution = self.execution
        if not isinstance(execution, Mapping):
            return (), {}
        reported: dict[str, tuple[str, str]] = {}
        observed_roots: list[str] = []
        declared = execution.get("roots")
        if isinstance(declared, Sequence) and not isinstance(declared, str | bytes):
            observed_roots.extend(
                item for item in declared if isinstance(item, str) and item
            )
        root = execution.get("root")
        if isinstance(root, str) and root and root not in observed_roots:
            observed_roots.append(root)
        entries = execution.get("modules")
        if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
            return tuple(observed_roots), reported
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            module = entry.get("module")
            path = entry.get("path")
            digest = entry.get("digest")
            if (
                isinstance(module, str)
                and module
                and isinstance(path, str)
                and path
                and isinstance(digest, str)
                and digest
            ):
                reported[module] = (path, digest)
        return tuple(observed_roots), reported

    def observed_foreign(self) -> tuple[str, ...]:
        """Component-owned content the process holds outside the boundary.

        The engine bound the component's executable closure; content of the
        component found elsewhere — in the workspace, in a verified root the
        boundary did not bind — is content no claim may stand on, whatever
        loaded it (§5, §19).
        """
        execution = self.execution
        if not isinstance(execution, Mapping):
            return ()
        foreign = execution.get("foreign")
        if not isinstance(foreign, Sequence) or isinstance(foreign, str | bytes):
            return ()
        return tuple(item for item in foreign if isinstance(item, str) and item)

    def document(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "health": dict(self.health) if self.health else None,
            "readiness": dict(self.readiness) if self.readiness else None,
            "error": self.error,
            "execution": dict(self.execution) if self.execution else None,
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


def _bound_modules(
    document: Mapping[str, Any] | None,
) -> tuple[tuple[str, ...], dict[str, tuple[str, str]]]:
    """Read an engine execution binding: its verified roots and bound content."""
    if not isinstance(document, Mapping):
        return (), {}
    roots: list[str] = []
    declared = document.get("roots")
    if isinstance(declared, Sequence) and not isinstance(declared, str | bytes):
        roots.extend(item for item in declared if isinstance(item, str) and item)
    root = document.get("root")
    if isinstance(root, str) and root and root not in roots:
        roots.append(root)
    bound: dict[str, tuple[str, str]] = {}
    entries = document.get("modules")
    if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
        return tuple(roots), bound
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        module = entry.get("module")
        path = entry.get("path")
        digest = entry.get("digest")
        if (
            isinstance(module, str)
            and module
            and isinstance(path, str)
            and path
            and isinstance(digest, str)
            and digest
        ):
            bound[module] = (path, digest)
    return tuple(roots), bound


def _inside(root: Path, path: Path) -> bool:
    """True when a path lies inside a root, after resolving both."""
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def verify_identity(
    bindings: Sequence[ComponentBinding],
    observations: Sequence[ComponentObservation],
    *,
    platform_id: str | None,
    instance_digest: str | None,
    executions: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Verify the actual running platform against the desired state (§8, §10).

    This is the second dimension of an honest claim: every runtime element must
    report exactly the pinned component identity and version, must serve exactly
    the platform instance the deployment was requested for, and must have been
    started from — and itself report — exactly the content this deployment bound
    to it before launch. The binding is the engine's own determination, so a
    runtime that reports the expected identity is still refused when the content
    behind that report is not the content the deployment bound.

    What is running is compared with what was pinned and bound — never with what
    was convenient, and never on the runtime's word alone.
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
        for entry in observation.observed_foreign():
            errors.append(
                f"{binding.component_id}: the running process holds content the "
                f"boundary did not bind ({entry}); what executes is not covered "
                "by what was verified"
            )
        bound_roots, bound_content = _bound_modules(
            executions.get(binding.component_id)
        )
        if not bound_roots or not bound_content:
            errors.append(
                f"{binding.component_id}: the deployment bound no execution "
                "content for this component; the content the running platform "
                "executes cannot be verified"
            )
        else:
            observed_roots, observed_content = observation.observed_execution()
            if not observed_roots or not observed_content:
                errors.append(
                    f"{binding.component_id}: the running component reported no "
                    "execution content; what it actually executes cannot be "
                    "verified"
                )
            else:
                verified = {Path(root).resolve() for root in bound_roots}
                observed = {Path(root).resolve() for root in observed_roots}
                if observed != verified:
                    errors.append(
                        f"{binding.component_id}: the running component executes "
                        f"content under {sorted(str(root) for root in observed)}; "
                        "this deployment bound the content under "
                        f"{sorted(str(root) for root in verified)}"
                    )
                for module in sorted(set(bound_content) | set(observed_content)):
                    bound_entry = bound_content.get(module)
                    observed_entry = observed_content.get(module)
                    if bound_entry is None:
                        errors.append(
                            f"{binding.component_id}: the running component "
                            f"reports content for {module!r}, which this "
                            "deployment did not bind"
                        )
                        continue
                    if observed_entry is None:
                        errors.append(
                            f"{binding.component_id}: this deployment bound the "
                            f"content of {module!r} and the running component "
                            "reported no content for it"
                        )
                        continue
                    bound_path, bound_digest = bound_entry
                    observed_path, observed_digest = observed_entry
                    if observed_digest != bound_digest:
                        errors.append(
                            f"{binding.component_id}: the running component "
                            f"loaded content {observed_digest} for {module!r} "
                            f"while this deployment bound {bound_digest}; what "
                            "executes is not what was verified"
                        )
                    if not any(
                        _inside(Path(root), Path(observed_path)) for root in bound_roots
                    ):
                        errors.append(
                            f"{binding.component_id}: the running component "
                            f"loaded {observed_path}, outside every verified "
                            f"execution root {sorted(bound_roots)}"
                        )
                    elif Path(observed_path).resolve() != Path(bound_path).resolve():
                        errors.append(
                            f"{binding.component_id}: the running component "
                            f"loaded {observed_path} for {module!r}; this "
                            f"deployment bound {bound_path}"
                        )
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
