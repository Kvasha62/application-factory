# ADR-0011 — SCS-001 Learning Content Authoring Boundary

**Status:** `RATIFIED` — принято владельцем проекта 2026-09-10
**Date:** 2026-09-09
**Ratified:** 2026-09-10
**Level:** D — domain/architecture boundary decision

## Context

SCS-001 currently has three accepted capability slices: Submission read, Teacher Submission Discovery, and immutable Teacher Review. The canonical component therefore proves the downstream part of the Learning flow, but the content hierarchy that originates an Assignment is not yet established as an implemented business capability.

This is a domain-capability gap, not a request for generic CRUD or API completeness.

## Decision under review

The next business capability of SCS-001 is **Learning Content Authoring**.

The minimal coherent business chain is:

`Course → Module → Lesson → Assignment → Submission → Teacher Review`

The first authoring slice establishes the upstream four entities and their business invariants without changing the accepted Submission/Review semantics.

## Proposed first authoring slice

### Teacher commands

1. Create Course.
2. Create Module inside a Course.
3. Create Lesson inside a Module.
4. Create Assignment inside a Lesson.
5. Publish Course.
6. Archive Course.

Child content is created in `DRAFT` state. Course publication is the single publication command in the first slice and publishes the complete valid child hierarchy together with the Course.

A Course is publishable only when its hierarchy is structurally valid for the slice. Publication is atomic: either the Course and its complete child hierarchy become `PUBLISHED`, or no publication effect is applied.

After publication, Course/Module/Lesson/Assignment structure is immutable in this first slice. There is no unpublish or editing of published content.

### Student reads

Students may read published learning content sufficient to consume the hierarchy:

`Course → Module → Lesson → Assignment`

Reads expose only content belonging to the caller's effective tenant and only content that is published. No student authoring capability is introduced.

### Lifecycle

Course, Module, Lesson, Assignment:

`DRAFT → PUBLISHED → ARCHIVED`

No `UNPUBLISH` transition.

Archive is a teacher command. Archiving a Course archives its child content as one atomic business operation. Archived content is not available through published-content student reads.

Submission remains exactly `DRAFT → SUBMITTED`.

Teacher Review remains an immutable fact represented by `reviewed_by` and `reviewed_at`; this ADR does not change its semantics.

## Authorization

Teacher authoring/publication/archive requires:

`Identity → effective tenant → Authorization → Learning ownership boundary`

Student content reads require verified identity, effective tenant context, authorization, and Learning ownership/publication checks.

No second identity, tenant, authorization, or idempotency mechanism is introduced.

## Idempotency

All state-changing authoring commands use IS-005 `Idempotency-Key`.

Publication and archive are atomic business commands and must not partially apply on replay, binding conflict, authorization denial, or dependency failure.

## Data ownership

Learning owns Course, Module, Lesson, Assignment, Enrollment, and Submission. All business records are tenant-scoped and owned by `learning`.

External identity/profile, credentials, payment, media, analytics, and other platform concerns remain outside Learning.

## Explicit non-goals

This ADR does not authorize:

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
- generic CRUD replacing business commands;
- content revision/versioning;
- reordering/moving published content;
- bulk authoring;
- search, pagination, or advanced filtering.

## Architectural authority

`docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains the sole architectural law.

Existing IS-001, IS-003, IS-004, IS-005 and IS-006 boundaries remain authoritative and unchanged.

No architecture version bump is proposed unless implementation review identifies a genuine conflict with the ratified architecture.

## Consequence

If ratified, an implementation Issue can be created as a narrow SCS-001 content-authoring slice. The implementation must extend the existing Learning boundary rather than redesigning the foundation or absorbing unrelated deferred capabilities.

## Ratification

RATIFIED by the project owner (`@Kvasha62`) on 2026-09-10. The ratification confirms the decision as written; it does not change `docs/ARCHITECTURE.md` v1.2.0 RATIFIED and does not bump the architecture version, as this ADR explicitly states.

**Process note (recorded, not rewritten).** The first authoring slice (Issue #31; SCS-001 learning component 0.2.0, including the `create_course` / `create_module` / `create_lesson` / `create_assignment` / `publish_course` / `archive_course` commands and the immutable published hierarchy) was merged into `main` (commit `8445e2b`) while this ADR was still `PROPOSED`. This deviation from the LAW-14/§34 sequence "ratify first, implement after" was identified by the 2026-09-09 conformance review (`docs/CONFORMANCE_REVIEW_1.2.0.md`, finding H-2) and is recorded here deliberately: the history is preserved, not re-dated, and the implementation is not claimed to pre-authorize itself. From this ratification onward, essential component decisions must be RATIFIED before their implementation is merged.
