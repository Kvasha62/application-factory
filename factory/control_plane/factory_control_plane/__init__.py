"""Application Factory Control Plane — Slice 1 document core.

Slice 1 implements only the ratified boundary: Project,
RequirementsVersion, Configuration (aggregate) and immutable
ConfigurationVersion with deterministic SHA-256 digests. Proposal,
Approval, Build and Release are attachment points only; intake channels,
the control-plane API and any Actual-state handling are out of scope.

Desired State only: nothing in this package reads, writes or models
runtime, deployment, health or observed-identity state.
"""

# Slice 2 (additive, S2-1 / S2-2 / S2-3) lives in sibling modules and changes
# nothing above: ``configuration_v2`` (the projectable configuration shape),
# ``proposals`` (immutable validation reports), ``approvals`` (the append-only
# approval ledger plus the RBAC attachment point), ``composition_requests``
# (the request record and its verifier), ``findings`` (the frozen finding
# registry) and ``support`` (shared settle/link helpers).
#
# The frozen boundaries stay in force: Desired state only, no Actual-state
# readers, no publication, no workflow dispatch, and no RBAC policy.
