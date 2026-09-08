"""IS-001 Identity / Tenant Context — Platform Service (class B), Level 0.

Version 0.2.0 adds the IS-002 integration: Tenant lifecycle state and Platform
Instance ownership are read from Tenant Authority through its published
contract, so no second source of Tenant state exists in the platform.

Version 0.3.0 adds one published read — ``GET /api/v1/context`` and the
value-only ``identity_service.reader.IdentityContextClient`` — so that the
Authorization Boundary (IS-003) can obtain a verified subject and the effective
tenant from this component instead of inventing a second identity or
tenant-context mechanism. The addition is additive: the existing operations are
unchanged, and the context read grants nothing by itself.
"""

COMPONENT_ID = "identity"
COMPONENT_VERSION = "0.3.0"
COMPONENT_CLASS = "platform_service"
