"""Composition-internal deployment of the Identity / Tenant Context component.

This module assembles the engine and the published contract app of one Platform
Instance, and wires the one thing this component cannot own: its **runtime
dependency on Tenant Authority (IS-002)**.

ADR-0021 §2.1/§3 fixes that dependency as a *network* reach over a transport,
with the provider's endpoint **statically declared** by the authoritative
environment binding — not discovered, not allocated, not guessed. Owner
decision P1 makes the same point from the consumer side: this component reads
the endpoint it is given and fails closed when it is not given one. There is
deliberately **no in-process fallback**: composing ``tenant_authority`` here,
importing its deployment, or serving its contract inside this process would
reintroduce exactly the hidden in-process path the decision rules out, and would
make ``tenant_authority`` undeployable on its own.

The endpoint and the service credential arrive at the process boundary, from the
environment binding, as process environment — not as component configuration.
They are operational inputs of the environment, so keeping them out of the
configuration keeps them out of the component's identity-bearing configuration
contract (ARCHITECTURE.md §20; ADR-0016 §12, §13).

The transport itself is the Python standard library (:mod:`http.client`). No
ADR selected a technology for this and none is needed: ADR-0017 §40 leaves the
deployment technology unselected, ADR-0021 prescribes the transport *kind* only,
and this slice adds no third-party dependency.
"""

from __future__ import annotations

import http.client
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from identity_service.config import ConfigurationError, IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.store import IdentityStore
from tenant_authority.contracts import LifecycleDecision, TenantSnapshot, TenantState
from tenant_authority.errors import (
    AuthenticationDenied,
    AuthorizationDenied,
    ContractViolation,
    DenyReason,
    InvalidTransition,
    OwnershipDenied,
    TenantAuthorityError,
    TenantConflict,
    TenantNotFound,
)

#: The provider's endpoint, as the environment binding declared it and as
#: Deployment & Operations injected it into this process.
TENANT_AUTHORITY_ENDPOINT_ENV = "FACTORY_DEPENDENCY_TENANT_AUTHORITY_ENDPOINT"
#: The one service credential this component presents. It is an operational
#: input, injected at the process boundary; it is never part of configuration
#: and is never written to state, logs or events (ADR-0016 §12).
TENANT_AUTHORITY_CREDENTIAL_ENV = "TENANT_AUTHORITY_SERVICE_TOKEN"
#: How long one read may take. A provider that does not answer is a failure of
#: this component's dependency, and it fails closed rather than guessing.
REQUEST_TIMEOUT_SECONDS = 10.0
#: The shape of the endpoint the environment binding declares. Duplicated here
#: rather than imported from Deployment & Operations on purpose: a component
#: must not depend on the deployment capability, so the boundary costs one
#: small pattern instead of an inverted dependency.
_ENDPOINT_RE = re.compile(r"^(?P<host>[A-Za-z0-9._-]+):(?P<port>[0-9]{1,5})$")

#: The published contract's machine-readable reasons rebuilt as the published
#: error classes, so a caller of this component catches the same exception types
#: the provider documents and no transport-specific error crosses the boundary.
_ERROR_BY_REASON: dict[DenyReason, type[TenantAuthorityError]] = {
    DenyReason.TENANT_NOT_FOUND: TenantNotFound,
    DenyReason.FOREIGN_TENANT: OwnershipDenied,
    DenyReason.INVALID_TRANSITION: InvalidTransition,
    DenyReason.TENANT_EXISTS: TenantConflict,
    DenyReason.IDEMPOTENCY_CONFLICT: TenantConflict,
    DenyReason.PLATFORM_MISMATCH: AuthorizationDenied,
    DenyReason.INSUFFICIENT_AUTHORIZATION: AuthorizationDenied,
    DenyReason.MISSING_SERVICE_IDENTITY: AuthenticationDenied,
    DenyReason.INVALID_SERVICE_IDENTITY: AuthenticationDenied,
    DenyReason.UNKNOWN_SERVICE: AuthenticationDenied,
}
_DEFAULT_REASON_BY_STATUS: dict[int, DenyReason] = {
    401: DenyReason.MISSING_SERVICE_IDENTITY,
    403: DenyReason.INSUFFICIENT_AUTHORIZATION,
    404: DenyReason.TENANT_NOT_FOUND,
    409: DenyReason.TENANT_EXISTS,
}


class TenantAuthorityUnavailable(Exception):
    """The declared provider endpoint could not be reached.

    This is never translated into a value, a cached answer or a local copy of
    Tenant state: a dependency that is unavailable is a failure of this
    component (ADR-0021 §3).
    """


class HttpTenantAuthorityClient:
    """The published read surface of Tenant Authority, over the network.

    It implements :class:`identity_service.ports.TenantAuthorityPort` and
    nothing more: two reads over the provider's published HTTP contract. It
    holds three values — a declared endpoint, a service credential and a
    Platform Instance binding — and no application, engine or store, so Tenant
    state has exactly one owner (invariant T-004, LAW-04).
    """

    __slots__ = ("_credential", "_endpoint", "_expected_platform_id", "_host", "_port")

    def __init__(
        self,
        endpoint: str,
        *,
        credential: str,
        expected_platform_id: str | None = None,
    ) -> None:
        if not credential:
            raise ConfigurationError(
                f"`{TENANT_AUTHORITY_CREDENTIAL_ENV}` is required: a network "
                "dependency on Tenant Authority is authenticated, and an empty "
                "credential is not a credential"
            )
        match = _ENDPOINT_RE.fullmatch(endpoint or "")
        if match is None:
            raise ConfigurationError(
                f"`{TENANT_AUTHORITY_ENDPOINT_ENV}`={endpoint!r} is not a "
                "declared '<host>:<port>' endpoint; the environment binding "
                "declares the provider's address statically and this component "
                "does not allocate, discover or substitute one"
            )
        self._endpoint = endpoint
        self._host = match.group("host")
        self._port = int(match.group("port"))
        self._credential = credential
        self._expected_platform_id = expected_platform_id

    @property
    def endpoint(self) -> str:
        """The declared endpoint this client reaches."""
        return self._endpoint

    def lookup(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantSnapshot:
        """Authoritative Tenant record: state plus Platform Instance ownership."""
        payload = self._get(
            f"/api/v1/tenants/{quote(tenant_id, safe='')}",
            expected_platform_id=expected_platform_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return TenantSnapshot(
            tenant_id=payload["tenant_id"],
            platform_id=payload["platform_id"],
            state=TenantState(payload["state"]),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            state_source=payload["state_source"],
        )

    def lifecycle_decision(
        self,
        tenant_id: str,
        *,
        expected_platform_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> LifecycleDecision:
        """Whether an ordinary tenant-scoped operation is permitted right now."""
        payload = self._get(
            f"/api/v1/tenants/{quote(tenant_id, safe='')}/lifecycle",
            expected_platform_id=expected_platform_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return LifecycleDecision(
            tenant_id=payload["tenant_id"],
            platform_id=payload["platform_id"],
            state=TenantState(payload["state"]),
            permitted=bool(payload["permitted"]),
            reason=payload["reason"],
        )

    # ------------------------------------------------------------------ internals
    def _get(
        self,
        path: str,
        *,
        expected_platform_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
    ) -> Mapping[str, Any]:
        platform = (
            self._expected_platform_id
            if expected_platform_id is None
            else expected_platform_id
        )
        headers = {"authorization": f"Bearer {self._credential}"}
        if platform:
            headers["x-platform-id"] = platform
        if request_id:
            headers["x-request-id"] = request_id
        if correlation_id:
            headers["x-correlation-id"] = correlation_id

        connection = http.client.HTTPConnection(
            self._host, self._port, timeout=REQUEST_TIMEOUT_SECONDS
        )
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except OSError as error:
            # Fail closed: the dependency is unavailable. There is no cached
            # answer, no default Tenant and no local fallback composition.
            raise TenantAuthorityUnavailable(
                f"tenant authority at the declared endpoint {self._endpoint} "
                f"is not reachable ({error.__class__.__name__}: {error})"
            ) from error
        finally:
            connection.close()

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractViolation(
                f"tenant authority answered status {status} with a body that is "
                "not JSON"
            ) from error
        if not isinstance(payload, Mapping):
            raise ContractViolation(
                f"tenant authority answered status {status} with a body that is "
                "not an object"
            )
        if 200 <= status < 300:
            return payload
        raise self._error(status, payload)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        if status >= 500:
            return ContractViolation(
                f"tenant authority contract failed with status {status}"
            )
        detail = payload.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        reason = _as_reason(detail.get("reason")) or _DEFAULT_REASON_BY_STATUS.get(
            status, DenyReason.TENANT_NOT_FOUND
        )
        error_type = _ERROR_BY_REASON.get(reason, TenantAuthorityError)
        message = str(detail.get("reason") or reason.value)
        return error_type(reason, message, details=dict(detail))


def _as_reason(value: Any) -> DenyReason | None:
    if not isinstance(value, str):
        return None
    try:
        return DenyReason(value)
    except ValueError:
        return None


@dataclass
class IdentityServiceDeployment:
    """Owned internals of one Identity instance, for the composition root."""

    engine: IdentityEngine
    config: IdentityConfig
    store: IdentityStore
    tenant_authority: HttpTenantAuthorityClient
    http_app: Any = None

    @property
    def current_platform_id(self) -> str:
        return self.config.current_platform_id

    def contract_app(self) -> Any:
        """The published HTTP contract of this component (ASGI application)."""
        if self.http_app is None:
            from identity_service.api import create_app

            self.http_app = create_app(self.engine)
        return self.http_app


def build_deployment(
    configuration: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    seed_demo: bool = False,
    tenant_authority: HttpTenantAuthorityClient | None = None,
) -> IdentityServiceDeployment:
    """Compose one Identity deployment of one Platform Instance.

    The runtime dependency on Tenant Authority is read from the environment
    binding — the provider's declared endpoint and one named service credential
    — and both are required. A missing endpoint or credential is a build error:
    this component does not compose Tenant Authority in-process, and it does not
    start without its authoritative Tenant source (ADR-0021 §3).

    ``environment`` and ``tenant_authority`` exist so a test or a composition
    root can supply them explicitly; a runtime process passes neither and reads
    the process environment.
    """
    config = IdentityConfig.from_mapping(configuration)
    if tenant_authority is None:
        bound = os.environ if environment is None else environment
        endpoint = str(bound.get(TENANT_AUTHORITY_ENDPOINT_ENV, "") or "")
        if not endpoint:
            raise ConfigurationError(
                f"`{TENANT_AUTHORITY_ENDPOINT_ENV}` is required: the environment "
                "binding must declare the Tenant Authority endpoint this "
                "component reaches, and this component neither discovers one "
                "nor substitutes an in-process Tenant Authority"
            )
        credential = str(bound.get(TENANT_AUTHORITY_CREDENTIAL_ENV, "") or "")
        tenant_authority = HttpTenantAuthorityClient(
            endpoint,
            credential=credential,
            expected_platform_id=config.current_platform_id,
        )
    store = IdentityStore()
    engine = IdentityEngine(
        store=store, tenant_authority=tenant_authority, config=config
    )
    if seed_demo:
        store.seed_demo()
    return IdentityServiceDeployment(
        engine=engine,
        config=config,
        store=store,
        tenant_authority=tenant_authority,
    )


__all__ = [
    "TENANT_AUTHORITY_CREDENTIAL_ENV",
    "TENANT_AUTHORITY_ENDPOINT_ENV",
    "HttpTenantAuthorityClient",
    "IdentityServiceDeployment",
    "TenantAuthorityUnavailable",
    "build_deployment",
]
