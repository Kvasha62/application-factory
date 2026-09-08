"""IS-003 depends on contracts of IS-001 / IS-002, never on their code.

A component that imports another component's modules is coupled to that
component's implementation instead of to its contract: a refactoring inside
IS-001 or IS-002 would then reach into the Authorization Boundary without
passing a contract at all (ARCHITECTURE.md §1.1, §6.1, LAW-04).

So the dependency is expressed twice over:

* the ports of IS-003 speak only its own value types
  (:mod:`authorization_service.consumed`);
* the published clients of IS-001 and IS-002 are converted into those values by
  this component's own adapters, which know the *shape* of a published answer
  and nothing else.

The consequence checked here is blunt: no module of ``authorization_service``
imports ``identity_service`` or ``tenant_authority``, importing the component
does not import them either, and the decision logic works when the ports are
implemented by objects that have nothing to do with those components. What does
*not* change: the effective tenant still comes from IS-001 — there is no second
tenant-context mechanism, and with a port that answers nothing no decision can
be made at all.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

import authorization_service
from authorization_service.adapters import identity_port, tenant_authority_port
from authorization_service.consumed import DependencyRefusal, SubjectContext, TenantVerdict
from authorization_service.contracts import Decision, Reason, ResourceRef
from authorization_service.deployment import build_deployment
from authorization_service.ports import IdentityContextPort, TenantAuthorityPort
from tests.conftest import DATA_OWNER_CREDENTIAL, PLATFORM_ID, monolith

FORBIDDEN_PACKAGES = ("identity_service", "tenant_authority")
COMPONENT_ROOT = pathlib.Path(authorization_service.__file__).resolve().parent
SRC_ROOT = COMPONENT_ROOT.parent
RESOURCE = ResourceRef("record", "rec_a1", "ten_a")


def imported_packages(path: pathlib.Path) -> set[str]:
    """Every package a module pulls in, including inside functions and methods."""
    found: set[str] = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # Also catch an import hidden behind importlib.
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in {"import_module", "__import__"}:
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        found.add(argument.value.split(".")[0])
    return found


def component_modules() -> list[pathlib.Path]:
    return sorted(COMPONENT_ROOT.glob("*.py"))


def test_no_module_of_this_component_imports_another_component():
    modules = component_modules()
    assert len(modules) >= 10, "the scan must cover the whole component"
    for path in modules:
        imported = imported_packages(path)
        for package in FORBIDDEN_PACKAGES:
            assert package not in imported, f"{path.name} imports {package}"


def test_the_import_scan_would_notice_a_forbidden_import():
    """Control: the scanner finds exactly what it is supposed to forbid.

    The composition root legitimately knows both components, so it is the honest
    place to prove the check is not vacuous.
    """
    composition_root = pathlib.Path(__file__).resolve().parent / "conftest.py"
    imported = imported_packages(composition_root)
    for package in FORBIDDEN_PACKAGES:
        assert package in imported


def test_importing_the_component_does_not_import_another_component():
    """Behavioural version of the same fact, in a fresh interpreter."""
    program = (
        "import importlib, sys\n"
        "for name in ("
        "'authorization_service', 'authorization_service.api',"
        "'authorization_service.engine', 'authorization_service.deployment',"
        "'authorization_service.reader', 'authorization_service.adapters',"
        "'authorization_service.ports', 'authorization_service.policy',"
        "'authorization_service.store', 'authorization_service.transport'):\n"
        "    importlib.import_module(name)\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in "
        f"{FORBIDDEN_PACKAGES!r})\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(SRC_ROOT),
        check=True,
    )
    assert result.stdout.strip() == "", result.stdout


def test_the_engine_decides_with_ports_that_are_no_component_at_all():
    """The decision logic depends on the port contract, not on who implements it."""

    class TinyIdentity:
        """Answers with values of IS-003 and knows nothing about IS-001."""

        def resolve_context(self, credential, **_):
            if credential == "green":
                return SubjectContext(identity_id="idn_green", tenant_id="ten_a")
            if credential == "other-tenant":
                return SubjectContext(identity_id="idn_green", tenant_id="ten_b")
            return DependencyRefusal("invalid_identity")

    class TinyAuthority:
        def lifecycle_decision(self, tenant_id, **_):
            if tenant_id == "ten_b":
                return TenantVerdict(permitted=False, reason_code="tenant_suspended")
            return TenantVerdict(permitted=True)

    identity, authority = TinyIdentity(), TinyAuthority()
    assert isinstance(identity, IdentityContextPort)
    assert isinstance(authority, TenantAuthorityPort)

    deployment = build_deployment(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        identity=identity,
        tenant_authority=authority,
        with_http=True,
    )
    deployment.store.grant("ten_a", "idn_green", "records.read")
    deployment.store.services.update(monolith().authorization.store.services)
    deployment.store.service_tokens[DATA_OWNER_CREDENTIAL] = "svc_records"
    client = deployment.publish(credential=DATA_OWNER_CREDENTIAL)

    allowed = client.decide("green", operation="records.read", resource=RESOURCE)
    assert allowed.decision is Decision.ALLOW
    assert allowed.subject_id == "idn_green"
    assert allowed.tenant_id == "ten_a"

    assert client.decide("green", operation="records.write", resource=RESOURCE).reason is (
        Reason.PERMISSION_NOT_GRANTED
    )
    assert client.decide("red", operation="records.read", resource=RESOURCE).reason is (
        Reason.INVALID_IDENTITY
    )
    other = client.decide(
        "other-tenant", operation="records.read", resource=ResourceRef("record", "rec_b1", "ten_b")
    )
    assert other.reason is Reason.TENANT_SUSPENDED


def test_a_port_that_answers_nothing_makes_every_decision_impossible():
    """No fallback: without an identity answer the component cannot decide (A-002)."""

    class Mute:
        def resolve_context(self, credential, **_):
            return DependencyRefusal(None)

    class Authority:
        def lifecycle_decision(self, tenant_id, **_):
            return TenantVerdict(permitted=True)

    instance = monolith()
    deployment = build_deployment(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        identity=Mute(),
        tenant_authority=Authority(),
        seed_demo=True,
        with_http=True,
    )
    answer = deployment.publish(credential=DATA_OWNER_CREDENTIAL).decide(
        "token-human-a", operation="records.read", resource=RESOURCE
    )
    assert answer.decision is Decision.DENY
    assert answer.reason is Reason.AUTHORITY_UNAVAILABLE
    assert answer.tenant_id is None
    # The real composition, for contrast, decides — the difference is the port.
    assert instance.authorization_client.decide(
        "token-human-a", operation="records.read", resource=RESOURCE
    ).allowed


def test_a_port_that_misbehaves_denies_instead_of_escaping():
    """Whatever a dependency does instead of answering, it does not open access."""

    class Exploding:
        def resolve_context(self, credential, **_):
            raise RuntimeError("boom")

    class WrongShape:
        def lifecycle_decision(self, tenant_id, **_):
            return "yes, sure"

    deployment = build_deployment(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        identity=Exploding(),
        tenant_authority=WrongShape(),
        seed_demo=True,
        with_http=True,
    )
    client = deployment.publish(credential=DATA_OWNER_CREDENTIAL)
    assert client.decide(
        "token-human-a", operation="records.read", resource=RESOURCE
    ).reason is Reason.AUTHORITY_UNAVAILABLE

    class Fine:
        def resolve_context(self, credential, **_):
            return SubjectContext(identity_id="idn_human_a", tenant_id="ten_a")

    deployment.engine.identity = Fine()
    # Now the identity port answers, and the malformed lifecycle answer is the
    # one that denies: an unreadable verdict is not a servable Tenant.
    assert client.decide(
        "token-human-a", operation="records.read", resource=RESOURCE
    ).reason is Reason.AUTHORITY_UNAVAILABLE


def test_the_adapter_returns_values_of_this_component_only():
    """Nothing of the provider's answer survives the adapter call."""
    instance = monolith()
    adapter = identity_port(instance.identity_context_client)
    context = adapter.resolve_context("token-human-a")
    assert isinstance(context, SubjectContext)
    assert type(context).__module__.startswith("authorization_service.")
    assert context.identity_id == "idn_human_a"
    assert context.tenant_id == "ten_a"
    # Only documented, machine-readable values are carried over — no provider
    # enum, no provider object, nothing to walk back into IS-001 through.
    for value in (context.kind, context.source, context.platform_id, context.subject):
        assert value is None or isinstance(value, str)

    verdict = tenant_authority_port(instance.tenant_authority_client).lifecycle_decision(
        "ten_a", expected_platform_id=PLATFORM_ID
    )
    assert isinstance(verdict, TenantVerdict)
    assert verdict.permitted is True


@pytest.mark.parametrize(
    "credential, reason_code",
    [
        (None, "missing_identity"),
        ("token-invalid", "invalid_identity"),
        ("token-unknown", "unknown_identity"),
    ],
)
def test_the_adapter_carries_a_published_refusal_code_and_nothing_else(credential, reason_code):
    instance = monolith()
    refusal = identity_port(instance.identity_context_client).resolve_context(credential)
    assert isinstance(refusal, DependencyRefusal)
    assert refusal.reason_code == reason_code
    assert set(vars(type(refusal))["__slots__"]) == {"reason_code"}


def test_an_unreadable_refusal_becomes_no_answer_at_all():
    """A dependency failing outside its contract states no reason: that denies."""

    class Rude:
        def resolve_context(self, credential, **_):
            raise ValueError("nothing machine-readable here")

    refusal = identity_port(Rude()).resolve_context("token-human-a")
    assert refusal == DependencyRefusal(None)


def test_no_object_of_another_component_is_reachable_from_a_decision():
    """The value handed to a consumer is built from this component's types only."""
    instance = monolith()
    decision = instance.authorization_client.decide(
        "token-human-a", operation="records.read", resource=RESOURCE
    )

    seen: set[int] = set()
    modules: set[str] = set()

    def walk(value, depth=0):
        if id(value) in seen or depth > 6:
            return
        seen.add(id(value))
        modules.add(str(getattr(type(value), "__module__", "")))
        for slot in getattr(type(value), "__slots__", ()) or ():
            if hasattr(value, slot):
                walk(getattr(value, slot), depth + 1)
        for attribute in vars(value).values() if hasattr(value, "__dict__") else ():
            walk(attribute, depth + 1)

    walk(decision)
    for module in modules:
        assert not module.startswith(FORBIDDEN_PACKAGES), module
