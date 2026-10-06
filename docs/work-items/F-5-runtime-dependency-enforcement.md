# F-5 Work Item — Runtime Dependency Enforcement

- **Status:** Approved — revision 2, approved by the owner on 2026-10-05 (permanent PR #138 comment `5992205274`) at the exact SHA `8bc54ccf0cfac1d0643814f90a578ac93d4eee8b`; **implementation complete** after PR #150 merged at `42ca157ae1a825223df8adcea024017085cbfc04` on 2026-10-06 (closure evidence in §6.2)
- **Revision:** 2 — corrected for owner decisions D-A / P1 / P2 (PR #138 comment `5991426192`). Governance/documentation correction only; no implementation.
- **Approval history:** Revision 1 was approved by the owner on 2026-10-05 (commit `2d01c00`); that approval covered the pre-correction text and did **not** carry over to revision 2, whose scope differs. Revision 2 was then approved by the owner on 2026-10-05 (PR #138 comment `5992205274`), limited to the exact text at SHA `8bc54ccf0cfac1d0643814f90a578ac93d4eee8b`. Sections §1–§5 below are byte-identical to that SHA; only this governance record was edited afterwards.
- **Governance basis:** ADR-0017 §44
- **Decision basis:** ADR-0021 (ratified 2026-10-05)
- **Owner-selected decisions:** D1=A, D2=B; D-A / P1 / P2 recorded in PR #138 comment `5991426192`
- **Implementation authorization:** Granted for revision 2 by PR #138 comment `5992205274`, only within the boundary of §2/§3 and the acceptance criteria of §4. It does **not** extend to the identity `0.4.0` cascade — see §6.1. Merge requires a separate owner authorization.

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

Revision 1 of this work item was **Approved** by the owner on 2026-10-05 (commit `2d01c00`); that approval covered the pre-correction text and did not carry over to revision 2.

**Revision 2 is Approved.** The owner approved it on 2026-10-05 in permanent PR #138 comment `5992205274`, limited to the exact revision at SHA `8bc54ccf0cfac1d0643814f90a578ac93d4eee8b`. Implementation is authorized **only** within the boundary of §2/§3 and the acceptance criteria of §4, and remains subject to the ADR-0017 §31 chain: implementation/verification → Quality Gate → independent review → owner approval → merge. This approval is not a merge authorization.

Implementation must remain bounded by this document and ADR-0021. Any material scope expansion requires a new owner decision or separately approved change.

### 6.1 The identity `0.4.0` cascade is outside this work item's boundary

The `identity 0.3.0 → 0.4.0` cascade carried in PR #150 is **not** part of revision 2's approved boundary. Sections §1–§5 of this document authorize no component-version change, no change to another component's declared dependency range, no digest-lock change and no Control-Plane artifact change; §1 and §4.11 require component identity/version/digest **semantics** to be preserved, which is a rule about the mechanism, not an authorization to re-pin versions.

That cascade is authorized by its own explicit owner decisions in permanent PR #138, recorded here for traceability — not by this work item:

| Owner decision | What it authorizes |
|---|---|
| `5993694601` | `SELECT: Bump identity 0.3.0 → 0.4.0 (full cascade)` — `COMPONENT_VERSION`, the canonical registry, the derived catalog, the identity contract, the golden bundle, the composer example request, the affected control-plane configs/fixtures, and the component digest lock |
| `5998321766` (G1) | byte-changing exactly four previously frozen Slice-1 artifacts |
| `5998764930` (G2 = Option A) | the in-place amendment of the immutable version recorded as a dated one-off owner exception, additive-only, in `factory/control_plane/README.md` |
| `5999402241` (G3) | the Slice-2 contract digest and fingerprint re-pins, `SLICE1_V1_DIGEST`, the five dependent ranges, the required derived artifacts and the required test expectations/pins |

No part of that cascade is authorized by revision 2 alone, and this work item's boundary is not broadened by recording it. D2=B remains out of scope (§3); its separate Level-C change set was subsequently owner-approved in PR #138 comment `6011355240` and merged via PR #151 (head `e59180227a527f6770c11da50e9bdaf2925b4568`, merge commit `795f3bd26ea844eae96521eb77305c21241a597a`). That separate closure does not add D2=B to this work item’s scope or deliverables.

### 6.2 Implementation closure evidence

**F-5 implementation status: COMPLETE and MERGED.** Revision 2 remains the exact owner-approved
work-item text at SHA `8bc54ccf0cfac1d0643814f90a578ac93d4eee8b` (PR #138 comment `5992205274`).
Sections §1–§5 above remain byte-identical to that approved revision. This closure record adds
outcome evidence only; it does not broaden or retroactively amend the approved scope. The separately
authorized identity `0.4.0` cascade remains distinct under §6.1.

| Gate / outcome | Evidence |
|---|---|
| Independent review | PR #138 comment `6001427278` reviewed PR #150 at `26ebae2be336f1a0b6539c56510bae195d856276`: F-5 technical acceptance **PASS**, overall **PASS WITH FINDINGS**. The F-1/S4 governance findings were subsequently dispositioned by the owner in comment `6010112046` and recorded closed in `6010183919`; the remaining documentation closures were recorded in `6010901930`. |
| Owner approval | Revision 2 implementation authorization: PR #138 comment `5992205274`, limited to the exact SHA above. Separate owner approval to merge PR #150 after the completed review and checks: comment `6010966700`. |
| PR #150 merge | PR #150 head `b0ea954c4df5e42b5030b363a24911b5a4c65356` merged on 2026-10-06 at `42ca157ae1a825223df8adcea024017085cbfc04`. |
| Quality Gate / CI | Post-merge Quality Gate run `37425867742` at merge SHA `42ca157…`: **success**, including compile, dependency check, Ruff, Black, root tests, and Control Plane tests. |
| Deterministic artifact build and publication | Publish component artifacts run `37425867719` at `42ca157…`: **success**; the deterministic build/verification job and GHCR publish-by-digest job both completed successfully. |
| Post-publication immutable-content verification | Read-only run `37428180050` (`workflow_dispatch`, head `42ca157…`): **success**; job “Fetch + independent SHA-256 of 9 digest-addressed artifacts” completed successfully. |

These records establish implementation, CI, artifact publication, and post-publication verification;
they do **not** assert that a production runtime bootstrap or deployment was performed.

## 7. References

- ADR-0017 §44 — separate approved work item and acceptance criteria for later implementation slices.
- ADR-0021 — Runtime Dependency Transport and Component Classification for F-5; §4 (D2=B is a separate Level-C change set) and §5 (the F-5 slice implements only the D1 mechanism) bound this work item.
- ADR-0020 §4–§5 — Platform Identity Surface and ownership/verification separation.
- ADR-0016 — deployment/runtime lifecycle ownership and component boundary model.
- `docs/work-items/D2B-library-reclassification.md` — the separate Level-C work item carrying the D2=B change set.
- Permanent PR #138 — owner-gate and Arena communication channel; owner decisions D-A / P1 / P2 recorded in comment `5991426192`.
