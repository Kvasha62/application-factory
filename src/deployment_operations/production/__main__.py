"""Run one Deployment & Operations operation in production mode.

    python -m deployment_operations.production deploy \\
        --instance  <platform-instance.json> \\
        --manifest  <platform-manifest.json> \\
        --environment <environment.json> \\
        --layer-r /srv/running-platform/cell/rp_runtime_adapter.py:<factory>

``<factory>`` is the owner's own factory in the owner's own module: this
repository neither names it nor guesses it.

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

The entry point serializes mutations of one deployment operation across
independent processes (``--lock-timeout`` bounds the wait) and holds that
serialization across ``attach`` and the operation it precedes, so two processes
can never interleave between the re-binding and what it re-binds for.

``--check`` constructs and reports the composition without running any
operation: it is how an operator verifies a declaration before touching a
Running Platform. It resolves no operational secret: secrets are read from the
environment only for an operation that is actually run (ADR-0016 §12).

Exit codes: 0 the operation completed, 2 the input, the composition or the
requested attempt was refused before any operation existed (including a refused
mutation lock), 3 the identity seam refused, 4 the operation failed for another
reason. A refusal is never reported as a failure of an operation that did
something, and an identity refusal is never reported as a generic failure: the
codes state what the record on disk can show.

A failure is reported as a controlled diagnostic — the error type and its
reasons, never a raw traceback — and the same diagnostic is recorded in the
transcript. ``--traceback`` prints the full traceback of an unexpected failure
for an operator who asks for it; it is off by default.

``--result-out`` writes the transcript to a file. The path is resolved and
checked before any operation exists: it may never name an input document, the
deployment's runtime root (deployment state, event journal, runtime workspaces
and mutation locks live there) or the owner's declared module. A rejected path
starts nothing. A *write* that fails after the operation completed is reported
as exactly that — a report that could not be written, not a failed operation —
and it names the record that already holds the outcome instead of inviting a
blind retry.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from deployment_operations.deployment import (
    DeploymentRequest,
    InstanceReference,
)
from deployment_operations.environment import load_environment
from deployment_operations.errors import (
    DeploymentOperationsError,
    IdentityVerificationFailed,
    ReconciliationEvidenceUnavailable,
    RestartFailed,
)
from deployment_operations.production import (
    ProductionCompositionError,
    ProductionCompositionRoot,
    ProductionOperationRefused,
    compose_production_root,
)
from deployment_operations.production.layer_r import parse_layer_r_reference
from deployment_operations.production.locking import DEFAULT_LOCK_TIMEOUT_SECONDS
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

_NO_OPERATION = "no deployment operation was created"


def _read_document(path: Path) -> dict[str, object]:
    """Read one input document, refusing an unreadable or unusable one.

    A file that is not UTF-8, not JSON or not a JSON object is an input
    refusal: reported, never traced back, and refused before anything is
    composed or addressed (ADR-0016 §8).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ProductionOperationRefused(
            [
                (
                    f"{path}: the input document is not valid UTF-8 "
                    f"(byte {error.start} cannot be decoded)"
                )
            ]
        ) from error
    except OSError as error:
        raise ProductionOperationRefused(
            [
                (
                    f"{path}: the input document could not be read "
                    f"({error.strerror or error.__class__.__name__})"
                )
            ]
        ) from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProductionOperationRefused(
            [
                (
                    f"{path}: the input document is not valid JSON "
                    f"(line {error.lineno}: {error.msg})"
                )
            ]
        ) from error
    if not isinstance(document, dict):
        raise ProductionOperationRefused([f"{path}: a JSON object is required"])
    return document


def _secrets(pairs: list[str]) -> dict[str, str]:
    """Read the declared operational secrets from their environment variables."""
    secrets: dict[str, str] = {}
    for pair in pairs:
        key, _, source = pair.partition("=")
        if not key or not source:
            raise ProductionOperationRefused(
                [f"--secret expects KEY=ENVVAR, got {pair!r}"]
            )
        value = os.environ.get(source)
        if value is None:
            raise ProductionOperationRefused(
                [
                    (
                        f"--secret {key}: the environment variable {source!r} is "
                        "not set; the operation was not started"
                    )
                ]
            )
        secrets[key] = value
    return secrets


def _options(pairs: list[str]) -> dict[str, str]:
    """The declared construction options of the owner's factory."""
    options: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise ProductionOperationRefused(
                [f"--layer-r-option expects KEY=VALUE, got {pair!r}"]
            )
        options[key.strip()] = value
    return options


def _same_path(left: Path, right: Path) -> bool:
    """True when two paths name the same file, directly or through link/``..``.

    ``resolve()`` normalizes symlinks and ``..``; ``os.path.samefile`` catches
    what a path comparison cannot — a hard link to the same content, and a
    symlink that exists on only one side of the comparison.
    """
    try:
        if left.resolve() == right.resolve():
            return True
    except OSError:  # pragma: no cover - an unresolvable path comparison
        return False
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:  # pragma: no cover - a race between exists and samefile
        return False


def _refuse_result_out(
    result_out: Path | None,
    *,
    inputs: Sequence[Path],
    runtime_root: Path,
    declared_target: str,
) -> None:
    """Refuse a ``--result-out`` that names authoritative state (ADR-0016 §9).

    Authoritative state is deployment state, the event journal, the runtime
    workspaces and the mutation locks — all inside the deployment's runtime
    root — the operator's input documents, and the owner's declared module.
    The check runs before any operation exists: a rejected path starts nothing,
    imports nothing and writes nothing.
    """
    if result_out is None:
        return
    errors: list[str] = []
    root = runtime_root.resolve()
    if result_out.is_relative_to(root):
        errors.append(
            f"--result-out {result_out} is inside the runtime root {root}: "
            "deployment state, the event journal, the runtime workspaces and "
            "the mutation locks live there, and no result transcript may be "
            "written over them"
        )
    elif root.is_relative_to(result_out):
        errors.append(
            f"--result-out {result_out} contains the runtime root {root}: a "
            "result transcript is a file, and this path would replace the "
            "deployment's own operational directory"
        )
    for path in inputs:
        if _same_path(result_out, path):
            errors.append(
                f"--result-out {result_out} names the input document {path} "
                "this invocation reads; writing the transcript would overwrite "
                "an operator's input"
            )
    target = declared_target
    if "/" in target or target.endswith(".py"):
        declared = Path(target)
        if declared.is_absolute() and _same_path(result_out, declared):
            errors.append(
                f"--result-out {result_out} names the declared owner-side "
                f"module {declared}; the Running Platform's own code is not "
                "this operation's output file"
            )
    if errors:
        raise ProductionOperationRefused(errors)


def _refuse_result_out_over_module(result_out: Path | None, module_path: Path) -> None:
    """Refuse a ``--result-out`` that names the loaded owner-side module."""
    if result_out is not None and _same_path(result_out, module_path):
        raise ProductionOperationRefused(
            [
                (
                    f"--result-out {result_out} names the declared owner-side "
                    f"module {module_path}; the Running Platform's own code is "
                    "not this operation's output file"
                )
            ]
        )


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


def _identity_refusal(error: BaseException) -> DeploymentOperationsError | None:
    """The identity-evidence refusal this failure is, if it is one (§10, §20).

    A refusal is about *identity evidence* in three shapes, and each of them is
    something other than a generic operation failure: an identity verification
    that failed, a restart attempt that stopped in its ``identity_verification``
    phase, and a reconciliation that could not establish actual state from
    independent evidence. Classifying any of them as "the operation failed"
    would hide the one thing the record actually shows. The whole causal chain
    is inspected, so a wrapped refusal is still reported as a refusal.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current, (IdentityVerificationFailed, ReconciliationEvidenceUnavailable)
        ):
            return current
        if (
            isinstance(current, RestartFailed)
            and current.phase == "identity_verification"
        ):
            return IdentityVerificationFailed(
                list(current.errors) or [str(current)],
                stage="identity_verification",
            )
        current = current.__cause__ or current.__context__
    return None


def _emit(
    result: dict[str, object],
    result_out: Path | None,
    *,
    completed: bool,
) -> None:
    """Write the transcript; a write that failed is reported, never raised.

    The transcript is not the operation: an operation that completed stays
    completed when stdout or ``--result-out`` cannot be written, and the record
    of what it did is deployment state. The warning says so, and it says why a
    blind retry is the wrong answer — the attempt is refused as a duplicate, and
    re-executing a runtime is the explicit restart operation.
    """
    payload = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    problems: list[str] = []
    try:
        sys.stdout.write(payload + "\n")
    except OSError as error:
        problems.append(
            "the transcript could not be written to stdout "
            f"({error.__class__.__name__}: {error})"
        )
    if result_out is not None:
        try:
            result_out.parent.mkdir(parents=True, exist_ok=True)
            result_out.write_text(payload + "\n", encoding="utf-8")
        except OSError as error:
            problems.append(
                f"--result-out {result_out} could not be written "
                f"({error.__class__.__name__}: {error})"
            )
    for problem in problems:
        sys.stderr.write(problem + "\n")
    if not problems:
        return
    if completed:
        sys.stderr.write(
            "the operation itself completed; its outcome is recorded in "
            "deployment state, which holds what was done. Do not retry blindly: "
            "deploying the same attempt again is refused as a duplicate, and "
            "re-executing a runtime is the explicit restart operation.\n"
        )
    else:
        sys.stderr.write(
            "the operation did not complete; deployment state holds what was "
            "reached, and only that state says what can be done next.\n"
        )


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
        "--lock-timeout",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "how long to wait for another process mutating the same deployment "
            "operation before refusing (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="construct and report the composition; run no operation",
    )
    parser.add_argument(
        "--traceback",
        action="store_true",
        help="print the full traceback of an unexpected failure",
    )
    parser.add_argument(
        "--result-out",
        type=Path,
        default=None,
        help="write the transcript to this file (never over authoritative state)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)

    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "evidence_note": EVIDENCE_NOTE,
        "operation": arguments.operation,
    }

    # -- the operator's inputs, read before anything is composed ------------
    try:
        instance_document = _read_document(arguments.instance)
        manifest_document = _read_document(arguments.manifest)
        environment = load_environment(
            _read_document(arguments.environment),
            base_dir=arguments.environment.resolve().parent,
        )
        options = _options(arguments.layer_r_option)
    except DeploymentOperationsError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write(_NO_OPERATION + "\n")
        return EXIT_USAGE

    # -- the output path, resolved and refused before anything starts -------
    try:
        result_out = (
            arguments.result_out.resolve() if arguments.result_out is not None else None
        )
        _refuse_result_out(
            result_out,
            inputs=(arguments.instance, arguments.manifest, arguments.environment),
            runtime_root=environment.runtime_root,
            declared_target=parse_layer_r_reference(arguments.layer_r)[0],
        )
    except DeploymentOperationsError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write(_NO_OPERATION + "\n")
        return EXIT_USAGE

    # -- the production composition, before any operation exists ------------
    try:
        root = compose_production_root(arguments.layer_r, options)
        _refuse_result_out_over_module(result_out, root.layer_r.module_path)
    except ProductionCompositionError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write(_NO_OPERATION + "\n")
        return EXIT_USAGE
    except ProductionOperationRefused as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write(_NO_OPERATION + "\n")
        return EXIT_USAGE
    result["composition"] = root.document()
    if arguments.check:
        result["result"] = "composition checked; no operation was run"
        _emit(result, result_out, completed=True)
        return EXIT_COMPLETED

    # -- the operation's own inputs: secrets are read only for an operation --
    if arguments.attempt < 1:
        sys.stderr.write("--attempt counts from 1; this attempt was refused\n")
        sys.stderr.write(_NO_OPERATION + "\n")
        return EXIT_USAGE
    try:
        secrets = _secrets(arguments.secret)
        reference = InstanceReference.from_document(instance_document)
    except DeploymentOperationsError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write(_NO_OPERATION + "\n")
        return EXIT_USAGE
    if arguments.digest is not None and arguments.digest != reference.instance_digest:
        sys.stderr.write(
            f"--digest {arguments.digest!r} does not name the loaded instance "
            f"({reference.instance_digest!r}); a floating selector is refused\n"
        )
        sys.stderr.write(_NO_OPERATION + "\n")
        return EXIT_USAGE
    if secrets:
        environment = dataclasses.replace(environment, secrets=secrets)
    request = DeploymentRequest(
        instance=reference,
        instance_document=instance_document,
        manifest_document=manifest_document,
        environment=environment,
        attempt=arguments.attempt,
    )

    try:
        return _run(root, request, arguments, result, result_out)
    except Exception as error:  # noqa: BLE001 - the CLI boundary is total
        return _report_failure(error, arguments, result, result_out)


def _report_failure(
    error: Exception,
    arguments: argparse.Namespace,
    result: dict[str, object],
    result_out: Path | None,
) -> int:
    """Classify a failure of the operation layer and report it (exit 2/3/4).

    Nothing escapes this boundary as a traceback: an unexpected exception is a
    controlled diagnostic and exit 4, its original type and message visible, and
    the full traceback only when the operator asked for it with ``--traceback``.
    """
    refusal = _identity_refusal(error)
    if refusal is not None:
        result["result"] = "refused"
        result["error_type"] = type(error).__name__
        result["identity_refusal"] = True
        result["errors"] = list(refusal.errors) or [str(refusal)]
        sys.stderr.write(refusal.diagnostics() + "\n")
        _emit(result, result_out, completed=False)
        return EXIT_REFUSED
    if isinstance(error, ProductionOperationRefused):
        result["result"] = "refused"
        result["error_type"] = type(error).__name__
        result["errors"] = list(error.errors) or [str(error)]
        sys.stderr.write(error.diagnostics() + "\n")
        _emit(result, result_out, completed=False)
        return EXIT_USAGE
    if isinstance(error, DeploymentOperationsError):
        result["result"] = "failed"
        result["error_type"] = type(error).__name__
        result["errors"] = list(error.errors) or [str(error)]
        sys.stderr.write(error.diagnostics() + "\n")
        if arguments.traceback:
            sys.stderr.write(traceback.format_exc())
        _emit(result, result_out, completed=False)
        return EXIT_FAILED
    result["result"] = "failed"
    result["error_type"] = type(error).__name__
    result["errors"] = [f"{type(error).__name__}: {error}"]
    sys.stderr.write(
        "the operation failed with an unexpected error "
        f"({type(error).__name__}: {error}); deployment state holds what was "
        "reached, and this entry point does not retry or repair it\n"
    )
    if arguments.traceback:
        sys.stderr.write(traceback.format_exc())
    _emit(result, result_out, completed=False)
    return EXIT_FAILED


def _run(
    root: ProductionCompositionRoot,
    request: DeploymentRequest,
    arguments: argparse.Namespace,
    result: dict[str, object],
    result_out: Path | None,
) -> int:
    """Run the selected operation through the production seams, serialized.

    The lock is held across ``attach`` and the operation it precedes: a second
    process either waits for the whole unit or is refused, and can never
    interleave between the re-binding and what it re-binds for.
    """
    with root.mutation(request, timeout=arguments.lock_timeout):
        if arguments.operation == "deploy":
            deployment = root.deploy(request)
            result["result"] = "accepted" if deployment.deployed else "realized"
            result["record"] = _record_summary(deployment.record)
            _emit(result, result_out, completed=True)
            return EXIT_COMPLETED

        # ``reconcile`` and ``restart`` act on one already-realized operation:
        # the re-binding restores this process's reference to the standing
        # Layer R.
        deployment = root.attach(request)
        result["attached"] = True
        if arguments.operation == "reconcile":
            reconciliation = root.reconcile(
                ReconciliationRequest(
                    deployment=deployment,
                    reconciliation_id=derive_reconciliation_id(deployment),
                )
            )
            result["result"] = "observed"
            result["outcome"] = reconciliation.outcome
            result["record"] = _record_summary(deployment.record)
            _emit(result, result_out, completed=True)
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
        _emit(result, result_out, completed=True)
        return EXIT_COMPLETED


if __name__ == "__main__":
    raise SystemExit(main())
