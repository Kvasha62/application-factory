# ADR-0014 — SCS-003 Booking Boundary

**Status:** PROPOSED  
**Date:** 2026-09-13  
**Decision:** Define Booking as the third independent Class A Business System / SCS at Level 0 — Modular Monolith.

## 1. Context

`ARCHITECTURE.md` defines Class A Business System / SCS as an independent business system with its own UI, data ownership, lifecycle and public contract. It explicitly names Booking as an example of this class.

The project currently has two confirmed independent Business Systems:

- SCS-001 Learning;
- SCS-002 Commerce.

Booking is considered as the third independent business system. It must have a genuine business boundary and must not be introduced merely to satisfy the factory gate of three independently delivered components.

This ADR defines only the business boundary and invariants. It does not activate Level 2 factory mechanics and does not define Component Catalog, Registry, Composer, Golden Bundles or release trains.

## 2. Decision

Booking is an independent Class A Business System / SCS at Level 0 — Modular Monolith.

Booking owns the business facts required to manage bookable resources, their availability windows and reservations over time.

### 2.1. Owned business concepts

The minimal Booking domain consists of:

- `Resource` — a tenant-owned resource that can be booked;
- `Availability Window` — a time window during which a Resource may be booked;
- `Reservation` — a booking fact connecting a Resource, a booker and a time interval.

Booking owns the lifecycle of these facts.

## 3. Resource

A Resource is tenant-scoped and has at minimum:

- `resource_id`;
- `tenant_id`;
- `name`;
- `status`.

Resource status is minimal:

- `ACTIVE`;
- `INACTIVE`.

Only an `ACTIVE` Resource may receive new Reservations.

A Resource does not own price, payment, product, course, lesson, enrollment, analytics or notification facts.

## 4. Time model

Booking uses half-open intervals:

`[start_at, end_at)`

The invariant is:

`start_at < end_at`

Both timestamps are timezone-aware at the API/domain boundary. Persistent representation is normalized to UTC.

Two adjacent intervals such as `10:00–11:00` and `11:00–12:00` do not overlap and may coexist.

Daylight-saving behavior is handled by converting valid timezone-aware input to UTC before persistence and comparison. Booking does not persist naive local timestamps.

## 5. Availability semantics

Availability is an explicit allowed booking window, not a per-minute slot inventory and not a cached statement that a Resource is currently free.

A Reservation is valid only when its complete interval is contained within an applicable Availability Window.

Availability does not itself represent occupancy. Occupancy is determined by ACTIVE Reservations.

Therefore, for a Resource and interval, booking availability is the conjunction of:

1. the Resource exists and is `ACTIVE`;
2. the requested interval is valid;
3. the requested interval is fully contained in an applicable Availability Window;
4. no conflicting ACTIVE Reservation exists.

## 6. Reservation

A Reservation is tenant-scoped and has at minimum:

- `reservation_id`;
- `tenant_id`;
- `resource_id`;
- `booker_identity_id`;
- `start_at`;
- `end_at`;
- `status`;
- `created_at`.

`booker_identity_id` is an opaque reference to the existing Identity component. Booking does not copy Identity profile data into its ownership domain.

Reservation status is minimal:

`ACTIVE → CANCELLED`

Cancellation does not delete the Reservation record. It preserves the historical business fact.

`CANCELLED → ACTIVE` is forbidden.

Repeated cancellation is rejected as an invalid state transition.

## 7. Double-booking invariant

For a given `resource_id`, two ACTIVE Reservations must never have overlapping intervals.

For any two ACTIVE Reservations belonging to the same Resource:

`[start_1, end_1) ∩ [start_2, end_2) = ∅`

The invariant must hold under concurrent requests. A check-then-insert sequence that permits two concurrent requests to observe the same free interval is insufficient.

The implementation must enforce the invariant atomically at the storage/business boundary using an appropriate serialization, locking, exclusion/uniqueness mechanism, or an equivalent correctness-preserving mechanism.

A conflicting booking returns a deterministic business conflict (`409 reservation_conflict`).

## 8. Authorization and tenant isolation

Booking reuses the existing foundation components for Identity, Tenant Authority, Authorization, Idempotency and Audit.

Tenant context is derived from verified identity. A caller-supplied `tenant_id` is only a cross-check and is rejected on mismatch, except for explicitly authorized platform/system operations permitted by the architecture.

A caller may act only within the tenant boundary and according to the published Booking authorization policy.

At minimum, cancellation must verify that the caller is authorized to cancel the target Reservation. The implementation must not bypass the component ownership boundary.

## 9. Minimal public operation surface

The initial Booking contract is intentionally small:

### Resource

- `POST /resources`
- `GET /resources/{resource_id}`

### Availability

- `POST /resources/{resource_id}/availability`
- `GET /resources/{resource_id}/availability`

### Reservation

- `POST /reservations`
- `GET /reservations/{reservation_id}`
- `POST /reservations/{reservation_id}/cancel`

The exact machine-readable contract, grants, reason codes and HTTP details belong to the implementation task and Component Contract. This ADR defines the business semantics, not the final OpenAPI syntax.

## 10. Error semantics

The initial domain error vocabulary may include:

- `resource_not_found`;
- `resource_inactive`;
- `availability_not_found`;
- `outside_availability`;
- `reservation_conflict`;
- `reservation_not_found`;
- `invalid_state_transition`;
- `booker_mismatch`.

Foundation-level errors remain owned by the corresponding foundation components, including authorization denial/unavailability, idempotency conflicts and tenant mismatch.

## 11. Component boundaries

Booking owns:

- Resources;
- Availability Windows;
- Reservations;
- booking-specific authorization decisions and audit facts required by its contract;
- booking lifecycle and consistency invariants.

Booking does not own or directly access:

- Identity internals;
- Tenant Authority internals;
- Commerce Product, Offer, Price, Cart, Checkout, Order or Payment data;
- Learning Course, Module, Lesson, Assignment, Submission or Enrollment data;
- analytics;
- notifications;
- CRM;
- external calendar synchronization;
- payment providers.

Cross-component interaction is permitted only through published contracts, events, exports/CDC or other officially defined integration mechanisms.

## 12. Explicit non-goals

The initial Booking MVP does not introduce:

- pricing;
- payment;
- refunds;
- promotions;
- subscriptions;
- marketplace functionality;
- customer CRM;
- analytics;
- notifications as a Booking-owned subsystem;
- calendar synchronization;
- recurring reservations;
- waitlists;
- resource capacity greater than one;
- separate Payment Service;
- new foundation services;
- microservices;
- Component Catalog;
- Component Registry;
- Composer;
- Golden Bundles;
- release trains;
- Level 2 factory mechanics.

These may require separate architectural decisions if they become necessary later.

## 13. Relationship to Commerce and Learning

Booking remains independently usable without Commerce or Learning.

Commerce may in the future publish a contract stating that a customer purchased a product that grants some booking-related entitlement. Learning may in the future publish a contract that refers to a booking capability. Neither integration is part of the initial Booking MVP.

There is no implicit rule such as `paid order → reservation` in this ADR.

## 14. Consequences

Positive consequences:

- Booking has a clear and independently testable business boundary;
- Resource, Availability and Reservation ownership is explicit;
- temporal semantics are deterministic;
- adjacent intervals are unambiguous;
- concurrent double-booking is treated as a correctness invariant rather than an incidental implementation detail;
- Learning and Commerce remain isolated;
- existing foundation services are reused;
- Level 0 remains sufficient.

Costs and constraints:

- concurrent reservation creation requires an atomic correctness mechanism;
- timestamp handling must remain timezone-aware and UTC-normalized;
- Availability and Reservation consistency must be covered by tests;
- any future pricing/payment/entitlement integration requires a published contract and, where architecture changes, a separate ADR.

## 15. Factory gate consequence

If Booking is subsequently implemented and independently delivered, the project will have three confirmed independent Business Systems: Learning, Commerce and Booking.

At that point the project must perform a separate formal architecture readiness review for factory Gate #1 (`independently delivered components ≥ 3`). Reaching the gate does not by itself authorize construction of factory mechanics; the gate only makes the transition eligible for a new architectural decision.

## 16. Ratification

This ADR is `PROPOSED` until independently reviewed and explicitly ratified by the owner.

Implementation of SCS-003 Booking must not begin before ratification.
