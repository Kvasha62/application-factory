"""Commerce engine: the enforcement chain of the data owner.

This component is the final enforcement boundary for the Commerce data it
owns (ARCHITECTURE.md §5.3, §6.2): IS-003 decides, and this component
applies the decision at its own boundary — including refusing when the
decision cannot be obtained at all. The chain is fixed, published, and
every step can only deny:

```text
Request (subject credential, record id, operation, [claimed tenant])
      ↓
ownership_boundary       the record exists here and its single owner is
                         this component                  else DENY
      ↓
authorization_decision   IS-003 decides through the port; the default
                         outcome is DENY; a dependency that does not answer
                         or answers outside its contract fails closed
      ↓
owned_data_operation     only now is the owned data read or written
```

An ``ALLOW`` is not data access: the owned-data operation runs only here,
at the end, and a request denied earlier never reaches it. Both the served
accesses and every refusal are audited with ``request_id`` and
``correlation_id``.

Stage 2 publishes exactly two operations on this chain: create one owned
Product and read one owned Product. The create command derives the
effective tenant from the verified identity (its target does not exist
yet), asks IS-003 about the product-to-be, and delivers the creation
effect to the IS-005 guard exactly once.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from commerce_service import COMPONENT_ID, COMPONENT_VERSION
from commerce_service.config import CommerceConfig
from commerce_service.consumed import (
    ALLOW,
    DENY,
    DENY_REASONS,
    PERMITTED,
    DependencyRefusal,
    TenantContextRefusal,
)
from commerce_service.contracts import (
    OPERATION_PRODUCT_CREATE,
    OPERATION_PRODUCT_READ,
    OwnDenyReason,
    ProductView,
)
from commerce_service.errors import AccessRefused
from commerce_service.models import (
    AccessAuditEvent,
    ObservabilityContext,
    OwnedProduct,
)
from commerce_service.ports import (
    AuthorizationPort,
    CommandSafetyPort,
    TenantContextPort,
)
from commerce_service.store import CommerceStore
from idempotency.errors import IdempotencyConflict
from idempotency.guard import IdempotencyGuard

#: The published order of enforcement. Also used by the contract tests: a
#: change of order is a change of behaviour and must be visible.
ENFORCEMENT_CHAIN: tuple[str, ...] = (
    "ownership_boundary",
    "authorization_decision",
    "owned_data_operation",
)

#: The audit action of a request the published schema rejected before any
#: handler ran: an access attempt that could not even be read.
ACTION_UNREADABLE = "commerce.access"

#: Identity-family denial reasons of IS-003 map to 401; every other denial
#: is an authorization refusal (403). An unmapped reason never falls through
#: to a permissive status.
_AUTHENTICATION_DENIALS = frozenset(
    {"missing_identity", "invalid_identity", "unknown_identity"}
)

_NOT_FOUND_REASONS = frozenset({OwnDenyReason.PRODUCT_UNKNOWN})

#: Payload content rules of the create-product command. A payload that
#: violates them is schema-valid but refused as ``validation_error`` before
#: any access decision: an unreadable command is not an access attempt.
NAME_MAX = 512
DESCRIPTION_MAX = 4096


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _status_for(reason: str) -> int:
    """HTTP status of a refusal: 401 authentication, 404 unknown, 409
    idempotency conflict, 400 missing idempotency key, 422 invalid payload,
    503 fail-closed dependency, 403 everything else."""
    if reason in _AUTHENTICATION_DENIALS:
        return 401
    if reason in _NOT_FOUND_REASONS:
        return 404
    if reason == OwnDenyReason.IDEMPOTENCY_CONFLICT:
        return 409
    if reason == OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED:
        return 400
    if reason == OwnDenyReason.VALIDATION_ERROR:
        return 422
    if reason == OwnDenyReason.AUTHORIZATION_UNAVAILABLE:
        return 503
    return 403


def _payload_fingerprint(operation: str, payload: dict[str, Any]) -> str:
    """The command identity of one command: operation plus payload.

    The fingerprint carries the payload, so a changed payload with the same
    key is a conflict. The create command has no pre-existing target: the
    IS-005 resource binding is empty and the tenant binding is the effective
    tenant of the chain.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{operation}\n{canonical}\n".encode()).hexdigest()


def _validation_problem(name: Any, description: Any) -> str | None:
    """The first content rule a create-product payload violates, or ``None``.

    ``name`` must be a non-empty string of at most :data:`NAME_MAX` chars;
    ``description`` must be a string of at most :data:`DESCRIPTION_MAX`
    chars.
    """
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string"
    if len(name) > NAME_MAX:
        return f"name must be at most {NAME_MAX} characters"
    if not isinstance(description, str):
        return "description must be a string"
    if len(description) > DESCRIPTION_MAX:
        return f"description must be at most {DESCRIPTION_MAX} characters"
    return None


def _product_view_of(product: OwnedProduct) -> ProductView:
    return ProductView(
        product_id=product.product_id,
        name=product.name,
        description=product.description,
        status=product.status,
        created_by=product.created_by,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


@dataclass
class CommerceEngine:
    """The enforcement boundary of Commerce in one Platform Instance.

    It owns products and an access audit journal. It owns no identity, no
    permission, no tenant registry and no tenant state: the decision is
    read, per access, from the published contract of IS-003 through the
    port. It owns no second idempotency mechanism either: the create
    command is delivered to the IS-005 guard through the port.
    """

    store: CommerceStore
    config: CommerceConfig
    authorization: AuthorizationPort
    clock: Callable[[], str] = field(default=_utc_now)
    idempotency: CommandSafetyPort | None = field(default=None)
    tenant_context: TenantContextPort | None = field(default=None)

    def __post_init__(self) -> None:
        if self.idempotency is None:
            # No second idempotency mechanism: the default is the IS-005 guard
            # itself, reporting its replays and conflicts into this component's
            # audit journal.
            self.idempotency = IdempotencyGuard(audit_sink=self._idempotency_audit)

    @property
    def current_platform_id(self) -> str:
        """Deployment identity of this Platform Instance (never a caller claim)."""
        return self.config.platform_id

    # ------------------------------------------------------------- operations
    def create_product(
        self,
        subject_credential: str | None,
        *,
        name: str,
        description: str,
        claimed_tenant_id: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ProductView, ObservabilityContext, AccessAuditEvent]:
        """Create one owned Product — the command ``commerce.products.create``.

        The enforced chain is the published one, with the effective tenant
        resolved first because the target does not exist yet: the tenant-
        context port (IS-001) derives it from the verified identity, then
        IS-003 decides ``commerce.products.create`` for a product-to-be in
        that tenant, then the IS-005 guard executes the creation effect
        exactly once. The Product's Tenant is the effective tenant of the
        chain — never a caller claim — and a new Product is created
        ``ACTIVE``.
        """
        action = OPERATION_PRODUCT_CREATE
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 0: the payload content rules --------------------------------
        problem = _validation_problem(name, description)
        if problem is not None:
            raise self._refuse(
                OwnDenyReason.VALIDATION_ERROR,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=None,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"kind": "validation_error"},
            )

        # --- step 1: the effective tenant of the verified subject (IS-001) ---
        obs, _, effective_tenant = self._effective_tenant(
            subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
            resource_id=None,
        )
        product_id = _new_id("prd")

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="product",
            resource_id=product_id,
            resource_tenant_id=effective_tenant,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs
        if tenant_id != effective_tenant:
            # The decision and the identity context must agree about the
            # effective tenant; a disagreement is a non-authoritative answer.
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_disagreement"},
            )

        # --- step 3: the owned-data operation, and only here ------------------
        if not idempotency_key:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_KEY_REQUIRED,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
            )

        fingerprint = _payload_fingerprint(
            action, {"name": name, "description": description}
        )
        try:
            created = self.idempotency.execute(
                idempotency_key,
                identity=subject_id,
                tenant_id=tenant_id,
                operation=action,
                resource=None,
                fingerprint=fingerprint,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
                effect=lambda: self.store.create_product(
                    tenant_id=tenant_id,
                    name=name,
                    description=description,
                    created_by=subject_id or "",
                    now=self.clock(),
                ),
            )
        except IdempotencyConflict:
            raise self._refuse(
                OwnDenyReason.IDEMPOTENCY_CONFLICT,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=effective_tenant,
                claimed_tenant_id=claimed_tenant_id,
                details={"idempotency_key": idempotency_key},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=created.product_id,
            resource_tenant_id=created.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_product_view_of(created), obs, event)

    def read_product(
        self,
        subject_credential: str | None,
        product_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[ProductView, ObservabilityContext, AccessAuditEvent]:
        """Serve one owned Product — and only after the chain allowed it."""
        action = OPERATION_PRODUCT_READ
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        # --- step 1: the ownership boundary of this component ----------------
        product = self.store.ownership_of_product(product_id)
        if product is None:
            raise self._refuse(
                OwnDenyReason.PRODUCT_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=product_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
            )
        if product.owner_component != COMPONENT_ID:
            raise self._refuse(
                OwnDenyReason.OWNER_MISMATCH,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=product_id,
                resource_tenant_id=product.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_owner": product.owner_component},
            )

        # --- step 2: the decision of IS-003 through the port ------------------
        answer_obs, subject_id, tenant_id = self._decide(
            subject_credential,
            operation=action,
            resource_type="product",
            resource_id=product.product_id,
            resource_tenant_id=product.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            obs=obs,
            action=action,
        )
        obs = answer_obs

        # --- step 3: the owned-data operation, and only here ------------------
        try:
            served = self.store.serve_product(product.product_id)
        except KeyError:
            raise self._refuse(
                OwnDenyReason.PRODUCT_UNKNOWN,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=product_id,
                resource_tenant_id=product.tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "vanished_after_decision"},
            )

        event = self.audit(
            action=action,
            decision=ALLOW,
            reason=PERMITTED,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=product_id,
            resource_tenant_id=product.tenant_id,
            claimed_tenant_id=claimed_tenant_id,
        )
        return (_product_view_of(served), obs, event)

    # ------------------------------------------------------------------ chain
    def _effective_tenant(
        self,
        subject_credential: str | None,
        *,
        claimed_tenant_id: str | None,
        obs: ObservabilityContext,
        action: str,
        resource_id: str | None,
    ) -> tuple[ObservabilityContext, str, str]:
        """Resolve the effective tenant of the verified subject (IS-001).

        Consumed by create-product only: its target does not exist yet, so
        the resource Tenant of the authorization question can only be the
        effective tenant of the verified identity. The tenant-context port
        is the published identity contract adapted by the composition root;
        a port that is absent, does not answer or answers outside its
        contract fails closed. The caller-supplied tenant identifier is
        forwarded as the cross-check only — it can never select the
        effective tenant.
        """
        if self.tenant_context is None:
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details={"refused": "tenant_context_port_not_wired"},
            )
        try:
            answer = self.tenant_context.resolve(
                subject_credential,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception:
            answer = TenantContextRefusal(None)
        if isinstance(answer, TenantContextRefusal):
            code = answer.reason_code
            if code in _AUTHENTICATION_DENIALS or code in DENY_REASONS:
                reason, subject_id, tenant_id = code, None, None
            else:
                reason = OwnDenyReason.AUTHORIZATION_UNAVAILABLE
                subject_id, tenant_id = None, None
            raise self._refuse(
                reason,
                action=action,
                obs=obs,
                subject_id=subject_id,
                tenant_id=tenant_id,
                resource_id=resource_id,
                resource_tenant_id=None,
                claimed_tenant_id=claimed_tenant_id,
                details=(
                    None
                    if reason != OwnDenyReason.AUTHORIZATION_UNAVAILABLE
                    else {"stated_code": code}
                ),
            )
        return (
            self.observability(
                tenant_id=answer.tenant_id,
                subject_id=answer.identity_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            ),
            answer.identity_id,
            answer.tenant_id,
        )

    def refuse_unreadable_request(
        self,
        *,
        path: str,
        schema_problems: list[str],
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AccessRefused:
        """Refuse — and audit — a request the published schema rejected."""
        obs = self.observability(
            tenant_id=None,
            subject_id=None,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        exc = AccessRefused(
            "malformed_request",
            status_code=422,
            details={
                "refused": "malformed_request",
                "schema_problems": list(schema_problems),
                "path": path,
            },
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        self.audit(
            action=ACTION_UNREADABLE,
            decision=DENY,
            reason="malformed_request",
            obs=obs,
            subject_id=None,
            tenant_id=None,
            resource_id=None,
            resource_tenant_id=None,
            claimed_tenant_id=None,
            details=dict(exc.details),
        )
        return exc

    # ------------------------------------------------------------------ chain
    def _decide(
        self,
        subject_credential: str | None,
        *,
        operation: str,
        resource_type: str,
        resource_id: str,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        obs: ObservabilityContext,
        action: str,
    ) -> tuple[ObservabilityContext, str | None, str | None]:
        """Ask the port and enforce the answer; returns the decided context.

        Raises the audited refusal for every outcome that is not an explicit
        authoritative ALLOW. The owned-data operation runs only after this
        returns.
        """
        try:
            answer = self.authorization.decide(
                subject_credential,
                operation=operation,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                request_id=obs.request_id,
                correlation_id=obs.correlation_id,
            )
        except Exception as exc:
            code = getattr(exc, "reason", None)
            code = getattr(code, "value", code)
            answer = DependencyRefusal(code if isinstance(code, str) and code else None)
        if isinstance(answer, DependencyRefusal):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=obs,
                subject_id=None,
                tenant_id=None,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={"stated_code": answer.reason_code},
            )
        if answer.decision == DENY and answer.reason in DENY_REASONS:
            raise self._refuse(
                answer.reason,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
            )
        if not (answer.decision == ALLOW and answer.reason == PERMITTED):
            raise self._refuse(
                OwnDenyReason.AUTHORIZATION_UNAVAILABLE,
                action=action,
                obs=self.observability(
                    tenant_id=answer.tenant_id,
                    subject_id=answer.subject_id,
                    request_id=obs.request_id,
                    correlation_id=obs.correlation_id,
                ),
                subject_id=answer.subject_id,
                tenant_id=answer.tenant_id,
                resource_id=resource_id,
                resource_tenant_id=resource_tenant_id,
                claimed_tenant_id=claimed_tenant_id,
                details={
                    "nonauthoritative_answer": {
                        "decision": answer.decision,
                        "reason": answer.reason,
                    }
                },
            )
        decided_obs = self.observability(
            tenant_id=answer.tenant_id,
            subject_id=answer.subject_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )
        return (decided_obs, answer.subject_id, answer.tenant_id)

    # -------------------------------------------------------------- outcomes
    def _refuse(
        self,
        reason: str,
        *,
        action: str,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessRefused:
        """Refuse one access attempt and audit the refusal on the way out."""
        self.audit(
            action=action,
            decision=DENY,
            reason=reason,
            obs=obs,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            details=details,
        )
        return AccessRefused(
            reason,
            status_code=_status_for(reason),
            details=details,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
        )

    # --------------------------------------------------------- observability
    def observability(
        self,
        *,
        tenant_id: str | None,
        subject_id: str | None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ObservabilityContext:
        rid = request_id or _new_id("req")
        cid = correlation_id or rid
        return ObservabilityContext(
            timestamp=self.clock(),
            environment=self.config.environment,
            platform_id=self.config.platform_id,
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            request_id=rid,
            trace_id=rid,
            correlation_id=cid,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    def audit(
        self,
        *,
        action: str,
        decision: str,
        reason: str | None,
        obs: ObservabilityContext,
        subject_id: str | None,
        tenant_id: str | None,
        resource_id: str | None,
        resource_tenant_id: str | None,
        claimed_tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AccessAuditEvent:
        event = AccessAuditEvent(
            event_id=_new_id("aud"),
            action=action,
            decision=decision,
            reason=reason,
            subject_id=subject_id,
            tenant_id=tenant_id,
            resource_id=resource_id,
            resource_tenant_id=resource_tenant_id,
            claimed_tenant_id=claimed_tenant_id,
            platform_id=obs.platform_id,
            request_id=obs.request_id,
            correlation_id=obs.correlation_id,
            timestamp=self.clock(),
            details=details or {},
        )
        self.store.audit.append(event)
        return event

    # ------------------------------------------------------ IS-005 observability
    def _idempotency_audit(self, action: str, details: dict[str, Any]) -> None:
        """Map a report of the IS-005 guard into this component's audit journal.

        The guard reports ``idempotency_replay`` and ``idempotency_conflict``:
        both are security-relevant (a replayed command produced no second
        effect, a differing binding was refused) and are recorded in the same
        journal as every other access outcome.
        """
        request_id = details.get("request_id")
        correlation_id = details.get("correlation_id") or request_id
        obs = self.observability(
            tenant_id=details.get("tenant_id"),
            subject_id=details.get("identity"),
            request_id=request_id,
            correlation_id=correlation_id,
        )
        self.audit(
            action=action,
            decision=ALLOW if action == "idempotency_replay" else DENY,
            reason=action,
            obs=obs,
            subject_id=details.get("identity"),
            tenant_id=details.get("tenant_id"),
            resource_id=details.get("resource"),
            resource_tenant_id=None,
            claimed_tenant_id=None,
            details={key: value for key, value in details.items() if key != "resource"},
        )


__all__ = ["ACTION_UNREADABLE", "ENFORCEMENT_CHAIN", "CommerceEngine"]
