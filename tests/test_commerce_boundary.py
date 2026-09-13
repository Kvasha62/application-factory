"""Behavioral proof of the Commerce component boundary (SCS-002 Stage 2).

No module of ``commerce_service`` imports another component's service
package: the published clients are adapted at the composition boundary
and only values owned by this component reach the enforcement logic. This
module proves the property by importing the whole package in a fresh
interpreter and inspecting what came with it — ``idempotency`` is the one
allowed library surface (IS-005 guard), everything else of another
component must be absent.

This module also defines the stub decision port and the standalone
deployment helper used by the contract tests.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from commerce_service.consumed import DecisionAnswer
from commerce_service.deployment import CommerceDeployment
from commerce_service.deployment import build_deployment as build_commerce
from commerce_service.reader import CommerceClient
from commerce_service.store import CommerceStore
from tests.conftest import PLATFORM_ID

#: Service packages of other components: importing any of them from
#: commerce_service would be a boundary violation (LAW-04). ``idempotency``
#: is deliberately absent: it is the library surface of IS-005, consumed
#: by direct import like every other component does.
FORBIDDEN_PACKAGES = frozenset(
    {
        "identity_service",
        "authorization_service",
        "tenant_authority",
        "learning_service",
        "records_service",
        "saga",
    }
)

COMMERCE_MODULES = [
    "commerce_service",
    "commerce_service.config",
    "commerce_service.models",
    "commerce_service.contracts",
    "commerce_service.errors",
    "commerce_service.consumed",
    "commerce_service.ports",
    "commerce_service.adapters",
    "commerce_service.store",
    "commerce_service.engine",
    "commerce_service.api",
    "commerce_service.reader",
    "commerce_service.transport",
    "commerce_service.deployment",
]


class StubAuthorizationPort:
    """A decision port with a fixed outcome: a value to return or an exception.

    The commerce engine must behave identically for any port answering its
    own vocabulary — this stub is how the tests prove the enforcement logic
    depends on nothing of the real provider.
    """

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


def commerce_deployment(
    port: Any,
    *,
    store: CommerceStore | None = None,
    seed_demo: bool = True,
) -> CommerceDeployment:
    """Standalone Commerce deployment over a given decision port."""
    return build_commerce(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        authorization=port,
        store=store,
        seed_demo=seed_demo and store is None,
        with_http=True,
    )


# ------------------------------------------------------- import isolation
def test_importing_commerce_pulls_in_no_other_service_package():
    probe = (
        "import sys; "
        f"modules = {COMMERCE_MODULES!r}; "
        "[__import__(name) for name in modules]; "
        "roots = sorted({name.split('.')[0] for name in sys.modules}); "
        "print('\\n'.join(roots))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path("src").resolve())
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    roots = set(completed.stdout.split())
    assert FORBIDDEN_PACKAGES.isdisjoint(roots), roots & FORBIDDEN_PACKAGES
    # The one allowed companion: the IS-005 library surface.
    assert "idempotency" in roots
    assert "commerce_service" in roots


def test_adapters_hold_no_provider_module_reference():
    from commerce_service import adapters

    namespace = dict(vars(adapters))
    for name, value in namespace.items():
        module = getattr(value, "__module__", "")
        assert not module.split(".")[0] in FORBIDDEN_PACKAGES, (name, module)


# ------------------------------------------------------------- client surface
def test_published_client_exposes_only_the_published_operations():
    methods = {
        name
        for name, member in inspect.getmembers(CommerceClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == {"create_product", "read_product"}


def test_published_client_holds_an_opaque_handle_and_nothing_else():
    deployment = commerce_deployment(
        StubAuthorizationPort(
            DecisionAnswer(
                decision="ALLOW",
                reason="permitted",
                subject_id="idn_human_a",
                tenant_id="ten_a",
            )
        )
    )
    client = deployment.publish()
    assert isinstance(client, CommerceClient)
    assert isinstance(client._channel, str)
    assert client._channel
    for forbidden in ("engine", "store", "deployment", "app", "audit", "config"):
        assert not hasattr(client, forbidden), forbidden
    with pytest.raises(ValueError):
        CommerceClient("")
