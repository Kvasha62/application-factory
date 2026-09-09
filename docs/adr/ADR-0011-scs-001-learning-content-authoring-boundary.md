# ADR-0011 — SCS-001 Learning Content Authoring Boundary

**Status:** PROPOSED
**Date:** 2026-09-09
**Level:** D — domain/architecture boundary decision

## Context

SCS-001 currently provides the canonical Submission capability slices: individual Submission read, Teacher Submission Discovery, and immutable Teacher Review. The current implementation/contract owns `assignments` and `submissions` for these slices.

The capability review identified a real business gap: the current Learning implementation cannot itself establish the upstream learning-content chain required to originate an Assignment inside the SCS boundary. This is a domain capability gap, not an API-completeness gap.

## Decision under review

The next Learning capability should be **Learning Content Authoring**, beginning with the smallest coherent content hierarchy:

`Course → Module → Lesson → Assignment`

The purpose is to establish and maintain the learning content that later produces student enrollments and submissions.

## Proposed boundary

SCS-001 Learning owns:
- Course
- Module
- Lesson
- Assignment
- Enrollment
- Submission

External identity/profile/payment/media/analytics/etc. remain outside Learning.

All Learning business data is tenant-scoped and owned by `learning`.

## Proposed lifecycle vocabulary

Course:
`DRAFT → PUBLISHED → ARCHIVED`

Module/Lesson/Assignment:
`DRAFT → PUBLISHED → ARCHIVED`

No `UNPUBLISH` transition.

After Course publication, the first implementation slice should treat course structure as immutable unless a later decision explicitly introduces revision semantics.

## Explicit exclusions

This ADR does not authorize implementation of:
- grading;
- score;
- feedback/comments;
- LearningResult;
- progress tracking;
- Enrollment completion rules;
- analytics;
- media service;
- payment;
- events/CDC solely for this capability;
- new foundation services;
- Component Catalog/Composer/Golden Bundles;
- microservices;
- generic CRUD as a replacement for business commands.

## Architectural constraints

`docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains the sole architectural law.

Use the existing chain:

`Identity → effective tenant → Authorization → Learning ownership boundary`

State-changing commands use IS-005 idempotency.

No second identity, tenant, authorization, or idempotency mechanism may be introduced.

## Open questions before ratification

1. What is the minimum authoring/read capability required to establish the content hierarchy without prematurely implementing the full authoring UI?
2. Which operations are required for Teacher and which content reads are required for Student?
3. Which lifecycle transitions belong in the first content-authoring slice?
4. What exact relationship between Course publication and child publication is required?

## Expected outcome

Ratify a minimal, implementation-ready Learning Content Authoring slice only after the above semantics are decided. The resulting implementation Issue must remain narrow and self-contained.
