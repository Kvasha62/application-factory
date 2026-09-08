"""IS-003 Authorization Boundary / Permission Authority — Platform Service (class B), Level 0.

This component answers exactly one question: may this verified subject perform
this operation on this tenant-scoped resource? The answer is a minimal decision,
``ALLOW`` or ``DENY``, with a stable reason code. Enforcement stays with the
component that owns the data (ARCHITECTURE.md §6.2).
"""

COMPONENT_ID = "authorization"
COMPONENT_VERSION = "0.1.0"
COMPONENT_CLASS = "platform_service"
