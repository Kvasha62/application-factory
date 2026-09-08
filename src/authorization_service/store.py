"""Owned data of the Authorization Boundary component (logical schema `authorization`).

This module is internal. No other component has direct access to it: grants and
the audit journal are reachable only through the published contract of this
component (invariant 10, ARCHITECTURE.md §1.1, LAW-04).

The component owns permission grants and its own audit journal — and nothing
else. In particular it owns **no** tenant registry, **no** tenant state and
**no** identity: a grant merely mentions a ``tenant_id`` and a ``subject_id``
whose existence, ownership and lifecycle are decided elsewhere (IS-001, IS-002).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from authorization_service.models import AuditEvent, PermissionGrant, ServiceAccess

#: The only permission this component checks on the component that asks it.
PERM_DECIDE = "authorization.decide"

CONSUMER_PERMISSIONS = frozenset({PERM_DECIDE})

DEMO_SEED_TIMESTAMP = "2026-09-08T00:00:00+00:00"


@dataclass
class AuthorizationStore:
    """In-memory owned storage of permission grants, service access and audit."""

    grants: dict[tuple[str, str], PermissionGrant] = field(default_factory=dict)
    services: dict[str, ServiceAccess] = field(default_factory=dict)
    service_tokens: dict[str, str] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)

    # ------------------------------------------------------------------ grants
    def grant(self, tenant_id: str, subject_id: str, *operations: str) -> PermissionGrant:
        """Add operations to the grant of one subject inside one Tenant.

        Internal operation: provisioning grants is the job of the composition
        root (and of this component's own tests). IS-003 publishes no grant
        management API — user, credential and permission administration is an
        explicit non-goal.
        """
        key = (tenant_id, subject_id)
        existing = self.grants.get(key)
        merged = frozenset(operations) | (existing.operations if existing else frozenset())
        record = PermissionGrant(tenant_id=tenant_id, subject_id=subject_id, operations=merged)
        self.grants[key] = record
        return record

    def revoke(self, tenant_id: str, subject_id: str, *operations: str) -> None:
        """Remove operations from a grant; an emptied grant disappears entirely."""
        key = (tenant_id, subject_id)
        existing = self.grants.get(key)
        if existing is None:
            return
        remaining = existing.operations - frozenset(operations)
        if remaining:
            self.grants[key] = PermissionGrant(tenant_id, subject_id, remaining)
        else:
            self.grants.pop(key, None)

    def is_granted(self, tenant_id: str, subject_id: str, operation: str) -> bool:
        """Explicit grant only: no grant, no access (invariant 6)."""
        grant = self.grants.get((tenant_id, subject_id))
        return grant is not None and operation in grant.operations

    # -------------------------------------------------------------- demo data
    def seed_demo(
        self,
        platform_id: str = "plt_demo",
        *,
        foreign_platform_id: str | None = "plt_other",
    ) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        Subject and tenant identifiers are the demo ones of IS-001 and IS-002:
        this component does not define them, it only refers to them. A subject
        with a grant in one Tenant deliberately has none in another, and one
        subject is authenticated with no grant at all — authentication is not
        authorization (invariant 5).
        """
        foreign_platform = foreign_platform_id or platform_id
        self.services = {
            # The demo data owner: it may ask for decisions, nothing more.
            "svc_records": ServiceAccess("svc_records", platform_id, CONSUMER_PERMISSIONS),
            # Verified, but not allowed to ask this authority for decisions.
            "svc_reporting": ServiceAccess("svc_reporting", platform_id, frozenset()),
            # A service identity of another Platform Instance.
            "svc_foreign_owner": ServiceAccess(
                "svc_foreign_owner", foreign_platform, CONSUMER_PERMISSIONS
            ),
        }
        self.service_tokens = {
            "authz-svc-token-records": "svc_records",
            "authz-svc-token-reporting": "svc_reporting",
            "authz-svc-token-foreign": "svc_foreign_owner",
            # Resolves to a subject that no longer exists (revoked service identity).
            "authz-svc-token-unknown": "svc_missing",
        }
        self.grants = {}
        self.grant("ten_a", "idn_human_a", "records.read", "records.write")
        self.grant("ten_a", "idn_human_c", "records.read")
        self.grant("ten_b", "idn_human_b", "records.read", "records.write")
        self.grant("ten_a", "idn_service_jobs", "records.read")
        # Grants that exist but must not help: the Tenant is not in a state that
        # may be served, so the lifecycle denial happens before the grant is used.
        self.grant("ten_suspended", "idn_human_a", "records.read")
        self.grant("ten_deleted", "idn_human_a", "records.read")
        self.audit = []
