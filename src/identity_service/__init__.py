"""IS-001 Identity / Tenant Context — Platform Service (class B), Level 0.

Version 0.2.0 adds the IS-002 integration: Tenant lifecycle state and Platform
Instance ownership are read from Tenant Authority through its published
contract, so no second source of Tenant state exists in the platform.
"""

COMPONENT_ID = "identity"
COMPONENT_VERSION = "0.2.0"
COMPONENT_CLASS = "platform_service"
