"""Shared test fixtures.

The Level 0 modular monolith composes IS-001 (Identity / Tenant Context),
IS-002 (Tenant Authority), IS-003 (Authorization Boundary) and IS-004
(Resource Boundary — the data owner). Each component owns its store; every
cross-component read goes through a published contract client.

This module is also the composition root used by the tests: building a component
app from another component's deployment happens in the harness, never inside a
component's published API (see ``monolith()``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from authorization_service.adapters import identity_port, tenant_authority_port
from authorization_service.contracts import (
    AuthorizationDecision,
    ContractViolation as AuthorizationContractViolation,
    ResourceRef,
)
from authorization_service.deployment import (
    AuthorizationDeployment,
    build_deployment as build_authorization,
)
from authorization_service.reader import AuthorizationClient
from identity_service.api import create_app as create_identity_app
from identity_service.config import IdentityConfig
from identity_service.engine import IdentityEngine
from identity_service.errors import AccessDenied as IdentityAccessDenied
from identity_service.errors import ContractViolation as IdentityContractViolation
from identity_service.reader import IdentityContextClient, build_client as build_identity_client
from identity_service.store import IdentityStore
from records_service.adapters import authorization_port as records_authorization_port
from records_service.deployment import RecordsDeployment, build_deployment as build_records
from records_service.errors import AccessRefused as RecordsAccessRefused
from records_service.errors import ContractViolation as RecordsContractViolation
from records_service.reader import RecordsClient
from records_service.store import RecordsStore
from saga.consumed import TenantContextAnswer, TenantContextRefusal
from saga.errors import StepFailure
from saga.executor import SagaExecutor
from saga.models import RetryPolicy, SagaDefinition, StepDefinition
from tenant_authority.contracts import TenantState
from tenant_authority.deployment import TenantAuthorityDeployment, build_deployment
from tenant_authority.lifecycle import LIFECYCLE_CHAIN
from tenant_authority.reader import TenantAuthorityClient

PLATFORM_ID = "plt_demo"
DEMO_CONFIG = {"platform_id": PLATFORM_ID, "environment": "test"}
#: Demo service identity of a consumer component: contract reads and nothing else.
CONSUMER_CREDENTIAL = "svc-token-identity"
#: Service identity IS-003 uses when it reads Tenant lifecycle from IS-002.
AUTHORIZATION_TA_CREDENTIAL = "svc-token-authorization"
#: Demo service identity of a data owner allowed to ask IS-003 for decisions.
DATA_OWNER_CREDENTIAL = "authz-svc-token-records"


def tenant_authority(seed_demo: bool = True) -> TenantAuthorityDeployment:
    """Standalone Tenant Authority deployment (IS-002 under test).

    The deployment is composition-internal: it is what the component's own tests
    and a composition root use. A consumer is handed :func:`client_for`.
    """
    return build_deployment(DEMO_CONFIG, seed_demo=seed_demo, with_http=True)


def client_for(
    authority: TenantAuthorityDeployment,
    credential: str = CONSUMER_CREDENTIAL,
    *,
    expected_platform_id: str | None = PLATFORM_ID,
) -> TenantAuthorityClient:
    """The published consumer surface of ``authority`` — values in, values out."""
    return authority.publish(
        credential=credential,
        expected_platform_id=expected_platform_id,
    )


@dataclass
class Monolith:
    """One composed Platform Instance: the published APIs over one deployment."""

    identity: IdentityEngine
    authority: TenantAuthorityDeployment
    authorization: AuthorizationDeployment
    records: RecordsDeployment
    tenant_authority_client: TenantAuthorityClient
    identity_context_client: IdentityContextClient
    authorization_client: AuthorizationClient
    identity_app: Any
    authority_app: Any
    authorization_app: Any
    records_app: Any

    def identity_client(self) -> TestClient:
        return TestClient(self.identity_app)

    def authority_client(self) -> TestClient:
        return TestClient(self.authority_app)

    def authorization_http(self) -> TestClient:
        return TestClient(self.authorization_app)

    def records_http(self) -> TestClient:
        return TestClient(self.records_app)

    def records_client(self) -> RecordsClient:
        """The published consumer surface of the data owner (Level 0 client)."""
        return self.records.publish()

    def data_owner(self, records: dict[str, str] | None = None) -> "RecordsBoundary":
        """A demo data owner enforcing IS-003 decisions at its own boundary."""
        return RecordsBoundary(
            authorization=self.authorization_client,
            records=dict(records or {"rec_a1": "ten_a", "rec_b1": "ten_b"}),
        )


def monolith() -> Monolith:
    """Compose the three components for the duration of one test.

    Fresh instances every call: a test that suspends a Tenant must not disturb
    the demo state another test relies on.
    """
    authority = tenant_authority()
    store = IdentityStore()
    store.seed_demo()
    identity_config = IdentityConfig.from_mapping(
        {
            # All components are bound to the same Platform Instance; a mismatch
            # would be denied by the contract cross-check, not ignored.
            "current_platform_id": authority.current_platform_id,
            "environment": authority.config.environment,
        }
    )
    client = client_for(authority, expected_platform_id=identity_config.current_platform_id)
    identity = IdentityEngine(store=store, tenant_authority=client, config=identity_config)
    identity_app = create_identity_app(identity)

    # IS-003 consumes both published contracts and owns neither: it receives a
    # value-only client of each, with its own service identity where the provider
    # authenticates one.
    identity_context_client = build_identity_client(identity_app)
    authorization_authority_client = client_for(
        authority,
        AUTHORIZATION_TA_CREDENTIAL,
        expected_platform_id=identity_config.current_platform_id,
    )
    # The published clients are adapted to the ports of IS-003 by IS-003's own
    # adapters: the component consumes contracts, never another component's
    # types. Wiring them is the job of this composition root.
    authorization = build_authorization(
        {
            "platform_id": authority.current_platform_id,
            "environment": authority.config.environment,
        },
        identity=identity_port(identity_context_client),
        tenant_authority=tenant_authority_port(authorization_authority_client),
        seed_demo=True,
        with_http=True,
    )
    # IS-004 is the data owner. It consumes the published decision contract of
    # IS-003 through its own adapter: the composition root hands it a
    # value-only client with the component's own service identity, and nothing
    # else of IS-003 crosses the boundary.
    records = build_records(
        {
            "platform_id": authority.current_platform_id,
            "environment": authority.config.environment,
        },
        authorization=records_authorization_port(
            authorization.publish(credential=DATA_OWNER_CREDENTIAL)
        ),
        seed_demo=True,
        with_http=True,
    )
    return Monolith(
        identity=identity,
        authority=authority,
        authorization=authorization,
        records=records,
        tenant_authority_client=client,
        identity_context_client=identity_context_client,
        authorization_client=authorization.publish(credential=DATA_OWNER_CREDENTIAL),
        identity_app=identity_app,
        authority_app=authority.contract_app(),
        authorization_app=authorization.contract_app(),
        records_app=records.contract_app(),
    )


class Refused(Exception):
    """A demo data owner's own refusal, carrying the reason it was given."""

    def __init__(self, decision: AuthorizationDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason.value)


@dataclass
class RecordsBoundary:
    """Demo data owner: it asks IS-003 and enforces the answer itself.

    This is the consumer side of the contract boundary (invariant 8). The
    component holds a value-only client, receives a value back and performs the
    refusal on its own: IS-003 never touches a record, and nothing it returns
    can serve data by itself.
    """

    authorization: AuthorizationClient
    records: dict[str, str] = field(default_factory=dict)

    def read(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        return self._access(
            subject_credential,
            record_id,
            operation="records.read",
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def write(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        return self._access(
            subject_credential,
            record_id,
            operation="records.write",
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def decision_for(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        operation: str = "records.read",
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AuthorizationDecision:
        return self.authorization.decide(
            subject_credential,
            operation=operation,
            resource=ResourceRef("record", record_id, self.records.get(record_id)),
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def _access(
        self,
        subject_credential: str | None,
        record_id: str,
        *,
        operation: str,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
    ) -> str:
        decision = self.decision_for(
            subject_credential,
            record_id,
            operation=operation,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not decision.allowed:
            # The data owner enforces: the authority only answered.
            raise Refused(decision)
        return f"{record_id}:{self.records[record_id]}"


# ---------------------------------------------------------------- IS-004 harness
class CountingRecordsStore(RecordsStore):
    """A records store that counts the owned-data operations.

    The counts are the behavioral proof of invariant 3 and 4: a denied access
    must leave both counters at zero, whatever else it did.
    """

    def __init__(self) -> None:
        super().__init__()
        self.served_reads = 0
        self.applied_transitions = 0

    def serve_resource(self, resource_id: str) -> Any:
        served = super().serve_resource(resource_id)
        self.served_reads += 1
        return served

    def apply_transition(self, resource_id: str, transition: str) -> Any:
        # Counted after the write: the counters mean "owned-data operations
        # that actually executed", so a refusal inside the store counts as
        # nothing executed.
        updated = super().apply_transition(resource_id, transition)
        self.applied_transitions += 1
        return updated


class StubAuthorizationPort:
    """A decision port with a fixed outcome: a value to return or an exception.

    The records engine must behave identically for any port answering its own
    vocabulary — this stub is how the tests prove the enforcement logic depends
    on nothing of the real provider.
    """

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def decide(self, subject_credential, *, operation, resource_type, resource_id,
               resource_tenant_id, claimed_tenant_id=None, request_id=None,
               correlation_id=None):
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


def records_deployment(
    port: Any,
    *,
    store: RecordsStore | None = None,
    seed_demo: bool = True,
) -> RecordsDeployment:
    """Standalone Resource Boundary deployment over a given decision port."""
    return build_records(
        {"platform_id": PLATFORM_ID, "environment": "test"},
        authorization=port,
        store=store,
        seed_demo=seed_demo,
    )


def composed() -> tuple[IdentityEngine, TenantAuthorityDeployment]:
    """Identity engine wired to its own Tenant Authority through the contract."""
    instance = monolith()
    return instance.identity, instance.authority


def engine() -> IdentityEngine:
    return composed()[0]


def provision_tenant(
    authority: TenantAuthorityDeployment,
    name: str,
    state: TenantState = TenantState.PROVISIONING,
) -> str:
    """Create a Tenant and bring it to ``state`` using only accepted transitions.

    States are never assigned by hand: a test that fakes a lifecycle state would
    not prove that the state machine works.
    """
    tenant_id = f"ten_{name}"
    authority.engine.create_tenant("svc-token-admin", tenant_id=tenant_id)
    for next_state in LIFECYCLE_CHAIN[1 : LIFECYCLE_CHAIN.index(state) + 1]:
        authority.engine.transition_tenant("svc-token-admin", tenant_id, next_state)
    assert authority.engine.lookup(tenant_id).state is state
    return tenant_id


# ---------------------------------------------------------------- IS-006 harness
#
# The Saga boundary is a platform service with no business data of its own, so
# its proof needs a composition: workflows whose steps reach a data owner
# through a published contract. Everything below is composition-root material —
# the same role ``RecordsBoundary`` plays for IS-004 — and none of it lives
# inside the component.

#: Human of Tenant A; the composition root grants it the reservation operations.
SAGA_CREDENTIAL_A = "token-human-a"
#: Service identity of Tenant A with ``records.read`` only: authenticated, but
#: not authorized for the reservation operations (authentication is not
#: authorization).
SAGA_CREDENTIAL_SERVICE = "token-service"
#: Human of Tenant B, used to prove that a workflow of Tenant A cannot act for B.
SAGA_CREDENTIAL_B = "token-human-b"
#: A second human of Tenant A: same tenant, another identity.
SAGA_CREDENTIAL_C = "token-human-c"

RESERVATION_TYPE = "reservation"
RESERVATION_RESERVE = "reservations.reserve"
RESERVATION_RELEASE = "reservations.release"
RESERVATION_OPERATIONS = (RESERVATION_RESERVE, RESERVATION_RELEASE)

#: Denial reasons that are security-relevant: they must stay observable.
SECURITY_REFUSAL_REASONS = frozenset(
    {
        "missing_identity",
        "invalid_identity",
        "unknown_identity",
        "missing_tenant_context",
        "tenant_mismatch",
        "tenant_unknown",
        "platform_ownership_mismatch",
        "resource_tenant_unknown",
        "resource_tenant_mismatch",
        "permission_not_granted",
    }
)

#: Denial reasons that mean "the dependency could not answer": transient, and
#: therefore the only refusals a step declares retryable.
DEPENDENCY_REFUSAL_REASONS = frozenset({"authorization_unavailable"})


class DomainRefused(Exception):
    """The demo data owner's own domain refusal, with a machine-readable reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ReservationView:
    """The value an allowed reservation operation returns. Business data."""

    reservation_id: str
    tenant_id: str
    state: str


@dataclass
class ReservationsBoundary:
    """Demo data owner for IS-006: reversible operations behind the real IS-003 chain.

    Reserve and release are exact inverses, which is what makes a compensation
    observable as a real business operation instead of a no-op. The boundary
    owns its data, asks IS-003 for every operation and enforces the answer
    itself — exactly the consumer side of the contract boundary that
    ``RecordsBoundary`` demonstrates for IS-004. The counters are the
    behavioral proof that a refused or replayed step applied no effect.
    """

    authorization: AuthorizationClient
    tenants: dict[str, str] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    applied_reserves: int = 0
    applied_releases: int = 0

    @property
    def applied_effects(self) -> int:
        return self.applied_reserves + self.applied_releases

    def reserve(
        self,
        subject_credential: str | None,
        reservation_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ReservationView:
        self._decide(
            subject_credential,
            RESERVATION_RESERVE,
            reservation_id,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if reservation_id not in self.tenants:
            raise DomainRefused("reservation_unknown")
        if self.states[reservation_id] == "reserved":
            # The owner's own state machine: a repeated reserve is a refusal,
            # never a second effect.
            raise DomainRefused("already_reserved")
        self.states[reservation_id] = "reserved"
        self.applied_reserves += 1
        return ReservationView(reservation_id, self.tenants[reservation_id], "reserved")

    def release(
        self,
        subject_credential: str | None,
        reservation_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ReservationView:
        self._decide(
            subject_credential,
            RESERVATION_RELEASE,
            reservation_id,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if reservation_id not in self.tenants:
            raise DomainRefused("reservation_unknown")
        if self.states[reservation_id] != "reserved":
            raise DomainRefused("not_reserved")
        self.states[reservation_id] = "available"
        self.applied_releases += 1
        return ReservationView(reservation_id, self.tenants[reservation_id], "available")

    def _decide(
        self,
        subject_credential: str | None,
        operation: str,
        reservation_id: str,
        *,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
    ) -> AuthorizationDecision:
        decision = self.authorization.decide(
            subject_credential,
            operation=operation,
            resource=ResourceRef(
                RESERVATION_TYPE, reservation_id, self.tenants.get(reservation_id)
            ),
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not decision.allowed:
            # The data owner enforces; the authority only answered.
            raise Refused(decision)
        return decision


class IdentityContextAdapter:
    """IS-006's tenant-context port over the published client of IS-001.

    This is the composition root's translation, not a second identity
    mechanism: the answer is reduced to the values of
    :mod:`saga.consumed`, and a refusal stays a refusal.
    """

    def __init__(self, client: IdentityContextClient) -> None:
        self._client = client

    def resolve(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TenantContextAnswer | TenantContextRefusal:
        try:
            context = self._client.resolve_context(
                subject_credential,
                claimed_tenant_id=claimed_tenant_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )
        except IdentityAccessDenied as exc:
            return TenantContextRefusal(exc.reason.value)
        except IdentityContractViolation:
            return TenantContextRefusal("tenant_context_unavailable")
        return TenantContextAnswer(
            identity_id=context.identity_id,
            tenant_id=context.tenant_id,
            platform_id=context.platform_id,
            kind=context.kind.value,
        )


def contract_step(call: Any) -> Any:
    """Wrap a component-contract call in the failure vocabulary of IS-006.

    A refusal of a business boundary becomes a :class:`saga.errors.StepFailure`
    carrying the published reason unchanged; only a dependency that could not
    answer is declared retryable, and identity/tenant/permission denials are
    marked security-sensitive so the workflow audits them as such.
    """

    def handler(ctx: Any) -> Any:
        try:
            return call(ctx)
        except Refused as exc:
            reason = exc.decision.reason.value
            raise StepFailure(
                reason,
                retryable=reason in DEPENDENCY_REFUSAL_REASONS,
                security_sensitive=reason in SECURITY_REFUSAL_REASONS,
            ) from exc
        except RecordsAccessRefused as exc:
            raise StepFailure(
                exc.reason,
                retryable=exc.reason in DEPENDENCY_REFUSAL_REASONS,
                security_sensitive=exc.reason in SECURITY_REFUSAL_REASONS,
            ) from exc
        except DomainRefused as exc:
            raise StepFailure(exc.reason) from exc
        except (
            AuthorizationContractViolation,
            RecordsContractViolation,
            IdentityContractViolation,
        ) as exc:
            # The dependency did not answer at all: transient, never a success.
            raise StepFailure("dependency_unavailable", retryable=True) from exc

    return handler


@dataclass
class SagaHarness:
    """One composed Platform Instance plus the Saga executor under test."""

    instance: Monolith
    executor: SagaExecutor
    reservations: ReservationsBoundary
    records_client: RecordsClient
    records_store: CountingRecordsStore

    @property
    def guard(self) -> Any:
        """The IS-005 command-safety boundary this executor delivers through."""
        return self.executor.idempotency

    def actions(self, saga_id: str) -> list[str]:
        return [event.action for event in self.executor.audit_trail(saga_id)]

    def events(self, saga_id: str) -> list[Any]:
        return list(self.executor.audit_trail(saga_id))

    def definition(self, name: str, *steps: StepDefinition) -> SagaDefinition:
        return SagaDefinition(name, steps)

    def reserve_step(
        self,
        step_id: str,
        reservation_id: str,
        *,
        retry: RetryPolicy | None = None,
        timeout_seconds: float | None = None,
        fail_before: Any = None,
        fail_after: Any = None,
        undo_fails: Any = None,
        payload: dict[str, Any] | None = None,
    ) -> StepDefinition:
        """A step reserving one resource through the demo data owner's contract.

        ``fail_before`` / ``fail_after`` / ``undo_fails`` accept an exception or
        a callable receiving the step context, so a test can fail the first
        attempt only, fail after the business effect was applied, or fail the
        compensation — deterministically and without threads.
        """
        boundary = self.reservations

        def raise_if(declared: Any, ctx: Any) -> None:
            if declared is None:
                return
            outcome = declared(ctx) if callable(declared) else declared
            if outcome is not None:
                raise outcome

        def call(ctx: Any) -> Any:
            raise_if(fail_before, ctx)
            view = boundary.reserve(
                ctx.subject_credential,
                reservation_id,
                claimed_tenant_id=ctx.tenant_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
            )
            raise_if(fail_after, ctx)
            return view

        def undo(ctx: Any) -> Any:
            raise_if(undo_fails, ctx)
            return boundary.release(
                ctx.subject_credential,
                reservation_id,
                claimed_tenant_id=ctx.tenant_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
            )

        return StepDefinition(
            step_id=step_id,
            operation=RESERVATION_RESERVE,
            execute=contract_step(call),
            compensate=contract_step(undo),
            payload={"reservation_id": reservation_id, **(payload or {})},
            retry=retry or RetryPolicy(),
            timeout_seconds=timeout_seconds,
        )

    def records_step(
        self,
        step_id: str,
        resource_id: str,
        *,
        transition: str | None = None,
        retry: RetryPolicy | None = None,
        fail_before: Any = None,
    ) -> StepDefinition:
        """A step against the real IS-004 data owner, through its published contract.

        Reads change no owned state, so the compensation is an explicit no-op:
        the absence of an undo is declared, not forgotten.
        """
        client = self.records_client

        def call(ctx: Any) -> Any:
            if fail_before is not None:
                outcome = fail_before(ctx) if callable(fail_before) else fail_before
                if outcome is not None:
                    raise outcome
            if transition is not None:
                return client.transition_resource(
                    ctx.subject_credential,
                    resource_id,
                    transition,
                    claimed_tenant_id=ctx.tenant_id,
                    request_id=ctx.request_id,
                    correlation_id=ctx.correlation_id,
                )
            return client.read_resource(
                ctx.subject_credential,
                resource_id,
                claimed_tenant_id=ctx.tenant_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
            )

        def undo(ctx: Any) -> None:
            return None

        return StepDefinition(
            step_id=step_id,
            operation="records.write" if transition else "records.read",
            execute=contract_step(call),
            compensate=contract_step(undo),
            payload={"resource_id": resource_id, "transition": transition},
            retry=retry or RetryPolicy(),
        )

    def start(
        self,
        definition: SagaDefinition,
        credential: str | None = SAGA_CREDENTIAL_A,
        **kwargs: Any,
    ) -> Any:
        return self.executor.start(definition, subject_credential=credential, **kwargs)


def saga_harness(
    *,
    reservations: dict[str, str] | None = None,
    actor_service_id: str = "svc-saga",
    **executor_kwargs: Any,
) -> SagaHarness:
    """Compose IS-001/IS-002/IS-003/IS-004 plus the IS-006 executor for one test.

    The saga receives an IS-001 tenant-context port and its own IS-005 guard;
    the business steps it is given reach the data owners through their published
    contracts. Nothing of any other component's internals crosses into it.
    """
    instance = monolith()
    # Provisioning the grant vocabulary of the demo data owner is the
    # composition root's job: IS-003 publishes no grant management API.
    instance.authorization.store.grant(
        "ten_a", "idn_human_a", *RESERVATION_OPERATIONS
    )
    instance.authorization.store.grant(
        "ten_b", "idn_human_b", *RESERVATION_OPERATIONS
    )

    owned = reservations if reservations is not None else {
        "res_a1": "ten_a",
        "res_a2": "ten_a",
        "res_a3": "ten_a",
        "res_b1": "ten_b",
    }
    boundary = ReservationsBoundary(
        authorization=instance.authorization_client,
        tenants=dict(owned),
        states={reservation_id: "available" for reservation_id in owned},
    )

    # A second data owner over the real IS-004 component, with a counting store
    # so "no business effect" is a measured fact and not an assumption.
    counting = CountingRecordsStore()
    counting.seed_demo()
    records = build_records(
        {
            "platform_id": instance.authority.current_platform_id,
            "environment": instance.authority.config.environment,
        },
        authorization=records_authorization_port(
            instance.authorization.publish(credential=DATA_OWNER_CREDENTIAL)
        ),
        store=counting,
        seed_demo=False,
        with_http=True,
    )

    executor = SagaExecutor(
        tenant_context=IdentityContextAdapter(instance.identity_context_client),
        actor_service_id=actor_service_id,
        **executor_kwargs,
    )
    return SagaHarness(
        instance=instance,
        executor=executor,
        reservations=boundary,
        records_client=records.publish(),
        records_store=counting,
    )
