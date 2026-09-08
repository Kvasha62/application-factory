"""Owned data of the Tenant Authority component (logical schema `tenant_authority`).

This module is internal. Other components have no direct access to it: the
registry is reachable only through the published operations of
:class:`tenant_authority.engine.TenantAuthorityEngine` / the HTTP contract
(invariant T-009, ARCHITECTURE.md §1.1, LAW-04).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tenant_authority.contracts import TenantState
from tenant_authority.models import (
    AuditEvent,
    IdempotencyRecord,
    LifecycleTransition,
    ServiceAccess,
    TenantRecord,
)

PERM_CREATE = "tenant.create"
PERM_READ = "tenant.read"
PERM_LIST = "tenant.list"
PERM_TRANSITION = "tenant.transition"
PERM_LIFECYCLE_LOOKUP = "tenant.lifecycle.lookup"

REGISTRY_PERMISSIONS = frozenset(
    {PERM_CREATE, PERM_READ, PERM_LIST, PERM_TRANSITION, PERM_LIFECYCLE_LOOKUP}
)
LOOKUP_PERMISSIONS = frozenset({PERM_READ, PERM_LIFECYCLE_LOOKUP})

DEMO_SEED_TIMESTAMP = "2026-09-08T00:00:00+00:00"


@dataclass
class TenantAuthorityStore:
    """In-memory owned storage of the Tenant Registry, audit and replay keys."""

    tenants: dict[str, TenantRecord] = field(default_factory=dict)
    services: dict[str, ServiceAccess] = field(default_factory=dict)
    service_tokens: dict[str, str] = field(default_factory=dict)
    transitions: list[LifecycleTransition] = field(default_factory=list)
    audit: list[AuditEvent] = field(default_factory=list)
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)

    def seed_demo(
        self,
        platform_id: str = "plt_demo",
        timestamp: str = DEMO_SEED_TIMESTAMP,
        *,
        foreign_platform_id: str | None = "plt_other",
    ) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        At Level 0 one logical schema may hold rows of several Platform Instances
        (ARCHITECTURE.md §5.1), so the demo registry contains a Tenant of another
        Platform Instance: ownership must be enforced by the authority check, not
        by the accident of an empty table.
        """

        def tenant(tenant_id: str, state: TenantState, owner: str | None = None) -> TenantRecord:
            return TenantRecord(
                tenant_id=tenant_id,
                platform_id=owner or platform_id,
                state=state,
                created_at=timestamp,
                updated_at=timestamp,
            )

        foreign_platform = foreign_platform_id or platform_id
        self.tenants = {
            "ten_a": tenant("ten_a", TenantState.ACTIVE),
            "ten_b": tenant("ten_b", TenantState.ACTIVE),
            "ten_provisioning": tenant("ten_provisioning", TenantState.PROVISIONING),
            "ten_suspended": tenant("ten_suspended", TenantState.SUSPENDED),
            "ten_deletion_requested": tenant("ten_deletion_requested", TenantState.DELETION_REQUESTED),
            "ten_deleted": tenant("ten_deleted", TenantState.DELETED),
            "ten_foreign": tenant("ten_foreign", TenantState.ACTIVE, foreign_platform),
        }
        self.services = {
            "svc_tenant_admin": ServiceAccess(
                "svc_tenant_admin", platform_id=platform_id, permissions=REGISTRY_PERMISSIONS
            ),
            "svc_identity": ServiceAccess(
                "svc_identity", platform_id=platform_id, permissions=LOOKUP_PERMISSIONS
            ),
            # Each consuming component has its own service identity: the actor in
            # this journal is the component that actually asked (IS-003 included).
            "svc_authorization": ServiceAccess(
                "svc_authorization", platform_id=platform_id, permissions=LOOKUP_PERMISSIONS
            ),
            "svc_foreign_admin": ServiceAccess(
                "svc_foreign_admin", platform_id=foreign_platform, permissions=REGISTRY_PERMISSIONS
            ),
        }
        self.service_tokens = {
            "svc-token-admin": "svc_tenant_admin",
            "svc-token-identity": "svc_identity",
            "svc-token-authorization": "svc_authorization",
            "svc-token-foreign-admin": "svc_foreign_admin",
            # Resolves to a subject that no longer exists (revoked service identity).
            "svc-token-unknown": "svc_missing",
        }
        self.transitions = []
        self.audit = []
        self.idempotency = {}
