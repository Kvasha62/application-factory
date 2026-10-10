"""S4 runbook ↔ implementation consistency (LD-1 regression).

The LD-1 audit (PR #138, Phase 1) found the accepted S4 runbook stating a
request shape no shipped code implements, a contamination-refusal sentence
stronger than the code and citing a stale anchor, and a drifted verified-anchor
list.  These tests pin the *agreement* between the runbook and the shipped
seam so the same classes of defect cannot return unnoticed:

* the request shape the runbook documents is the shape the real transport
  reader produces;
* the contamination claim cites the anchor where the refusal actually lives;
* the LD-1-critical line anchors of the runbook point at the cited definitions.

They read documentation and source as data and call the real
``request_body``; no production behaviour and no production evidence is
produced here.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from deployment_operations import deployment as deployment_module
from deployment_operations import platform_identity as platform_identity_module
from deployment_operations.platform_identity import PlatformIdentityBinding
from running_platform import owner_state as owner_state_module
from running_platform.http_owner_state import (
    BINDING_TOKEN_KEY,
    HttpRunningPlatformOwnerStateReader,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "docs" / "arena" / "S4-PRODUCTION-RUNTIME-BOOTSTRAP-RUNBOOK.md"


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _source_line(module: object, number: int) -> str:
    path = Path(inspect.getsourcefile(module) or "")
    return path.read_text(encoding="utf-8").splitlines()[number - 1]


def test_runbook_states_the_shipped_request_shape() -> None:
    """The documented request is exactly what the shipped reader sends."""

    text = _runbook()
    assert '{"binding_token":"<H>"}` (`POST /observe`)' in text
    assert '{"op":"observe","handle"' not in text
    assert '`{"op","handle"}`' not in text

    reader = HttpRunningPlatformOwnerStateReader(
        "http://127.0.0.1:1/observe", timeout_seconds=1.0
    )
    body = reader.request_body(PlatformIdentityBinding("ev-" + "a" * 32))
    assert body == b'{"binding_token": "ev-' + b"a" * 32 + b'"}'


def test_runbook_request_field_names_are_reconciled() -> None:
    """`handle`, `binding_token` and `correlation.token` are stated to be H."""

    text = _runbook()
    assert "Terminology: the same opaque value" in text
    assert BINDING_TOKEN_KEY == "binding_token"
    assert BINDING_TOKEN_KEY in text
    assert "`correlation.token`" in text


def test_runbook_contamination_anchor_matches_the_code() -> None:
    """The refusal claim cites where the refusal actually lives."""

    text = _runbook()
    assert "owner_state.py:117-120" in text
    assert "owner_state.py:72" not in text
    assert (
        "It does not currently reject every possible expected-derived field by name."
        in text
    )
    assert (
        "not as a verified guarantee of the current implementation." in text
    )
    assert (
        "MUST NOT emit deployment identifiers or other expected-derived material."
        in text
    )
    assert "refuse to accept, any document containing" not in text

    line = _source_line(owner_state_module, 117)
    assert '"instance_digest" in document' in line
    assert '"expected_instance" in document' in line


def test_runbook_verified_anchors_match_the_definitions() -> None:
    """The LD-1-critical `[V]` anchors still point at their definitions."""

    assert "EVALUATION_HANDLE_PREFIX" in _source_line(deployment_module, 194)
    assert "def new_evaluation_handle" in _source_line(deployment_module, 197)
    assert "def require_correlated_evidence" in _source_line(
        platform_identity_module, 339
    )
    text = _runbook()
    assert "`deployment.py:194-213`" in text
    assert "`platform_identity.py:339`" in text

    # The corrected S4 runbook anchors must continue to identify the
    # implementation they describe.
    assert "`deployment.py:841-848`" in text
    assert "`deployment.py:849`" in text
    assert "`deployment.py:984-1004`" in text

    assert "if identity_provider is None:" in _source_line(deployment_module, 841)
    assert "OwnerStateSnapshotSource(" in _source_line(deployment_module, 843)
    assert (
        "adapter: RuntimeAdapter = runtime or LocalProcessRuntime(source_paths=paths)"
        in _source_line(deployment_module, 849)
    )
    assert "_verify_running_platform_identity(" in _source_line(deployment_module, 984)
    assert "recorder.identity_verified()" in _source_line(deployment_module, 1004)
