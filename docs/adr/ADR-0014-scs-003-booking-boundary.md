# ADR-0014 — SCS-003 Booking Boundary

**Status:** RATIFIED  
**Date:** 2026-09-13  
**Ratified:** 2026-09-13 by owner approval

## Decision

Booking is an independent Class A Business System / SCS at Level 0 — Modular Monolith.

Booking owns Resource, Availability Window and Reservation business facts. Resource and Reservation are tenant-scoped. Booking uses timezone-aware timestamps normalized to UTC and half-open intervals `[start_at, end_at)`. An ACTIVE reservation must be fully contained in an applicable Availability Window. Two ACTIVE reservations for the same Resource must never overlap, including under concurrent requests; the invariant must be enforced atomically at the storage/business boundary. Reservation lifecycle is `ACTIVE -> CANCELLED` and cancellation preserves the record.

Booking reuses existing Identity, Tenant Authority, Authorization, Idempotency and Audit foundations. It does not access Learning or Commerce internals and does not introduce pricing, payment, analytics, notifications, calendar synchronization, new foundation services, microservices or Level 2 factory mechanics.

Initial public operations are:

- POST /resources
- GET /resources/{resource_id}
- POST /resources/{resource_id}/availability
- GET /resources/{resource_id}/availability
- POST /reservations
- GET /reservations/{reservation_id}
- POST /reservations/{reservation_id}/cancel

The initial domain error vocabulary may include resource_not_found, resource_inactive, availability_not_found, outside_availability, reservation_conflict, reservation_not_found, invalid_state_transition and booker_mismatch. Foundation errors remain owned by foundation components.

There is no implicit `paid order -> reservation` rule. Future entitlement integration requires a published cross-component contract.

If Booking is implemented and independently delivered, the project will have three confirmed independent Business Systems. That triggers a separate formal readiness review for factory Gate #1. Reaching the gate does not itself authorize Level 2 factory mechanics.

## Boundaries and non-goals

Booking owns Resources, Availability Windows, Reservations, booking lifecycle and consistency invariants. It does not directly access Identity internals, Tenant Authority internals, Commerce data, Learning data, analytics, notifications, CRM, external calendar synchronization or payment providers. Cross-component interaction uses published contracts, events, exports/CDC or officially defined integration mechanisms.

The initial MVP excludes pricing, payment, refunds, promotions, subscriptions, marketplace functionality, CRM, analytics, Booking-owned notification subsystems, calendar synchronization, recurring reservations, waitlists, resource capacity greater than one, separate Payment Service, new foundation services, microservices and Level 2 factory mechanics.

## Ratification

This ADR was independently reviewed and received PASS. It is now RATIFIED by explicit owner approval on 2026-09-13. Implementation of SCS-003 Booking is authorized to proceed according to the ratified boundary and the project's operating model.
