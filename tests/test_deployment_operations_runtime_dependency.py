"""F-5 runtime dependency enforcement (ADR-0021 §2.1, §3).

These tests cover the mechanism the owner approved for F-5 revision 2: a network
reach from ``identity`` to ``tenant_authority``, over an endpoint **statically
declared** in the authoritative environment binding, served by the **provider's
own runtime process**, in a **dependency-derived start order**, and failing
closed when any part of it does not hold.

The end-to-end tests deploy the *real* two-component instance through the
committed artifacts — the instance, its manifest and the authoritative
environment binding — so the evidence is the deployment itself and not a
hand-written stand-in (ADR-0016 §3).
"""

from __future__ import annotations

import ast
import http.client
import json
import socket
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _deployment_helpers import (
    PLATFORM_ID,
    ROOT,
    binding,
    environment_for,
    instance_for,
    manifest_for,
    request_for,
)

from deployment_operations import deploy as _deploy
from deployment_operations.deployment import DeploymentRequest, start_order
from deployment_operations.environment import (
    ComponentRuntimeBinding,
    DependencyEndpoint,
    load_environment,
    validate_environment,
)
from deployment_operations.errors import StartupFailed
from deployment_operations.platform_identity import (
    ActualComponentIdentity,
    IdentityField,
    PlatformIdentityBinding,
)
from deployment_operations.platform_identity_source import (
    ActualPlatformSnapshot,
    OwnerSuppliedPlatformIdentityProvider,
)
from deployment_operations.provisioning import ArtifactSource, runtime_spec_document
from deployment_operations.published_endpoint import (
    PublishedEndpointError,
    PublishedEndpointServer,
)
from deployment_operations.runtime import (
    DEPENDENCY_ENDPOINT_PREFIX,
    PUBLISHED_ENDPOINT_ENV,
    RuntimeElement,
    endpoint_environment,
)
from deployment_operations.verification import ComponentBinding, verify_instance
from identity_service.config import ConfigurationError
from identity_service.deployment import (
    TENANT_AUTHORITY_CREDENTIAL_ENV,
    TENANT_AUTHORITY_ENDPOINT_ENV,
    HttpTenantAuthorityClient,
    TenantAuthorityUnavailable,
    build_deployment,
)
from platform_instance import Instance
from tenant_authority.deployment import build_deployment as build_provider
from tenant_authority.errors import AuthenticationDenied, TenantNotFound

#: The committed F-5 artifacts.
ENVIRONMENT_PATH = (
    ROOT / "factory/environments/example_runtime_dependency_environment.json"
)
INSTANCE_PATH = ROOT / "factory/environments/example_runtime_dependency_instance.json"
MANIFEST_PATH = ROOT / "factory/environments/example_runtime_dependency_manifest.json"

PROVIDER = "tenant_authority"
CONSUMER = "identity"
#: The seeded service identity of the Identity component in Tenant Authority.
SERVICE_TOKEN = "svc-token-identity"


# ---------------------------------------------------------------------------
# Shared doubles and helpers
# ---------------------------------------------------------------------------
class _MeasuredIdentitySource:
    """An owner-side running platform identity source that measures the request.

    Mirrors the double the other Deployment & Operations test modules use: the
    running platform answers with exactly the identity the instance declares,
    because what these tests exercise is the runtime dependency mechanism, not
    identity reconciliation.
    """

    def __init__(self, request: DeploymentRequest) -> None:
        self.request = request

    def observe(self, binding: PlatformIdentityBinding) -> ActualPlatformSnapshot:
        components = []
        for component in self.request.manifest_document.get("components", []):
            artifact = component.get("artifact")
            components.append(
                ActualComponentIdentity(
                    component["component_id"],
                    component["component_version"],
                    artifact if isinstance(artifact, Mapping) else None,
                )
            )
        configuration = self.request.instance_document.get("configuration")
        return ActualPlatformSnapshot(
            platform_id=self.request.instance_document["platform_id"],
            manifest={
                "manifest_id": self.request.manifest_document["manifest_id"],
                "manifest_version": self.request.manifest_document["manifest_version"],
                "manifest_digest": self.request.manifest_document["manifest_digest"],
            },
            manifest_state=self.request.manifest_document["lifecycle"]["state"],
            components=tuple(components),
            membership_established=True,
            configuration=(
                IdentityField.present(configuration)
                if configuration is not None
                else IdentityField.absent()
            ),
            golden_bundle=IdentityField.absent(),
            golden_bundle_inventory_established=True,
            extensions=IdentityField.absent(),
            branding=IdentityField.absent(),
            provenance="MEASURED",
            correlation_token=binding.token,
            freshness_current=True,
        )


def deploy(request: DeploymentRequest, **kwargs: object):
    provider = OwnerSuppliedPlatformIdentityProvider(_MeasuredIdentitySource(request))
    return _deploy(request, identity_provider=provider, **kwargs)


def free_port() -> int:
    """An OS-assigned free TCP port, released before the test binds it."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def get_json(host: str, port: int, path: str) -> tuple[int, Any]:
    """One plain HTTP GET over a real socket — no in-process shortcut."""
    connection = http.client.HTTPConnection(host, port, timeout=10.0)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8")) if raw else None
    finally:
        connection.close()


def element(
    component_id: str,
    *,
    published_endpoint: str = "",
    dependencies: tuple[DependencyEndpoint, ...] = (),
) -> RuntimeElement:
    """A runtime element whose binding declares the given endpoints."""
    return RuntimeElement(
        deployment_id="dep-1",
        environment_id="local-test",
        platform_id=PLATFORM_ID,
        instance_digest="sha256:0",
        component=ComponentBinding(
            component_id=component_id,
            component_version="0.1.0",
            artifact_type="none",
            artifact_digest=None,
            artifact_pinned=False,
            artifact_canonical_form=None,
        ),
        configuration={},
        spec_path=Path("/unused/runtime.json"),
        workspace=Path("/unused"),
        binding=ComponentRuntimeBinding(
            component_id=component_id,
            deployment_module=f"{component_id}.deployment",
            deployment_factory="build_deployment",
            published_endpoint=published_endpoint,
            dependency_endpoints=dependencies,
        ),
        artifact_source=ArtifactSource(path=None, digest=None, verified=False),
        interpreter="python3",
        source_paths=(),
    )


def provider_app(platform_id: str = PLATFORM_ID, *, seed_demo: bool = False) -> Any:
    """The real Tenant Authority contract app, as the provider publishes it."""
    return build_provider(
        {"platform_id": platform_id}, seed_demo=seed_demo
    ).contract_app()


def stage_detail(record: Mapping[str, Any], stage: str) -> Mapping[str, Any]:
    for entry in record.get("stages", []):
        if entry.get("name") == stage:
            detail = entry.get("detail")
            return detail if isinstance(detail, Mapping) else {}
    raise AssertionError(f"the record has no {stage!r} stage")


# ---------------------------------------------------------------------------
# The authoritative environment binding artifact (criteria 2, 3, 4)
# ---------------------------------------------------------------------------
class TestAuthoritativeEnvironmentBinding:
    def test_the_committed_binding_declares_the_endpoint_statically(self):
        """Criterion 2: the endpoint is declared in the binding, not allocated."""
        document = json.loads(ENVIRONMENT_PATH.read_text(encoding="utf-8"))
        by_id = {entry["component_id"]: entry for entry in document["bindings"]}

        assert by_id[PROVIDER]["published_endpoint"] == "127.0.0.1:9101"
        declared = by_id[CONSUMER]["dependency_endpoints"][PROVIDER]
        assert declared["endpoint"] == by_id[PROVIDER]["published_endpoint"]
        assert declared["version_range"] == ">=0.1.0,<0.2.0"

    def test_the_binding_loads_and_validates_against_the_committed_instance(self):
        instance = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        verification = verify_instance(instance, manifest, root=ROOT)
        environment = load_environment(
            json.loads(ENVIRONMENT_PATH.read_text(encoding="utf-8")),
            base_dir=ENVIRONMENT_PATH.parent,
        )
        assert validate_environment(environment, verification) == []

        provider = environment.binding_for(PROVIDER)
        assert provider is not None
        assert provider.published_endpoint == "127.0.0.1:9101"
        consumer = environment.binding_for(CONSUMER)
        assert consumer is not None
        declared = consumer.dependency_endpoint(PROVIDER)
        assert declared is not None
        assert declared.endpoint == provider.published_endpoint
        assert declared.version_range == ">=0.1.0,<0.2.0"


# ---------------------------------------------------------------------------
# Endpoint provenance and correspondence (criteria 2, 6)
# ---------------------------------------------------------------------------
class TestEndpointProvenance:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://127.0.0.1:9101",  # a scheme is not an endpoint
            "127.0.0.1:9101/api/v1",  # neither is a path
            "127.0.0.1",  # no port declared
            "127.0.0.1:9101 ",  # whitespace
            "latest:9101",  # a floating selector is not an address
            "127.0.0.1:0",  # outside the usable range
            "127.0.0.1:70000",
            "",
        ],
    )
    def test_a_published_endpoint_that_is_not_host_and_port_is_refused(
        self, endpoint: str
    ):
        errors = binding_errors(published=endpoint)
        assert any("published_endpoint" in entry for entry in errors), errors

    @pytest.mark.parametrize(
        "endpoint",
        ["http://127.0.0.1:9101", "127.0.0.1:9101/x", "127.0.0.1", "127.0.0.1:0"],
    )
    def test_a_dependency_endpoint_that_is_not_host_and_port_is_refused(
        self, endpoint: str
    ):
        errors = binding_errors(dependency=endpoint)
        assert any("dependency_endpoints" in entry for entry in errors), errors

    def test_two_components_cannot_publish_one_endpoint(self):
        """One endpoint resolves to one deployed component."""
        errors = binding_errors(published="127.0.0.1:9101", duplicate_published=True)
        assert any("already published by" in entry for entry in errors), errors

    def test_a_dependency_endpoint_must_correspond_to_the_provider(self):
        """Criterion 6: a wrong endpoint is rejected, never substituted."""
        errors = binding_errors(dependency="127.0.0.1:9999")
        assert any("does not correspond" in entry for entry in errors), errors

    def test_a_dependency_on_an_undeclared_component_is_refused(self):
        errors = binding_errors(dependency_on="commerce")
        assert any("does not declare component" in entry for entry in errors), errors

    def test_a_dependency_on_a_component_without_a_binding_is_refused(self):
        errors = binding_errors(dependency_on="commerce", declare_commerce_member=True)
        # The environment is rejected either way: the dependency names a
        # provider this environment gives no runtime entrypoint.
        assert any(
            "commerce" in entry and "runtime binding" in entry for entry in errors
        ), errors

    def test_a_dependency_on_a_provider_without_an_endpoint_is_refused(self):
        errors = binding_errors(provider_endpoint="")
        assert any("declares no published_endpoint" in entry for entry in errors)

    def test_a_component_cannot_depend_on_itself(self):
        errors = binding_errors(dependency_on=PROVIDER, consumer_is_provider=True)
        assert any("does not depend on itself" in entry for entry in errors), errors


# ---------------------------------------------------------------------------
# Declared version-range compatibility (criterion 4)
# ---------------------------------------------------------------------------
class TestDeclaredVersionRange:
    @pytest.mark.parametrize(
        ("version_range", "expected"),
        [
            (">=0.1.0,<0.2.0", []),  # the pinned 0.1.0 is admitted
            ("==0.1.0", []),
            (">=0.2.0", ["does not admit"]),  # the pinned version is too old
            ("<0.1.0", ["does not admit"]),
            ("latest", ["not a version range"]),  # floating, and not a range
            ("", []),  # undeclared: no range to enforce
        ],
    )
    def test_the_pinned_version_must_satisfy_the_declared_range(
        self, version_range: str, expected: list[str]
    ):
        errors = binding_errors(version_range=version_range)
        for fragment in expected:
            assert any(fragment in entry for entry in errors), (errors, fragment)
        if not expected:
            assert not any("version_range" in entry for entry in errors), errors


# ---------------------------------------------------------------------------
# Endpoint injection at the process boundary
# ---------------------------------------------------------------------------
class TestEndpointInjection:
    def test_declared_endpoints_reach_the_process_environment(self):
        env = endpoint_environment(
            ComponentRuntimeBinding(
                component_id=CONSUMER,
                deployment_module="identity_service.deployment",
                deployment_factory="build_deployment",
                published_endpoint="127.0.0.1:9200",
                dependency_endpoints=(
                    DependencyEndpoint(
                        PROVIDER, "127.0.0.1:9101", version_range=">=0.1.0,<0.2.0"
                    ),
                ),
            )
        )
        assert env[PUBLISHED_ENDPOINT_ENV] == "127.0.0.1:9200"
        assert env[f"{DEPENDENCY_ENDPOINT_PREFIX}TENANT_AUTHORITY_ENDPOINT"] == (
            "127.0.0.1:9101"
        )
        assert env[f"{DEPENDENCY_ENDPOINT_PREFIX}TENANT_AUTHORITY_VERSION_RANGE"] == (
            ">=0.1.0,<0.2.0"
        )

    def test_an_undeclared_endpoint_is_absent_and_never_an_empty_string(self):
        env = endpoint_environment(
            ComponentRuntimeBinding(
                component_id=PROVIDER,
                deployment_module="tenant_authority.deployment",
                deployment_factory="build_deployment",
            )
        )
        assert env == {}

    def test_the_runtime_spec_carries_the_declared_topology(self):
        """The spec states the endpoint as declared; nothing is allocated."""
        versions = registry_versions()
        manifest = manifest_for(
            [
                {"component_id": PROVIDER, "component_version": versions[PROVIDER]},
                {"component_id": CONSUMER, "component_version": versions[CONSUMER]},
            ],
            manifest_id=PLATFORM_ID,
        )
        instance = instance_for(manifest, platform_id=PLATFORM_ID)
        verification = verify_instance(instance.document, manifest, root=ROOT)
        environment = environment_for(
            Path("/unused/runtime"),
            bindings=(
                binding(component_id=PROVIDER, published_endpoint="127.0.0.1:9101"),
                binding(
                    component_id=CONSUMER,
                    module="identity_service.deployment",
                    dependency_endpoints=(
                        DependencyEndpoint(PROVIDER, "127.0.0.1:9101"),
                    ),
                ),
            ),
        )
        component = verification.component(CONSUMER)
        assert component is not None
        spec = runtime_spec_document(
            environment=environment,
            verification=verification,
            binding=component,
            deployment_id="dep-1",
            configuration={"current_platform_id": PLATFORM_ID},
            workspace=Path("/unused/workspace"),
            artifact_source=ArtifactSource(path=None, digest=None, verified=False),
        )
        assert spec["runtime"]["published_endpoint"] == ""
        assert spec["runtime"]["dependency_endpoints"] == [
            {
                "component_id": PROVIDER,
                "endpoint": "127.0.0.1:9101",
                "version_range": "",
            }
        ]


# ---------------------------------------------------------------------------
# Dependency-aware start order (criterion 10)
# ---------------------------------------------------------------------------
class TestDependencyAwareStartOrder:
    def test_the_provider_starts_before_the_consumer(self):
        elements = {
            CONSUMER: element(
                CONSUMER,
                dependencies=(DependencyEndpoint(PROVIDER, "127.0.0.1:9101"),),
            ),
            PROVIDER: element(PROVIDER, published_endpoint="127.0.0.1:9101"),
        }
        order = start_order(elements)
        assert order == (PROVIDER, CONSUMER)
        # Alphabetical order would start the consumer first, which is exactly
        # the wrong order the owner rejected (decision P2).
        assert order != tuple(sorted(elements))

    def test_the_order_is_deterministic_whatever_the_element_order(self):
        provider = element(PROVIDER, published_endpoint="127.0.0.1:9101")
        consumer = element(
            CONSUMER, dependencies=(DependencyEndpoint(PROVIDER, "127.0.0.1:9101"),)
        )
        assert start_order({CONSUMER: consumer, PROVIDER: provider}) == (
            PROVIDER,
            CONSUMER,
        )
        assert start_order({PROVIDER: provider, CONSUMER: consumer}) == (
            PROVIDER,
            CONSUMER,
        )

    def test_a_three_level_chain_is_fully_ordered(self):
        chain = {
            "identity": element(
                "identity", dependencies=(DependencyEndpoint("records", "h:1"),)
            ),
            "records": element(
                "records",
                published_endpoint="h:1",
                dependencies=(DependencyEndpoint(PROVIDER, "h:2"),),
            ),
            PROVIDER: element(PROVIDER, published_endpoint="h:2"),
        }
        assert start_order(chain) == (PROVIDER, "records", CONSUMER)

    def test_a_dependency_cycle_fails_closed_instead_of_retrying(self):
        """No start order satisfies a cycle, and a retry is not a substitute."""
        cyclic = {
            PROVIDER: element(
                PROVIDER, dependencies=(DependencyEndpoint(CONSUMER, "h:1"),)
            ),
            CONSUMER: element(
                CONSUMER, dependencies=(DependencyEndpoint(PROVIDER, "h:2"),)
            ),
        }
        with pytest.raises(StartupFailed) as caught:
            start_order(cyclic)
        assert any("cycle" in entry for entry in caught.value.errors)


# ---------------------------------------------------------------------------
# Provider-side listener (criterion 3)
# ---------------------------------------------------------------------------
class TestProviderSideListener:
    def test_the_listener_serves_the_asgi_contract_on_the_declared_endpoint(self):
        port = free_port()
        endpoint = f"127.0.0.1:{port}"
        with PublishedEndpointServer(provider_app(), endpoint) as server:
            assert server.endpoint == endpoint
            status, body = get_json("127.0.0.1", port, "/health")
        assert status == 200
        assert body["component_id"] == PROVIDER
        assert body["platform_id"] == PLATFORM_ID

    def test_the_listener_serves_an_authenticated_read_over_the_network(self):
        port = free_port()
        with PublishedEndpointServer(provider_app(seed_demo=True), f"127.0.0.1:{port}"):
            client = HttpTenantAuthorityClient(
                f"127.0.0.1:{port}",
                credential=SERVICE_TOKEN,
                expected_platform_id=PLATFORM_ID,
            )
            snapshot = client.lookup("ten_a")
        assert snapshot.tenant_id == "ten_a"
        assert snapshot.platform_id == PLATFORM_ID

    def test_the_lifecycle_read_crosses_the_same_transport(self):
        port = free_port()
        with PublishedEndpointServer(provider_app(seed_demo=True), f"127.0.0.1:{port}"):
            client = HttpTenantAuthorityClient(
                f"127.0.0.1:{port}",
                credential=SERVICE_TOKEN,
                expected_platform_id=PLATFORM_ID,
            )
            decision = client.lifecycle_decision("ten_a")
        assert decision.tenant_id == "ten_a"
        assert decision.permitted is True

    def test_an_endpoint_that_cannot_be_bound_is_a_hard_failure(self):
        """Criteria 5/8: no substitute address, no in-process fallback."""
        port = free_port()
        endpoint = f"127.0.0.1:{port}"
        with (
            PublishedEndpointServer(provider_app(), endpoint),
            pytest.raises(PublishedEndpointError) as caught,
        ):
            PublishedEndpointServer(provider_app(), endpoint).serve()
        assert endpoint in str(caught.value)

    def test_the_listener_releases_the_endpoint_when_it_stops(self):
        port = free_port()
        endpoint = f"127.0.0.1:{port}"
        server = PublishedEndpointServer(provider_app(), endpoint)
        server.serve()
        server.stop()
        # The address is free again: nothing keeps serving after a stop.
        with PublishedEndpointServer(provider_app(), endpoint):
            status, _ = get_json("127.0.0.1", port, "/health")
        assert status == 200

    @pytest.mark.parametrize("endpoint", ["", "127.0.0.1", "127.0.0.1:port", ":9101"])
    def test_a_malformed_endpoint_is_refused_before_anything_is_bound(
        self, endpoint: str
    ):
        with pytest.raises(PublishedEndpointError):
            PublishedEndpointServer(object(), endpoint)


# ---------------------------------------------------------------------------
# Consumer fail-closed behaviour (criteria 1, 5, 8)
# ---------------------------------------------------------------------------
class TestConsumerFailClosed:
    def test_an_unavailable_provider_is_never_substituted(self):
        port = free_port()  # nothing is listening there
        client = HttpTenantAuthorityClient(
            f"127.0.0.1:{port}", credential=SERVICE_TOKEN
        )
        with pytest.raises(TenantAuthorityUnavailable) as caught:
            client.lookup("ten_a")
        assert f"127.0.0.1:{port}" in str(caught.value)

    def test_a_wrong_credential_is_refused_by_the_provider_not_hidden(self):
        port = free_port()
        client = HttpTenantAuthorityClient(
            f"127.0.0.1:{port}", credential="svc-token-wrong"
        )
        with (
            PublishedEndpointServer(provider_app(seed_demo=True), f"127.0.0.1:{port}"),
            pytest.raises(AuthenticationDenied),
        ):
            client.lookup("ten_a")

    def test_a_tenant_of_another_platform_is_not_disclosed(self):
        port = free_port()
        with PublishedEndpointServer(provider_app(seed_demo=True), f"127.0.0.1:{port}"):
            client = HttpTenantAuthorityClient(
                f"127.0.0.1:{port}",
                credential=SERVICE_TOKEN,
                expected_platform_id=PLATFORM_ID,
            )
            with pytest.raises(TenantNotFound):
                client.lookup("ten_foreign")

    def test_a_missing_endpoint_is_a_build_error_not_a_local_tenant_authority(self):
        """Criterion 8: there is no hidden in-process fallback."""
        with pytest.raises(ConfigurationError) as caught:
            build_deployment(
                {"current_platform_id": PLATFORM_ID},
                environment={TENANT_AUTHORITY_CREDENTIAL_ENV: SERVICE_TOKEN},
            )
        assert TENANT_AUTHORITY_ENDPOINT_ENV in str(caught.value)

    def test_a_missing_credential_is_a_build_error(self):
        with pytest.raises(ConfigurationError) as caught:
            build_deployment(
                {"current_platform_id": PLATFORM_ID},
                environment={TENANT_AUTHORITY_ENDPOINT_ENV: "127.0.0.1:9101"},
            )
        assert TENANT_AUTHORITY_CREDENTIAL_ENV in str(caught.value)

    def test_a_malformed_endpoint_is_a_build_error(self):
        with pytest.raises(ConfigurationError):
            build_deployment(
                {"current_platform_id": PLATFORM_ID},
                environment={
                    TENANT_AUTHORITY_ENDPOINT_ENV: "http://tenant-authority:9101",
                    TENANT_AUTHORITY_CREDENTIAL_ENV: SERVICE_TOKEN,
                },
            )

    def test_the_consumer_reaches_the_provider_only_over_the_network(self):
        """Criteria 1/12: the consumer holds no provider internals."""
        source = (ROOT / "src/identity_service/deployment.py").read_text(
            encoding="utf-8"
        )
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {
            "tenant_authority.deployment",
            "tenant_authority.engine",
            "tenant_authority.store",
            "tenant_authority.transport",
            "tenant_authority.api",
        }
        assert not (imported & forbidden), sorted(imported & forbidden)
        # The published contract values and error vocabulary are the only
        # provider surface this component touches.
        assert "tenant_authority.contracts" in imported
        assert "tenant_authority.errors" in imported


# ---------------------------------------------------------------------------
# End-to-end through the committed artifacts (criteria 1, 2, 3, 9, 10)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def committed_instance() -> Instance:
    document = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return instance_for(manifest, platform_id=document["platform_id"], root=ROOT)


def committed_request(
    tmp_path: Path, instance: Instance, endpoint: str, *, secrets: Mapping[str, str]
) -> DeploymentRequest:
    """A deployment request of the committed artifacts at one endpoint."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document = json.loads(ENVIRONMENT_PATH.read_text(encoding="utf-8"))
    by_id = {entry["component_id"]: entry for entry in document["bindings"]}
    by_id[PROVIDER]["published_endpoint"] = endpoint
    by_id[CONSUMER]["dependency_endpoints"][PROVIDER]["endpoint"] = endpoint
    document["runtime_root"] = str(tmp_path / "runtime")
    environment = replace(
        load_environment(document, base_dir=tmp_path), secrets=dict(secrets)
    )
    return request_for(instance, manifest, environment)


class TestEndToEndRuntimeDependency:
    def test_the_provider_serves_its_declared_endpoint_from_its_own_process(
        self, tmp_path: Path, committed_instance: Instance
    ):
        port = free_port()
        endpoint = f"127.0.0.1:{port}"
        request = committed_request(
            tmp_path,
            committed_instance,
            endpoint,
            secrets={TENANT_AUTHORITY_CREDENTIAL_ENV: SERVICE_TOKEN},
        )
        with deploy(request) as deployment:
            started = stage_detail(deployment.record.document(), "starting")
            # Criterion 10: the order is dependency-derived, not alphabetical.
            assert started["started_in"] == [PROVIDER, CONSUMER]
            assert started["started_in"] != sorted(started["started"])

            # Criterion 3: the provider's own runtime process answers the
            # declared endpoint. This is a real socket to a real subprocess.
            status, body = get_json("127.0.0.1", port, "/health")
            assert status == 200
            assert body["component_id"] == PROVIDER

            # Criterion 1: the consumer's own client reaches that endpoint over
            # the network and is authenticated by the provider.
            client = HttpTenantAuthorityClient(
                endpoint,
                credential=SERVICE_TOKEN,
                expected_platform_id=committed_instance.document["platform_id"],
            )
            with pytest.raises(AuthenticationDenied):
                # The provider process holds no seeded service identity, so the
                # honest answer is its own authentication refusal: the request
                # reached the provider over the network and was judged there.
                # There is no cached value and no local Tenant to fall back on.
                client.lookup("ten_a")

    def test_an_endpoint_that_is_already_taken_fails_the_deployment_closed(
        self, tmp_path: Path, committed_instance: Instance
    ):
        port = free_port()
        endpoint = f"127.0.0.1:{port}"
        request = committed_request(
            tmp_path,
            committed_instance,
            endpoint,
            secrets={TENANT_AUTHORITY_CREDENTIAL_ENV: SERVICE_TOKEN},
        )
        occupant = build_provider({"platform_id": "other-platform"})
        with (
            PublishedEndpointServer(occupant.contract_app(), endpoint),
            pytest.raises(Exception) as caught,
            deploy(request),
        ):
            raise AssertionError("the deployment must not succeed")
        diagnostics = [*getattr(caught.value, "errors", []), str(caught.value)]
        assert any(endpoint in entry for entry in diagnostics), diagnostics

    def test_the_consumer_is_not_deployed_without_its_credential(
        self, tmp_path: Path, committed_instance: Instance
    ):
        """The dependency credential is required; an empty one is refused."""
        port = free_port()
        request = committed_request(
            tmp_path, committed_instance, f"127.0.0.1:{port}", secrets={}
        )
        with pytest.raises(Exception) as caught, deploy(request):
            raise AssertionError("the deployment must not succeed")
        diagnostics = [*getattr(caught.value, "errors", []), str(caught.value)]
        assert any(
            TENANT_AUTHORITY_CREDENTIAL_ENV in entry for entry in diagnostics
        ), diagnostics

    def test_tenant_authority_remains_independently_deployable(
        self, tmp_path: Path, committed_instance: Instance
    ):
        """Criterion 9: the provider needs no consumer to be deployed."""
        port = free_port()
        provider_manifest = manifest_for(
            [
                {
                    "component_id": PROVIDER,
                    "component_version": registry_versions()[PROVIDER],
                }
            ],
            manifest_id=PLATFORM_ID,
            configuration={PROVIDER: {"platform_id": PLATFORM_ID}},
        )
        instance = instance_for(provider_manifest, platform_id=PLATFORM_ID)
        document = json.loads(ENVIRONMENT_PATH.read_text(encoding="utf-8"))
        document["bindings"] = [
            entry for entry in document["bindings"] if entry["component_id"] == PROVIDER
        ]
        document["bindings"][0]["published_endpoint"] = f"127.0.0.1:{port}"
        document["runtime_root"] = str(tmp_path / "runtime")
        environment = load_environment(document, base_dir=tmp_path)
        request = request_for(instance, provider_manifest, environment)
        with deploy(request):
            status, body = get_json("127.0.0.1", port, "/health")
        assert status == 200
        assert body["component_id"] == PROVIDER
        # The committed instance declares the same provider identity.
        declared = {
            entry["component_id"]: entry["component_version"]
            for entry in committed_instance.document["components"]
        }
        assert declared[PROVIDER] == "0.1.0"


# ---------------------------------------------------------------------------
# Shared builder for the binding-validation tests
# ---------------------------------------------------------------------------
def registry_versions() -> dict[str, str]:
    """The authoritative component versions, read from the Component Registry."""
    document = json.loads(
        (ROOT / "factory/registry/component_registry.json").read_text(encoding="utf-8")
    )
    return {
        entry["component_id"]: entry["component_version"]
        for entry in document["components"]
    }


def _verification(members: Sequence[str]):
    versions = registry_versions()
    selection = [
        {
            "component_id": component_id,
            "component_version": versions[component_id],
        }
        for component_id in members
    ]
    manifest = manifest_for(selection, manifest_id=PLATFORM_ID)
    instance = instance_for(manifest, platform_id=PLATFORM_ID)
    return verify_instance(instance.document, manifest, root=ROOT)


def binding_errors(
    *,
    published: str = "127.0.0.1:9101",
    dependency: str | None = None,
    dependency_on: str = PROVIDER,
    provider_endpoint: str | None = None,
    version_range: str = ">=0.1.0,<0.2.0",
    duplicate_published: bool = False,
    declare_commerce_member: bool = False,
    consumer_is_provider: bool = False,
) -> list[str]:
    """Validate one environment whose consumer declares one dependency."""
    provider = published if provider_endpoint is None else provider_endpoint
    declared_endpoint = dependency if dependency is not None else provider
    consumer_id = PROVIDER if consumer_is_provider else CONSUMER
    consumer_module = (
        "tenant_authority.deployment"
        if consumer_is_provider
        else "identity_service.deployment"
    )
    bindings = [
        ComponentRuntimeBinding(
            component_id=PROVIDER,
            deployment_module="tenant_authority.deployment",
            deployment_factory="build_deployment",
            published_endpoint=provider,
        ),
        ComponentRuntimeBinding(
            component_id=consumer_id,
            deployment_module=consumer_module,
            deployment_factory="build_deployment",
            dependency_endpoints=(
                DependencyEndpoint(dependency_on, declared_endpoint, version_range),
            ),
        ),
    ]
    if duplicate_published:
        bindings.append(
            ComponentRuntimeBinding(
                component_id=CONSUMER,
                deployment_module="identity_service.deployment",
                deployment_factory="build_deployment",
                published_endpoint=published,
            )
        )
    members = [PROVIDER, CONSUMER]
    if declare_commerce_member:
        members.append("commerce")
    environment = environment_for(Path("/unused/runtime"), bindings=tuple(bindings))
    return validate_environment(environment, _verification(members))
