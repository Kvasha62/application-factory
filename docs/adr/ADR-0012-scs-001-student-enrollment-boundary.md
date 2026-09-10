# ADR-0012 — SCS-001 Student Enrollment Boundary

**Status:** `RATIFIED` — принято владельцем проекта 2026-09-10  
**Date:** 2026-09-10  
**Ratified:** 2026-09-10  
**Level:** D — domain/architecture boundary decision

## Context

SCS-001 Learning already provides the core learning-content chain:

`Course → Module → Lesson → Assignment → Submission → Teacher Review`

ADR-0011 explicitly lists `Enrollment` among Learning-owned business entities, but its business semantics and API boundary were not yet defined. The implementation currently has no Enrollment model or corresponding business operation.

External verification of mature learning platforms confirms that enrollment is commonly modeled as a distinct relationship between a learner and a course, separate from identity/authentication. Mature platforms also support broader enrollment mechanisms and lifecycle states, but those capabilities are intentionally outside the current Level-0 scope.

This ADR defines the minimum coherent Enrollment capability without importing the wider LMS feature set or changing existing Course Read semantics.

## Decision

SCS-001 Learning introduces a minimal business entity `Enrollment` representing the participation of a specific student identity in a specific Course within a tenant.

The first capability slice provides:

1. Student self-enrollment.
2. Enrollment creation.
3. Reading the student's own Enrollment.
4. Course existence validation.
5. Enrollment only into a `PUBLISHED` Course.
6. Tenant isolation.
7. Authorization through the existing SCS-001 enforcement chain.
8. Duplicate protection for Student + Course.
9. Mandatory `Idempotency-Key` for the state-changing command.
10. Audit through the existing Learning audit mechanism.

## Business semantics

`Enrollment` is the business fact:

> A specific student identity is enrolled in a specific Course within a specific tenant.

Learning owns Enrollment. It is a tenant-scoped business record with, at minimum:

`enrollment_id`, `tenant_id`, `course_id`, `student_identity_id`, `status`, `created_at`, `updated_at`.

The authenticated subject creates an Enrollment only for its own student identity; callers cannot use this capability to enroll another identity.

## Lifecycle

The first capability slice has only one Enrollment state:

`ACTIVE`

No `INVITED`, `SUSPENDED`, `COMPLETED`, `CANCELLED`, `DELETED`, or `EXPIRED` states are introduced by this ADR.

## Self-enrollment

The first capability slice supports only student self-enrollment.

Teacher/admin enrollment, invitations, bulk enrollment, cohort enrollment, and external enrollment sources are deferred.

## Course eligibility

Enrollment is permitted only when the referenced Course exists and has status `PUBLISHED`.

Enrollment into `DRAFT` or `ARCHIVED` Course is rejected.

This follows the publication boundary established by ADR-0011, where a published Course exposes a consistent published hierarchy for student consumption.

## Enrollment and Course Read

This ADR does **not** change the existing Course Read contract.

In particular, it does not establish:

`Enrollment required → Course Read allowed`

nor:

`No Enrollment → Course Read denied`

Existing published-course read semantics remain unchanged. Making Enrollment a prerequisite for Course Read would be a separate architectural decision because it changes existing business behavior and compatibility expectations.

## Authorization

Enrollment uses the existing SCS-001 enforcement chain:

`Identity → effective tenant → Authorization → Learning ownership boundary → owned data operation`

No second identity, tenant, authorization, or idempotency mechanism is introduced.

## Tenant isolation

Enrollment is tenant-scoped.

The Enrollment tenant and Course tenant must match the caller's effective tenant. Cross-tenant enrollment is rejected.

## Duplicate protection

For the first slice, the business invariant is:

`one Student + one Course → at most one ACTIVE Enrollment`

Repeated enrollment must not create a second active business record.

When persistent storage is introduced, this invariant must be protected by an appropriate storage-level constraint in addition to application checks.

## Idempotency

Enrollment creation is a state-changing operation and therefore uses IS-005 `Idempotency-Key`.

No new idempotency mechanism is introduced.

## Read semantics

The first read capability is limited to the authenticated student's own Enrollment.

This ADR does not introduce roster, participant listing, teacher/admin enrollment views, or cross-tenant reads.

## Audit

Enrollment creation and security-sensitive refusals use the existing Learning audit mechanism. At minimum, successful creation and refusals caused by authorization, tenant boundary, Course state, duplicate protection, or idempotency rules must remain observable through existing audit semantics.

No new audit subsystem is introduced.

## API boundary

The capability is exposed through the existing Learning component contract. The proposed business operations are:

- `learning.enrollments.create`
- `learning.enrollments.read`

Concrete HTTP paths and schema details are implementation-level decisions and must remain aligned with the component contract and OpenAPI.

No new component is created; Enrollment remains part of SCS-001 Learning.

## Data ownership

Learning owns Enrollment.

Other components must not access Learning storage directly. Cross-component access occurs only through established component contracts.

## Compatibility

ADR-0012 does not change Identity, Tenant Authority, Authorization, Course publication, Course Read, Submission, or Review semantics.

It adds a new capability boundary without making Enrollment a hidden prerequisite for existing consumers.

No architecture version bump is proposed unless implementation review identifies a genuine conflict with ratified architecture law.

## Explicit non-goals

This ADR does not authorize:

- unenrollment;
- cancellation, suspension, completion, or expiration;
- teacher/admin enrollment;
- invitations or bulk enrollment;
- cohort enrollment or external SIS enrollment;
- roster, participant listing, groups, sections, or enrollment roles;
- progress tracking or completion rules;
- grading, scores, feedback, or LearningResult;
- prerequisites, seat limits, waiting lists, enrollment windows, or payment;
- notifications or certificates;
- analytics or reporting;
- SIS/OneRoster/LTI integration;
- new foundation services;
- Component Catalog/Registry, Composer, or Golden Bundles;
- microservices;
- generic CRUD replacing business commands;
- events/CDC introduced solely for Enrollment.

## Acceptance criteria for implementation

Implementation is conformant only if:

1. Enrollment is owned by Learning.
2. Enrollment is tenant-scoped.
3. A student can self-enroll in a Course.
4. Only an existing `PUBLISHED` Course is eligible.
5. Cross-tenant enrollment is rejected.
6. A student cannot enroll another identity through this capability.
7. Duplicate active Enrollment is prevented.
8. Creation requires `Idempotency-Key`.
9. The student can read their own Enrollment.
10. Existing Course Read semantics remain unchanged.
11. Authorization follows the existing SCS-001 enforcement chain.
12. Security-sensitive operations use existing audit semantics.
13. No new foundation service or component is introduced.
14. No direct foreign database access is introduced.
15. No explicit non-goal is implemented as part of this slice.
16. Existing Quality Gate remains green.
17. Component Contract and OpenAPI remain aligned.
18. No architecture version bump is required unless a genuine conflict with ratified architecture law is discovered.

## Web verification note

External documentation from mature learning platforms was consulted to validate the general semantics of enrollment as a distinct learner-course relationship and to distinguish common enrollment capabilities from the minimal scope selected here.

External LMS behavior is semantic validation only. It does not constitute project law. `docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains the project's technical law, and ratified ADRs record project decisions within that law.

## Architectural authority

`docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains authoritative.

Existing IS-001, IS-003, IS-004, IS-005 and IS-006 boundaries remain authoritative and unchanged.

## Ratification

RATIFIED by the project owner (`@Kvasha62`) on 2026-09-10.

This ratification confirms the decision as written. It does not change `docs/ARCHITECTURE.md` v1.2.0 and does not authorize implementation beyond the scope defined above.
