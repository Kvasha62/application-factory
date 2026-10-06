#!/usr/bin/env python3
"""Prepare the local Running Platform definition and identity state.

LOCAL DOCKER / NON-PRODUCTION. This preparation is offline and one-shot, and it
produces both sides of the local stack before any evaluation runs:

* the Factory side — the Platform Manifest composed and validated by the real
  surfaces, and the Platform Instance assembled from it (the expected state of
  the local deployment operation);
* the owner side — the authoritative local identity state of the Running
  Platform stand-in, prepared from the same platform definition (manifest
  identity, platform id, the component set the platform runs, the configuration
  it serves), plus one deliberately foreign state used to exercise refusal.

Nothing here is production evidence and nothing here is a production identity:
the state document is a seeded stand-in for an owner's own authoritative state.

Usage::

    python local/tools/prepare_local_platform.py --work-dir local/work
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Sequence
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _LOCAL_DIR.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from composer import compose_request_document
from composer.request import build_request_document
from platform_instance import assemble_document, discover_root
from platform_manifest import compute_manifest_digest, validate_document

#: Every artifact this preparation writes carries this classification.
CLASSIFICATION = "LOCAL DOCKER / NON-PRODUCTION"

DEFAULT_PLATFORM_ID = "local-docker-platform"
DEFAULT_FOREIGN_PLATFORM_ID = "local-docker-platform-other"
DEFAULT_COMPONENT_ID = "tenant_authority"
DEFAULT_COMPONENT_VERSION = "0.1.0"
DEFAULT_ENVIRONMENT_ID = "local-docker-dno"
DEFAULT_MANIFEST_VERSION = "1.0.0"
#: The schema-valid component version the mismatch scenario reports: another
#: identity-bearing instance of the same platform, so the refusal comes from the
#: digest comparison (MISMATCH) and not from an invalid document.
MISMATCHED_COMPONENT_VERSION = "0.2.0"
VALIDATED_AT = "2026-10-06T00:00:00Z"

_OWNER_STATE_COMMENT = (
    f"{CLASSIFICATION}: authoritative local identity state of the local Running "
    "Platform stand-in. Prepared by local/tools/prepare_local_platform.py from "
    "the same platform definition the Factory used to compose the manifest and "
    "assemble the instance. It is not a production identity, not production "
    "evidence, and not owned by Deployment & Operations."
)

_ENVIRONMENT_COMMENT = (
    f"{CLASSIFICATION}: deployment environment of the local Docker stack. "
    "runtime_root is an absolute path inside the Deployment & Operations "
    "container; the stack deliberately keeps no running_platform_identity.json "
    "there — identity is obtained only across the transport boundary. Secrets "
    "are absent by design: they are operator inputs, never environment document "
    "content (ADR-0016 §12)."
)


def compose_manifest(
    *,
    manifest_id: str,
    platform_id: str,
    component_id: str,
    component_version: str,
    environment: str,
    root: Path,
) -> dict[str, object]:
    """Compose and validate one Platform Manifest through the real surfaces."""
    request = build_request_document(
        manifest_id=manifest_id,
        manifest_version=DEFAULT_MANIFEST_VERSION,
        components=[
            {"component_id": component_id, "component_version": component_version}
        ],
        configuration={
            component_id: {"platform_id": platform_id, "environment": environment}
        },
    )
    document = copy.deepcopy(
        dict(compose_request_document(request, root=root).document)
    )
    document["lifecycle"] = {"state": "validated"}
    document["validation_attestation"] = {
        "validated_by": "local-docker-preparation",
        "validated_at": VALIDATED_AT,
        "checks": ["schema", "registry", "composition"],
    }
    document["manifest_digest"] = compute_manifest_digest(document)
    errors = validate_document(document, root=root)
    if errors:
        message = f"composed manifest is invalid: {errors}"
        raise SystemExit(message)
    return document


#: The authority the local owner side declares for the observations it produces.
OBSERVATION_AUTHORITY = "running-platform-owner/local-docker-identity-service"
#: How the local owner side grounds an observation: its own authoritative state.
OBSERVATION_BASIS = "owner-identity-state/1"


def owner_identity_state(
    manifest: dict[str, object],
    *,
    platform_id: str,
    state_comment: str,
    binding_scope: str,
) -> dict[str, object]:
    """Build the owner-side authoritative identity state for a platform.

    The state is the owner's own declaration: the platform identity it observed,
    the binding space it serves (``binding_scope``), and the attribution of any
    observation it produces from this state (authority, basis). Nothing here is
    supplied by the evaluator.
    """
    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        message = "the platform definition pins no configuration"
        raise SystemExit(message)
    components = manifest.get("components")
    if not isinstance(components, list):
        message = "the platform definition names no component set"
        raise SystemExit(message)
    return {
        "$comment": state_comment,
        "platform_id": platform_id,
        "manifest": {
            "manifest_id": manifest["manifest_id"],
            "manifest_version": manifest["manifest_version"],
            "manifest_digest": manifest["manifest_digest"],
        },
        "manifest_state": manifest["lifecycle"]["state"],  # type: ignore[index]
        "components": [
            {
                "component_id": component["component_id"],
                "component_version": component["component_version"],
                "artifact_identity": copy.deepcopy(component["artifact"]),
            }
            for component in components
        ],
        "membership_established": True,
        "configuration": {"state": "PRESENT", "value": copy.deepcopy(configuration)},
        "golden_bundle": {"state": "ABSENT", "value": None},
        "golden_bundle_inventory_established": True,
        "extensions": {"state": "ABSENT", "value": None},
        "branding": {"state": "ABSENT", "value": None},
        "provenance": "MEASURED",
        "binding_scope": binding_scope,
        "observation_sequence": 1,
        "observation_authority": OBSERVATION_AUTHORITY,
        "observation_basis": OBSERVATION_BASIS,
        "freshness_current": True,
    }


def mismatched_identity_state(
    state: dict[str, object],
    *,
    component_version: str,
) -> dict[str, object]:
    """The same platform identity with different identity-bearing content.

    Used only to prove that an established correlation is not acceptance: a
    producer that is correctly correlated but reports another component version
    must be refused by the digest comparison, never accepted. The version stays
    schema-valid, so the refusal is the digest comparison itself.
    """
    document = copy.deepcopy(state)
    components = document.get("components")
    if not isinstance(components, list) or not components:
        message = "identity state names no component set to vary"
        raise SystemExit(message)
    first = components[0]
    if not isinstance(first, dict):
        message = "identity state component record is malformed"
        raise SystemExit(message)
    first["component_version"] = component_version
    document["$comment"] = (
        f"{CLASSIFICATION}: the same platform identity with a different "
        "identity-bearing component version (mismatch scenario)."
    )
    return document


def environment_document(
    *,
    environment_id: str,
    runtime_root: Path,
    component_id: str,
) -> dict[str, object]:
    """Build the deployment environment document of the local stack."""
    return {
        "$comment": _ENVIRONMENT_COMMENT,
        "environment_id": environment_id,
        "runtime_root": str(runtime_root),
        "probe_timeout_seconds": 120.0,
        "bindings": [
            {
                "component_id": component_id,
                "deployment_module": f"{component_id}.deployment",
                "deployment_factory": "build_deployment",
            }
        ],
        # The instance pins every configuration key it declares; an overlay
        # that pinned one of them would be refused by the environment loader,
        # so this local environment adds nothing to the desired state.
        "configuration_overlay": {},
    }


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def prepare(
    work_dir: Path,
    *,
    platform_id: str = DEFAULT_PLATFORM_ID,
    foreign_platform_id: str = DEFAULT_FOREIGN_PLATFORM_ID,
    component_id: str = DEFAULT_COMPONENT_ID,
    component_version: str = DEFAULT_COMPONENT_VERSION,
    environment_id: str = DEFAULT_ENVIRONMENT_ID,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    """Write both sides of the local stack under ``work_dir``."""
    root = discover_root()
    work_dir = work_dir.resolve()
    dno_dir = work_dir / "dno"
    rp_dir = work_dir / "rp"
    resolved_runtime_root = (
        runtime_root.resolve() if runtime_root is not None else dno_dir / "runtime"
    )

    manifest = compose_manifest(
        manifest_id=platform_id,
        platform_id=platform_id,
        component_id=component_id,
        component_version=component_version,
        environment=f"{environment_id}",
        root=root,
    )
    instance = assemble_document(manifest, platform_id=platform_id, root=root)
    foreign_manifest = compose_manifest(
        manifest_id=foreign_platform_id,
        platform_id=foreign_platform_id,
        component_id=component_id,
        component_version=component_version,
        environment=f"{environment_id}",
        root=root,
    )

    environment = environment_document(
        environment_id=environment_id,
        runtime_root=resolved_runtime_root,
        component_id=component_id,
    )
    _write(dno_dir / "platform-manifest.json", manifest)
    _write(dno_dir / "platform-instance.json", dict(instance.document))
    _write(dno_dir / "environment.json", environment)
    owner_state = owner_identity_state(
        manifest,
        platform_id=platform_id,
        state_comment=_OWNER_STATE_COMMENT,
        binding_scope=environment_id,
    )
    _write(rp_dir / "identity.json", owner_state)
    _write(
        rp_dir / "identity-foreign.json",
        owner_identity_state(
            foreign_manifest,
            platform_id=foreign_platform_id,
            state_comment=(
                f"{CLASSIFICATION}: authoritative local identity state of a "
                "different platform, used only to exercise foreign-identity "
                "refusal at the D&O acceptance seam."
            ),
            binding_scope=environment_id,
        ),
    )
    _write(
        rp_dir / "identity-mismatch.json",
        mismatched_identity_state(
            owner_state, component_version=MISMATCHED_COMPONENT_VERSION
        ),
    )
    summary: dict[str, object] = {
        "classification": CLASSIFICATION,
        "work_dir": str(work_dir),
        "platform_id": platform_id,
        "foreign_platform_id": foreign_platform_id,
        "component": f"{component_id}@{component_version}",
        "environment_id": environment_id,
        "runtime_root": str(resolved_runtime_root),
        "manifest_digest": manifest["manifest_digest"],
        "instance_digest": instance.document["instance_digest"],
        "documents": {
            "manifest": str(dno_dir / "platform-manifest.json"),
            "instance": str(dno_dir / "platform-instance.json"),
            "environment": str(dno_dir / "environment.json"),
            "owner_state": str(rp_dir / "identity.json"),
            "owner_state_foreign": str(rp_dir / "identity-foreign.json"),
            "owner_state_mismatch": str(rp_dir / "identity-mismatch.json"),
        },
    }
    _write(work_dir / "preparation.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=_LOCAL_DIR / "work")
    parser.add_argument("--platform-id", default=DEFAULT_PLATFORM_ID)
    parser.add_argument("--foreign-platform-id", default=DEFAULT_FOREIGN_PLATFORM_ID)
    parser.add_argument("--component-id", default=DEFAULT_COMPONENT_ID)
    parser.add_argument("--component-version", default=DEFAULT_COMPONENT_VERSION)
    parser.add_argument("--environment-id", default=DEFAULT_ENVIRONMENT_ID)
    parser.add_argument("--runtime-root", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = prepare(
        args.work_dir,
        platform_id=args.platform_id,
        foreign_platform_id=args.foreign_platform_id,
        component_id=args.component_id,
        component_version=args.component_version,
        environment_id=args.environment_id,
        runtime_root=args.runtime_root,
    )
    sys.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
