"""Boundary proof of SCS-001 Learning — canonicalized Slice 1 / 2 / 3 baseline.

No new identity/tenant/authz mechanism was introduced:

* no module of ``learning_service`` imports another component;
* a dependency that answers nothing makes every access impossible;
* the resource tenant in the authorization question is always the store's
  fact, never a caller claim;
* the published surface offers exactly the enforced operations — the two
  reads and the review command — and reaches no internal;
* every published route refuses without an ALLOW;
* only the approved error envelope is accepted — any other refusal shape
  fails closed.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from learning_service import transport
from learning_service.consumed import DENY_REASONS, DecisionAnswer, DependencyRefusal
from learning_service.contracts import OPERATION_LIST, OPERATION_READ, OwnDenyReason
from learning_service.deployment import LearningDeployment, build_deployment
from learning_service.engine import LearningEngine
from learning_service.errors import AccessRefused, ContractViolation
from learning_service.models import OwnedAssignment, OwnedSubmission
from learning_service.reader import LearningClient
from learning_service.store import LearningStore
from tests.conftest import PLATFORM_ID

TEACHER_A = "token-human-a"

STATE_DUNDERS = {"__closure__", "__func__", "__self__", "__defaults__", "__dict__"}

FORBIDDEN_NAMES = (
    "store",
    "engine",
    "config",
    "http_app",
    "app",
    "deployment",
    "assignments",
    "submissions",
    "audit",
    "_store",
    "_engine",
    "_deployment",
    "_app",
)

INTERNAL_TYPES = (
    FastAPI,
    LearningDeployment,
    LearningEngine,
    LearningStore,
    transport.ASGIContractTransport,
)


class CountingLearningStore(LearningStore):
    """A learning store that counts the owned-data operations."""

    def __init__(self) -> None:
        super().__init__()
        self.served_reads = 0
        self.served_lists = 0

    def serve_submission(self, submission_id: str) -> Any:
        served = super().serve_submission(submission_id)
        self.served_reads += 1
        return served

    def list_submissions_for_assignment(self, assignment_id: str) -> Any:
        served = super().list_submissions_for_assignment(assignment_id)
        self.served_lists += 1
        return served


class StubAuthorizationPort:
    """A decision port with a fixed outcome: a value to return or an exception."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def decide(
        self,
        subject_credential,
        *,
        operation,
        resource_type,
        resource_id,
        resource_tenant_id,
        claimed_tenant_id=None,
        request_id=None,
        correlation_id=None,
    ):
        self.calls.append(
            {
                "operation": operation,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_tenant_id": resource_tenant_id,
                "claimed_tenant_id": claimed_tenant_id,
            }
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def learning_deployment(
    port: Any,
    *,
    store: LearningStore | None = None,
    seed_demo: bool = True,
) -> LearningDeployment:
    """Standalone Learning deployment over a given decision port."""
    deployment = build_deployment(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        authorization=port,
        store=store,
        seed_demo=False,
        with_http=True,
    )
    if seed_demo and store is None:
        deployment.store.seed_demo()
    elif seed_demo and store is not None and not store.assignments:
        store.seed_demo()
    return deployment


def deny(reason: str) -> DecisionAnswer:
    return DecisionAnswer(
        decision="DENY",
        reason=reason,
        subject_id="idn_human_a",
        tenant_id="ten_a",
    )


ALLOW = DecisionAnswer(
    decision="ALLOW",
    reason="permitted",
    subject_id="idn_human_a",
    tenant_id="ten_a",
)


def walk_state(root: object, *, depth: int = 5) -> list[object]:
    seen: set[int] = set()
    found: list[object] = []
    frontier: list[tuple[int, object]] = [(0, root)]
    while frontier:
        level, value = frontier.pop(0)
        if level > depth or id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        if isinstance(value, (str, bytes, int, float, complex, type(None))):
            continue
        members: list[object] = []
        for name in dir(value):
            if name.startswith("__") and name not in STATE_DUNDERS:
                continue
            try:
                members.append(getattr(value, name))
            except Exception:
                continue
        for cell in getattr(value, "__closure__", None) or ():
            try:
                members.append(cell.cell_contents)
            except ValueError:
                continue
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            if isinstance(value, Mapping):
                members.extend(list(value.keys()) + list(value.values()))
            else:
                members.extend(value)
        for member in members:
            if isinstance(member, type) and member is not type(None):
                continue
            frontier.append((level + 1, member))
    return found


# ---------------------------------------------------------------- route surface
def test_every_published_route_refuses_without_an_allow():
    from tests.test_learning_slice1 import envelope_of, learning_harness

    harness = learning_harness()
    app = harness.learning.contract_app()
    client = harness.http()

    resource_routes = [
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if route.path.startswith("/api/") and method in {"GET", "POST"}
    ]
    assert len(resource_routes) == 10

    substitutions = {
        "{submission_id}": "sub_a1_1",
        "{assignment_id}": "asg_a1",
        "{course_id}": "crs_a1",
        "{module_id}": "mod_a1",
        "{lesson_id}": "les_a1",
    }
    for method, path in resource_routes:
        concrete = path
        for placeholder, demo_id in substitutions.items():
            concrete = concrete.replace(placeholder, demo_id)
        kwargs = {}
        if method == "POST" and path == "/api/v1/learning/courses":
            # A schema-readable command body: without a credential the refusal
            # must still come from the chain (401), not from the schema (422).
            kwargs["json"] = {"title": "t", "description": "d"}
        response = getattr(client, method.lower())(concrete, **kwargs)
        assert response.status_code in {401, 403, 404, 422, 503}, (method, path)
        body = envelope_of(response)
        code = body["error"]["code"]
        if response.status_code == 503:
            # The harness wires no tenant-context port, so the create-course
            # command fails closed on the unwired dependency.
            assert code == "DEPENDENCY_UNAVAILABLE", (method, path)
        elif response.status_code == 422 and code == "INVALID_REQUEST":
            # A command without a readable body is refused by the published
            # schema before any handler runs.
            assert body["error"]["details"]["reason"] == "malformed_request", (
                method,
                path,
            )
        else:
            # Every route refuses at the identity step without a credential.
            assert code == "AUTHENTICATION_REQUIRED", (method, path)
        assert "submission_id" not in response.json(), (method, path)
        assert "items" not in response.json(), (method, path)


def test_published_surface_is_exactly_the_contract_operations():
    from tests.test_learning_slice1 import learning_harness

    harness = learning_harness()
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    implemented = {
        (route.path, method)
        for route in harness.learning.contract_app().routes
        for method in getattr(route, "methods", set())
        if route.path not in framework_routes
        and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    assert implemented == {
        ("/health", "GET"),
        ("/ready", "GET"),
        ("/api/v1/learning/submissions/{submission_id}", "GET"),
        ("/api/v1/learning/assignments/{assignment_id}/submissions", "GET"),
        ("/api/v1/learning/submissions/{submission_id}/review", "POST"),
        ("/api/v1/learning/courses", "POST"),
        ("/api/v1/learning/courses/{course_id}", "GET"),
        ("/api/v1/learning/courses/{course_id}/modules", "POST"),
        ("/api/v1/learning/modules/{module_id}/lessons", "POST"),
        ("/api/v1/learning/lessons/{lesson_id}/assignments", "POST"),
        ("/api/v1/learning/courses/{course_id}/publish", "POST"),
        ("/api/v1/learning/courses/{course_id}/archive", "POST"),
    }


# ------------------------------------------------------------ consumer surface
def test_client_state_is_a_single_opaque_value():
    from tests.test_learning_slice1 import learning_harness

    client = learning_harness().client()

    state = [
        getattr(client, name) for name in client.__slots__ if not name.startswith("__")
    ]
    assert len(state) == 1
    assert isinstance(state[0], str) and state[0]

    published = {name for name in dir(client) if not name.startswith("_")}
    assert published == {
        "read_submission",
        "list_submissions",
        "review_submission",
        "create_course",
        "read_course",
        "create_module",
        "create_lesson",
        "create_assignment",
        "publish_course",
        "archive_course",
    }


def test_client_object_graph_reaches_nothing_internal():
    from tests.test_learning_slice1 import learning_harness

    client = learning_harness().client()

    reachable = walk_state(client) + walk_state(LearningClient)
    for value in reachable:
        assert not isinstance(value, INTERNAL_TYPES), type(value)


def test_revoked_channel_fails_closed():
    from tests.test_learning_slice1 import learning_harness

    client = learning_harness().client()
    handle = client._channel
    transport.close_channel(handle)

    with pytest.raises(ContractViolation):
        client.read_submission(TEACHER_A, "sub_a1_1")
    with pytest.raises(ContractViolation):
        client.list_submissions(TEACHER_A, "asg_a1")


def test_an_unknown_channel_handle_is_a_closed_channel():
    client = LearningClient("lrn-forged-handle")
    with pytest.raises(ContractViolation):
        client.read_submission(TEACHER_A, "sub_a1_1")
    with pytest.raises(ContractViolation):
        client.list_submissions(TEACHER_A, "asg_a1")


def test_importing_the_published_surface_binds_no_transport_reference():
    module = importlib.import_module("learning_service.reader")
    namespace = dict(vars(module))
    assert "transport" not in namespace
    assert not any(
        value is transport or value is transport.call_contract
        for value in namespace.values()
    )


# ------------------------------------------------------- envelope strictness
def _stub_app(payload: dict, status: int = 403):
    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


def test_a_legacy_detail_envelope_is_rejected_and_fails_closed():
    """No parallel old envelope: a ``detail``-shaped refusal is a violation."""
    legacy = {"detail": {"decision": "DENY", "reason": "permission_not_granted"}}
    handle = transport.open_channel(_stub_app(legacy))
    try:
        client = LearningClient(handle)
        with pytest.raises(ContractViolation):
            client.read_submission(TEACHER_A, "sub_a1_1")
        with pytest.raises(ContractViolation):
            client.list_submissions(TEACHER_A, "asg_a1")
    finally:
        transport.close_channel(handle)


def test_an_envelope_without_request_context_is_rejected():
    payload = {
        "error": {
            "code": "AUTHORIZATION_DENIED",
            "message": "Operation is not permitted.",
            "details": {"reason": "permission_not_granted"},
        }
    }
    handle = transport.open_channel(_stub_app(payload))
    try:
        client = LearningClient(handle)
        with pytest.raises(ContractViolation):
            client.read_submission(TEACHER_A, "sub_a1_1")
        with pytest.raises(ContractViolation):
            client.list_submissions(TEACHER_A, "asg_a1")
    finally:
        transport.close_channel(handle)


def test_an_envelope_with_an_unknown_reason_is_rejected():
    payload = {
        "error": {
            "code": "AUTHORIZATION_DENIED",
            "message": "Operation is not permitted.",
            "details": {"reason": "invented_reason"},
        },
        "request_id": "req-1",
        "correlation_id": "corr-1",
    }
    handle = transport.open_channel(_stub_app(payload))
    try:
        client = LearningClient(handle)
        with pytest.raises(ContractViolation):
            client.read_submission(TEACHER_A, "sub_a1_1")
        with pytest.raises(ContractViolation):
            client.list_submissions(TEACHER_A, "asg_a1")
    finally:
        transport.close_channel(handle)


# ------------------------------------------------------------ isolation of code
def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name in (
                "identity_service",
                "tenant_authority",
                "authorization_service",
            ):
                if node.value == name or node.value.startswith(name + "."):
                    modules.add(name)
    return modules


def test_learning_service_imports_no_other_component():
    """The dependency on IS-003 is on the published contract, not on code."""
    foreign = {"identity_service", "tenant_authority", "authorization_service"}
    package = Path("src/learning_service")
    offenders = {}
    for path in sorted(package.glob("*.py")):
        imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        hit = foreign & imported
        if hit:
            offenders[path.name] = sorted(hit)
    assert offenders == {}

    conftest = _imported_modules(
        ast.parse(Path("tests/conftest.py").read_text(encoding="utf-8"))
    )
    assert foreign & conftest


def test_a_fresh_interpreter_imports_no_other_component():
    code = (
        "import sys, learning_service.api, learning_service.reader, "
        "learning_service.engine, learning_service.deployment, "
        "learning_service.adapters, learning_service.transport, "
        "learning_service.store, learning_service.contracts, "
        "learning_service.consumed, learning_service.ports, "
        "learning_service.models, learning_service.config, learning_service.errors;"
        "foreign = [m for m in sys.modules if m.split('.')[0] in "
        "('identity_service', 'tenant_authority', 'authorization_service')];"
        "assert not foreign, foreign"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # the exit code is asserted below, with its stderr as context
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------- no second tenant mechanism
def test_a_silent_dependency_makes_every_access_impossible():
    silent = StubAuthorizationPort(DependencyRefusal(None))
    store = CountingLearningStore()
    deployment = learning_deployment(silent, store=store)

    with pytest.raises(AccessRefused):
        deployment.engine.read_submission(TEACHER_A, "sub_a1_1")
    with pytest.raises(AccessRefused):
        deployment.engine.list_submissions(TEACHER_A, "asg_a1")

    assert store.served_reads == 0
    assert store.served_lists == 0
    assert len(silent.calls) == 2, "every access attempt still went to the boundary"


@pytest.mark.parametrize("reason", sorted(DENY_REASONS))
def test_every_published_deny_keeps_the_owned_data_untouched(reason):
    store = CountingLearningStore()
    deployment = learning_deployment(StubAuthorizationPort(deny(reason)), store=store)
    engine = deployment.engine

    with pytest.raises(AccessRefused) as refused:
        engine.read_submission(TEACHER_A, "sub_a1_1")
    assert refused.value.reason == reason
    with pytest.raises(AccessRefused) as refused:
        engine.list_submissions(TEACHER_A, "asg_a1")
    assert refused.value.reason == reason
    assert store.served_reads == 0
    assert store.served_lists == 0


def test_an_allow_invokes_each_owned_data_operation_exactly_once():
    store = CountingLearningStore()
    deployment = learning_deployment(StubAuthorizationPort(ALLOW), store=store)
    engine = deployment.engine

    view, _, _ = engine.read_submission(TEACHER_A, "sub_a1_1")
    assert view.submission_id == "sub_a1_1"
    views, _, _ = engine.list_submissions(TEACHER_A, "asg_a1")
    assert [item.submission_id for item in views] == ["sub_a1_1", "sub_a1_2"]
    assert store.served_reads == 1
    assert store.served_lists == 1


def test_the_resource_tenant_is_never_taken_from_the_request():
    port = StubAuthorizationPort(DependencyRefusal(None))
    deployment = learning_deployment(port, store=CountingLearningStore())

    with pytest.raises(AccessRefused):
        deployment.engine.read_submission(
            TEACHER_A, "sub_b1_1", claimed_tenant_id="ten_a"
        )
    with pytest.raises(AccessRefused):
        deployment.engine.list_submissions(
            TEACHER_A, "asg_b1", claimed_tenant_id="ten_a"
        )

    read_question, list_question = port.calls
    assert read_question["resource_tenant_id"] == "ten_b"
    assert read_question["claimed_tenant_id"] == "ten_a"
    assert read_question["operation"] == OPERATION_READ
    assert list_question["resource_tenant_id"] == "ten_b"
    assert list_question["claimed_tenant_id"] == "ten_a"
    assert list_question["operation"] == OPERATION_LIST


def test_unknown_records_are_denied_without_asking_the_authority():
    port = StubAuthorizationPort(deny("permission_not_granted"))
    store = CountingLearningStore()
    deployment = learning_deployment(port, store=store)

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_submission(TEACHER_A, "sub_missing")
    assert refused.value.reason == OwnDenyReason.SUBMISSION_UNKNOWN
    assert refused.value.status_code == 404

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.list_submissions(TEACHER_A, "asg_missing")
    assert refused.value.reason == OwnDenyReason.ASSIGNMENT_UNKNOWN
    assert refused.value.status_code == 404

    assert port.calls == []
    assert store.served_reads == 0
    assert store.served_lists == 0


def test_a_foreign_owner_record_is_never_served():
    port = StubAuthorizationPort(ALLOW)
    store = CountingLearningStore()
    deployment = learning_deployment(port, store=store)
    store.submissions["sub_foreign"] = OwnedSubmission(
        submission_id="sub_foreign",
        assignment_id="asg_a1",
        tenant_id="ten_a",
        student_identity_id="idn_human_c",
        attempt=1,
        content={},
        status="SUBMITTED",
        created_at="2026-09-08T00:00:00+00:00",
        updated_at="2026-09-08T00:00:00+00:00",
        owner_component="identity",
    )
    store.assignments["asg_foreign"] = OwnedAssignment(
        assignment_id="asg_foreign",
        tenant_id="ten_a",
        owner_component="identity",
        status="PUBLISHED",
        created_at="2026-09-08T00:00:00+00:00",
        updated_at="2026-09-08T00:00:00+00:00",
    )

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.read_submission(TEACHER_A, "sub_foreign")
    assert refused.value.reason == "owner_mismatch"

    with pytest.raises(AccessRefused) as refused:
        deployment.engine.list_submissions(TEACHER_A, "asg_foreign")
    assert refused.value.reason == "owner_mismatch"

    assert store.served_reads == 0
    assert store.served_lists == 0
    assert port.calls == []


def test_nonauthoritative_answers_fail_closed():
    for decision, reason in [
        ("ALLOW", "wrong_reason"),
        ("DENY", "unknown_reason"),
        ("MAYBE", "permitted"),
    ]:
        port = StubAuthorizationPort(DecisionAnswer(decision=decision, reason=reason))
        deployment = learning_deployment(port, store=CountingLearningStore())
        with pytest.raises(AccessRefused) as refused:
            deployment.engine.read_submission(TEACHER_A, "sub_a1_1")
        assert refused.value.reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE
        assert refused.value.status_code == 503
        with pytest.raises(AccessRefused) as refused:
            deployment.engine.list_submissions(TEACHER_A, "asg_a1")
        assert refused.value.reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE
        assert refused.value.status_code == 503


def test_operations_use_the_documented_grant_vocabulary():
    port = StubAuthorizationPort(ALLOW)
    deployment = learning_deployment(port, store=CountingLearningStore())
    deployment.engine.read_submission(TEACHER_A, "sub_a1_1")
    deployment.engine.list_submissions(TEACHER_A, "asg_a1")
    assert [call["operation"] for call in port.calls] == [
        OPERATION_READ,
        OPERATION_LIST,
    ]
    assert OPERATION_READ == "learning.submissions.read"
    assert OPERATION_LIST == "learning.submissions.list"
