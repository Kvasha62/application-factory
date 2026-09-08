"""IS-004 Data Ownership / Resource Boundary — Platform Service (class B), Level 0.

This component is a data owner. It owns tenant-scoped proof resources and
proves what ``ALLOW`` from IS-003 means at the boundary that owns the data:
an ``ALLOW`` is not direct data access — the data owner remains the final
enforcement boundary (ARCHITECTURE.md §5.3, §6.2).

Every published access path runs one fixed enforcement chain:

* ``ownership_boundary`` — the resource exists here and its single owner is
  this component;
* ``authorization_decision`` — IS-003 decides; the default outcome is DENY and
  a dependency that does not answer, or answers outside its contract, fails
  closed to DENY;
* ``owned_data_operation`` — only now is the resource read or changed.

The component owns resources and an access audit journal, and nothing else:
no identity verification, no tenant registry and no tenant derivation of its
own — the effective tenant of a request comes from IS-001 through the IS-003
decision, and a caller-supplied ``tenant_id`` is forwarded as a cross-check
only (LAW-16, LAW-16a).
"""

COMPONENT_ID = "records"
COMPONENT_VERSION = "0.1.0"
COMPONENT_CLASS = "platform_service"

#: The single data owner of every resource this component serves.
OWNER_COMPONENT = COMPONENT_ID
