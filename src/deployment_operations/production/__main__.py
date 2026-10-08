"""Run one Deployment & Operations operation in production mode.

    python -m deployment_operations.production deploy \\
        --instance  <platform-instance.json> \\
        --manifest  <platform-manifest.json> \\
        --environment <environment.json> \\
        --layer-r /srv/running-platform/cell/rp_runtime_adapter.py:build_layer_r_wiring

Production mode is selected visibly and explicitly: ``--layer-r`` is required,
and there is no default to fall back to. The declared owner-side entry point
supplies the ``RuntimeAdapter`` and the identity evidence source; the local
defaults of the engine (``LocalProcessRuntime`` and the D&O-owned
``<runtime_root>/running_platform_identity.json``) are not reachable from this
entry point.

``deploy`` realizes one accepted Platform Instance through those seams.
``reconcile`` and ``restart`` re-bind the already-realized operation first
(``attach``, the approved F-1 re-binding path) and then run the operation; a
restart keeps the existing ``StopStartPolicy`` — this entry point passes no
weakened policy, and it never stops a platform after a successful operation,
because the lifetime of the runtime belongs to Layer R, not to this caller.

``--check`` constructs and reports the composition without running any
operation: it is how an operator verifies a declaration before touching a
Running Platform.

Exit codes: 0 the operation completed, 2 the composition or the input was
refused before any operation existed, 3 the identity seam refused, 4 the
operation failed for another reason.

Secrets are operational inputs read from the process environment
(``--secret KEY=ENVVAR``) and are never recorded (ADR-0016 §12).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from deployment_operations.deployment import (
    DeploymentRequest,
    InstanceReference,
)
from deployment_operations.environment import load_environment
from deployment_operations.errors import (
    DeploymentOperationsError,
    DeploymentStateError,
    IdentityVerificationFailed,
)
from deployment_operations.production import (
    ProductionCompositionError,
    ProductionCompositionRoot,
    compose_production_root,
)
from deployment_operations.reconciliation import (
    ReconciliationRequest,
    derive_reconciliation_id,
)
from deployment_operations.restart import (
    STRICT_STOP_START_POLICY,
    RestartRequest,
    derive_restart_id,
)

#: Every transcript this entry point writes carries this classification.
CLASSIFICATION = "PRODUCTION COMPOSITION ROOT (Layer O) — wiring evidence"

#: What a transcript from this entry point does and does not establish.
EVIDENCE_NOTE = (
    "wiring evidence only: this record states which owner-side dependencies "
    "were injected. It is not production acceptance evidence — that requires a "
    "real Running Platform run and an independent D1-D8 conformance audit."
)

EXIT_COMPLETED = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_FAILED = 4

OPERATIONS = ("deploy", "reconcile", "restart")


def _read_document(path: Path) -> dict[str, object]:
    """Read one input document, refusing an unreadable or unusable one."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DeploymentStateError(
            f"{path}: the input document could not be read "
            f"({error.strerror or error.__class__.__name__})"
        ) from error
    except json.JSONDecodeError as error:
        raise DeploymentStateError(
            f"{path}: the input document is not valid JSON "
            f"(line {error.lineno}: {error.msg})"
        ) from error
    if not isinstance(document, dict):
        raise DeploymentStateError(f"{path}: a JSON object is required")
    return document


def _secrets(pairs: list[str]) -> dict[str, str]:
    """Read the declared operational secrets from their environment variables."""
    secrets: dict[str, str] = {}
    for pair in pairs:
        key, _, source = pair.partition("=")
        if not key or not source:
            raise DeploymentStateError(f"--secret expects KEY=ENVVAR, got {pair!r}")
        value = os.environ.get(source)
        if value is None:
            raise DeploymentStateError(
                f"--secret {key}: the environment variable {source!r} is not set"
            )
        secrets[key] = value
    return secrets


def _options(pairs: list[str]) -> dict[str, str]:
    """The declared construction options of the owner's factory."""
    options: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise DeploymentStateError(
                f"--layer-r-option expects KEY=VALUE, got {pair!r}"
            )
        options[key.strip()] = value
    return options


def _record_summary(record: object) -> dict[str, object]:
    failure = getattr(record, "failure", None)
    return {
        "deployment_id": getattr(record, "deployment_id", None),
        "platform_id": getattr(record, "platform_id", None),
        "environment_id": getattr(record, "environment_id", None),
        "attempt": getattr(record, "attempt", None),
        "lifecycle": getattr(record, "lifecycle", None),
        "ready": getattr(record, "ready", None),
        "running": getattr(record, "running", None),
        "identity_verified": getattr(record, "identity_verified", None),
        "deployed": getattr(record, "deployed", None),
        "stages": [
            {"name": stage.name, "status": stage.status}
            for stage in getattr(record, "stages", ())
        ],
        "failure": dataclasses.asdict(failure) if failure is not None else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deployment_operations.production",
        description=(
            "Run one Deployment & Operations operation against a declared, "
            "owner-operated Running Platform (production mode)."
        ),
    )
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--instance", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument(
        "--layer-r",
        required=True,
        metavar="<PATH-OR-MODULE>:<FACTORY>",
        help=(
            "the explicit owner-side entry point of the Running Platform; "
            "there is no default and no local fallback"
        ),
    )
    parser.add_argument(
        "--layer-r-option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a declared construction option of the owner-side factory",
    )
    parser.add_argument(
        "--digest",
        default=None,
        help=(
            "state the instance digest explicitly; it must name the loaded "
            "instance, in place of reading it from the document"
        ),
    )
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument(
        "--reason",
        default="explicit_runtime_management_request",
        help="restart only: the recorded reason of the attempt",
    )
    parser.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="KEY=ENVVAR",
        help="inject an operational secret read from an environment variable",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="construct and report the composition; run no operation",
    )
    parser.add_argument("--result-out", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)

    # -- the operator's inputs, read before anything is composed ------------
    try:
        instance_document = _read_document(arguments.instance)
        manifest_document = _read_document(arguments.manifest)
        environment = load_environment(
            _read_document(arguments.environment),
            base_dir=arguments.environment.resolve().parent,
        )
        options = _options(arguments.layer_r_option)
        secrets = _secrets(arguments.secret)
    except DeploymentOperationsError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write("no deployment operation was created\n")
        return EXIT_USAGE

    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "evidence_note": EVIDENCE_NOTE,
        "operation": arguments.operation,
    }

    # -- the production composition, before any operation exists ------------
    try:
        root = compose_production_root(arguments.layer_r, options)
    except ProductionCompositionError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write("no deployment operation was created\n")
        return EXIT_USAGE
    result["composition"] = root.document()
    if arguments.check:
        result["result"] = "composition checked; no operation was run"
        _write(result, arguments.result_out)
        return EXIT_COMPLETED

    if secrets:
        environment = dataclasses.replace(environment, secrets=secrets)

    try:
        reference = InstanceReference.from_document(instance_document)
    except DeploymentOperationsError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write("no deployment operation was created\n")
        return EXIT_USAGE
    if arguments.digest is not None and arguments.digest != reference.instance_digest:
        sys.stderr.write(
            f"--digest {arguments.digest!r} does not name the loaded instance "
            f"({reference.instance_digest!r}); a floating selector is refused\n"
        )
        sys.stderr.write("no deployment operation was created\n")
        return EXIT_USAGE
    request = DeploymentRequest(
        instance=reference,
        instance_document=instance_document,
        manifest_document=manifest_document,
        environment=environment,
        attempt=arguments.attempt,
    )

    try:
        return _run(root, request, arguments, result)
    except IdentityVerificationFailed as error:
        result["result"] = "refused"
        result["error_type"] = type(error).__name__
        result["errors"] = list(getattr(error, "errors", ())) or [str(error)]
        _write(result, arguments.result_out)
        return EXIT_REFUSED
    except DeploymentOperationsError as error:
        result["result"] = "failed"
        result["error_type"] = type(error).__name__
        result["errors"] = list(getattr(error, "errors", ())) or [str(error)]
        _write(result, arguments.result_out)
        return EXIT_FAILED


def _run(
    root: ProductionCompositionRoot,
    request: DeploymentRequest,
    arguments: argparse.Namespace,
    result: dict[str, object],
) -> int:
    """Run the selected operation through the production seams."""
    if arguments.operation == "deploy":
        deployment = root.deploy(request)
        result["result"] = "accepted" if deployment.deployed else "realized"
        result["record"] = _record_summary(deployment.record)
        _write(result, arguments.result_out)
        return EXIT_COMPLETED

    # ``reconcile`` and ``restart`` act on one already-realized operation: the
    # re-binding restores this process's reference to the standing Layer R.
    deployment = root.attach(request)
    result["attached"] = True
    if arguments.operation == "reconcile":
        reconciliation = root.reconcile(
            ReconciliationRequest(
                deployment=deployment,
                reconciliation_id=derive_reconciliation_id(deployment),
            )
        )
        result["result"] = "in_correspondence"
        result["outcome"] = reconciliation.outcome
        result["record"] = _record_summary(deployment.record)
        _write(result, arguments.result_out)
        return EXIT_COMPLETED

    restarted = root.restart(
        RestartRequest(
            deployment=deployment,
            restart_id=derive_restart_id(deployment),
            reason=str(arguments.reason),
            policy=STRICT_STOP_START_POLICY,
        )
    )
    result["result"] = "restarted"
    result["outcome"] = restarted.outcome
    result["record"] = _record_summary(deployment.record)
    _write(result, arguments.result_out)
    return EXIT_COMPLETED


def _write(result: dict[str, object], result_out: Path | None) -> None:
    payload = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write(payload + "\n")
    if result_out is not None:
        result_out.parent.mkdir(parents=True, exist_ok=True)
        result_out.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
