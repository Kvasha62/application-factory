# ADR-0016 — Deployment and Operations Boundary

**Status:** `PROPOSED`

**Date:** 2026-09-16

**Level:** C — platform architecture (deployment model; `docs/ARCHITECTURE.md` §34.1)

**Initiated by:** post-Slice F boundary review (Issue #74, PR #75)

> This ADR fixes an architectural boundary. It implements nothing: no deployment code, no provisioning, no rollout automation, no pipeline and no infrastructure definition are authorized by this document. It selects no deployment technology. It deliberately changes nothing in `docs/ARCHITECTURE.md`: the controlled architecture update is deferred until after owner ratification, per §34.1 / §42.

## 1. Context

`docs/ARCHITECTURE.md` v1.2.0 RATIFIED defines the project lifecycle as the composition chain that terminates at the assembled platform (§37, «Главная модель всей системы»):

```text
COMPONENT CATALOG → Compatibility Matrix → GOLDEN BUNDLES → COMPOSER
→ PLATFORM MANIFEST → PLATFORM INSTANCE
```

ADR-0015 (RATIFIED 2026-09-14) activated Level 2 — Component Factory, and its implementation slices are now complete: Slice A — Component Registry, Slice B — Component Catalog, Slice C — Platform Manifest, Slice D — Golden Bundles, Slice E — Composer, Slice F — Platform Instance Assembly (Issue #74, PR #75, merged 2026-09-16). The project can therefore deterministically assemble a Platform Instance representation from an accepted, validated Platform Manifest. Slice F is explicitly a representation step only: no deployment, provisioning, rollout, approval or publication behavior was introduced by it.

The ratified law ends at `PLATFORM INSTANCE`. It defines how the authoritative description of a platform is produced, but it does not define how such an instance becomes a running, operated platform. The region between «the instance exists as a validated artifact» and «the platform is running and operated» is architecturally unallocated:

```text
Platform Instance → ??? → Running Platform
```

ADR-0015 already anticipated this at the documentation level: the ADR catalog entry records that deployment/provisioning/rollout execution on top of an assembled Platform Instance requires a separate approved work item. However, no ratified decision defines the boundary, ownership and rules of that stage.

Deployment model is a named Level C concern in `docs/ARCHITECTURE.md` §34.1:

```text
Level C — platform architecture. Меняет component boundaries, contracts,
data ownership, versioning, deployment model или factory behavior.
Требуется ADR.
```

Defining the Deployment and Operations boundary defines the platform's deployment model boundary at the architectural level; it therefore requires this ADR before any deployment capability may be implemented and before `docs/ARCHITECTURE.md` may be updated to record the stage.

## 2. Decision

**The Deployment and Operations boundary is established as the platform-level stage that follows Platform Instance assembly:**

```text
Platform Instance → Deployment & Operations → Running Platform
```

Architectural rules of the boundary:

1. **Single input artifact.** Deployment & Operations consumes exactly one authoritative input: a validated Platform Instance — the Slice F representation bound by identity, version and digest to its accepted Platform Manifest. It must not re-resolve component versions, substitute artifacts or mutate the instance. What is deployed is exactly what was assembled.
2. **Reproducibility extends to runtime.** LAW-09 (Reproducible Platform) extends through this boundary: a Running Platform must be reproducible from the same Platform Instance, which remains the complete and sufficient description of the intended composition.
3. **Fail-closed on invalid input.** An instance whose digest integrity, validation state or artifact pinning cannot be verified must be rejected deterministically. Deployment of an unverifiable or floating composition is prohibited: floating version selectors (`latest`) remain prohibited at this boundary as everywhere else (ADR-0015 §6).
4. **Platform-level ownership, no business data.** Deployment & Operations owns only the operational state of the transition itself (attempts, runtime status). It owns no business facts of Learning, Commerce, Booking or any other component and does not alter component data ownership or contract rules (ADR-0015 §11).
5. **Not a business system.** The boundary is factory/platform machinery. It creates no new SCS, no Class A business boundary and no new component.
6. **No technology selection.** This ADR defines the architectural stage only. Container orchestration, packaging/templating, infrastructure provisioning, configuration management and cloud-provider choices are time-varying technical details (§36: «конкретный deployment engine») and are deliberately not selected here.

Level classification: this decision is **Level C — platform architecture** per `docs/ARCHITECTURE.md` §34.1, because it defines the platform's deployment model boundary. It is not Level D: it changes no fundamental rule, no component boundary, no contract, no data ownership, no versioning law and no existing factory behavior.

## 3. Semantic Gap — Platform Instance

The semantic gap recorded by this ADR is the missing architectural region:

```text
Platform Instance → Deployment & Operations → Running Platform
```

Evidence of the gap:

- `docs/ARCHITECTURE.md` §37 main model terminates at `PLATFORM INSTANCE`; nothing in the ratified law follows it.
- LAW-09 requires every Platform Instance to be reproducible from the Manifest, but no law states how an instance reaches runtime or what guarantees hold on that path.
- ADR-0015 §15 enumerates implementation slices A–E up to Composer; Slice F — Platform Instance Assembly was approved subsequently (Issue #74) and recorded in the ADR catalog. The completed chain still stops at the assembled instance.
- Slice F (PR #75) delivers the instance *representation* and explicitly excludes deployment, provisioning, rollout, approval and publication behavior.

Consequence of the gap: before this ADR, any deployment work would start from an undefined boundary — no owner, no contract, no input guarantee, no rejection rule — and would risk re-resolving or mutating what assembly had already fixed. This ADR names the region and fixes its architectural rules, while leaving its implementation and its reflection in `docs/ARCHITECTURE.md` to the post-ratification steps defined in §7 (Migration).

## 4. Explicit Non-Goals

This ADR does **not** authorize:

- any deployment implementation: no deployment, provisioning or rollout code, no pipeline, no infrastructure definition;
- a «Slice G» or any new implementation slice — implementation slices require separate approved work items after ratification, per ADR-0015 §15 and the ADR catalog note;
- a new SCS / business system / Class A component;
- selection of concrete deployment technologies — no Kubernetes, Helm, Terraform, Ansible, cloud-provider or CI/CD product selection is made;
- floating version selectors (`latest`) anywhere in the deployment path;
- any modification of `docs/ARCHITECTURE.md` — the architectural law remains v1.2.0 RATIFIED and is deliberately unchanged by this ADR; the controlled update is a separate step after ratification (§7);
- changes to Composer, Registry, Catalog, Platform Manifest, Golden Bundle or Platform Instance semantics;
- changes to Factory behavior as defined by ADR-0015;
- customer-specific forks or per-customer deployment divergence (LAW-11);
- microservice extraction or one deployment unit per component merely for architectural symmetry (ADR-0015 §13).

## 5. Consequences

### Positive

- The architectural region between an assembled instance and a running platform has a name, an owner and rules before any code exists (LAW-14: architecture changes through ADR).
- The Platform Instance becomes a closed hand-off artifact: runtime work cannot silently change composition.
- Future implementation work inherits a fixed boundary and does not need to re-litigate architecture.

### Constraints

- Level C status gates all subsequent work: nothing may be implemented until ratification and a separate approved work item exist.
- `docs/ARCHITECTURE.md` remains formally silent about the stage until the post-ratification controlled update; documentation must not describe the stage as implemented before then.
- Technology decisions are deferred: each later concrete choice of architectural significance requires its own record (Level B or C as applicable); time-varying tools remain replaceable implementation details (§36).

## 6. Alternatives Considered

### Treat deployment as a mere implementation detail (no ADR)

Rejected. Deployment model is explicitly named in §34.1 as a Level C concern requiring an ADR. Starting deployment work without a ratified boundary would repeat the kind of unratified-architecture drift that ADR-0010 eliminated.

### Extend Composer or Platform Instance assembly with deployment behavior

Rejected. Assembly must remain a pure, deterministic representation step (the Slice F boundary). Merging deployment into it would violate its defined scope, blur ownership and compromise reproducibility semantics (LAW-09).

### Update `docs/ARCHITECTURE.md` §37 directly alongside this ADR

Rejected — deferred, not forbidden. The architectural law is RATIFIED and changes only after the governing ADR is ratified (§42: Level C/D changes require ADR; ratified architecture is not edited directly). This ADR therefore deliberately leaves `docs/ARCHITECTURE.md` unchanged and schedules the controlled update inside the Migration path, after owner ratification.

### Select a concrete deployment stack now

Rejected. §36 classifies the concrete deployment engine as a time-varying detail; binding the architectural law to a current tool would invert «stable laws first, temporary details later». No technology is chosen by this ADR.

## 7. Migration

This ADR migrates nothing: it changes no code, no schema, no contract and no document other than itself. (The accompanying one-line correction in `docs/adr/README.md` records the already-merged state of PR #75 under the documentation-hygiene rule; it is not part of this decision.)

Adoption path:

```text
ADR-0016 PROPOSED (this document)
    → architectural review
    → owner ratification
    → controlled `docs/ARCHITECTURE.md` update recording the stage
      («Platform Instance → Deployment & Operations → Running Platform»
      in the §37 model, with the §34.1 Level C cross-reference)
    → separate approved implementation work item(s) for the
      Deployment & Operations capability — a future slice,
      not «Slice G» authorized by this ADR
```

The architecture update happens only after ratification, as a separate controlled change, and must not precede it.

## 8. Status

- **Current status: `PROPOSED`** (2026-09-16).
- While this ADR is `PROPOSED`, the boundary defined here has no implementation and no reflection in `docs/ARCHITECTURE.md`; the ratified description of the platform lifecycle remains the one that ends at Platform Instance.
- Transition to `RATIFIED` requires completed architectural review and explicit project-owner approval, per the workflow recorded in ADR-0013–ADR-0015.
- Ratification of this ADR does not by itself authorize implementation: it unlocks the controlled architecture update and the subsequent approved work item(s).

## 9. Ratification

Pending. Ratification is the exclusive act of the project owner after architectural review, per the project's established workflow (`Issue → ADR branch → PR → independent review → corrections → owner approval → merge → post-merge verification`).

## 10. Relationship to Other Documents

- `docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains the sole architectural law and is unchanged by this ADR. LAW-01…LAW-16 — including LAW-09 (Reproducible Platform), LAW-13 (Complexity Must Be Earned) and LAW-14 (Architecture Changes Through ADR) — remain in force.
- ADR-0015 remains the activation decision for Level 2 and the source of the implementation-slice discipline; this ADR is consistent with its rule that deployment/provisioning/rollout execution requires a separate approved work item.
- `docs/adr/README.md` remains the catalog of confirmed ADRs; a `PROPOSED` ADR is not a confirmed decision and enters the catalog's confirmed-state list only after ratification.
- ADR-0010–ADR-0014 are not modified by this ADR.

## 11. Final Decision

> **The Deployment and Operations boundary is established as the architectural stage between an assembled Platform Instance and a Running Platform. The Platform Instance is its single, immutable, sufficient input; deployment of unverifiable or floating compositions is prohibited; the stage owns no business data and creates no business system. Nothing is implemented and no deployment technology is selected. `docs/ARCHITECTURE.md` is deliberately unchanged: the controlled update follows only after owner ratification.**

**ADR-0016 status: PROPOSED.**
