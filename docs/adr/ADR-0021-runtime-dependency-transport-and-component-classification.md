# ADR-0021 — Runtime Dependency Transport and Component Classification for F-5

- **Status:** Ratified — owner approved on 2026-10-05
- **Date:** 2026-10-05
- **Decision scope:** F-5 prerequisite governance
- **Related decisions:** D1=A, D2=B
- **Supersedes:** None
- **Implementation authorization:** Not granted by this ADR. Ratification permits the F-5 work item to be prepared/opened; implementation requires that work item's own owner approval (ADR-0017 §44)

## 1. Context

The F-5 owner gate selected two decisions that change the deployment/component model:

- **D1=A:** declared runtime component dependencies are satisfied through network transport for published contracts, with dependency endpoints represented in environment binding.
- **D2=B:** `idempotency` and `saga` are classified as shared in-process libraries rather than independently deployed runtime components.

ADR-0017 §44 requires every later implementation slice to have its own approved work item and acceptance criteria. The owner-gate record in permanent PR #138 therefore requires a Level-C ADR before the F-5 implementation slice can begin.

The relevant runtime dependency is the declared `identity → tenant_authority` API dependency. The repository evidence establishes that this dependency is already represented as a published API relationship; this ADR determines how that dependency is satisfied at runtime and how the two non-runtime components are classified.

## 2. Decision

### 2.1 D1 — Runtime dependency satisfaction: A

For a deployed component with a declared dependency on another component's published API contract:

1. The dependency is satisfied through **network transport**.
2. The target component remains independently deployable.
3. The dependency endpoint is an explicit part of the component's **environment binding**.
4. The runtime MUST use the declared published contract and MUST NOT silently substitute:
   - an in-process implementation,
   - another component,
   - another artifact,
   - another endpoint selected by an implicit or floating selector.
5. Dependency resolution MUST fail closed when the endpoint is unavailable, incompatible with the declared version range, stale, unverifiable, or otherwise inconsistent with the binding/contract requirements.
6. This ADR does not prescribe a particular network protocol, cloud, Kubernetes, vendor, service-discovery product, or fleet topology. Those choices remain outside this decision unless separately approved.
7. The existing one-component/one-runtime-element model remains intact: a runtime component is not merged into another component merely to satisfy a declared dependency.

### 2.2 D2 — Component classification: B

The following are classified as **shared in-process libraries**, not independently deployed Level-0 runtime components:

- `idempotency`
- `saga`

Consequences:

1. They do not require independent runtime deployment artifacts solely by virtue of their library role.
2. They do not become independent members of the deployed Platform Instance merely because they exist in the repository.
3. Their consumers may import their supported library surface in-process.
4. They are not treated as published network API dependencies unless a later, separately approved decision introduces such a contract.
5. Their classification MUST be represented consistently across the component registry, derived catalog, canonical documentation, and Platform Instance definition.
6. Removing them from independently deployed instance membership is a **new Platform Instance definition**, not an in-place mutation of an existing instance identity. Any resulting instance digest MUST therefore be recomputed and explicitly recorded.

## 3. Runtime dependency contract

For the F-5 `identity → tenant_authority` dependency, the minimum runtime contract is:

| Property | Normative requirement |
|---|---|
| Dependency kind | Published API |
| Transport | Network |
| Endpoint source | Environment binding |
| Target | Independently deployed `tenant_authority` |
| Version selection | Exact declared dependency range; no floating substitution |
| Failure mode | Fail closed |
| Fallback | No hidden in-process fallback |
| Artifact substitution | Forbidden |
| Endpoint substitution | Forbidden |
| Service discovery | Not prescribed by this ADR |
| Deployment topology | Not prescribed by this ADR |

The endpoint is configuration/binding data, not a replacement for the component contract. Runtime acceptance MUST verify both that the endpoint is the bound target and that the target satisfies the declared contract/version requirements.

## 4. Component classification and instance consequences

D2=B changes the meaning of the current registry entries for `idempotency` and `saga`.

The implementation work following ratification MUST, as a separate Level-C change set where required:

- extend the registry schema so the library classification is representable;
- classify `idempotency` and `saga` consistently in the canonical registry and derived catalog;
- update the canonical documentation that currently describes the Level-0 component set;
- remove `idempotency` from Platform Instance membership if it is currently represented as an independently deployed member;
- regenerate/recompute the affected instance digest and update the canonical instance artifact;
- update tests and boundary checks so the new classification is machine-verifiable.

No implementation of these changes is authorized merely by this ADR's existence. They require the normal approved work-item and quality-gate chain.

## 5. F-5 implementation boundary

After owner ratification, the approved F-5 implementation slice may implement only the minimum mechanism required to satisfy the selected D1 decision for `identity → tenant_authority`.

### In scope

- network transport for the published `tenant_authority` contract;
- explicit endpoint representation in environment binding;
- version compatibility enforcement against the declared dependency range;
- fail-closed handling for unavailable, wrong, stale, or unverifiable endpoints;
- preservation of exact component identity/version/digest semantics;
- cross-component isolation and provenance checks;
- adversarial tests for endpoint provenance, binding, version compatibility, unavailable dependency, wrong endpoint, and isolation;
- full Quality Gate, independent review, and owner approval before merge.

### Explicitly out of scope

- Kubernetes or other orchestration-platform selection;
- cloud/vendor selection;
- generalized service discovery;
- fleet orchestration;
- changes to business/data ownership;
- automatic remediation;
- upgrade/rollback redesign;
- OCI/GHCR publication changes;
- unrelated component-boundary refactoring;
- implementation of a generalized dependency framework beyond the minimum F-5 contract.

## 6. Alternatives considered

### D1 alternatives

**D1=B — single-process composition**

Rejected for this decision because it would replace the existing independently deployable component shape with a different runtime composition rule and would require a broader deployment-model decision.

**D1=C — dependency not satisfied at runtime**

Rejected because it leaves the existing `identity → tenant_authority` API dependency unsatisfied and makes the next F-5 implementation slice empty.

### D2 alternative

**D2=A — independently deployed runtime components**

Rejected because the current evidence shows `idempotency` is consumed through direct in-process imports and lacks a published runtime API, while `saga` has no independently deployable contract surface or runtime dependents. Treating either as a deployed service would therefore introduce a new contract and deployment obligation not justified by the current architecture.

## 7. Consequences

### Positive

- Preserves independent deployment of `identity` and `tenant_authority`.
- Makes the existing published API dependency explicit at runtime.
- Prevents hidden dependency substitution and accidental in-process coupling.
- Keeps library-only components out of the independently deployed runtime graph.
- Establishes a precise governance boundary before implementation begins.

### Negative / trade-offs

- Network dependency availability becomes part of runtime correctness.
- Environment binding must carry authoritative endpoint information.
- D2 classification requires synchronized registry/catalog/documentation/instance updates.
- Removing a component from instance membership changes instance identity and therefore requires a new digest.
- A future service-discovery or transport standard will require a separate architectural decision if it introduces behavior beyond this ADR.

## 8. Acceptance criteria for the subsequent F-5 work item

The implementation slice opened after ratification MUST demonstrate at minimum:

1. `identity` reaches `tenant_authority` only through the bound network endpoint.
2. The endpoint used at runtime is traceable to the declared environment binding.
3. The target satisfies the declared dependency version range.
4. An unavailable dependency causes a deterministic fail-closed result.
5. A wrong endpoint is rejected.
6. A stale or unverifiable dependency state is rejected.
7. No hidden in-process fallback or artifact substitution exists.
8. `tenant_authority` remains independently deployable.
9. Exact component identity/version/digest semantics remain intact.
10. `idempotency` and `saga` are not treated as independently deployed runtime dependencies.
11. Adversarial tests cover endpoint provenance, binding, compatibility, failure, and cross-component isolation.
12. Quality Gate passes and an independent review plus owner approval are recorded before merge.

## 9. Governance and ratification

This ADR is **Ratified**: the owner ratified it on 2026-10-05, selecting **D1=A** and **D2=B** (permanent PR #138 comment `5988274862`; the record is repeated in §11).

Ratification is a decision act, not an implementation authorization. Per ADR-0017 §44 this document does not pre-approve implementation merely by defining the decision. Two approval gates remain outside it:

- the **F-5 implementation slice** requires its own approved work item and acceptance criteria — `docs/work-items/F-5-runtime-dependency-enforcement.md`;
- the **D2=B change set** (§4) requires its own separate Level-C work item — `docs/work-items/D2B-library-reclassification.md`, which is **not approved**.

This ADR neither grants nor withdraws either approval. The current state of each is recorded in the work item that owns it, and that document — not this ADR — is authoritative for it.

Ratification does not relax §5 or §8. The F-5 implementation remains bounded by that scope and those acceptance criteria and must proceed through the repository's normal review and merge gates (ADR-0017 §31: implementation/verification → Quality Gate → independent review → owner approval → merge).

## 10. Normative references

- **ADR-0016** — deployment and operational ownership model; governs deployment/runtime lifecycle ownership and the one-component/one-runtime-element model.
- **ADR-0017 §44** — requires a separate approved work item and acceptance criteria for later slices and states that the ADR does not pre-approve later implementation.
- **ADR-0020 §4–§5** — governs the Platform Identity Surface, actual identity ownership, and the separation between owner-provided facts and D&O verification.
- **docs/ARCHITECTURE.md** — canonical architecture rules, including component boundaries, runtime ownership, and Level-C change classification.
- **Permanent PR #138 owner-gate record** — records the owner selection D1=A and D2=B and the prerequisite Level-C ADR gate for F-5.

## 11. Decision record

**Owner-selected decisions:** D1=A, D2=B.

**ADR state:** Ratified by the owner on 2026-10-05.

**Implementation state (recorded 2026-10-05):** the F-5 implementation slice exists at `30e570de23a7466d18a7e9e28caea26bd14b3c8f` and is carried, together with the separately owner-ratified `identity 0.4.0` cascade, in draft PR #150 — **HOLD / NOT MERGE**. Its merge remains subject to the ADR-0017 §31 chain, and the approval state of the F-5 work item is recorded in `docs/work-items/F-5-runtime-dependency-enforcement.md` §6, not here.
