"""Learning — Business System (class A), Level 0.

The first implementation slice of the Learning component: courses, modules,
lessons, assignments, enrollments and submissions behind an explicit
command-shaped API (``/api/v1/learning``). This component owns its business
data (logical schema ``learning``) and is the final enforcement boundary for
it (ARCHITECTURE.md §5.3, §6.2).

Facts this component does not own are consumed, never re-implemented:

* verified identity and the effective tenant — IS-001, read per request
  through the published context contract;
* the access decision — IS-003, read per access through the published
  decision contract; the default outcome is DENY and a dependency that does
  not answer fails closed;
* exactly-once business effect for state-changing commands — IS-005, the
  published command-safety guard every required command is delivered through.

Every path a request can take runs one fixed chain:

    identity → effective tenant → ownership boundary → authorization
    → data ownership (the owned-data operation, inside IS-005 for commands)

There is no generic CRUD surface, no grading, no events, no CDC and no
second identity, tenant-context, authorization or idempotency mechanism.
"""

COMPONENT_ID = "learning"
COMPONENT_VERSION = "0.1.0"
COMPONENT_CLASS = "business_system"

#: The single data owner of every course, module, lesson, assignment,
#: enrollment and submission served by this component.
OWNER_COMPONENT = COMPONENT_ID
