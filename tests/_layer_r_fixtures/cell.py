"""Owner-side Running Platform cell — the Layer R double of the production root.

This module stands in for the owner's own cell (the S4 runbook's
``<RP_CELL_HOME>``, e.g. ``/srv/running-platform/cell``), playing the role of the
running supervisor process that owns the platform's runtime and its identity
facts. It is **not** part of Deployment & Operations and it is never imported by
``deployment_operations``: the production composition root reaches it only as an
explicitly declared entry point under a path copied outside this repository, so
the ownership rule the root enforces (owner-side code is not D&O code) is
exercised rather than bypassed.

It models the two things a Running Platform owner supplies:

* ``runtime_adapter`` — the ``RuntimeAdapter`` of this cell. The cell supervises
  its own members, and its supervision state belongs to the cell, not to a
  caller: it is kept per cell home (``state_file``'s directory) so a *later*
  wiring instance of the same cell — a fresh Layer O process in production —
  re-binds to the members this cell already runs through ``attach``. The cell's
  own policy is one member per component: starting a component again retires the
  member it replaces, and ``shutdown`` retires everything the cell supervises.
  ``stop`` is **detach-only** (S4 runbook §7, A11): ``deploy()``'s fail-closed
  path calls it for every handle, so a detach that destroyed runtime would let a
  failed orchestration attempt tear Layer R down. Whether a member actually
  terminates stays this cell's decision.
* ``identity_source`` — the cell's own Platform Identity Surface reader. It
  answers one observation per evaluation from the cell's own authoritative state
  document, states, as its own facts, the binding identity it answered and its
  attribution, and records in its own audit what it was asked and what it
  answered.

The state document is this double's own format, not a Deployment & Operations
artifact: a real owner measures its own cell. Its control keys exist so that a
test can make the owner answer with another platform, another handle, stale
facts, an unavailable surface, an unexpected error or something that is not a
snapshot at all — the scenarios the production path must refuse.

This double is deliberately *not* an authority of any kind: it consumes only the
opaque evaluation binding it is handed, and its answer never contains a
DeploymentRecord, a request value or an expected instance digest.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    ActualIdentityUnavailable,
    EvidenceCorrelation,
    IdentityField,
    PlatformIdentityBinding,
    PresenceState,
)
from deployment_operations.platform_identity_source import ActualPlatformSnapshot
from deployment_operations.runtime import (
    LocalProcessRuntime,
    RuntimeHandle,
    RuntimeProcessError,
    verify_bound_content,
)

#: The attribution this cell states for its own observations (ADR-0020 §17).
CELL_AUTHORITY = "layer-r/cell-producer"
CELL_BASIS = "layer-r/cell-state/1"
CELL_OBSERVED_AT = "2026-10-08T00:00:00Z"

#: This double's control keys: they drive what the owner answers with.
ANSWER_CORRELATION = "correlation"
ANSWER_UNAVAILABLE = "unavailable"
ANSWER_MALFORMED = "malformed"
ANSWER_ERROR = "error"

#: How this double's runtime seam fails when a test asks it to. A real
#: owner-side adapter is external code and may raise anything at all, so the
#: three shapes that matter are covered: its own runtime error, an ordinary
#: Python exception, and a ``BaseException`` that no operation-level handler is
#: meant to catch (the interrupted-process shape).
FAIL_RUNTIME_ERROR = "runtime_error"
FAIL_ERROR = "error"
FAIL_INTERRUPT = "interrupt"

#: The name of this cell's own supervision record, under its home. A real cell
#: keeps its supervision in its own supervisor process; a test process plays
#: that role, so the record is kept where a later process — a fresh Layer O
#: process re-binding to the standing platform — can read it. This is the
#: cell's own state, never a Deployment & Operations record.
SUPERVISION_FILENAME = "supervision.json"


class CellRuntimeAdapter:
    """The runtime seam of this cell: it supervises what it starts.

    ``stop`` detaches and never destroys (S4 runbook §7): the member's fate is
    this cell's own policy, applied by :meth:`shutdown` or when a member is
    replaced.
    """

    def __init__(
        self,
        cell_home: Path,
        audit_file: Path | None,
        *,
        failures: Mapping[str, str] | None = None,
    ) -> None:
        self._inner = LocalProcessRuntime()
        self._cell_home = Path(cell_home).resolve()
        self._audit_file = audit_file
        self.calls: list[str] = []
        #: operation -> failure shape: what this cell's seam does when asked.
        self._failures = dict(failures or {})
        #: component id -> the live handle this process holds for that member.
        #: A member survives this process; the handle to its process does not,
        #: which is why the durable part of the supervision is the record below.
        self._live: dict[str, RuntimeHandle] = {}

    # -- the cell's own supervision state ----------------------------------
    def _supervision_path(self) -> Path:
        return self._cell_home / SUPERVISION_FILENAME

    def _supervision(self) -> dict[str, dict[str, str]]:
        """The members this cell supervises, as the cell's own durable state."""
        try:
            document = json.loads(self._supervision_path().read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(document, dict):
            return {}
        return {
            str(component_id): dict(entry)
            for component_id, entry in document.items()
            if isinstance(entry, dict)
        }

    def _write_supervision(self, members: Mapping[str, Mapping[str, str]]) -> None:
        path = self._supervision_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {k: dict(v) for k, v in members.items()}, indent=2, sort_keys=True
            ),
            encoding="utf-8",
        )

    def supervised(self) -> tuple[str, ...]:
        """The runtime elements this cell currently supervises."""
        return tuple(sorted(self._supervision()))

    def _retire(self, component_id: str) -> None:
        """Retire one member under the cell's own policy (never a D&O action)."""
        members = self._supervision()
        if members.pop(component_id, None) is None:
            return
        self._record(f"retire:{component_id}")
        self._write_supervision(members)
        handle = self._live.pop(component_id, None)
        if handle is not None:
            self._inner.stop(handle)

    def shutdown(self) -> None:
        """This cell's own retirement policy, applied when the owner decides."""
        for component_id in self.supervised():
            self._retire(component_id)
        self._record("shutdown")

    # -- the cell's own audit (never a Deployment & Operations record) ------
    def _record(self, call: str) -> None:
        self.calls.append(call)
        if self._audit_file is not None:
            self._audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"call": call}, sort_keys=True) + "\n")

    # -- this double's failure control --------------------------------------
    def _maybe_fail(self, operation: str, component_id: str = "") -> None:
        """Fail the way a real owner-side adapter may fail, when told to.

        The attempted call is recorded *before* the failure so a test can see
        that this cell was asked and refused, rather than never reached.
        """
        shape = self._failures.get(operation)
        if shape is None:
            return
        self._record(f"failed:{operation}:{component_id}")
        context = f"this cell's runtime seam failed in {operation}"
        if shape == FAIL_INTERRUPT:
            # A BaseException no operation-level handler is meant to catch: the
            # shape of a process that died mid-operation.
            raise KeyboardInterrupt(f"{context} (interrupted)")
        if shape == FAIL_RUNTIME_ERROR:
            raise RuntimeProcessError(f"{context} (runtime error)")
        raise ValueError(f"{context} (unexpected error)")

    # -- the RuntimeAdapter seam -------------------------------------------
    def materialize(self, element: Any) -> Any:
        component_id = element.component.component_id
        self._record(f"materialize:{component_id}")
        self._maybe_fail("materialize", component_id)
        return self._inner.materialize(element)

    def migrate(self, element: Any) -> Any:
        component_id = element.component.component_id
        self._record(f"migrate:{component_id}")
        self._maybe_fail("migrate", component_id)
        return self._inner.migrate(element)

    def start(self, element: Any) -> Any:
        component_id = element.component.component_id
        # One member per component: the cell replaces the member it supersedes.
        self._retire(component_id)
        self._record(f"start:{component_id}")
        self._maybe_fail("start", component_id)
        handle = self._inner.start(element)
        self._live[component_id] = handle
        members = self._supervision()
        members[component_id] = {
            "deployment_id": str(element.deployment_id),
            "instance_digest": str(element.instance_digest),
        }
        self._write_supervision(members)
        return handle

    def attach(self, element: Any) -> Any:
        component_id = element.component.component_id
        self._record(f"attach:{component_id}")
        self._maybe_fail("attach", component_id)
        execution = getattr(element, "execution", None)
        if execution is None:
            raise RuntimeProcessError(
                f"{component_id}: this element names no execution content; a cell "
                "binds exactly what it supervises and refuses to hand out "
                "content it cannot verify"
            )
        problems = verify_bound_content(execution)
        if problems:
            raise RuntimeProcessError(
                f"{component_id}: the content this element names does not match "
                "what was bound for it",
                errors=list(problems),
            )
        member = self._supervision().get(component_id)
        if member is None:
            raise RuntimeProcessError(
                f"{component_id}: this cell supervises no such runtime element; "
                "attach binds an existing element and creates none"
            )
        if member.get("deployment_id") != str(element.deployment_id) or member.get(
            "instance_digest"
        ) != str(element.instance_digest):
            raise RuntimeProcessError(
                f"{component_id}: this cell supervises an element of another "
                "deployment or instance; attach binds exactly the member this "
                "operation realized"
            )
        live = self._live.get(component_id)
        if live is not None:
            return live
        # This process does not hold the member's process — a fresh Layer O
        # process binds the standing member the cell supervises. The handle it
        # gets is this cell's reference, not a newly created runtime element.
        return RuntimeHandle(element=element, process=None, started=True)

    def request(
        self, handle: Any, operation: str, *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        component_id = handle.component_id
        self._record(f"request:{operation}:{component_id}")
        self._maybe_fail("request", component_id)
        return self._inner.request(handle, operation, timeout=timeout)

    def stop(self, handle: Any) -> Mapping[str, Any]:
        """Detach: release the caller's reference, leave the member to this cell."""
        component_id = handle.component_id
        self._record(f"detach:{component_id}")
        self._maybe_fail("stop", component_id)
        return {"status": "detached", "component_id": component_id}


class CellIdentitySource:
    """This cell's Platform Identity Surface reader (owner-side source)."""

    def __init__(self, state_file: Path, audit_file: Path | None) -> None:
        self._state_file = state_file
        self._audit_file = audit_file

    def _record(self, binding: PlatformIdentityBinding, answer: str) -> None:
        if self._audit_file is None:
            return
        context = binding.context
        record = {
            "answered_for_handle": binding.token,
            "binding_keys": sorted(
                name
                for name in ("token", "context")
                if getattr(binding, name, None) is not None
            ),
            "context_fields": sorted(
                name
                for name in (
                    "authority",
                    "basis",
                    "target",
                    "scope",
                    "sequence",
                    "established_at",
                )
                if getattr(context, name, None) not in (None, "")
            ),
            "binding_scope": getattr(context, "scope", None),
            "binding_sequence": getattr(context, "sequence", None),
            "answer": answer,
        }
        self._audit_file.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _document(self) -> dict[str, Any]:
        try:
            document = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ActualIdentityUnavailable(
                "this cell has no readable authoritative state"
            ) from error
        if not isinstance(document, dict):
            raise ActualIdentityUnavailable("this cell's state document is invalid")
        return document

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        document = self._document()
        answer = str(document.get("answer", ANSWER_CORRELATION))
        self._record(binding, answer)
        if answer == ANSWER_UNAVAILABLE:
            raise ActualIdentityUnavailable(
                "this cell's identity surface is not served"
            )
        if answer == ANSWER_ERROR:
            message = "this cell's identity surface failed unexpectedly"
            raise RuntimeError(message)
        if answer == ANSWER_MALFORMED:
            # Deliberately not a snapshot: the seam must refuse it, not adapt it.
            return {"platform_id": document.get("platform_id")}  # type: ignore[return-value]

        return ActualPlatformSnapshot(
            platform_id=str(document.get("platform_id", "")),
            manifest=dict(document.get("manifest") or {}),
            manifest_state=str(document.get("manifest_state", "")),
            components=tuple(
                ActualComponentIdentity(
                    str(entry.get("component_id", "")),
                    str(entry.get("component_version", "")),
                    entry.get("artifact_identity"),
                )
                for entry in document.get("components") or ()
            ),
            membership_established=document.get("membership_established") is True,
            configuration=_field(document.get("configuration")),
            golden_bundle=_field(document.get("golden_bundle")),
            golden_bundle_inventory_established=(
                document.get("golden_bundle_inventory_established") is True
            ),
            extensions=_field(document.get("extensions")),
            branding=_field(document.get("branding")),
            provenance=str(document.get("provenance", "")),
            correlation=_correlation(document, binding),
            freshness_current=document.get("freshness_current") is True,
        )


def _field(value: Any) -> IdentityField:
    """One presence-stated identity field of the cell's state document."""
    if not isinstance(value, dict):
        raise ActualIdentityUnavailable("this cell's state omits an identity field")
    if value.get("state") == PresenceState.PRESENT:
        return IdentityField.present(value.get("value"))
    if value.get("state") == PresenceState.ABSENT:
        return IdentityField.absent()
    raise ActualIdentityUnavailable("this cell's state has an invalid identity field")


def _correlation(
    document: Mapping[str, Any],
    binding: PlatformIdentityBinding,
) -> EvidenceCorrelation:
    """This cell's own statement of the binding it answered.

    The defaults are what an honest producer states: it answers the binding it
    was handed, under that binding's own scope and position, and reports the
    platform identity it actually observed. The document may override any field
    — which is how the refusal scenarios (another handle, another target,
    another sequence) are produced.
    """
    declared = document.get("correlation")
    overrides = dict(declared) if isinstance(declared, dict) else {}
    context = binding.context
    return EvidenceCorrelation(
        token=overrides.get("token", binding.token),
        scope=str(overrides.get("scope", getattr(context, "scope", ""))),
        sequence=overrides.get("sequence", getattr(context, "sequence", 0)),
        target=str(overrides.get("target", document.get("platform_id", ""))),
        authority=str(overrides.get("authority", CELL_AUTHORITY)),
        basis=str(overrides.get("basis", CELL_BASIS)),
        established_at=str(overrides.get("established_at", CELL_OBSERVED_AT)),
    )


class CellWiring:
    """What the cell hands the production composition root: its two seams."""

    def __init__(
        self,
        runtime_adapter: CellRuntimeAdapter,
        identity_source: CellIdentitySource,
    ) -> None:
        self.runtime_adapter = runtime_adapter
        self.identity_source = identity_source


def build_layer_r_wiring(
    state_file: str,
    audit_file: str = "",
    runtime_audit_file: str = "",
    fail_on: str = "",
    fail_kind: str = "",
) -> CellWiring:
    """Build this cell's wiring for the declared options.

    The declared options (``--layer-r-option``) arrive as keyword arguments, so
    every parameter is a string; a real cell reads its own configuration the
    same way. Nothing here is discovered and nothing is defaulted by
    Deployment & Operations.

    ``fail_on`` names the runtime operations this cell's seam should refuse
    (comma separated) and ``fail_kind`` how — see the ``FAIL_*`` shapes above.
    They exist so a test can drive the failure paths an owner-side adapter
    produces in production.
    """
    if not state_file:
        message = "state_file is required: this cell owns its authoritative state"
        raise ValueError(message)
    shape = fail_kind or FAIL_ERROR
    if shape not in (FAIL_RUNTIME_ERROR, FAIL_ERROR, FAIL_INTERRUPT):
        message = f"fail_kind must be one of the FAIL_* shapes, got {fail_kind!r}"
        raise ValueError(message)
    failures = {
        operation.strip(): shape
        for operation in str(fail_on).split(",")
        if operation.strip()
    }
    state_path = Path(state_file)
    return CellWiring(
        runtime_adapter=CellRuntimeAdapter(
            state_path.parent,
            Path(runtime_audit_file) if runtime_audit_file else None,
            failures=failures,
        ),
        identity_source=CellIdentitySource(
            state_path,
            Path(audit_file) if audit_file else None,
        ),
    )
