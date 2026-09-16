# ADR-0016 — Deployment and Operations Boundary

**Status:** `PROPOSED`

**Date:** 2026-09-16

**Level:** C — platform architecture (deployment model; `docs/ARCHITECTURE.md` §34.1)

**Initiated by:** post-Slice F boundary review (Issue #74, PR #75)

> This ADR fixes an architectural boundary. It implements nothing: no deployment code, no provisioning, no rollout automation, no pipeline and no infrastructure definition are authorized by this document. It selects no deployment technology. It deliberately changes nothing in `docs/ARCHITECTURE.md`: ratified architecture changes only through ratified artifacts (§34, §40), so the controlled architecture update is deferred until after owner ratification.

## 1. Context

ADR-0015 (RATIFIED 2026-09-14) activated Level 2 — Component Factory. Its implementation slices are now complete: Slice A — Component Registry, Slice B — Component Catalog, Slice C — Platform Manifest, Slice D — Golden Bundles, Slice E — Composer, Slice F — Platform Instance Assembly (Issue #74, PR #75, merged 2026-09-16). The Factory can therefore deterministically assemble a Platform Instance representation from an accepted, validated Platform Manifest. Slice F is explicitly a representation step only: no deployment, provisioning, rollout, approval or publication behavior was introduced by it.

The ratified law currently ends at the assembled instance. `docs/ARCHITECTURE.md` §37 («Главная модель всей системы») terminates at `PLATFORM INSTANCE` and contains no stage after it. At the same time, §2.2 defines:

```text
Platform Instance — конкретный развёрнутый продукт заказчика.
```

The current terminology therefore uses one term — Platform Instance — for both the fixed description produced by the Factory and the deployed customer product. Between «the instance exists as a validated artifact» and «the platform is actually running and operated» lies an architecturally unallocated region:

```text
Platform Instance → ??? → Running Platform
```

ADR-0015 already anticipated this at the documentation level: the ADR catalog records that deployment/provisioning/rollout execution on top of an assembled Platform Instance requires a separate approved work item. No ratified decision, however, defines the boundary, ownership and rules of that stage.

Deployment model is a named Level C concern in `docs/ARCHITECTURE.md` §34.1: «Level C — platform architecture. Меняет component boundaries, contracts, data ownership, versioning, deployment model или factory behavior. Требуется ADR.» Defining the Deployment and Operations boundary therefore requires this ADR before any deployment capability may be implemented and before `docs/ARCHITECTURE.md` may be updated to record the stage.

## 2. Decision

**The Deployment and Operations capability is established as the platform-level stage that follows the Factory chain:**

```text
Factory
    ↓
Platform Manifest
    ↓
Platform Instance
    ↓
Deployment & Operations
    ↓
Running Platform
```

The Platform Instance is the boundary artifact: the Factory ends at its deterministic assembly; Deployment & Operations begins by consuming it as its exact, immutable input.

This decision is **Level C — platform architecture** per `docs/ARCHITECTURE.md` §34.1, because it defines the platform's deployment model boundary. It is not Level D: it changes no fundamental rule, no component boundary, no contract, no data ownership, no versioning law and no existing Factory behavior. `docs/ARCHITECTURE.md` remains v1.2.0 RATIFIED and is deliberately not modified by this ADR (see §29).

## 3. Factory Boundary

The Factory ends at a deterministic Platform Instance.

The Factory chain — Registry, Catalog, Platform Manifest, Golden Bundles, Composer, Platform Instance assembly — produces descriptions: a complete, pinned, reproducible statement of what a platform is. It does not make any platform run.

The Factory must not:

- provision, deploy or operate runtime environments;
- hold live runtime or deployment state;
- manage health, readiness, upgrades or rollbacks;
- perform database migration orchestration against real environments.

Factory behavior as defined by ADR-0015 and implemented in Slices A–F is **not changed** by this ADR.

## 4. Deployment & Operations Boundary

Deployment & Operations is a separate platform capability outside the Factory. It begins where the Factory ends: it consumes the Platform Instance and is responsible for turning that desired state into a Running Platform and keeping the actual state aligned with it.

Deployment & Operations owns:

- **provisioning** — preparation of the runtime environment required by the instance (§11);
- **deployment** — realization of the instance as a running platform (§10);
- **migration orchestration** — coordinated execution of component database migrations during deployment and upgrade (§14);
- **runtime management** — operational control of the running platform (§18);
- **health/readiness** — establishment and evaluation of health and readiness (§18, §19);
- **deployment state** — the authoritative operational record of actual state (§9);
- **upgrade** (§15) and **rollback** (§16);
- **operational observability** — signals about deployment and runtime health (§19).

This list defines the capability's scope; §21–§22 define what it must never become.

## 5. Desired State and Actual State

This ADR introduces the distinction the ratified law currently lacks:

```text
Platform Instance  = desired state  — a complete, fixed description prepared by the Factory
Running Platform   = actual state   — what is actually deployed, running and observable
```

Deployment & Operations reconciles actual state to desired state. Desired state is never edited at runtime; changing what a platform *is* requires a new Platform Instance produced by the Factory.

**Semantic gap recorded.** The ratified `docs/ARCHITECTURE.md` §2.2 currently says «Platform Instance — конкретный развёрнутый продукт заказчика», i.e. one term covers both the description and the deployed product. Under this ADR the terms separate: the Platform Instance is the desired-state artifact; the Running Platform is the actual state produced from it by Deployment & Operations. This ADR deliberately does **not** edit §2.2 (or any part of `docs/ARCHITECTURE.md`); the wording correction is scheduled as the controlled architecture update after ratification (§29). Until then, the coexistence of the old wording and this PROPOSED decision is documented honestly rather than silently rewritten.

## 6. Ownership

Deployment & Operations owns only operational state of the Deployment & Operations capability itself:

- deployment state records (§9);
- deployment/upgrade/rollback attempt history;
- runtime and reconciliation status of platforms under management.

It owns **no business facts**. Business components keep ownership of their business data: Learning, Commerce, Booking and any future Business System remain the sole owners of their facts (LAW-03, ADR-0015 §11). The Factory keeps factory-level metadata (ADR-0015 §11). Foundation boundaries — Identity, Tenant Authority, Authorization, Idempotency, Audit — remain authoritative and unchanged.

Deployment & Operations must not read component internal databases directly (LAW-04); cross-component access remains limited to published contracts.

## 7. Immutable Manifest Principle

The Platform Manifest is immutable input to everything downstream of it, and the Platform Instance is bound to it by identity, version and digest.

Deployment & Operations:

- must not modify the Manifest;
- must not substitute component versions;
- must not replace artifacts;
- must not redefine component identity;
- must not use floating version selectors (`latest`) — prohibited at this boundary as everywhere else (ADR-0015 §6);
- must not change business ownership.

What is deployed is exactly what was assembled. Any composition change re-enters through the Factory, never through runtime mutation.

## 8. Platform Instance Identity

A Platform Instance is identified by its platform identity, version and digest, bound exactly to its accepted Manifest (Slice F).

Deployment & Operations must verify the instance's digest integrity before acting on it, must reference the instance digest in every deployment state record (§9) and every observability signal (§19), and must refuse to act on an unverifiable or name-only reference. There is no «deploy the platform called X» — only «deploy this exact instance».

## 9. Deployment State

Deployment & Operations maintains deployment state — its own operational record of actual state:

- the target instance (identity, version, digest) currently being realized;
- lifecycle position of the deployment (in progress, realized, failed, superseded, rolled back);
- attempt history and reconciliation results;
- summarized health/readiness status.

Deployment state is operational metadata, not business data (§6). It must be honest: it never reports `realized` for something that was not verified (§10, §20), and it is the only authoritative answer to «what is actually running where».

## 10. Meaning of `deployed`

The word `deployed` gains an exact architectural meaning:

> A platform is `deployed` if and only if the actual, observable Running Platform has been verified to correspond to the desired state of a specific Platform Instance, referenced by identity, version and digest.

Consequences of the definition:

- merely starting artifacts, processes or containers is **not** `deployed`;
- `deployed` is always relative to a concrete instance — there is no versionless `deployed`;
- a `deployed` claim without digest-bound verification violates this boundary;
- health/readiness must hold for the claim to stand (§18, §19).

## 11. Provisioning

Provisioning prepares the runtime environment required by the Platform Instance.

- Provisioning derives its requirements only from the instance and its declared configuration path (§13); it must not introduce undocumented components, dependencies or composition changes.
- Provisioning is fail-closed: an environment whose properties cannot be verified against the instance's requirements is not used (§20).
- Provisioning is technology-neutral (§23): no provisioning tool, infrastructure-as-code system or cloud provider is selected by this ADR.

## 12. Secrets

Secrets are operational inputs and are never architectural content.

- Secrets never enter the Platform Manifest or the Platform Instance — consistent with the ratified law (§20: «Секреты не входят в Platform Manifest»).
- Secrets are injected at the Deployment & Operations boundary at realization time, through a secret-provisioning mechanism; no secret provider is selected (§23).
- Secrets must not be copied into deployment state records, logs or observability signals.
- Secret rotation is an operational concern and must not change composition identity.
- Absence of a required secret is a fail-closed rejection (§20), never a reason to substitute composition.

## 13. Configuration

Environment-specific configuration reaches the running platform through the existing configuration contract, not through composition changes.

- Configuration is declarative and versioned with the component; an unknown configuration key is a build error; environments use overlays (§20 of `docs/ARCHITECTURE.md`).
- Slice F already routes environment-specific settings through this configuration path; Deployment & Operations binds those settings to the runtime environment it provisions.
- Configuration must never change composition identity: versions, artifacts, component identity and the bundle reference remain exactly those of the instance (§7).

## 14. Database Migrations

Migration orchestration belongs to Deployment & Operations; migration content belongs to the components.

- Each component owns its schema and data (LAW-03) and defines its migrations under Forward Migration (LAW-08): schema develops forward, via `EXPAND → MIGRATE → CONTRACT`.
- Deployment & Operations orchestrates execution — triggering components' migrations in the order and conditions their contracts allow — as part of deploying or upgrading to an instance.
- Deleting or rewriting production schema to «fix» a deployment is prohibited (LAW-08); a failed migration is a fail-closed state (§20), not an invitation to improvise.

## 15. Upgrade

Upgrade is the deliberate transition of a Running Platform from one desired state to another.

- An upgrade targets a new Platform Instance (new identity/version/digest) produced by the Factory — never a silent move of «the same» platform.
- No floating selectors: upgrade input is pinned exactly as any deployment input (§7).
- Migration orchestration (§14) is part of upgrade execution.
- Every upgrade is recorded in deployment state (§9) and observable (§19).

## 16. Rollback

Rollback returns actual state to a previously known-good desired state.

- A rollback target is a previous Platform Instance identified by digest — the same immutability rules apply (§7, §8).
- Rollback respects Forward Migration (LAW-08): operational code paths may return to a previous instance, while production schema continues to develop forward; rollback must not destroy or rewrite tenant data.
- Rollback is an explicit, recorded operation (§9), not an automatic silent behavior.

## 17. Tenant Provisioning

Tenant lifecycle remains the responsibility of Tenant Authority (IS-002) and the ratified tenant law (§2.3, LAW-16):

`provisioning → active → suspended → deletion_requested → deleted`.

Deployment & Operations may provision tenant-scoped runtime resources only within a lawful tenant context issued by the authoritative foundation services. Deployment & Operations:

- does not own tenant business data;
- does not decide tenant lifecycle transitions;
- must not expose one tenant's resources or data to another (LAW-16).

## 18. Runtime Operations

Runtime Operations is the operational management of a Running Platform: starting and stopping runtime elements, health/readiness establishment, restarts and comparable operational actions.

- Operational actions never edit desired state; any change to what should run is a new Platform Instance from the Factory (§5, §7).
- Every operational action is reflected in deployment state (§9) and observable signals (§19).
- Unmanaged drift — actual state diverging from desired state without an operation — is surfaced as a failure signal, not silently patched (§20).

## 19. Observability

Deployment & Operations owns operational observability:

- deployment, upgrade and rollback events;
- health/readiness status of platforms under management;
- reconciliation results between desired and actual state.

All operational signals must be correlatable with the Platform Instance digest they relate to (§8). Business-meaning telemetry remains owned by the components and the existing observability context rules. No observability stack is selected by this ADR (§23).

## 20. Failure Semantics

Deployment & Operations is fail-closed:

- an instance that fails digest or validation checks is rejected deterministically before any action (§8);
- a failed deployment, migration, upgrade or rollback leaves deployment state honest — never a false `deployed` (§9, §10);
- retries are idempotent; repeating an operation must not duplicate effects;
- no automatic «fix» may alter composition, versions, artifacts or ownership (§7);
- every failure is surfaced through operational observability (§19).

## 21. Factory Does Not Become a Deployment Engine

The Factory must never acquire deployment behavior.

- The Factory produces descriptions of platforms; Deployment & Operations realizes platforms.
- Factory components must not hold runtime state, issue runtime actions or orchestrate migrations against real environments (§3).
- Keeping assembly a pure, deterministic representation step preserves reproducibility (LAW-09) and the Slice F boundary.
- Any future pressure to «just deploy from the Composer» is rejected by this boundary.

## 22. Deployment Does Not Become a Business System

Deployment & Operations is platform machinery, not a product.

- It creates no new Business System / SCS and no new component (§4, §27).
- It owns no business facts and exposes no business APIs (§6).
- Business features must not accumulate inside it; complexity must be earned (LAW-13) and business capabilities enter only as proper business systems through their own ADRs.

## 23. Technology Neutrality

This ADR defines an architectural stage, not a tool selection. `docs/ARCHITECTURE.md` §36 is the governing principle: stable laws first; concrete deployment engines are time-varying technical details («конкретный deployment engine»).

This ADR deliberately does **not** select:

- Kubernetes;
- Helm;
- Terraform;
- Ansible;
- a cloud provider;
- a CI/CD platform;
- a service mesh;
- a broker;
- a secret provider;
- a scheduler;
- a container runtime;
- any concrete deployment engine.

Any future concrete choice that rises to architectural significance requires its own record (Level B or Level C per §34.1) and must honor this boundary.

## 24. Alternatives Considered

### Treat deployment as a mere implementation detail (no ADR)

Rejected. Deployment model is explicitly named in `docs/ARCHITECTURE.md` §34.1 as a Level C concern requiring an ADR. Starting deployment work without a ratified boundary would repeat the unratified-architecture drift that ADR-0010 eliminated.

### Extend the Factory — Composer or Platform Instance assembly — with deployment behavior

Rejected. Assembly must remain a pure, deterministic representation step (the Slice F boundary). Merging deployment into it would violate its defined scope, blur ownership and compromise reproducibility semantics (LAW-09, §21).

### Update `docs/ARCHITECTURE.md` §37 and §2.2 directly alongside this ADR

Rejected — deferred, not forbidden. Ratified architecture changes only through ratified artifacts (§34; status and evidence discipline per §40): the ADR must first be ratified by the owner, and only then does the controlled architecture update record the new stage (§29). This ADR therefore deliberately leaves `docs/ARCHITECTURE.md` unchanged.

### Select a concrete deployment stack now

Rejected. §36 classifies the concrete deployment engine as a time-varying detail; binding the architectural law to a current tool would invert «stable laws first, temporary details later». No technology is chosen by this ADR (§23).

## 25. Consequences

### Positive

- The region between an assembled instance and a running platform has a name, an owner and rules before any code exists (LAW-14).
- The Platform Instance becomes a closed hand-off artifact: runtime work cannot silently change composition.
- Desired state and actual state are architecturally separated; `deployed` acquires a verifiable meaning.
- Future implementation work inherits a fixed boundary and does not need to re-litigate architecture.

### Constraints

- Level C status gates all subsequent work: nothing may be implemented until ratification and a separate approved work item exist (§30).
- Until the post-ratification controlled update, `docs/ARCHITECTURE.md` remains formally silent about the stage (§29); documentation must never describe the stage as implemented — status claims must rest on confirmed artifacts (§40).
- Deployment state must be maintained honestly; operational honesty is architectural, not optional (§9, §20).
- Technology decisions are deferred; each later concrete choice needs its own record (§23).

## 26. Migration

This ADR itself migrates nothing: it implements nothing and changes no code, no schema, no contract and no document other than itself. The transition it defines is:

```text
CURRENT

Factory
  ↓
Platform Manifest
  ↓
Platform Instance


TARGET

Factory
  ↓
Platform Manifest
  ↓
Platform Instance
  ↓
Deployment & Operations
  ↓
Running Platform
```

Explicit statements of this Migration:

- this ADR itself implements nothing;
- Factory behavior is not changed by this ADR (§3);
- `docs/ARCHITECTURE.md` is not changed by this PR (§29);
- after ratification, a separate controlled architecture update is required (§29);
- after ratification, a separate approved implementation work item is required (§30);
- a deployment capability is not considered to exist merely because this ADR is ratified — existence claims must rest on confirmed artifacts (§40).

Adoption path:

```text
ADR-0016 PROPOSED (this document)
    → architectural review
    → owner ratification
    → controlled `docs/ARCHITECTURE.md` update (§29)
    → separate approved implementation work item(s) for the
      Deployment & Operations capability — a future slice,
      not «Slice G» authorized by this ADR
```

(The accompanying one-line correction in `docs/adr/README.md` records only the already-merged state of PR #75 under the documentation-hygiene rule; it is not part of this decision.)

## 27. Non-Goals

This ADR does **not** authorize:

- any implementation: no deployment, provisioning or rollout code, no pipeline, no infrastructure definition;
- a «Slice G» or any other new implementation slice — slices require separate approved work items after ratification (§30, ADR-0015 §15);
- a new SCS / Business System / Class A component, or any new component at all;
- selection of concrete deployment technologies — no Kubernetes, Helm, Terraform, Ansible, cloud provider, CI/CD platform, service mesh, broker, secret provider, scheduler, container runtime or any concrete deployment engine (§23);
- floating version selectors (`latest`) anywhere in the deployment path (§7);
- any modification of `docs/ARCHITECTURE.md` — the architectural law remains v1.2.0 RATIFIED and is deliberately unchanged by this ADR; the controlled update is a separate step after ratification (§29);
- changes to Composer, Registry, Catalog, Platform Manifest, Golden Bundle or Platform Instance semantics;
- changes to Factory behavior as defined by ADR-0015;
- changes to Tenant Authority or tenant lifecycle (§17);
- customer-specific forks or per-customer deployment divergence (LAW-11);
- microservice extraction or one deployment unit per component merely for architectural symmetry (ADR-0015 §13).

## 28. Architectural Impact

This decision adds one named platform-level stage — Deployment & Operations — with explicit ownership and rules, at Level C per `docs/ARCHITECTURE.md` §34.1.

It does **not** change:

- component boundaries, contracts, data ownership or versioning law;
- Factory behavior or any factory component semantics (§3);
- foundation boundaries: Identity, Tenant Authority, Authorization, Idempotency, Audit;
- LAW-01…LAW-16, including LAW-09 (Reproducible Platform) and LAW-16 (Tenant Isolation).

`docs/ARCHITECTURE.md` remains v1.2.0 RATIFIED. The terminology gap at §2.2 and the missing stage at §37 are explicitly acknowledged (§5) and scheduled for the controlled update (§29) rather than silently patched. This ADR is consistent with ADR-0015: the catalog rule that deployment/provisioning/rollout execution requires a separate approved work item remains in force and is given its architectural boundary here.

## 29. Required Architecture Update

After owner ratification — and only then — a separate controlled update of `docs/ARCHITECTURE.md` must record this decision. The required content of that update:

1. extend the §37 main model with the new stage:
   `PLATFORM INSTANCE → DEPLOYMENT & OPERATIONS → RUNNING PLATFORM`;
2. refine §2.2 so that Platform Instance denotes the desired-state description, and the deployed/running product is described through the new stage (resolving the recorded semantic gap, §5);
3. add the cross-reference that the stage exists under Level C discipline (§34.1);
4. align affected terminology wherever «Platform Instance» currently implies «deployed».

The update is a separate controlled change after ratification. This PR performs none of it.

## 30. Implementation Rule

- No implementation of Deployment & Operations may begin before this ADR is ratified **and** a separate implementation work item is approved (ADR-0015 §15 discipline).
- Ratification does not make the capability exist: by the project's status-and-evidence rule (§40), nothing is described as implemented until it has been independently verified and accepted.
- When implementation is approved, it must verify the boundary rules by test: immutability of manifest/instance input (§7), digest verification (§8), fail-closed semantics (§20), prohibition of floating selectors (§7), honest deployment state (§9) and the ownership limits (§6, §17, §21, §22).

## 31. Governance

- `docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains the sole architectural law; LAW-01…LAW-16 — including LAW-14 (Architecture Changes Through ADR) — remain in force and unchanged.
- Fundamental change occurs only through ADR (§34); this decision follows that path as a Level C change (§34.1).
- The status-and-evidence discipline (§40) applies to everything this ADR touches: ratified status rests on confirmed artifacts, violations of fundamental law are formalized through ADR, and no capability may be claimed as existing on an unconfirmed basis.
- `docs/adr/README.md` remains the catalog of confirmed ADRs; a `PROPOSED` ADR is not a confirmed decision and enters the catalog's confirmed-state list only after ratification.
- ADR-0010–ADR-0015 are not modified by this ADR.

## 32. Decision, Status and Next Action

> **Final decision. The Deployment and Operations boundary is established as the platform capability between Factory output and runtime: `Factory → Platform Manifest → Platform Instance → Deployment & Operations → Running Platform`. The Platform Instance is the boundary artifact — desired state, immutable and digest-bound. Deployment & Operations owns provisioning, deployment, migration orchestration, runtime management, health/readiness, deployment state, upgrade, rollback and operational observability; it owns no business data, changes no Factory behavior, becomes neither a deployment engine inside the Factory nor a Business System, selects no technology and permits no floating selectors. `docs/ARCHITECTURE.md` is deliberately unchanged: the controlled update follows only after owner ratification, and implementation follows only after a separate approved work item.**

**Status:** `PROPOSED` (2026-09-16). This ADR is not ratified and is not part of the ratified architecture while `PROPOSED`.

**Next action:**

```text
1. independent architectural review
2. owner ratification — the exclusive act of the project owner
3. controlled `docs/ARCHITECTURE.md` update per §29
4. separate approved implementation work item(s) per §30
```

No merge, ratification or architecture edit is performed by this document itself.

**ADR-0016 status: PROPOSED.**
