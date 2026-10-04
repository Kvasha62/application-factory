"""Slice 2 determinism proofs: findings and documents are pure functions.

Proves: the frozen finding order and de-duplication hold under shuffled input;
finding ids are content-addressed and recomputable; the same inputs produce the
same bytes in-process twice and in two cold processes with different hash
seeds; and nothing in the Slice 2 sources can introduce wall-clock, random or
identifier-based variability.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from factory_control_plane import composition_requests, findings, proposals
from factory_control_plane.canonical import canonical_bytes
from factory_control_plane.findings import (
    CODE_COMPOSER_UNCERTIFIED,
    CODE_OPEN_REQUIREMENTS,
    CODE_PROJECT_NOT_ACTIVE,
    finding,
    finding_id,
    registry_fingerprint,
    sort_findings,
    warning_ids,
)
from slice2_fixtures import (
    APPROVER,
    DECIDED_AT,
    EXAMPLES,
    REASON,
    REPOSITORY_ROOT,
    SUBTREE_ROOT,
    build_proposal,
    build_v2,
)

PACKAGE_DIR = SUBTREE_ROOT / "factory_control_plane"
FORBIDDEN_IMPORTS = ("time", "datetime", "uuid", "random", "secrets", "os.urandom")


def _sample_findings():
    return [
        finding(
            CODE_OPEN_REQUIREMENTS, "warning", "demo_shop/configurations/2", "open"
        ),
        finding(CODE_PROJECT_NOT_ACTIVE, "error", "demo_shop", "archived"),
        finding(
            CODE_COMPOSER_UNCERTIFIED,
            "info",
            "demo_shop/configurations/2",
            "not certified",
            {"component_id": "identity"},
        ),
    ]


def test_finding_ids_are_content_addressed_and_recomputable():
    first = finding(CODE_OPEN_REQUIREMENTS, "warning", "target", "message")
    second = finding(CODE_OPEN_REQUIREMENTS, "warning", "target", "message")
    assert first == second
    assert first["finding_id"] == finding_id(first)
    assert first["finding_id"].startswith("fnd-")

    for changed in (
        finding(CODE_OPEN_REQUIREMENTS, "warning", "target", "another message"),
        finding(CODE_OPEN_REQUIREMENTS, "warning", "other-target", "message"),
        finding(CODE_OPEN_REQUIREMENTS, "info", "target", "message"),
        finding(CODE_COMPOSER_UNCERTIFIED, "warning", "target", "message"),
    ):
        assert changed["finding_id"] != first["finding_id"]

    # A rewritten id is never trusted: recomputation is the contract.
    forged = dict(first, finding_id="fnd-000000000000")
    assert finding_id(forged) == first["finding_id"]


def test_frozen_order_and_dedup_hold_under_shuffling():
    findings_ = _sample_findings()
    ordered = sort_findings(findings_)
    assert [item["severity"] for item in ordered] == ["error", "warning", "info"]

    import random

    for seed in range(5):
        shuffled = list(findings_)
        random.Random(seed).shuffle(shuffled)
        shuffled.extend(findings_[:2])  # duplicates must collapse
        assert sort_findings(shuffled) == ordered

    counts = findings.severity_counts(ordered)
    assert counts == {"error": 1, "warning": 1, "info": 1}
    assert warning_ids(ordered) == [
        item["finding_id"] for item in ordered if item["severity"] == "warning"
    ]


def test_registry_fingerprint_is_stable_and_an_input_only():
    first = registry_fingerprint(REPOSITORY_ROOT)
    second = registry_fingerprint(REPOSITORY_ROOT)
    assert first == second
    assert first is None or first.startswith("sha256:")
    assert (
        REPOSITORY_ROOT / "factory" / "registry" / "component_registry.json"
    ).is_file()
    # The fingerprint never substitutes for the registry's own facts.
    assert (
        first
        == "sha256:"
        + hashlib.sha256(
            (
                REPOSITORY_ROOT / "factory" / "registry" / "component_registry.json"
            ).read_bytes()
        ).hexdigest()
    )


def test_proposal_bytes_are_stable_in_process():
    first = build_proposal()
    second = build_proposal()
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["digest"] == second["digest"]
    assert (
        first["inputs"]["registry_fingerprint"]
        == second["inputs"]["registry_fingerprint"]
    )


def test_request_record_bytes_are_stable_in_process():
    from factory_control_plane.approvals import (
        ApprovalLedger,
        new_approval_record,
    )

    configuration = build_v2()
    proposal = build_proposal(configuration=configuration)
    approval = new_approval_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        decision="granted",
        approver=dict(APPROVER),
        decided_at=DECIDED_AT,
        reason=REASON,
        acknowledged_findings=warning_ids(proposal["findings"]),
    )
    ledger = ApprovalLedger.empty("demo_shop").append(approval)
    first = composition_requests.new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
    )
    second = composition_requests.new_request_record(
        project_ref="demo_shop",
        configuration=configuration,
        proposal=proposal,
        approval=approval,
        ledger=ledger,
        root=REPOSITORY_ROOT,
    )
    assert canonical_bytes(first) == canonical_bytes(second)
    assert composition_requests.request_fingerprint(
        first
    ) == composition_requests.request_fingerprint(second)


COLD_SCRIPT = """
import json
import sys
from pathlib import Path

from factory_control_plane import digests
from slice2_fixtures import build_proposal, build_v2

configuration = build_v2()
proposal = build_proposal(configuration=configuration)
print(json.dumps({
    "configuration_digest": configuration["digest"],
    "proposal_digest": proposal["digest"],
    "proposal_state": proposal["state"],
    "finding_ids": [item["finding_id"] for item in proposal["findings"]],
    "validator_set": proposal["inputs"]["validator_set"],
}, sort_keys=True))
"""


def _cold_probe(seed: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(Path(__file__).resolve().parent),
            str(SUBTREE_ROOT),
            str(REPOSITORY_ROOT / "src"),
        ]
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = seed
    completed = subprocess.run(
        [sys.executable, "-c", COLD_SCRIPT],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_cold_processes_with_different_hash_seeds_agree():
    first = _cold_probe("0")
    second = _cold_probe("12345")
    assert first == second

    local = build_proposal()
    assert first["proposal_digest"] == local["digest"]
    assert first["configuration_digest"] == build_v2()["digest"]
    assert first["finding_ids"] == [item["finding_id"] for item in local["findings"]]
    assert first["validator_set"] == proposals.VALIDATOR_SET


def test_slice2_sources_carry_no_wall_clock_or_randomness():
    sources = sorted(PACKAGE_DIR.glob("*.py"))
    assert sources
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_IMPORTS:
            assert (
                f"import {forbidden}" not in text
            ), f"{source.name} imports {forbidden}"
            assert (
                f"from {forbidden} import" not in text
            ), f"{source.name} imports {forbidden}"
        for mutator in (
            "write_text",
            "write_bytes",
            "os.remove",
            "shutil",
            "subprocess",
        ):
            assert mutator not in text, f"{source.name} can write or spawn: {mutator}"


def test_documents_contain_no_unrecorded_timestamps():
    """The only timestamp in a document is the approval's explicit decision time."""
    allowed = {"decided_at"}
    slice2_examples = ("v2_version", "proposal", "approval", "request")
    documents = [build_v2(), build_proposal()]
    documents.extend(
        json.loads(EXAMPLES[name].read_text(encoding="utf-8"))
        for name in slice2_examples
    )
    for document in documents:
        for key in _all_keys(document):
            if key.endswith("_at") or key in {"timestamp", "created", "updated"}:
                assert key in allowed, f"unexpected time field {key!r}"


def _all_keys(value, parent: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child, parent)
