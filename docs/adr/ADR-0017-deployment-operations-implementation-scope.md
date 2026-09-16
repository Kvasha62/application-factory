# ADR-0017 — Deployment & Operations Implementation Scope

**Status:** `RATIFIED`

**Date:** 2026-09-16

**Ratified:** 2026-09-16 by project owner

**Level:** C — platform architecture (deployment model; implementation scope of the decision ratified in ADR-0016; `docs/ARCHITECTURE.md` §34.1). This document changes no architecture.

**Depends on:** `ADR-0016 — Deployment and Operations Boundary` (`RATIFIED` 2026-09-16)

**Initiated by:** continuation of ADR-0016 after owner ratification and the controlled `docs/ARCHITECTURE.md` update (PR #77, merged 2026-09-16)

**Authorizes:** nothing — no implementation, no work item, no slice, no technology choice

> This ADR fixes the scope of the future implementation of the Deployment & Operations capability: the full Capability Scope (§4), the first minimal vertical implementation slice (§42), the status lifecycle governing this document (§22), the vocabulary separating `ready`, `deployed` and `realized` (§33–§35), and the minimal initial deployment path (§36). It implements nothing: no code, no infrastructure, no pipeline, no work item and no slice is created or authorized by it. It selects no deployment technology. It changes no other document — not `docs/ARCHITECTURE.md`, not ADR-0016, not the ADR catalog, not the documentation baseline.

## 1. Context

ADR-0016 — Deployment and Operations Boundary was ratified by the project owner on 2026-09-16. The controlled `docs/ARCHITECTURE.md` update required by ADR-0016 §29 was then executed as a separate documentation change and merged through PR #77 (merge commit `58036d138ff082f1e649c537965b7f23e9c4627d`, `docs/ARCHITECTURE.md` §2.2, §37, §37.1; the document remains v1.2.0 RATIFIED per the Level C convention of ADR-0011). The architectural boundary is therefore law:

```text
Factory → Platform Manifest → Platform Instance → Deployment & Operations → Running Platform
```

The capability itself does not exist yet. `docs/DOCUMENTATION_BASELINE.md` records deployment/provisioning/rollout execution as `NOT YET IMPLEMENTED`, and ADR-0016 §30 gates any implementation on two conditions: this boundary ADR being ratified, and a separate approved implementation work item existing.

Before such a work item can be responsibly approved, the scope of the future implementation must be fixed: what the capability covers in full, what the first implementation slice covers, which lifecycle and vocabulary rules govern the work, and what remains explicitly out of scope. That is the subject of this ADR.

This ADR is documentation work only. It is `PROPOSED`. While it is `PROPOSED`, no implementation under it is permitted.

## 2. Decision

The future implementation of Deployment & Operations — if and when it becomes authorized through the governance chain of §19 — is bound to the scope fixed by this ADR:

- the full Capability Scope is §4;
- implementation starts from the Initial Implementation Slice of §42, not from the whole capability;
- the lifecycle of this ADR and of implementation authorization is §22–§27;
- the vocabulary `ready` / `deployed` / `realized` is used exactly as fixed in §33–§35;
- the first deployment flow follows the minimal initial deployment path of §36;
- the scope remains technology-neutral (§40) and changes nothing architectural (§41);
- the first slice is accepted only against the observable-behavior acceptance criteria of §43.

This ADR authorizes no implementation. Implementation authorization arises only from the governance chain of §19, never from this document alone.

## 3. Purpose and Scope of This ADR

This ADR:

- fixes the boundaries and volume of the future implementation of Deployment & Operations;
- separates the full capability scope from the first implementation slice;
- fixes the status lifecycle and the implementation-authorization condition;
- removes ambiguity between `ready`, `deployed` and `realized`;
- fixes the minimal initial deployment path;
- states acceptance criteria for the first slice at the level of observable behavior.

This ADR does **not**:

- implement anything;
- create Deployment & Operations, a slice, a work item, an Issue or a PR;
- select or recommend any concrete technology;
- change the architecture, ADR-0016, the Architecture Baseline or the ADR catalog.

The only artifact of this ADR is the text of this file.

## 4. Capability Scope

The Deployment & Operations capability, as established by ADR-0016 §4, has the following full scope. This is the complete scope of what the capability must eventually provide — not an implementation schedule:

- **provisioning** — preparation of the runtime environment required by the Platform Instance (ADR-0016 §11);
- **deployment** — realization of the instance as a running platform (ADR-0016 §10);
- **migration orchestration** — coordinated execution of component database migrations during deployment and upgrade (ADR-0016 §14);
- **runtime management** — operational control of the running platform (ADR-0016 §18);
- **health/readiness** — establishment and evaluation of health and readiness (ADR-0016 §18, §19);
- **deployment state** — the authoritative operational record of actual state (ADR-0016 §9);
- **upgrade** — deliberate transition to a new desired state (ADR-0016 §15);
- **rollback** — return to a previously known-good desired state (ADR-0016 §16);
- **operational observability** — signals about deployment and runtime health (ADR-0016 §19);
- the cross-cutting operational rules already defined by ADR-0016: the Immutable Manifest Principle (§7), platform instance identity and digest verification (§8), secrets handling (§12), configuration path (§13), tenant provisioning context (§17) and fail-closed failure semantics (§20).

The nine responsibilities above are exactly the responsibilities established by ADR-0016 §4 — no more and no fewer. The cross-cutting rules listed after them are binding rules inherited from ADR-0016, not additional responsibilities. ADR-0016 §4 remains authoritative for the capability scope, and this ADR adds no new responsibility (§14 records what is explicitly outside this scope). Each responsibility belongs to the capability regardless of when its implementation is taken up. Which parts the first implementation slice covers is fixed in §42; every later part requires its own slice and its own acceptance criteria (§29–§30, §44).

## 5. Capability Scope Mirrors ADR-0016

| Responsibility | Defined in ADR-0016 | Position in this ADR |
|---|---|---|
| Provisioning | §11 | Capability scope; participates in the first slice (§42) |
| Deployment | §10 | Capability scope; participates in the first slice (§42) |
| Migration orchestration | §14 | Capability scope; bounded treatment in §10 |
| Runtime management | §18 | Capability scope; bounded treatment in §11 |
| Health/readiness | §18–§19 | Capability scope; participates in the first slice (§42) |
| Deployment state | §9 | Capability scope; participates in the first slice (§42) |
| Upgrade | §15 | Capability scope; deferred (§39) |
| Rollback | §16 | Capability scope; deferred (§39) |
| Operational observability | §19 | Capability scope; minimal surface in the first slice (§15) |

The table mirrors exactly the responsibility list of ADR-0016 §4; it adds no row that ADR-0016 does not establish (§14).

## 6. Provisioning — Implementation Scope

Provisioning is in the capability scope in full (ADR-0016 §11): it derives requirements only from the instance and its declared configuration path, is fail-closed, and is technology-neutral. The first implementation slice includes the minimum of environment preparation required to realize one accepted Platform Instance end to end (§42). Generalized, repeatable provisioning across many environments and instances is a later-stage concern with its own acceptance criteria (§44).

## 7. Deployment — Implementation Scope

Deployment is in the capability scope in full (ADR-0016 §10): realization of the exact, immutable instance, with digest-bound verification and without composition mutation. The first implementation slice realizes one accepted Platform Instance (§42). Upgrade-time and rollback-time realization are separate stages (§12–§13).

## 8. Deployment State — Implementation Scope

Deployment state is in the capability scope in full (ADR-0016 §9): it is the authoritative operational record of actual state, honest by requirement, referenceable by instance identity, version and digest. The first implementation slice must already maintain deployment state for its single deployment operation: progression, honest failure recording and the `realized`/`deployed` claims only after verification (§43). The full attempt history and reconciliation records accumulate across later stages.

## 9. Health/Readiness — Implementation Scope

Health/readiness is in the capability scope in full (ADR-0016 §18, §19): establishment and evaluation of health and readiness of platforms under management. The first implementation slice includes exactly the health/readiness checks needed to establish the `ready` condition for the deployed instance (§33, §42). Without health/readiness, no `ready` condition exists and no `deployed` claim may stand (ADR-0016 §10).

## 10. Migration Orchestration — Implementation Scope

Migration orchestration is in the capability scope in full (ADR-0016 §14): Deployment & Operations orchestrates execution of component-owned migrations during deployment and upgrade, while migration content remains owned by the components (LAW-03) and schema develops only forward (LAW-08).

Its participation in the first implementation slice is fixed by this ADR, not by the approved work item:

- the first slice includes the orchestration of exactly those component-defined migrations, if any, that the accepted Platform Instance requires as part of its deployment — executed forward-only, in the order and conditions the component contracts allow, with a failed migration as a fail-closed state (ADR-0016 §14, §20; LAW-08);
- nothing beyond that is included: upgrade-time migration orchestration and any broader orchestration surface are deferred (§39, §44);
- within this fixed scope the approved work item defines only concrete engineering artifacts, tests and operational form; it must not expand or reduce the scope fixed here and in §42–§43 — an implementation work item executes the fixed scope, it does not re-select it (§28).

Deleting or rewriting production schema to «fix» a deployment remains prohibited (LAW-08; ADR-0016 §14).

## 11. Runtime Management — Implementation Scope

Runtime management is in the capability scope in full (ADR-0016 §18): operational control of the Running Platform, with every operational action reflected in deployment state and observable signals. The first slice includes only the minimal operational control needed to start the platform and evaluate its health/readiness within one deployment operation (§36–§37). Ongoing runtime management — restarts, stop/start policy, drift surfacing — is a later stage (§39).

## 12. Upgrade — Implementation Scope

Upgrade is in the capability scope in full (ADR-0016 §15): deliberate transition of a Running Platform to a new Platform Instance with new identity/version/digest, pinned input, migration orchestration, recorded and observable. Upgrade is **not** part of the first implementation slice; it is introduced at a later implementation stage with its own acceptance criteria (§39, §44).

## 13. Rollback — Implementation Scope

Rollback is in the capability scope in full (ADR-0016 §16): return to a previously known-good Platform Instance identified by digest, respecting Forward Migration (LAW-08), explicit and recorded. Rollback is **not** part of the first implementation slice; it is introduced at a later implementation stage with its own acceptance criteria (§39, §44).

## 14. Retirement Is Not Part of This Scope

Retirement is **not** part of the Capability Scope fixed by this ADR (§4). The authoritative responsibility list of ADR-0016 §4 does not establish retirement as a responsibility of Deployment & Operations, and this ADR does not add it:

- ADR-0017 does not establish retirement as part of the Capability Scope;
- ADR-0017 does not extend, amend or supplement ADR-0016;
- no requirements and no acceptance criteria for retirement are set by this ADR;
- any future decision about retirement — including whether it belongs to this capability at all — requires separate architectural consideration under the project's acting rules (LAW-14; `docs/ARCHITECTURE.md` §34, §34.1).

This section exists to record the exclusion, not to create scope.

## 15. Operational Observability — Implementation Scope

Operational observability is in the capability scope in full (ADR-0016 §19): deployment/upgrade/rollback events, health/readiness status, reconciliation results, all correlatable with the Platform Instance digest. The first implementation slice provides the minimal observability surface: the operational state of the deployment operation and the health/readiness status of the instance, correlatable with the instance digest (§43). The full event and reconciliation surface is a later stage (§39). No observability stack is selected (§40).

## 16. Cross-Cutting Operational Rules — Implementation Scope

The following ADR-0016 rules bind **every** implementation stage, including the first slice; no stage may relax them:

- fail-closed failure semantics and honest deployment state (ADR-0016 §20, §9);
- the Immutable Manifest Principle — no mutation of the published Manifest (ADR-0016 §7; `docs/ARCHITECTURE.md` §37.1);
- platform instance identity and digest verification before any action (ADR-0016 §8);
- secrets are operational inputs injected at the boundary, never Manifest/Instance content, never copied into records, logs or signals (ADR-0016 §12);
- environment-specific configuration enters through the existing configuration contract, never through composition changes (ADR-0016 §13);
- tenant-scoped runtime resources exist only within a lawful tenant context; Deployment & Operations never owns tenant data or lifecycle (ADR-0016 §17);
- no floating version selectors anywhere in the deployment path (ADR-0016 §7; `docs/ARCHITECTURE.md` §1.3).

When implementation is approved, these rules must be verified by test (ADR-0016 §30).

## 17. Capability Scope ≠ First Implementation Slice

This ADR fixes the distinction explicitly:

```text
Capability Scope ≠ First Implementation Slice
```

- §4 (Capability Scope) = what the Deployment & Operations capability must eventually provide, in full;
- §42 (Initial Implementation Slice) = the minimal vertical slice from which implementation begins.

The first slice does not shrink the capability scope: everything in §4 remains in scope for later stages. The capability scope does not promise the first slice's content: nothing beyond §42–§43 is scheduled, approved or pre-authorized. Further slices each require their own acceptance criteria (§30, §44).

## 18. ADR-0016 Is the Authoritative Architectural Decision

> ADR-0016 is the authoritative architectural decision for the Deployment & Operations boundary.
>
> ADR-0017 does not replace, amend, or supersede ADR-0016.

ADR-0017 refers to the boundary, ownership, prohibitions and terminology established by ADR-0016 and by ratified `docs/ARCHITECTURE.md` (§2.2, §37, §37.1); it does not redefine them. If any statement of this ADR conflicts with ADR-0016 or with ratified `docs/ARCHITECTURE.md`, ADR-0016 and ratified `docs/ARCHITECTURE.md` prevail (source priority per `docs/GIT_OPERATING_PROTOCOL.md` §13).

## 19. Governance Chain

The sequence governing this capability is fixed as follows:

```text
ADR-0016 RATIFIED
→ controlled ARCHITECTURE.md update
→ ADR-0017 approval/ratification
→ separate approved implementation work item
→ implementation
```

The roles in this chain are distinct and must not be conflated:

- **ADR-0016** defines the architectural boundary and the decision;
- **ADR-0017** (this document) defines the scope of the future implementation;
- the **separate approved implementation work item** grants the actual authorization to implement;
- **ADR-0017 does not automatically authorize implementation** — neither its proposal, nor its ratification, nor both together substitute for an approved implementation work item.

After the merge of PR #77, the statement that ADR-0016 is `RATIFIED` is a fact of the repository, not an expectation (§20).

## 20. Recorded Repository Facts

Verified against `origin/main` at the time of writing this document:

- PR #77 («docs: ratify ADR-0016 and apply controlled ARCHITECTURE.md update (#77)») is merged; its merge commit on `main` is `58036d138ff082f1e649c537965b7f23e9c4627d` (2026-09-16);
- ADR-0016 carries `**Status:** RATIFIED` and `**Ratified:** 2026-09-16 by project owner`, and its §33 records the post-ratification state;
- the controlled `docs/ARCHITECTURE.md` update required by ADR-0016 §29 has been executed: §2.2 fixes Platform Instance as desired state / deployment input, §37 extends the main model with `PLATFORM INSTANCE → DEPLOYMENT & OPERATIONS → RUNNING PLATFORM`, and §37.1 records the boundary; the document version remains v1.2.0 RATIFIED (Level C convention per ADR-0011);
- ADR-0016 is recorded in the confirmed list of `docs/adr/README.md`;
- Deployment & Operations itself is not implemented: `docs/DOCUMENTATION_BASELINE.md` records deployment/provisioning/rollout execution as `NOT YET IMPLEMENTED`.

## 21. Ratification Does Not Authorize Implementation

Ratification of this ADR — when and if it happens — confirms the implementation scope as written. It does not create a work item, does not approve a slice, and does not authorize any implementation. Implementation remains prohibited until the implementation-authorization condition of §25 holds. No part of this document may be read as granting implementation permission.

## 22. Status Lifecycle

The lifecycle governing this ADR and its implementation is explicit:

```text
PROPOSED
    ↓
RATIFIED
    ↓
IMPLEMENTATION AUTHORIZED
    ↓
IMPLEMENTED
```

Before using these names, the existing governance conventions of the project were checked (`docs/ARCHITECTURE.md` §34, §40–§41; `docs/OPERATING_MODEL.md` §5.1; `docs/adr/README.md`; ADR-0010…ADR-0016). The result:

| Name | Category | Basis in existing conventions |
|---|---|---|
| `PROPOSED` | formal ADR status | established (all ADRs enter as PROPOSED; confirmed in `docs/adr/README.md` and ADR-0016 §31) |
| `RATIFIED` | formal ADR status | established (ratification is the exclusive act of the project owner; ADR-0015 §20, ADR-0016 §33) |
| `IMPLEMENTATION AUTHORIZED` | governance condition, **not** a formal ADR status | no such ADR status exists in the project; the underlying rule already exists: implementation requires a separate approved work item (ADR-0015 §15; ADR-0016 §30) |
| `IMPLEMENTED` | implementation outcome record, **not** an ADR status | established for work items and slices (`docs/OPERATING_MODEL.md` §5.1; `docs/DOCUMENTATION_BASELINE.md` Slices A–F), never as an ADR status |

This ADR introduces **no new formal ADR status**. The document itself carries exactly one formal status at a time — currently `RATIFIED`. `IMPLEMENTATION AUTHORIZED` and `IMPLEMENTED` are used here only with the meanings fixed in §25–§26.

## 23. PROPOSED

The document is under consideration.

Implementation under this ADR is prohibited. No work item, slice, code or infrastructure may derive from a `PROPOSED` ADR.

## 24. RATIFIED

The project owner has confirmed the architectural scope of this ADR.

Ratification does **not** automatically mean that implementation is permitted. It advances the governance chain of §19 by one step and only by one step: the separate approved implementation work item remains a mandatory, separate gate (§25, §28).

## 25. Implementation Authorization — a Governance Condition, Not a Formal Status

Implementation becomes permitted only when the following governance condition holds:

> A separate approved implementation work item exists, approved according to the project's rules (ADR-0015 §15; ADR-0016 §30; `docs/adr/README.md` — confirmed state for ADR-0016; `docs/OPERATING_MODEL.md` §6).

This condition is named `IMPLEMENTATION AUTHORIZED` in §22 purely for readability. It is not a formal ADR status: project conventions do not define such a status, and this ADR does not invent one. The condition is evidenced by the approved work item in GitHub (`docs/OPERATING_MODEL.md` §2), not by a status change of this document. Until the condition holds, implementation remains prohibited even if this ADR is `RATIFIED`.

## 26. IMPLEMENTED — an Implementation Outcome Record

The `IMPLEMENTED` outcome is reachable only after actual implementation and after passing all of the following, per the project's existing slice discipline (ADR-0015 §15; `docs/OPERATING_MODEL.md` §5.1, §12; `docs/GIT_OPERATING_PROTOCOL.md` §2):

- tests and verification;
- the Quality Gate;
- independent review;
- owner approval;
- merge into `main`.

`IMPLEMENTED` is recorded the way Slices A–F were recorded — in `docs/DOCUMENTATION_BASELINE.md` — not as a status of this ADR. Precedent: ADR-0015 remained `RATIFIED` while its slices were recorded `IMPLEMENTED`. A deployment capability is not claimed to exist merely because this ADR is ratified: status-and-evidence discipline applies (`docs/ARCHITECTURE.md` §40).

## 27. SUPERSEDED and RETIRED Are Not Established ADR Statuses

The project's governance conventions were checked: `SUPERSEDED` and `RETIRED` are not established ADR statuses. The word «superseded» appears in `docs/ARCHITECTURE.md` §41 only as a revision label for the historical document version 1.1.0, not as an ADR status.

This ADR therefore introduces neither status. If ADR-0017 must ever be replaced or withdrawn, that happens through the established ADR process (LAW-14; `docs/ARCHITECTURE.md` §34) with a record in `docs/adr/README.md`, not through a newly invented status.

## 28. Implementation Work Item Rule

Implementation of Deployment & Operations may begin only under a separate approved implementation work item. The work item:

- follows the Arena assignment standard (`docs/OPERATING_MODEL.md` §6): goal, context, sources of truth, concrete task, constraints, readiness criteria, expected result;
- cannot redefine the architectural boundary established by ADR-0016 or the scope fixed by this ADR — an implementation work item executes, it does not re-legislate (precedent: ADR-0014 on Issue #57);
- is approved by the project owner; approval is recorded in GitHub (`docs/OPERATING_MODEL.md` §2, §13).

## 29. Implementation Slices Rule

Implementation proceeds through separate, small, reviewable slices (ADR-0015 §15). This ADR creates no slice — no «Slice G» and no other (ADR-0016 §27). Slice names and boundaries arise only inside approved work items. The first slice is fixed in scope by §42–§43; every further slice requires its own approved work item (§30, §44).

## 30. Per-Slice Acceptance Criteria Rule

The first implementation slice has the acceptance criteria fixed by §43 of this ADR. The approved work item translates those criteria into concrete deliverables and test suites without expanding, reducing or redefining them. Every later implementation slice has its own acceptance criteria, defined inside its approved work item. Acceptance criteria are stated at the level of observable behavior and capability, and are verified by tests that check architectural rules, not merely successful construction (ADR-0015 §16; ADR-0016 §30). No slice is accepted on narrative claims; the chain of §31 applies to each.

## 31. Verification, Quality Gate, Independent Review, Owner Approval, Merge

Every implementation slice passes, in order:

```text
tests / verification → Quality Gate → independent review → owner approval → merge
```

The Quality Gate is the project's existing gate (`scripts/quality-gate.ps1`, `.github/workflows/quality-gate.yml`; `docs/DOCUMENTATION_BASELINE.md` §2). Independent review and owner approval follow `docs/OPERATING_MODEL.md` §5.1, §12; the final merge into `main` is performed by the project owner (`docs/GIT_OPERATING_PROTOCOL.md` §1.1). A task is complete only after merge (`docs/OPERATING_MODEL.md` §12).

## 32. Desired State and Actual State — Inherited, Not Redefined

The distinction introduced by ADR-0016 §5 and recorded in `docs/ARCHITECTURE.md` §37.1 is inherited unchanged:

```text
Platform Instance = desired state — a complete, fixed description prepared by the Factory
Running Platform  = actual state  — what is actually deployed, running and observable
```

Nothing in this ADR redefines desired state or actual state. All scope statements here preserve the distinction: the capability reconciles actual state to desired state; desired state is never edited at runtime.

## 33. `ready` — a Verified Operational Condition

This ADR fixes the meaning of `ready`:

```text
ready = verified operational condition
```

`ready` means that the Running Platform has passed the health/readiness checks provided for it (ADR-0016 §18, §19) and has reached a verified operational condition. `ready` is a condition **of the Running Platform**, established by verification — merely starting artifacts, processes or containers is not `ready`, exactly as merely starting them is not `deployed` (ADR-0016 §10).

## 34. `ready` ≠ `deployed` and `ready` ≠ `realized`

The project's governance terminology distinguishes these notions, and this ADR keeps them distinct:

- `ready` ≠ `deployed`. `deployed` has the exact meaning of ADR-0016 §10: the actual, observable Running Platform is verified to correspond to the desired state of a specific Platform Instance referenced by identity, version and digest, and health/readiness must hold for the claim to stand. `ready` alone — without digest-bound verification against a specific instance — does not make a platform `deployed`; and no platform may be claimed `deployed` while it is not `ready`.
- `ready` ≠ `realized`. `realized` is a lifecycle position of the deployment state record (ADR-0016 §9): it is what the deployment operation's record states. Deployment state must never report `realized` for something that was not verified (ADR-0016 §9, §20). `ready` is the operational condition of the platform itself, not a record.

Link to ADR-0016: `ready` is a **necessary** verified operational condition for a `deployed` / `realized` claim, **but it is not sufficient by itself**. In the terminology already established by ADR-0016 (§9–§10), an honest claim requires both dimensions together:

```text
health/readiness holding (ready)
+
identity/version/digest verification against a specific Platform Instance
→
honest deployed / realized claim
```

Without `ready`, no such claim is honest; and `ready` alone, without digest-bound verification, fixes nothing in deployment state. Neither dimension may be dropped.

## 35. Operational Condition vs Lifecycle State vs Deployment Record

Three different notions must never be mixed:

- **operational condition** — how the Running Platform is: `ready` (§33);
- **lifecycle state** — the position of the deployment operation in deployment state: `in progress`, `realized`, `failed`, `superseded`, `rolled back` (ADR-0016 §9);
- **deployment record** — deployment state itself: the authoritative operational record of actual state and the only authoritative answer to «what is actually running where» (ADR-0016 §9).

The initial deployment path (§36) describes the progression of one deployment operation; `ready` terminates that progression as a condition; `realized` and `deployed` are recorded afterwards, on that basis, in the deployment record.

## 36. Initial Deployment Path

For the first minimal deployment flow, this ADR fixes:

```text
requested
→ validated
→ provisioning
→ deploying
→ starting
→ health_check
→ ready
```

This path is fixed with the following exact meaning:

- it is a **minimal observable deployment sequence** for the first implementation slice;
- it describes the observable progression of **one deployment operation** for one Platform Instance;
- it is **not** the full deployment lifecycle and does not replace or redefine the deployment lifecycle of ADR-0016 (deployment state positions per §9; upgrade per §15; rollback per §16);
- it is **not** a new global or architectural state machine: nothing outside the first implementation slice is bound by it;
- it does **not** require the internal implementation to have exactly these discrete technical states or objects — the internal form of progression is chosen by the approved work item; what this ADR fixes is the observable behavior.

## 37. Meaning of the Path States

- **requested** — a deployment operation is requested for a specific Platform Instance identified by identity, version and digest; there is no «deploy the platform called X» — only «deploy this exact instance» (ADR-0016 §8).
- **validated** — the instance's digest integrity and validity are verified; an unverifiable or name-only reference is rejected fail-closed (ADR-0016 §8, §20).
- **provisioning** — the runtime environment is prepared from the instance's requirements only; fail-closed; technology-neutral (ADR-0016 §11).
- **deploying** — the instance is being realized as a running platform (ADR-0016 §10).
- **starting** — runtime elements of the platform are started (ADR-0016 §18).
- **health_check** — health/readiness are established and evaluated (ADR-0016 §18, §19).
- **ready** — the verified operational condition is reached (§33).

Relation to ADR-0016 §9: while the operation moves along this path, deployment state is `in progress`. Upon `ready` together with digest-bound verification, the deployment state may honestly be fixed as `realized` and the platform claimed `deployed` per ADR-0016 §10. A failure at any step is fail-closed and leaves deployment state honest — never a false `deployed` (ADR-0016 §20).

The names above label observable stages of the operation for the purpose of this scope; they do not prescribe internal implementation objects, classes or states (§36).

## 38. The Initial Path Is Minimal — Not the Full Lifecycle

The path of §36 covers one deployment operation for one Platform Instance. It deliberately does not cover upgrade, rollback, `superseded` handling, reconciliation of drift, or multi-instance coordination. None of these is removed from the Capability Scope (§4): they are deferred implementation stages (§39).

## 39. Deferred States and Scenarios

The following states and scenarios, all defined by ADR-0016, are introduced at later implementation stages, each under its own acceptance criteria (§30, §44):

- `superseded` (ADR-0016 §9);
- rollback (ADR-0016 §16);
- upgrade (ADR-0016 §15).

Retirement is not a deferred stage of this scope — it is outside the Capability Scope entirely (§14).

Nothing in this ADR schedules, approves or pre-authorizes any stage.

## 40. Technology Neutrality

This ADR is technology-neutral. It describes capabilities and contracts, not implementation technologies. It does not select and does not recommend:

- Kubernetes;
- Docker;
- Docker Compose;
- Helm;
- Terraform;
- Ansible;
- any concrete cloud provider;
- any concrete broker;
- any concrete orchestrator;
- any concrete CI/CD system;
- any concrete observability stack;
- any other concrete deployment engine or tool.

This continues ADR-0016 §23 and `docs/ARCHITECTURE.md` §36: concrete deployment engines are time-varying technical details. Any future concrete choice that rises to architectural significance requires its own record (Level B or Level C per `docs/ARCHITECTURE.md` §34.1) and must honor this boundary.

## 41. Architectural Non-Changes

This ADR does not change the architecture. It is a Level C governance/documentation decision that fixes the implementation scope of an already ratified architectural decision (ADR-0016); it introduces no architectural change. It does not change and does not redefine:

- ownership — component data ownership remains with the components (LAW-03; ADR-0016 §6);
- the Factory boundary — the Factory ends at the deterministic Platform Instance (ADR-0016 §3, §21);
- the Platform Manifest — immutable input, changed only through the Factory (ADR-0016 §7; `docs/ARCHITECTURE.md` §16);
- Platform Instance semantics — desired state / deployment input (`docs/ARCHITECTURE.md` §2.2);
- Running Platform semantics — actual state produced by Deployment & Operations (`docs/ARCHITECTURE.md` §37.1);
- Tenant Authority — owner of tenant lifecycle (ADR-0016 §17; `docs/ARCHITECTURE.md` §2.3);
- versioning rules — everything that matters is versioned and pinned (LAW-07; `docs/ARCHITECTURE.md` §11);
- the floating-selector prohibition — `latest` and equivalents remain prohibited at this boundary as everywhere else (`docs/ARCHITECTURE.md` §1.3; ADR-0016 §7);
- the creation of any new SCS / Business System / component (ADR-0016 §22, §27).

All of these are already defined by ratified `docs/ARCHITECTURE.md` and ADR-0016. This ADR refers to them; it does not redefine them.

## 42. Initial Implementation Slice

This section fixes only the **first minimal vertical slice** of the Deployment & Operations implementation. It is not a reduction of the Capability Scope (§4, §17).

The first slice is one end-to-end deployment operation for one accepted Platform Instance, traversing the capability vertically at the minimum needed to prove the boundary of ADR-0016 in action:

- it follows the initial deployment path of §36: `requested → validated → provisioning → deploying → starting → health_check → ready`;
- it exercises, at minimum: instance validation (§16), provisioning (§6), deployment (§7), migration orchestration bounded exactly per §10, the minimal runtime control to start the platform (§11), health/readiness checks establishing `ready` (§9, §33), deployment state recording (§8) and the minimal observability surface (§15);
- it is bounded by the cross-cutting rules of §16 without relaxation;
- it is accepted only against the acceptance criteria of §43.

The slice is described in capabilities and observable behavior, not in technologies: no implementation stack is selected by it (§40). The approved implementation work item fixes concrete artifacts, tests and engineering form within these bounds (§28).

This section creates no slice named «Slice G» and no slice under any other name; slice naming belongs to the approved work item (ADR-0016 §27).

## 43. Acceptance Criteria — First Vertical Slice

The first vertical slice is accepted when all of the following hold, stated at the level of observable behavior and capability:

1. a concrete Platform Instance is accepted as deployment input, identified by identity, version and digest;
2. the instance's validity is verified — digest integrity checked; an unverifiable or name-only input is rejected fail-closed;
3. a deployment operation is created and its progression is recorded;
4. provisioning passes — the runtime environment is prepared per the instance's requirements;
5. deployment execution proceeds — the Platform Instance is brought through the deployment realization process toward its intended runtime state; neither deployment completion nor any `realized` / `deployed` claim is asserted at this point (such a claim is fixed only under criterion 10, after verification; §34–§35);
6. the component-defined migrations required by the accepted instance, if any, are executed under migration orchestration: forward-only, in the order and conditions the component contracts allow; a failed migration is a fail-closed state that leaves deployment state honest (§10; LAW-08; ADR-0016 §14, §20);
7. the platform starts;
8. health/readiness checks are executed;
9. `ready` is reached as a verified operational condition (§33);
10. deployment state is fixed according to ADR-0016 §9: `realized` / `deployed` only after verification, the record references the instance digest, and failures are recorded honestly;
11. the Manifest is not modified — the Immutable Manifest Principle holds (ADR-0016 §7);
12. no version substitution occurs;
13. no floating selectors appear anywhere in the deployment input (ADR-0016 §7; `docs/ARCHITECTURE.md` §1.3);
14. business data ownership is not violated — the capability owns no business facts, no new SCS/component appears, and no direct access to component databases occurs (LAW-03, LAW-04; ADR-0016 §6, §22);
15. the state of the operation is observable and correlatable with the instance digest (ADR-0016 §19).

These criteria select no implementation technology (§40). They verify observable behavior of the deployment operation; they do not prescribe how the implementation internally represents progression (§36). Tests for the slice verify architectural rules, not merely successful object construction (ADR-0015 §16; ADR-0016 §30). The approved work item formulates these criteria into concrete deliverables and test suites.

## 44. Later Slices Require Their Own Acceptance Criteria

Every implementation slice beyond the first requires its own approved implementation work item and its own acceptance criteria. This includes any later slice covering upgrade, rollback, `superseded` handling, extended runtime management, broader observability or reconciliation. Every later slice additionally passes:

- the full chain of §31: tests / verification → Quality Gate → independent review → owner approval → merge.

Nothing in this ADR pre-approves any later slice or its criteria.

## 45. Non-Goals

This ADR does **not**:

- authorize or perform any implementation — no production code of any kind;
- create Deployment & Operations;
- create «Slice G» or any other implementation slice;
- select or recommend Kubernetes, Docker, Docker Compose, Helm, Terraform, Ansible, a cloud provider, a broker, an orchestrator, a CI/CD system, an observability stack or any other concrete technology (§40);
- create an implementation work item;
- create an Issue or a PR;
- change the architecture, `docs/ARCHITECTURE.md`, ADR-0016, the Architecture Baseline or `docs/adr/README.md`;
- change any ADR catalog content — a `PROPOSED` ADR enters the confirmed list only upon ratification (`docs/adr/README.md`; ADR-0016 §31);
- claim that this ADR is approved or ratified without confirmed evidence — at the time of writing as `PROPOSED` it was not; ratification is recorded as a confirmed governance fact in §52 (§50).

## 46. Alternatives Considered

### Proceed directly to an implementation work item, without a scope ADR

Rejected. The scope of the capability would then be negotiated under implementation pressure, against a ratified boundary, with high risk of drift. ADR-0016 §30 requires the boundary rules to be verifiable by test when implementation is approved; fixing the scope, vocabulary and first-slice criteria first makes the future work item precise and reviewable. This follows the project's established sequence discipline (ADR-0010 eliminated unratified-architecture drift; ADR-0016 §24 rejected starting without a ratified boundary).

### Let this ADR authorize implementation directly

Rejected. Ratification and implementation authorization are separate acts in this project (ADR-0015 §15, §20; ADR-0016 §30). Merging them would break the governance chain of §19. The process note recorded in ADR-0011's ratification documents exactly why the «ratify first, implement after» sequence is enforced rather than assumed.

### Select a concrete deployment stack in this ADR

Rejected. `docs/ARCHITECTURE.md` §36 classifies concrete deployment engines as time-varying details; ADR-0016 §23 deliberately selects none. Binding the scope to a tool would invert «stable laws first, temporary details later».

### Implement the whole capability in one slice

Rejected. The project's slice discipline requires small, reviewable increments, each with its own work item, verification, independent review and owner approval (ADR-0015 §15). A whole-capability slice would be unreviewable against the boundary rules.

### Invent new formal statuses (`IMPLEMENTATION AUTHORIZED`, `SUPERSEDED`, `RETIRED`)

Rejected. Existing project conventions already cover the needed semantics: `PROPOSED` / `RATIFIED` as ADR statuses, the separate-approved-work-item rule as the implementation gate, and `IMPLEMENTED` as the work-item/slice outcome record. Where an equivalent rule already exists, no new governance rule is invented (§22–§27).

## 47. Consequences

### Positive

- The future implementation has a fixed scope before any code exists: full capability scope (§4), first slice (§42), criteria (§43) — implementation work will not re-litigate architecture or scope.
- The `ready` / `deployed` / `realized` ambiguity is removed before implementation vocabulary ossifies (§33–§35).
- The first slice is small, vertical and reviewable against observable behavior, consistent with the slice discipline of ADR-0015.
- Status discipline is explicit: nothing in this document can be read as authorizing implementation (§22–§27).

### Constraints

- Nothing may be implemented until this ADR is ratified **and** a separate approved implementation work item exists (§19, §25).
- Until then, documentation must not describe Deployment & Operations as implemented or even as partially implemented (`docs/ARCHITECTURE.md` §40).
- Every later stage (upgrade, rollback, superseded) needs its own work item and acceptance criteria — scope cannot be silently widened during implementation (§39, §44).
- Technology choices remain deferred; each later concrete choice needs its own record (§40).

## 48. Migration

This ADR migrates nothing: it is documentation work, changes exactly one file (itself), and creates no code, schema, contract, slice, work item, Issue or PR. Its adoption path is the governance chain itself:

```text
ADR-0017 PROPOSED (this document)
    → independent architectural review
    → owner ratification
    → separate approved implementation work item
    → implementation of the first vertical slice (§42, §43)
    → further slices, each with its own work item and acceptance criteria
```

## 49. Governance

- `docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains the sole architectural law; LAW-01…LAW-16 — including LAW-14 (Architecture Changes Through ADR) — remain in force and unchanged.
- The status-and-evidence discipline of `docs/ARCHITECTURE.md` §40 applies: no capability is claimed to exist on an unconfirmed basis; ratified status rests on confirmed artifacts.
- `docs/adr/README.md` remains the catalog of confirmed ADRs: this `PROPOSED` ADR is not a confirmed decision and enters the catalog's confirmed-state list only after ratification.
- Source priority and conflict handling follow `docs/GIT_OPERATING_PROTOCOL.md` §13; the owner decides architecture and performs the final merge (`docs/GIT_OPERATING_PROTOCOL.md` §1).
- ADR-0010…ADR-0016 are not modified by this ADR.

## 50. Current Status

**Status: `RATIFIED`.**

This ADR is ratified by the project owner (2026-09-16); the ratification and the recorded state after it are fixed in §52. Ratification confirms the implementation scope as written — it authorizes no implementation (§21, §24). Until a separate approved implementation work item exists, no implementation under this ADR is permitted.

## 51. Decision Summary and Next Action

> **The future implementation of Deployment & Operations is scoped: the full Capability Scope is fixed in §4 and mirrors the ratified boundary of ADR-0016; implementation, if and when authorized, begins from the minimal vertical slice of §42 along the initial deployment path of §36, accepted only against the observable-behavior criteria of §43; `ready` is a verified operational condition distinct from `deployed` and `realized`; the scope is technology-neutral and changes nothing architectural. This ADR authorizes no implementation: authorization arises only from the chain ADR-0016 RATIFIED → controlled ARCHITECTURE.md update → ADR-0017 ratification → separate approved implementation work item → implementation.**

Next action — as recorded while `PROPOSED`:

```text
1. independent architectural review of this ADR
2. owner ratification — the exclusive act of the project owner
3. separate approved implementation work item for the first slice (§42, §43)
4. implementation — only after 2 and 3, under the slice discipline of §31
```

No merge, ratification, architecture edit, work-item creation or implementation is performed by this document.

## 52. Ratification

RATIFIED by the project owner on 2026-09-16. The ratification confirms the implementation scope as written: the full Capability Scope of §4 mirrors the ratified boundary of ADR-0016, the first implementation slice is fixed by §42–§43, and the lifecycle, vocabulary and initial deployment path are fixed by §22–§38.

Recorded state after ratification:

- the independent architectural review of this ADR was performed and concluded with no blocking findings before ratification;
- this ratification does not change `docs/ARCHITECTURE.md`, ADR-0016, any other ADR or the documentation baseline substance: ADR-0017 is a Level C governance/documentation decision that scopes future implementation and changes no architecture (§41);
- per the catalog rule (ADR-0016 §31; this ADR §45, §49), this ADR enters the confirmed list of `docs/adr/README.md` upon this ratification;
- no implementation of Deployment & Operations is authorized by this ratification: implementation still requires a separate approved implementation work item (§19, §25, §28), and no «Slice G» or any other implementation slice is approved or authorized (§29);
- the governance chain of §19 advances by exactly one step: `ADR-0016 RATIFIED → controlled ARCHITECTURE.md update → ADR-0017 ratification` are now confirmed repository facts; the next and only remaining gate before any implementation is the separate approved implementation work item.

**ADR-0017 status: RATIFIED.**
