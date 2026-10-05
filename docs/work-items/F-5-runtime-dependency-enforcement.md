# F-5 Work Item — Runtime Dependency Enforcement

- **Status:** Approved — owner approved on 2026-10-05
- **Governance basis:** ADR-0017 §44
- **Decision basis:** ADR-0021 (ratified 2026-10-05)
- **Owner-selected decisions:** D1=A, D2=B
- **Implementation authorization:** Approved by the owner on 2026-10-05; implementation may begin within this work item's boundaries

## 1. Objective

Implement the minimum F-5 runtime mechanism required by ratified ADR-0021:

- satisfy the `identity → tenant_authority` published API dependency through network transport;
- obtain the dependency endpoint from explicit environment binding;
- enforce the declared dependency contract/version requirements;
- fail closed on unavailable, wrong, stale, or unverifiable dependency state;
- preserve component identity/version/digest and isolation semantics.

## 2. In scope

1. Runtime network transport for the published `tenant_authority` contract.
2. Explicit dependency endpoint in environment binding.
3. Enforcement that the bound endpoint corresponds to the declared dependency target.
4. Declared version-range compatibility validation.
5. Deterministic fail-closed behavior for:
   - unavailable dependency;
   - wrong endpoint/target;
   - incompatible version;
   - stale or unverifiable dependency state.
6. Provenance and cross-component isolation checks.
7. Tests covering normal and adversarial paths.
8. Required registry/catalog/documentation/Platform Instance updates arising from D2=B:
   - represent `idempotency` and `saga` as shared in-process libraries;
   - remove independently deployed membership where currently present;
   - recompute affected Platform Instance identity/digest;
   - update machine-verifiable boundaries.
9. Quality Gate and independent review evidence.

## 3. Explicitly out of scope

- Kubernetes/orchestrator selection.
- Cloud/vendor selection.
- Generalized service discovery.
- Fleet orchestration.
- Business/data ownership changes.
- Automatic remediation.
- Upgrade/rollback redesign.
- OCI/GHCR publication changes unrelated to F-5.
- Generalized dependency framework beyond the minimum contract.
- New network contracts for `idempotency` or `saga`.

## 4. Acceptance criteria

The F-5 implementation is acceptable only if all are demonstrated:

1. `identity` reaches `tenant_authority` only through the bound network endpoint.
2. The runtime endpoint is traceable to authoritative environment binding.
3. The target satisfies the declared dependency version range.
4. An unavailable dependency produces a deterministic fail-closed result.
5. A wrong endpoint is rejected.
6. A stale or unverifiable dependency state is rejected.
7. No hidden in-process fallback or artifact substitution exists.
8. `tenant_authority` remains independently deployable.
9. Exact component identity/version/digest semantics remain intact.
10. `idempotency` and `saga` are not independently deployed runtime dependencies.
11. Registry, derived catalog, canonical documentation, and affected Platform Instance definition are mutually consistent with D2=B.
12. Affected Platform Instance identity/digest is recomputed and explicitly recorded rather than mutated in place.
13. Adversarial tests cover endpoint provenance, binding, compatibility, failure, and cross-component isolation.
14. Quality Gate passes.
15. Independent review is recorded.
16. Owner approval is recorded before merge.

## 5. Deliverables

- F-5 implementation changes on a dedicated branch/work item.
- Tests and adversarial coverage.
- Updated canonical registry/catalog/documentation/instance artifacts required by D2=B.
- Quality Gate result.
- Independent review result.
- Owner approval record.

## 6. Governance gate

This work item is **Approved** by the owner on 2026-10-05. It is the separate implementation authorization required by ADR-0017 §44.

**No implementation, merge, or release action is authorized until the owner explicitly approves this work item.**

After approval, implementation must remain bounded by this document and ADR-0021. Any material scope expansion requires a new owner decision or separately approved change.

## 7. References

- ADR-0017 §44 — separate approved work item and acceptance criteria for later implementation slices.
- ADR-0021 — Runtime Dependency Transport and Component Classification for F-5.
- ADR-0020 §4–§5 — Platform Identity Surface and ownership/verification separation.
- ADR-0016 — deployment/runtime lifecycle ownership and component boundary model.
- Permanent PR #138 — owner-gate and Arena communication channel.
