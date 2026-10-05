# F-5 Work Item — Runtime Dependency Enforcement

- **Status:** Proposed — revision 2 awaits a new owner approval gate
- **Revision:** 2 — corrected for owner decisions D-A / P1 / P2 (PR #138 comment `5991426192`). Governance/documentation correction only; no implementation.
- **Approval history:** Revision 1 was approved by the owner on 2026-10-05 (commit `2d01c00`). That approval covered the pre-correction text and **does not carry over to revision 2**, whose scope differs.
- **Governance basis:** ADR-0017 §44
- **Decision basis:** ADR-0021 (ratified 2026-10-05)
- **Owner-selected decisions:** D1=A, D2=B; D-A / P1 / P2 recorded in PR #138 comment `5991426192`
- **Implementation authorization:** None for revision 2 until the owner explicitly approves this revision

## 1. Objective

Implement the minimum F-5 runtime mechanism required by ratified ADR-0021:

- satisfy the `identity → tenant_authority` published API dependency through network transport, served from the provider's own runtime process;
- obtain the dependency endpoint from a static, explicit declaration in the authoritative environment binding;
- start components in dependency order, so the provider is running before the component that consumes it;
- enforce the declared dependency contract/version requirements;
- fail closed on unavailable, wrong, stale, or unverifiable dependency state;
- preserve component identity/version/digest and isolation semantics.

## 2. In scope

1. Runtime network transport for the published `tenant_authority` contract, **including the provider-side listener** that serves that contract from the provider's own runtime process. A transport has two ends; the provider end is part of D1=A, not an extension of it.
2. Explicit dependency endpoint in the **authoritative environment binding**, **statically and explicitly declared** there (owner decision P1). Deployment & Operations does not dynamically allocate the endpoint within F-5.
3. The authoritative environment binding artifact itself, declaring the static dependency endpoint. No such repository artifact exists today; the loader (`environment.load_environment`) and the CLI entry point already do.
4. Enforcement that the bound endpoint corresponds to the declared dependency target.
5. Declared version-range compatibility validation.
6. Deterministic fail-closed behavior for:
   - unavailable dependency;
   - wrong endpoint/target;
   - incompatible version;
   - stale or unverifiable dependency state.
7. **Dependency-aware start order** (owner decision P2): a provider dependency is started before the component that consumes it. Ordering is derived from declared dependencies, not from component identifiers, and retry is not used as a substitute for correct ordering.
8. Provenance and cross-component isolation checks.
9. Tests covering normal and adversarial paths.
10. Quality Gate and independent review evidence.

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
- **D2=B component reclassification** (owner decision D-A): all registry / derived-catalog / canonical-documentation / Platform Instance changes arising from classifying `idempotency` and `saga` as shared in-process libraries. These are handled by the separate Level-C work item `docs/work-items/D2B-library-reclassification.md`, in accordance with ADR-0021 §4 ("as a separate Level-C change set") and §5 ("the approved F-5 implementation slice may implement only the minimum mechanism required to satisfy the selected D1 decision").
- **Dynamic allocation of the dependency endpoint** by Deployment & Operations (owner decision P1).
- **Retry/backoff as a substitute for correct start order** (owner decision P2).

## 4. Acceptance criteria

The F-5 implementation is acceptable only if all are demonstrated:

1. `identity` reaches `tenant_authority` only through the bound network endpoint.
2. The endpoint is **statically declared in** the authoritative environment binding and is traceable to it; Deployment & Operations allocates no endpoint dynamically (owner decision P1).
3. The bound endpoint is served by the provider's own runtime process — a provider-side listener — and neither by the consumer nor by Deployment & Operations.
4. The target satisfies the declared dependency version range.
5. An unavailable dependency produces a deterministic fail-closed result.
6. A wrong endpoint is rejected.
7. A stale or unverifiable dependency state is rejected.
8. No hidden in-process fallback or artifact substitution exists.
9. `tenant_authority` remains independently deployable.
10. Start order is derived from declared dependencies, not from component identifiers: the provider (`tenant_authority`) is running before the consumer (`identity`) is started, and retry is not used to compensate for a wrong order (owner decision P2).
11. Exact component identity/version/digest semantics remain intact.
12. Adversarial tests cover endpoint provenance, binding, compatibility, failure, and cross-component isolation.
13. Quality Gate passes.
14. Independent review is recorded.
15. Owner approval is recorded before merge.

## 5. Deliverables

- F-5 implementation changes on a dedicated branch/work item.
- Provider-side listener serving the published `tenant_authority` contract from the provider's own runtime process.
- The authoritative environment binding artifact declaring the static dependency endpoint.
- Dependency-aware start ordering in the deployment path.
- Tests and adversarial coverage.
- Quality Gate result.
- Independent review result.
- Owner approval record.

The registry / catalog / documentation / Platform Instance artifacts arising from D2=B are **not** deliverables of this work item; they are deliverables of `docs/work-items/D2B-library-reclassification.md` (owner decision D-A).

## 6. Governance gate

Revision 1 of this work item was **Approved** by the owner on 2026-10-05 (commit `2d01c00`). Revision 2 is **Proposed**: it is the separate implementation authorization required by ADR-0017 §44, corrected for owner decisions D-A / P1 / P2.

**The revision-1 approval does not carry over to revision 2. No implementation, merge, or release action is authorized until the owner explicitly approves revision 2.**

After approval, implementation must remain bounded by this document and ADR-0021. Any material scope expansion requires a new owner decision or separately approved change.

## 7. References

- ADR-0017 §44 — separate approved work item and acceptance criteria for later implementation slices.
- ADR-0021 — Runtime Dependency Transport and Component Classification for F-5; §4 (D2=B is a separate Level-C change set) and §5 (the F-5 slice implements only the D1 mechanism) bound this work item.
- ADR-0020 §4–§5 — Platform Identity Surface and ownership/verification separation.
- ADR-0016 — deployment/runtime lifecycle ownership and component boundary model.
- `docs/work-items/D2B-library-reclassification.md` — the separate Level-C work item carrying the D2=B change set.
- Permanent PR #138 — owner-gate and Arena communication channel; owner decisions D-A / P1 / P2 recorded in comment `5991426192`.
