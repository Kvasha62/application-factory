"""SCS-001 Learning — Business System / SCS (class A), Level 0.

This component is a data owner. It owns tenant-scoped Learning data
(Assignments and Submissions) and proves what ``ALLOW`` from IS-003 means
at the boundary that owns the data: an ``ALLOW`` is not direct data
access — the data owner remains the final enforcement boundary
(ARCHITECTURE.md §5.3, §6.2).

Every published access path runs one fixed enforcement chain:

* ``ownership_boundary`` — the Submission exists here and its single owner
  is this component;
* ``authorization_decision`` — IS-003 decides; the default outcome is DENY
  and a dependency that does not answer, or answers outside its contract,
  fails closed to DENY;
* ``owned_data_operation`` — only now is the submission read.

The component owns Learning data and an access audit journal, and nothing
else: no identity verification, no tenant registry and no tenant
derivation of its own — the effective tenant of a request comes from
IS-001 through the IS-003 decision, and a caller-supplied ``tenant_id``
is forwarded as a cross-check only (LAW-16, LAW-16a).

Slice 1 publishes the single-submission read
``GET /api/v1/learning/submissions/{submission_id}``. Slice 2 (Issue #20)
adds exactly one business read operation — teacher submission discovery
``GET /api/v1/learning/assignments/{assignment_id}/submissions`` — on top
of it. Slice 3 adds exactly one state-changing command — the teacher
submission review
``POST /api/v1/learning/submissions/{submission_id}/review`` — which records
``reviewed_by`` / ``reviewed_at``, leaves the lifecycle ``SUBMITTED`` and is
guarded by IS-005. Errors use the approved SCS-001 envelope (``error.code``
/ ``error.message`` / ``error.details`` plus top-level ``request_id`` /
``correlation_id``).
"""

COMPONENT_ID = "learning"
COMPONENT_VERSION = "0.1.0"
COMPONENT_CLASS = "business_system"
SCS_ID = "SCS-001"

#: The single data owner of every resource this component serves.
OWNER_COMPONENT = COMPONENT_ID
