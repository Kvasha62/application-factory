"""SCS-003 Booking — Business System / SCS (class A), Level 0.

This component is a data owner. It owns tenant-scoped Booking data —
Resources, Availability Windows and Reservations — and proves what
``ALLOW`` from IS-003 means at the boundary that owns the data: an
``ALLOW`` is not direct data access — the data owner remains the final
enforcement boundary (ARCHITECTURE.md §5.3, §6.2).

The Booking business boundary is ratified by ADR-0014: the minimal model
``Resource → Availability Window → Reservation``, timezone-aware input
normalized to UTC, half-open ``[start_at, end_at)`` intervals, the
``ACTIVE → CANCELLED`` reservation lifecycle, the no-double-booking
invariant guaranteed atomically under concurrency, tenant isolation,
authorization, idempotency and audit. Booking is the third independent
business product of the project after Learning and Commerce; it depends
on neither.

Every published access path runs one fixed enforcement chain:

* ``ownership_boundary`` — the record exists here and its single owner
  is this component;
* ``authorization_decision`` — IS-003 decides; the default outcome is DENY
  and a dependency that does not answer, or answers outside its contract,
  fails closed to DENY;
* ``owned_data_operation`` — only now is the owned data read or written.

The component owns Booking data and an access audit journal, and nothing
else: no identity verification, no tenant registry and no tenant
derivation of its own — the effective tenant of a request comes from
IS-001 through the IS-003 decision, and a caller-supplied ``tenant_id``
is forwarded as a cross-check only (LAW-16, LAW-16a).

The live surface is the seven operations of ADR-0014 §14: create and
read one owned Resource, declare and list the Availability Windows of a
Resource, create, read and cancel one owned Reservation. Errors use the
approved SCS envelope (``error.code`` / ``error.message`` /
``error.details`` plus top-level ``request_id`` / ``correlation_id``).
"""

COMPONENT_ID = "booking"
COMPONENT_VERSION = "0.1.0"
COMPONENT_CLASS = "business_system"
SCS_ID = "SCS-003"

#: The single data owner of every record this component serves.
OWNER_COMPONENT = COMPONENT_ID
