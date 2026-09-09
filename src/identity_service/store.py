from __future__ import annotations

from dataclasses import dataclass, field

from identity_service.models import (
    AuditEvent,
    IdempotencyRecord,
    IdentityKind,
    ProtectedRecord,
    TenantAssociation,
    VerifiedIdentity,
)


@dataclass
class IdentityStore:
    """In-memory owned data of the identity component (logical schema `identity`)."""

    # Tenant registry and lifecycle state are NOT owned here: they belong to
    # Tenant Authority (IS-002). Identity owns identity and association data only.
    identities: dict[str, VerifiedIdentity] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    associations: dict[tuple[str, str], TenantAssociation] = field(default_factory=dict)
    records: dict[str, ProtectedRecord] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)

    def seed_demo(self) -> None:
        human_a = VerifiedIdentity(
            "idn_human_a",
            IdentityKind.HUMAN,
            "user-a@example.test",
            home_tenant_id="ten_a",
        )
        human_b = VerifiedIdentity(
            "idn_human_b",
            IdentityKind.HUMAN,
            "user-b@example.test",
            home_tenant_id="ten_b",
        )
        human_c = VerifiedIdentity(
            "idn_human_c",
            IdentityKind.HUMAN,
            "user-c@example.test",
            home_tenant_id="ten_a",
        )
        svc = VerifiedIdentity(
            "idn_service_jobs",
            IdentityKind.SERVICE,
            "svc://jobs",
            home_tenant_id="ten_a",
        )
        self.identities = {
            human_a.identity_id: human_a,
            human_b.identity_id: human_b,
            human_c.identity_id: human_c,
            svc.identity_id: svc,
        }
        self.tokens = {
            "token-human-a": human_a.identity_id,
            "token-human-b": human_b.identity_id,
            "token-human-c": human_c.identity_id,
            "token-service": svc.identity_id,
            "token-unknown": "idn_missing",
        }
        self.associations = {
            ("idn_human_a", "ten_a"): TenantAssociation(
                "idn_human_a", "ten_a", frozenset({"records.read", "records.write"})
            ),
            ("idn_human_a", "ten_suspended"): TenantAssociation(
                "idn_human_a", "ten_suspended", frozenset({"records.read"})
            ),
            ("idn_human_a", "ten_deleted"): TenantAssociation(
                "idn_human_a", "ten_deleted", frozenset({"records.read"})
            ),
            ("idn_human_b", "ten_b"): TenantAssociation(
                "idn_human_b", "ten_b", frozenset({"records.read", "records.write"})
            ),
            ("idn_human_c", "ten_a"): TenantAssociation(
                "idn_human_c", "ten_a", frozenset({"records.read", "records.write"})
            ),
            ("idn_service_jobs", "ten_a"): TenantAssociation(
                "idn_service_jobs", "ten_a", frozenset({"records.read"})
            ),
        }
        self.records = {
            "rec_a1": ProtectedRecord("rec_a1", "ten_a", "tenant-a-secret"),
            "rec_b1": ProtectedRecord("rec_b1", "ten_b", "tenant-b-secret"),
        }
