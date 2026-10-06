#!/usr/bin/env python3
"""D&O-side local runner: one real deployment operation across the boundary.

LOCAL DOCKER / NON-PRODUCTION. This runs the repository's own deployment
operation (:func:`deployment_operations.deploy`) against the local Running
Platform stand-in, with the identity provider composed over the local transport
boundary::

    OwnerSuppliedPlatformIdentityProvider(
        OwnerStateSnapshotSource(
            HttpRunningPlatformOwnerStateReader(endpoint, timeout_seconds=...)))

Nothing else about the D&O side is special: the operation, its acceptance seam,
every provenance/freshness/correlation refusal and its fail-closed behaviour are
the shipped ones. The local runtime root deliberately holds no
``running_platform_identity.json``: identity is obtained only across the
transport, and whether such an ambient file exists is reported in the result.

Exit codes: 0 accepted, 3 refused at the identity seam, 4 the operation failed
for another reason.

Usage::

    python local/dno/run_deployment.py \
        --instance local/work/dno/platform-instance.json \
        --manifest local/work/dno/platform-manifest.json \
        --environment local/work/dno/environment.json \
        --identity-endpoint http://127.0.0.1:8080/observe \
        --identity-timeout 2.0 --result-out local/work/result-correct.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _LOCAL_DIR.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from deployment_operations import (
    DeploymentOperationsError,
    DeploymentRequest,
    DeploymentStateStore,
    IdentityVerificationFailed,
    InstanceReference,
    deploy,
    derive_deployment_id,
    load_environment,
    load_record,
)
from deployment_operations.deployment import (
    AUTHORITY_DEPLOY,
    deployment_record_basis,
    evaluation_binding,
    new_evaluation_handle,
)
from deployment_operations.platform_identity import (
    compute_actual_digest,
    establish_identity_correspondence,
)
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from running_platform.http_owner_state import (
    HttpRunningPlatformOwnerStateReader,
)
from running_platform.owner_state import OwnerStateSnapshotSource

#: Every result this runner writes carries this classification.
CLASSIFICATION = "LOCAL DOCKER / NON-PRODUCTION"

AMBIENT_IDENTITY_FILENAME = "running_platform_identity.json"

EXIT_ACCEPTED = 0
EXIT_REFUSED = 3
EXIT_FAILED = 4


def _read_document(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        message = f"{path}: a JSON object is required"
        raise SystemExit(message)
    return document


def _record_summary(record: object) -> dict[str, object]:
    failure = getattr(record, "failure", None)
    return {
        "deployment_id": getattr(record, "deployment_id", None),
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


def _read_failed_record(
    environment: object, instance_document: dict[str, object], attempt: int
) -> dict[str, object] | None:
    deployment_id = derive_deployment_id(
        instance_document.get("platform_id"),
        instance_document.get("instance_digest"),
        environment.environment_id,  # type: ignore[attr-defined]
        attempt,
    )
    path = DeploymentStateStore.path_for(
        environment.operations_dir,  # type: ignore[attr-defined]
        deployment_id,
    )
    if not path.is_file():
        return None
    return _record_summary(load_record(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--identity-endpoint", required=True)
    parser.add_argument("--identity-timeout", type=float, default=2.0)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--result-out", type=Path, default=None)
    args = parser.parse_args(argv)

    instance_document = _read_document(args.instance)
    manifest_document = _read_document(args.manifest)
    environment = load_environment(
        _read_document(args.environment), base_dir=args.environment.parent
    )
    reader = HttpRunningPlatformOwnerStateReader(
        args.identity_endpoint, timeout_seconds=args.identity_timeout
    )
    provider = OwnerSuppliedPlatformIdentityProvider(OwnerStateSnapshotSource(reader))
    ambient_identity = environment.runtime_root / AMBIENT_IDENTITY_FILENAME

    request = DeploymentRequest(
        instance=InstanceReference.from_document(instance_document),
        instance_document=instance_document,
        manifest_document=manifest_document,
        environment=environment,
        attempt=args.attempt,
    )
    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "operation": "deploy",
        "identity": {
            "endpoint": reader.endpoint,
            "declared_bound_seconds": reader.timeout_seconds,
            "ambient_identity_file": str(ambient_identity),
            "ambient_identity_file_present": ambient_identity.exists(),
            "observations": reader.observations,
        },
        "expected_instance_digest": instance_document.get("instance_digest"),
    }
    deployed = False
    started = time.monotonic()
    try:
        deployment = deploy(request, identity_provider=provider)
        deployed = True
        record = deployment.record
        result["result"] = "accepted"
        result["record"] = _record_summary(record)
        # Post-acceptance correspondence check: a second observation, made
        # explicitly, shows the digest the shipped acceptance seam computed.
        binding = evaluation_binding(
            authority=AUTHORITY_DEPLOY,
            token=new_evaluation_handle(),
            basis=deployment_record_basis(record.deployment_id),
            target=str(record.platform_id),
            scope=record.environment_id,
            sequence=record.attempt,
            established_at=record.updated_at,
        )
        evidence = provider.observe_identity(binding)
        actual = compute_actual_digest(evidence)
        result["actual_instance_digest"] = actual
        result["correspondence"] = establish_identity_correspondence(
            instance_document, evidence, binding=binding
        )
        correlation = evidence.correlation
        result["correlation"] = {
            "token": correlation.token,
            "scope": correlation.scope,
            "sequence": correlation.sequence,
            "target": correlation.target,
            "authority": correlation.authority,
            "basis": correlation.basis,
            "established_at": correlation.established_at,
        }
    except IdentityVerificationFailed as error:
        result["result"] = "refused"
        result["error_type"] = type(error).__name__
        result["errors"] = list(getattr(error, "errors", ()))
        record_summary = _read_failed_record(
            environment, instance_document, args.attempt
        )
        result["record"] = record_summary
    except DeploymentOperationsError as error:
        result["result"] = "failed"
        result["error_type"] = type(error).__name__
        result["errors"] = list(getattr(error, "errors", ())) or [str(error)]
        result["record"] = _read_failed_record(
            environment, instance_document, args.attempt
        )
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["identity"]["observations"] = reader.observations
    result["identity"]["observations_made"] = len(reader.observations)
    payload = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write(payload + "\n")
    if args.result_out is not None:
        args.result_out.parent.mkdir(parents=True, exist_ok=True)
        args.result_out.write_text(payload + "\n", encoding="utf-8")
    if deployed:
        return EXIT_ACCEPTED
    if result.get("result") == "refused":
        return EXIT_REFUSED
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
