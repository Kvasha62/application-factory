"""The component runtime process of the first deployment slice.

One runtime element of a platform is one component, running in its own OS
process, started by Deployment & Operations from the runtime spec of a
deployment operation. This module is the entrypoint of that process — and also
of the component's transient **migration session**: the same component code, the
same pinned spec, but a session that runs the required migrations and ends
instead of becoming part of the Running Platform. Both roles are the *same*
protocol; which one a process is playing is decided by the engine that started
it, and only ``start`` makes it a runtime element of the platform (§36–§37)::

    python -m deployment_operations.runtime_worker <spec.json>

It speaks one JSON request per line over stdin and answers one JSON response
per line on stdout, so the deployment engine can start a component, orchestrate
its component-defined migrations and ask it for its health, readiness and
identity without touching anything of the component's own.

Division of labour, and the architectural rules behind it:

* the component constructs itself, through its **own** composition-internal
  deployment entrypoint named in the runtime spec — this module knows no
  component by name and contains no per-component behavior;
* migrations are **component-owned code**: this module only executes the
  declarations the component publishes, in the declared order, forward only
  (LAW-08). A declaration that carries a reverse/downgrade path is refused, so
  a migration can never rewrite history (ADR-0016 §14);
* health/readiness are the **component's own published surface**: the checks
  executed here are the component's own ``/health`` and ``/ready`` routes,
  invoked through its published ASGI application. This module never invents a
  health verdict, and the deployment engine — not this process — decides what
  the observations mean (ADR-0016 §18, §19).

Nothing here reads or writes business data: every store, journal and schema
belongs to the component and is only ever reached through the component's own
code. Secrets are never passed in the spec; they arrive through the process
environment, which the runtime adapter sets at start time (ADR-0016 §12).
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

#: Keys a forward migration declaration may carry. Anything else — in
#: particular a reverse or downgrade path — makes the declaration unusable:
#: schema develops forward only (LAW-08; ADR-0016 §14).
FORWARD_MIGRATION_KEYS = frozenset({"migration_id", "forward"})

#: Operation names of the deployment engine ↔ component process protocol.
OP_START = "start"
OP_MIGRATE = "migrate"
OP_PROBE = "probe"
OP_STOP = "stop"


class RuntimeWorkerError(Exception):
    """A refusal or failure inside the component runtime process."""


def _load_spec(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimeWorkerError("the runtime spec is not a JSON object")
    return document


def _import_paths(spec: Mapping[str, Any]) -> None:
    for entry in (spec.get("runtime") or {}).get("import_paths", []):
        if isinstance(entry, str) and entry not in sys.path:
            sys.path.insert(0, entry)


def _build_deployment(spec: Mapping[str, Any]) -> Any:
    """Construct the component's own deployment from its pinned configuration.

    The entrypoint is the one the deployment environment bound for this
    component in this environment; the configuration is exactly what the
    accepted Platform Instance declared, plus the environment-specific values
    bound to it (ADR-0016 §13). Construction failure is a fail-closed startup
    failure — there is no fallback composition.
    """
    runtime = spec.get("runtime") or {}
    module_name = runtime.get("deployment_module")
    factory_name = runtime.get("deployment_factory")
    if not isinstance(module_name, str) or not isinstance(factory_name, str):
        raise RuntimeWorkerError(
            "the runtime spec names no component deployment entrypoint"
        )
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name, None)
    if factory is None or not callable(factory):
        raise RuntimeWorkerError(
            f"{module_name}.{factory_name} is not a callable deployment entrypoint"
        )
    configuration = spec.get("configuration")
    if not isinstance(configuration, Mapping):
        configuration = {}
    return factory(dict(configuration))


def _contract_app(deployment: Any) -> Any:
    """The component's published ASGI contract, as the component exposes it."""
    app_factory = getattr(deployment, "contract_app", None)
    if app_factory is None or not callable(app_factory):
        raise RuntimeWorkerError(
            "the component deployment exposes no published contract application"
        )
    return app_factory()


def _normalize_migrations(
    declared: object,
    *,
    location: str,
) -> list[tuple[str, Callable[[Any], Any]]]:
    """Normalize a component's forward migration declaration.

    A declaration is a sequence whose entries are either callables or mappings
    with exactly ``migration_id`` and ``forward``. Order is the declared order
    and is never re-sorted: the component's contract decides the sequence in
    which its own migrations may run (ADR-0016 §14).
    """
    if not isinstance(declared, Sequence) or isinstance(declared, str | bytes):
        if callable(declared):
            return [("migration-1", declared)]
        raise RuntimeWorkerError(
            f"{location} does not declare a sequence of forward migrations"
        )

    migrations: list[tuple[str, Callable[[Any], Any]]] = []
    for index, entry in enumerate(declared):
        if callable(entry):
            migrations.append((f"migration-{index + 1}", entry))
            continue
        if not isinstance(entry, Mapping):
            raise RuntimeWorkerError(
                f"{location}[{index}] is neither a migration callable nor a declaration"
            )
        extra = sorted(set(entry) - FORWARD_MIGRATION_KEYS)
        if extra:
            raise RuntimeWorkerError(
                f"{location}[{index}] declares {extra!r}; migrations are forward "
                "only and a declaration may carry no reverse or downgrade path"
            )
        forward = entry.get("forward")
        if not callable(forward):
            raise RuntimeWorkerError(
                f"{location}[{index}] declares no forward migration callable"
            )
        migration_id = entry.get("migration_id")
        if not isinstance(migration_id, str) or not migration_id:
            raise RuntimeWorkerError(
                f"{location}[{index}] declares no concrete migration identity"
            )
        migrations.append((migration_id, forward))
    return migrations


class ComponentRuntime:
    """One component, running in one process, answering the engine's requests."""

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self._spec = spec
        self._deployment: Any = None
        self._app: Any = None

    def start(self) -> dict[str, Any]:
        self._ensure_built()
        return {"status": "ok"}

    def _ensure_built(self) -> None:
        if self._deployment is None:
            self._deployment = _build_deployment(self._spec)
        if self._app is None:
            self._app = _contract_app(self._deployment)

    def migrate(self) -> dict[str, Any]:
        """Execute the component's own required migrations, forward only."""
        migrations_binding = (self._spec.get("runtime") or {}).get("migrations")
        if not isinstance(migrations_binding, Mapping):
            return {"status": "ok", "declared": False, "executed": [], "required": []}

        module_name = migrations_binding.get("module")
        attribute = migrations_binding.get("attribute")
        if not isinstance(module_name, str) or not isinstance(attribute, str):
            raise RuntimeWorkerError(
                "the runtime spec names no component migration declaration"
            )
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute):
            raise RuntimeWorkerError(
                f"{module_name}.{attribute} is not declared by the component"
            )

        self._ensure_built()
        declared = getattr(module, attribute)
        location = f"{module_name}.{attribute}"
        if callable(declared) and not isinstance(declared, Sequence):
            declared = declared(self._deployment)
        migrations = _normalize_migrations(declared, location=location)

        executed: list[str] = []
        for migration_id, forward in migrations:
            try:
                forward(self._deployment)
            except Exception as error:  # noqa: BLE001 - fail closed, never mask
                return {
                    "status": "failed",
                    "declared": True,
                    "required": [entry[0] for entry in migrations],
                    "executed": executed,
                    "migration_id": migration_id,
                    "error": f"{error.__class__.__name__}: {error}",
                }
            executed.append(migration_id)

        return {
            "status": "ok",
            "declared": True,
            "required": [entry[0] for entry in migrations],
            "executed": executed,
        }

    def probe(self) -> dict[str, Any]:
        """Execute the component's own health and readiness routes.

        The observations are returned verbatim: this process reports what the
        component's published surface answered, and the deployment engine
        evaluates it.
        """
        self._ensure_built()
        from fastapi.testclient import TestClient

        client = TestClient(self._app)
        return {
            "status": "ok",
            "health": _call(client, "/health"),
            "ready": _call(client, "/ready"),
        }

    def stop(self) -> dict[str, Any]:
        self._app = None
        self._deployment = None
        return {"status": "ok"}


def _call(client: Any, path: str) -> dict[str, Any]:
    response = client.get(path)
    try:
        body = response.json()
    except ValueError:
        body = None
    return {"path": path, "status_code": response.status_code, "body": body}


def _respond(response: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(response), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run(spec_path: Path) -> int:
    """Serve one component process until the engine asks it to stop."""
    spec = _load_spec(spec_path)
    _import_paths(spec)
    runtime = ComponentRuntime(spec)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            _respond({"status": "error", "error": f"invalid request: {error}"})
            continue
        operation = request.get("op")
        try:
            if operation == OP_START:
                _respond(runtime.start())
            elif operation == OP_MIGRATE:
                _respond(runtime.migrate())
            elif operation == OP_PROBE:
                _respond(runtime.probe())
            elif operation == OP_STOP:
                _respond(runtime.stop())
                return 0
            else:
                _respond(
                    {"status": "error", "error": f"unknown operation {operation!r}"}
                )
        except RuntimeWorkerError as error:
            _respond({"status": "error", "error": str(error)})
        except Exception as error:  # noqa: BLE001 - report, never leak a traceback
            _respond(
                {"status": "error", "error": f"{error.__class__.__name__}: {error}"}
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        sys.stderr.write("usage: python -m deployment_operations.runtime_worker SPEC\n")
        return 2
    return run(Path(arguments[0]))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FORWARD_MIGRATION_KEYS",
    "ComponentRuntime",
    "RuntimeWorkerError",
    "main",
    "run",
]
