# ADR-0014 — SCS-003 Booking Boundary

**Status:** `RATIFIED`  
**Date:** 2026-09-13  
**Ratified:** 2026-09-13 by owner approval  
**Level:** D — domain/architecture boundary decision  
**Initiated by:** Issue #57

> This ADR fixes the architectural boundary of SCS-003 Booking. It authorizes implementation according to the project's operating model, but does not prescribe internal code structure, ORM, framework, database technology or deployment topology.

## 1. Context

The project already contains two confirmed independent Class A Business Systems / SCS:

- SCS-001 Learning;
- SCS-002 Commerce.

`docs/ARCHITECTURE.md` v1.2.0 explicitly names Learning, Commerce and Booking as examples of Class A — Business System / SCS.

SCS-003 Booking must therefore have its business boundary, ownership, invariants and non-goals fixed before implementation. The project remains at Level 0 — Modular Monolith. Reaching three independent Business Systems creates the factual basis for a separate Factory Gate #1 review, but does not itself activate Level 2 factory mechanics.

## 2. Decision

SCS-003 Booking is approved as an independent **Class A — Business System / SCS** at **Level 0 — Modular Monolith**.

Booking is independently usable and does not depend on Commerce or Learning.

Its minimal business model is:

```text
Resource
   ↓
Availability Window
   ↓
Reservation
```

Booking owns:

- Resources;
- Availability Windows;
- Reservations;
- Reservation lifecycle;
- Booking business invariants;
- Booking's published contract.

Booking does not own Identity, Tenant Authority, Commerce, Learning, analytics, notifications or other excluded capabilities.

## 3. Resource

A Resource represents an object that can be booked.

```text
Resource
├── resource_id
├── tenant_id
├── name
└── status
```

Allowed status values:

```text
ACTIVE
INACTIVE
```

Only ACTIVE Resources can receive new Reservations. INACTIVE Resources remain historical objects but cannot receive new Reservations.

## 4. Availability Window

An Availability Window represents an explicitly allowed booking interval.

Availability is a permission window, not occupancy and not per-minute slot inventory.

Occupancy is determined by ACTIVE Reservations.

A Reservation is valid only when its complete time interval is contained in an applicable Availability Window.

Therefore:

```text
Availability = when booking is allowed
Reservation  = what is booked
```

These concepts must not be conflated.

## 5. Time Semantics

All Booking intervals use half-open semantics:

```text
[start_at, end_at)
```

Invariant:

```text
start_at < end_at
```

API/domain timestamps must be timezone-aware. Persistent representation and comparisons are normalized to UTC.

Naive timestamps are not valid Booking input.

Adjacent intervals do not overlap. For example:

```text
10:00–11:00
11:00–12:00
```

may both exist for the same Resource.

DST handling follows the same rule: valid timezone-aware input is normalized to UTC before persistence and comparison.

## 6. Reservation

The minimal Reservation model is:

```text
Reservation
├── reservation_id
├── tenant_id
├── resource_id
├── booker_identity_id
├── start_at
├── end_at
├── status
└── created_at
```

`booker_identity_id` is an opaque reference to the existing Identity boundary. Booking does not own or duplicate Identity internals.

The initial lifecycle is:

```text
ACTIVE → CANCELLED
```

Cancellation preserves the historical Reservation fact and does not delete it.

The following transitions are forbidden:

```text
CANCELLED → ACTIVE
CANCELLED → CANCELLED
```

No additional lifecycle states are introduced by this ADR.

## 7. Double-Booking Invariant

For one Resource, two ACTIVE Reservations must never overlap.

For two ACTIVE Reservations belonging to the same Resource, their half-open intervals must not intersect.

A simple application-level check followed by insert is insufficient because concurrent requests can both observe the same free interval.

The implementation must guarantee the invariant atomically at the storage/business boundary. Acceptable mechanisms include serialization, locking, database exclusion constraints or another equivalent mechanism that actually guarantees the invariant.

A conflicting reservation has deterministic business semantics:

```text
HTTP 409
reservation_conflict
```

Concurrency tests must prove that concurrent requests cannot create two overlapping ACTIVE Reservations for one Resource.

## 8. Reservation Creation Conditions

A Reservation may be created only when all of the following hold:

1. Resource exists;
2. Resource is ACTIVE;
3. `start_at < end_at`;
4. the complete interval is contained in an applicable Availability Window;
5. no conflicting ACTIVE Reservation exists for that Resource.

Failure of any condition is rejected with the appropriate deterministic Booking domain semantics.

## 9. Tenant Ownership

Resource, Availability Window and Reservation are tenant-scoped business data.

The effective tenant is derived from the existing verified Identity / Tenant Authority mechanism. A caller-supplied `tenant_id` is only a cross-check and must not select the effective tenant.

A tenant mismatch is rejected according to the existing platform boundary. Cross-tenant access is forbidden except for explicitly authorized platform/system operations already supported by the architecture.

Booking creates no second Tenant Service or tenant-isolation mechanism.

## 10. Foundation Reuse

Booking reuses the existing foundation boundaries:

- Identity;
- Tenant Authority;
- Authorization;
- Idempotency;
- Audit.

Booking must not create replacement or duplicate foundation services.

In particular, Booking does not create its own Identity Service, Tenant Service, Authorization Service, Idempotency Service or Audit Service.

## 11. Authorization

All public Booking operations are subject to the existing Authorization boundary.

Creating, reading and cancelling Reservations must respect the existing authorization and tenant ownership rules.

`booker_identity_id` is an ownership reference, not an authorization bypass.

## 12. Idempotency

State-changing Booking commands use the existing Idempotency boundary and its published contract.

Booking must not implement a competing local idempotency mechanism.

A replay of the same idempotent command must not produce a second business effect.

## 13. Audit

State-changing and security-sensitive Booking operations use the existing Audit boundary and its published semantics.

Booking must not create a separate audit subsystem.

## 14. Public API

The initial public Booking API surface consists of these seven operations:

```text
POST /resources
GET  /resources/{resource_id}

POST /resources/{resource_id}/availability
GET  /resources/{resource_id}/availability

POST /reservations
GET  /reservations/{reservation_id}
POST /reservations/{reservation_id}/cancel
```

These are the minimum approved business operations. Generic CRUD expansion is not authorized merely for completeness.

Exact request/response schemas, HTTP mappings, authorization grants, idempotency representation and error payloads belong to the Component Contract/OpenAPI implementation layer, provided they preserve this ADR.

## 15. Domain Error Vocabulary

Booking-specific domain errors are:

```text
resource_not_found
resource_inactive
availability_not_found
outside_availability
reservation_conflict
reservation_not_found
invalid_state_transition
booker_mismatch
```

Foundation errors remain owned by the corresponding foundation boundary, including authorization denial, tenant mismatch and idempotency conflicts.

## 16. Component Contract

Booking must have a published Component Contract declaring, as applicable:

- component identity;
- ownership;
- public API;
- events;
- data export / CDC;
- UI surface;
- configuration schema;
- authentication;
- authorization;
- compatibility;
- dependencies.

The contract must describe actual behavior and must not promise capabilities that the implementation does not provide.

OpenAPI must be consistent with the public API and Component Contract.

## 17. Independence from Commerce

Booking does not access Commerce internals or database data.

It must not directly access:

- Product;
- Offer;
- Price;
- Cart;
- Checkout;
- Order;
- OrderLine;
- Payment state.

There is no implicit:

```text
paid order → reservation
```

Any future Commerce ↔ Booking integration must use a published contract and, where architectural semantics change, a separate architectural decision.

## 18. Independence from Learning

Booking does not access Learning internals or database data.

It must not directly access:

- Course;
- Module;
- Lesson;
- Assignment;
- Submission;
- Enrollment.

No automatic Learning → Booking dependency is introduced.

Any future integration must use published contracts.

## 19. Explicit Non-Goals

The initial Booking MVP does not implement or own:

- pricing;
- payment;
- refunds;
- promotions;
- subscriptions;
- marketplace functionality;
- CRM;
- analytics/reporting;
- Booking-owned notification infrastructure;
- external calendar synchronization;
- recurring reservations;
- waitlists;
- resource capacity greater than one;
- real payment providers;
- a separate Payment Service;
- new foundation services;
- microservices;
- Component Catalog;
- Component Registry;
- Composer;
- Golden Bundles;
- release trains;
- Level 2 factory mechanics;
- generic CRUD expansion.

Learning and Commerce must not be changed merely to make Booking implementation more convenient.

## 20. Deployment Boundary

Booking remains inside the Level-0 Modular Monolith.

This ADR does not authorize a separate deployment unit, network service, service-discovery entry or microservice.

Future physical separation requires a separate architectural justification.

## 21. Data Ownership

Booking owns its business facts:

```text
Resource
Availability Window
Reservation
```

Other components may not directly access Booking internal storage.

Cross-component access is limited to published API, events, data export / CDC, or another explicitly published integration contract.

Logical ownership is required even while physical storage remains compatible with Level 0.

## 22. Compatibility

Changes to the following Booking semantics are architectural changes and must follow the project's established ADR/change procedure:

- ownership;
- public API;
- reservation lifecycle;
- time semantics;
- concurrency invariant;
- tenant isolation;
- authorization;
- idempotency;
- integration semantics.

Breaking changes must follow the compatibility rules in `docs/ARCHITECTURE.md`.

## 23. Testing Requirements

Implementation must provide automated verification for at least:

- Resource lifecycle;
- ACTIVE/INACTIVE behavior;
- Availability Window creation and retrieval;
- invalid intervals;
- timezone-aware timestamps;
- UTC normalization;
- half-open interval semantics;
- adjacent non-overlapping intervals;
- full Reservation containment in Availability Window;
- Reservation lifecycle;
- cancellation history preservation;
- invalid state transitions;
- tenant isolation;
- authorization;
- idempotency;
- audit behavior;
- reservation conflicts;
- concurrent double-booking protection.

Sequential tests alone are insufficient. The implementation must demonstrate that concurrency cannot violate the no-overlap invariant.

## 24. Acceptance Boundary

Booking conforms to this ADR only when it:

1. exists as an independent Class A Business System / SCS;
2. remains Level 0 — Modular Monolith;
3. owns Resource, Availability Window and Reservation;
4. preserves tenant isolation;
5. accepts timezone-aware timestamps and normalizes persistence/comparison to UTC;
6. uses `[start_at, end_at)` interval semantics;
7. enforces `start_at < end_at`;
8. requires full Reservation containment in an Availability Window;
9. permits adjacent intervals;
10. forbids overlapping ACTIVE Reservations;
11. guarantees the invariant under concurrency;
12. maps conflicts to deterministic `reservation_conflict` semantics;
13. implements `ACTIVE → CANCELLED` lifecycle;
14. preserves cancelled Reservations as historical facts;
15. reuses Identity, Tenant Authority, Authorization, Idempotency and Audit;
16. remains independent from Commerce and Learning;
17. publishes a Component Contract;
18. publishes an OpenAPI description consistent with the contract;
19. does not implement the explicit non-goals above.

## 25. Factory Gate Consequence

If Booking is independently delivered, the project will have three confirmed independent Business Systems:

```text
SCS-001 Learning
SCS-002 Commerce
SCS-003 Booking
```

This creates the factual basis for a separate **Factory Gate #1** readiness review under `docs/ARCHITECTURE.md`.

Three Business Systems do **not** automatically activate Level 2.

A separate review must determine whether Gate #1 is actually satisfied. Until that review and decision, creation of Component Catalog, Component Registry, Composer, Golden Bundles, release trains or other Level-2 factory mechanics is not authorized.

## 26. Consequences

### Positive

Booking has an explicit independent business boundary, deterministic time semantics, a clear reservation lifecycle and an explicit concurrency invariant. It can be developed and tested independently without coupling Commerce or Learning to its internals.

### Constraints

Booking cannot directly consume Commerce or Learning data. Future integrations must use published contracts. Booking also cannot absorb pricing, payment, analytics, notifications or other excluded capabilities without a separate architectural decision.

## 27. Alternatives Considered

### Booking inside Learning

Rejected. Booking is an independent business capability and must not become a Learning subdomain.

### Booking inside Commerce

Rejected. Reservation is not a commercial fact and must not become a Commerce subdomain.

### Separate microservice

Rejected at the current stage. The project remains Level 0 and has no earned reason for a separate deployment boundary.

### External calendar as Availability authority

Rejected for the MVP. Calendar synchronization is an explicit non-goal.

### Precomputed free slots

Rejected. Availability represents allowed booking windows; occupancy is represented by ACTIVE Reservations.

### Application-only check before insert

Rejected. Check-then-insert does not guarantee correctness under concurrency.

## 28. Implementation Freedom

This ADR intentionally does not prescribe:

- database technology;
- ORM;
- Python framework;
- internal module/class structure;
- serialization details;
- exact locking or exclusion mechanism;
- repository directory structure;
- deployment tooling.

Those are implementation decisions. They are valid only while preserving the architectural invariants of this ADR and `docs/ARCHITECTURE.md`.

## 29. Ratification

This document is the architectural decision for SCS-003 Booking.

**Status: RATIFIED**  
**Ratified:** 2026-09-13 by owner approval.

The Booking implementation is authorized according to the project's Operating Model, but implementation remains subject to the required workflow:

```text
Issue → Arena implementation → PR → independent ChatGPT review
→ corrections if required → owner approval → merge → post-merge verification
```

Arena may execute the approved implementation but does not have authority to change this ADR or merge `main` unless that authority is separately delegated.

Any change to the Booking architectural boundary after ratification must use the project's established ADR/change procedure.

## 30. Relationship to Other Documents

This ADR does not replace `docs/ARCHITECTURE.md` and does not create a competing architectural constitution.

`docs/ARCHITECTURE.md` remains the project's technical law.

`docs/adr/README.md` remains the catalog of ADRs and architectural history.

Issue #57 is the implementation work item. It cannot independently redefine the architectural boundary established here.

## 31. Final Decision

> **SCS-003 Booking is an independent Class A Business System / SCS at Level 0 — Modular Monolith, owning Resource, Availability Window and Reservation, with tenant isolation, timezone-aware input, UTC persistence/comparison, half-open intervals, atomic protection against concurrent double booking, `ACTIVE → CANCELLED` lifecycle, reuse of existing foundation boundaries, and independence from Commerce and Learning.**

After independent delivery of Booking, the project stops for a separate Factory Gate #1 review.

**ADR-0014 RATIFIED.**
