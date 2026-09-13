"""SCS-002 Commerce — Business System / SCS (class A), Level 0.

This component is a data owner. It owns tenant-scoped Commerce data and
proves what ``ALLOW`` from IS-003 means at the boundary that owns the
data: an ``ALLOW`` is not direct data access — the data owner remains the
final enforcement boundary (ARCHITECTURE.md §5.3, §6.2).

The Commerce business boundary is ratified by ADR-0013: the minimal MVP
contour ``Product → Offer → Price`` and ``Cart → Checkout → Order →
OrderLine``, plus payment state, buyer identity reference, tenant
isolation, authorization, idempotency and audit. Commerce is the second
independent business product of the project after Learning; it owns
commercial facts (what is for sale, at what price, who selected what,
who bought what, and whether it is paid) and nothing else.

Every published access path runs one fixed enforcement chain:

* ``ownership_boundary`` — the record exists here and its single owner
  is this component;
* ``authorization_decision`` — IS-003 decides; the default outcome is DENY
  and a dependency that does not answer, or answers outside its contract,
  fails closed to DENY;
* ``owned_data_operation`` — only now is the owned data read or written.

The component owns Commerce data and an access audit journal, and nothing
else: no identity verification, no tenant registry and no tenant
derivation of its own — the effective tenant of a request comes from
IS-001 through the IS-003 decision, and a caller-supplied ``tenant_id``
is forwarded as a cross-check only (LAW-16, LAW-16a).

Stage 2 (Issue #55) establishes the component: identity, Component
Contract, OpenAPI, the domain/storage foundation for all six MVP record
types plus the payment state, and the enforcement-chain plumbing. Stage 3
implements the Commerce business logic on that foundation: the live
surface is twelve operations — create and read one owned Product, define
one Offer, set one Price, open/read/mutate one owned Cart (add, set
quantity, remove), checkout, read one owned Order and record its payment
— behind the same enforcement chain, the IS-005 guard, the audit journal
and the published client. Errors use the approved SCS-002 envelope
(``error.code`` / ``error.message`` / ``error.details`` plus top-level
``request_id`` / ``correlation_id``).
"""

COMPONENT_ID = "commerce"
COMPONENT_VERSION = "0.2.0"
COMPONENT_CLASS = "business_system"
SCS_ID = "SCS-002"

#: The single data owner of every record this component serves.
OWNER_COMPONENT = COMPONENT_ID
