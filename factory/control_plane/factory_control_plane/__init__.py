"""Application Factory Control Plane — Slice 1 document core.

Slice 1 implements only the ratified boundary: Project,
RequirementsVersion, Configuration (aggregate) and immutable
ConfigurationVersion with deterministic SHA-256 digests. Proposal,
Approval, Build and Release are attachment points only; intake channels,
the control-plane API and any Actual-state handling are out of scope.

Desired State only: nothing in this package reads, writes or models
runtime, deployment, health or observed-identity state.
"""
