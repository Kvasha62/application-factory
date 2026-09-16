"""Run one deployment operation from the command line.

    python -m deployment_operations \\
        --instance factory/platform_instance/example_instance.json \\
        --manifest path/to/platform_manifest.json \\
        --environment path/to/environment.json

The command realizes exactly the instance it is given, in exactly the
environment it is given, and prints the resulting deployment state record: the
lifecycle position, the operational conditions, every component's observed
identity and version, the migrations that ran, and the operation's identity.

Secrets are read from the process environment (``--secret KEY=ENVVAR``) so they
never appear in a command line, a file or a record (ADR-0016 §12). The platform
is stopped again after the operation unless ``--keep-running`` is passed:
ongoing runtime management is a later stage (ADR-0017 §39), while the record of
what was realized stays in deployment state (ADR-0016 §9).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from deployment_operations.deployment import (
    DeploymentRequest,
    InstanceReference,
    deploy,
)
from deployment_operations.environment import load_environment
from deployment_operations.errors import DeploymentOperationsError

EXIT_OK = 0
EXIT_DEPLOYMENT_FAILED = 1
EXIT_USAGE = 2


def _read_document(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _secrets(pairs: list[str]) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for pair in pairs:
        key, _, source = pair.partition("=")
        if not key or not source:
            message = f"--secret expects KEY=ENVVAR, got {pair!r}"
            raise SystemExit(message)
        value = os.environ.get(source)
        if value is None:
            message = f"--secret {key}: the environment variable {source!r} is not set"
            raise SystemExit(message)
        secrets[key] = value
    return secrets


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deployment_operations",
        description=(
            "Realize one accepted Platform Instance (ADR-0016 §5, §8; " "ADR-0017 §42)."
        ),
    )
    parser.add_argument("--instance", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument(
        "--digest",
        default=None,
        help="pin the instance digest explicitly (defaults to the document's own)",
    )
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="leave the platform's runtime elements running after the operation",
    )
    parser.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="KEY=ENVVAR",
        help="inject an operational secret read from an environment variable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    instance_document = _read_document(arguments.instance)
    manifest_document = _read_document(arguments.manifest)
    environment_document = _read_document(arguments.environment)
    try:
        environment = load_environment(
            environment_document, base_dir=arguments.environment.resolve().parent
        )
    except DeploymentOperationsError as error:
        # No operation exists yet: an environment that cannot be read is a usage
        # failure, reported honestly, not a deployment that failed (ADR-0016 §11).
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write("no deployment operation was created\n")
        return EXIT_USAGE
    if arguments.secret:
        environment = replace(environment, secrets=_secrets(arguments.secret))

    reference = InstanceReference(
        instance_digest=arguments.digest
        or str(instance_document.get("instance_digest", "")),
        platform_id=instance_document.get("platform_id"),
    )
    request = DeploymentRequest(
        instance=reference,
        instance_document=instance_document,
        manifest_document=manifest_document,
        environment=environment,
        attempt=arguments.attempt,
    )

    try:
        deployment = deploy(request)
    except DeploymentOperationsError as error:
        sys.stderr.write(error.diagnostics() + "\n")
        sys.stderr.write(
            "deployment failed; the failed deployment state is at "
            f"{environment.operations_dir}\n"
        )
        return EXIT_DEPLOYMENT_FAILED

    record = deployment.record
    sys.stdout.write(record.render() + "\n")
    sys.stdout.write(
        f"deployment {record.deployment_id}: lifecycle={record.lifecycle} "
        f"ready={record.ready} running={record.running} "
        f"identity_verified={record.identity_verified} deployed={record.deployed}\n"
    )
    if not arguments.keep_running:
        deployment.stop()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
