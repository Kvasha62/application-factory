# ADR-0015 — Activate Level 2 Component Factory

**Status:** `PROPOSED`  
**Date:** 2026-09-14  
**Level:** D — domain/architecture boundary decision  
**Initiated by:** Factory Gate #1 review after SCS-003 Booking

> This ADR proposes activation of architectural Level 2 — Component Factory after the project has earned Factory Gate #1. It does not authorize premature expansion of business systems, microservices or integrations, and it does not replace `docs/ARCHITECTURE.md`.

## 1. Context

The project has now established three independent Class A — Business System / SCS boundaries:

- SCS-001 Learning;
- SCS-002 Commerce;
- SCS-003 Booking.

`docs/ARCHITECTURE.md` v1.2.0 defines Level 2 as **Component Factory** and defines the factory gate as reached when at least one of the following is true:

- independently delivered components ≥ 3;
- vertical profiles ≥ 2;
- external consumers of components exist outside the team.

The first gate is therefore satisfied by the three independently delivered Business Systems. The formal Gate #1 review has passed.

The architecture law also states that factory mechanics are not to be built before a gate is reached. The gate is now reached, so continuing to prohibit all Level-2 factory mechanics would no longer reflect the earned maturity of the project.

At the same time, reaching the gate does not justify building every factory capability at once. The factory must be introduced incrementally, with each capability having a clear source of truth, contract, verification boundary and implementation issue.

## 2. Decision

**Architectural Level 2 — Component Factory is proposed for activation.**

Upon ratification of this ADR, the project may introduce the factory mechanisms explicitly defined by `docs/ARCHITECTURE.md`, provided they are implemented incrementally and remain subordinate to the architectural law.

Level 2 does **not** mean that every factory mechanism must exist immediately. The initial factory implementation is incremental.

The intended implementation progression is:

```text
Component Contract
        ↓
Component Registry
        ↓
Component Catalog
        ↓
Platform Manifest
        ↓
Golden Bundles
        ↓
Composer
        ↓
Platform Instance assembly
```

The exact implementation order may be adjusted by later implementation ADRs/issues when justified, but the architectural responsibilities and prohibitions in this ADR remain binding.

## 3. What Level-2 Activation Authorizes

The following capabilities are architecturally authorized **after this ADR is ratified**:

- a canonical machine-readable Component Registry;
- a discoverable Component Catalog derived from authoritative metadata;
- explicit component identity, class, version, ownership and lifecycle metadata;
- published machine-readable Component Contracts;
- declared component dependencies and compatibility policies;
- reproducible Platform Manifests containing explicit component versions;
- Golden Bundle definitions for validated compatible component sets;
- Composer capabilities that assemble platforms only from published metadata and compatible contracts;
- independent component delivery and release metadata;
- compatibility verification for platform composition;
- deterministic rejection of invalid or incompatible compositions.

Authorization to implement a capability is not authorization to claim that the capability exists before its implementation has been independently verified and accepted.

## 4. Component Registry

The Component Registry is the authoritative machine-readable inventory of components used by the factory.

At minimum, registry metadata must identify:

- component identity;
- component class;
- owner;
- public version;
- published contract references;
- data ownership and data scope;
- declared dependencies;
- compatibility policy;
- artifact identity;
- lifecycle/release state.

The Registry must not become a second source of truth for business data owned by Learning, Commerce, Booking or other components.

Registry metadata describes components; it does not own their business facts.

## 5. Component Catalog

The Component Catalog is the discoverable view of components available to the factory.

The Catalog must not silently diverge from authoritative registry/contract metadata.

The Catalog may provide human-oriented discovery and filtering, but composition decisions must ultimately use machine-readable authoritative metadata and compatibility rules.

The Catalog does not grant permission to access another component's internals.

## 6. Platform Manifest

A Platform Manifest is the explicit description of a Platform Instance composition.

A manifest must identify concrete component versions. It must not use `latest` as an architectural dependency.

Production artifacts must remain pinned according to the existing architecture law, including digest pinning where required.

A reproducible manifest must resolve to the same intended component set when evaluated against the same registry state and release metadata.

## 7. Golden Bundles

A Golden Bundle is a validated, reproducible set of compatible component versions intended to be used as a known-good composition.

A Golden Bundle must make its component versions and compatibility identity explicit.

Golden Bundles do not replace component contracts. They package validated compatibility relationships between independently versioned components.

A Golden Bundle must not silently change when an individual component releases a new version.

## 8. Composer

The Composer assembles Platform Manifests or Platform Instances using published component metadata and compatibility rules.

The Composer may consume:

- Component Registry metadata;
- Component Contracts;
- compatibility policies;
- Golden Bundle definitions;
- explicit Platform Manifest input.

The Composer must not:

- read component internal databases;
- import component internal modules as a composition mechanism;
- bypass published contracts;
- silently substitute `latest` versions;
- silently replace incompatible versions;
- create undocumented cross-component dependencies.

Invalid composition must fail deterministically and explain the compatibility or contract violation.

## 9. Independent Delivery

Level 2 permits independent delivery of components, but independent delivery does not require immediate microservice deployment.

A component may remain physically inside the Level-0 Modular Monolith while possessing an independently versioned public contract and release identity.

Physical separation remains an implementation decision governed by `docs/ARCHITECTURE.md` and is not required merely because Level 2 is active.

## 10. Compatibility

Component compatibility is a first-class factory concern.

The factory must distinguish at least:

- component identity;
- semantic version;
- dependency range;
- contract compatibility;
- breaking versus non-breaking changes;
- artifact identity.

Breaking changes continue to require a new major version or an explicitly approved compatibility/migration strategy according to `docs/ARCHITECTURE.md`.

Factory tooling must not weaken existing compatibility rules.

## 11. Data Ownership and Boundaries

Level 2 does not alter business data ownership.

The factory owns only factory-level metadata and composition knowledge, such as:

- component registry metadata;
- catalog metadata;
- bundle definitions;
- platform manifests;
- composition/compatibility metadata.

The factory does **not** own:

- Learning courses, lessons, assignments, submissions or enrollments;
- Commerce products, offers, prices, carts, checkouts, orders or payment state;
- Booking resources, availability windows or reservations;
- Identity internals;
- Tenant Authority internals;
- other component business facts.

The factory must not access component internal databases directly.

## 12. Foundation Reuse

Level 2 does not authorize creation of duplicate foundation services.

Existing Identity, Tenant Authority, Authorization, Idempotency and Audit boundaries remain authoritative.

A new shared service is not justified merely because factory tooling needs some capability. Any genuinely new platform-level boundary requires its own architectural decision.

## 13. Explicit Non-Goals

This ADR does **not** authorize:

- changing Learning, Commerce or Booking business boundaries;
- direct cross-component database access;
- direct imports of another component's internal implementation;
- `latest` dependencies;
- unpinned production artifacts;
- premature microservice extraction;
- one deployment unit per component merely for architectural symmetry;
- customer-specific forks as the default customization mechanism;
- Composer bypasses around contracts or compatibility checks;
- automatic Commerce ↔ Learning integration;
- automatic Commerce ↔ Booking integration;
- automatic Learning ↔ Booking integration;
- new payment infrastructure;
- new foundation services without a separate decision;
- building all factory mechanisms in one implementation step;
- weakening any ratified ADR;
- replacing `ARCHITECTURE.md` as the architectural law.

## 14. Transition State

Until this ADR is ratified, the architectural state remains the current ratified state.

After ratification, the intended state is:

```text
Architecture level: Level 2 — ACTIVE
Factory mechanics: incremental implementation
Factory Catalog: not yet assumed to exist until implemented and accepted
Component Registry: not yet assumed to exist until implemented and accepted
Golden Bundles: not yet assumed to exist until implemented and accepted
Composer: not yet assumed to exist until implemented and accepted
```

Documentation must describe implemented capabilities accurately. Activation of Level 2 must never be interpreted as evidence that unimplemented factory tooling already exists.

## 15. Implementation Slices

Factory implementation should proceed through separate small, reviewable slices.

### Slice A — Component Registry

Establish the canonical machine-readable inventory and its schema, identity rules, version rules, ownership metadata and compatibility metadata.

### Slice B — Component Catalog

Provide discoverable catalog views derived from authoritative registry/contract metadata.

### Slice C — Platform Manifest

Define and validate reproducible platform composition using explicit component versions and artifact identities.

### Slice D — Golden Bundles

Introduce validated known-good compatible component sets.

### Slice E — Composer

Introduce deterministic composition and rejection of incompatible or contract-invalid platform assemblies.

Each slice requires its own implementation work item, verification, independent review and owner approval before merge.

## 16. Testing Requirements

Factory implementation must provide automated verification for at least:

- registry schema validity;
- component identity uniqueness;
- version validity;
- contract references;
- declared dependency validity;
- compatibility policy evaluation;
- manifest reproducibility;
- rejection of `latest` dependencies;
- artifact pinning rules;
- Golden Bundle immutability/version identity;
- invalid composition rejection;
- deterministic Composer failures;
- preservation of data ownership boundaries;
- absence of direct cross-component database access;
- breaking/non-breaking compatibility behavior.

Factory tests must verify architecture rules, not merely successful object construction.

## 17. Acceptance Boundary

ADR-0015 is considered ratified only when:

1. Level 2 activation is approved through the project's normal ADR workflow;
2. Factory Gate #1 remains documented as satisfied by the three independent Business Systems;
3. no ratified business-system boundary is changed by the activation;
4. the transition state is documented accurately;
5. the first implementation slice has a separate approved work item before implementation begins;
6. no unimplemented factory capability is represented as already available;
7. existing `ARCHITECTURE.md` rules remain binding.

The acceptance of this ADR does not mean that Registry, Catalog, Golden Bundles or Composer are themselves implemented.

## 18. Consequences

### Positive

The project can now introduce the factory mechanisms that were intentionally deferred until the maturity gate was reached. The architecture moves from a theoretical factory model toward an earned, incremental factory implementation.

### Constraints

Level 2 introduces additional metadata, compatibility and composition responsibilities. These must be added incrementally rather than through a large framework-first implementation.

The project must preserve the distinction between architectural activation and actual implementation status.

## 19. Alternatives Considered

### Remain at Level 0 indefinitely

Rejected. Factory Gate #1 has been reached and the project now has three independently delivered Business Systems. Continuing to prohibit all Level-2 factory mechanics would contradict the maturity model's intended gate behavior.

### Activate Level 2 and build the complete factory immediately

Rejected. The gate authorizes factory mechanics but does not justify a large speculative implementation. Incremental slices provide smaller review boundaries and lower architectural risk.

### Activate Level 2 only after a fourth Business System

Rejected. The architecture defines three independent components as sufficient for the first factory gate.

### Activate Level 2 by changing ARCHITECTURE.md without an ADR

Rejected. Substantive architectural changes must be recorded through the ADR process.

## 20. Ratification

This document is proposed for owner ratification through the project's established workflow:

```text
Issue → ADR branch → PR → independent ChatGPT review
→ corrections if required → owner approval → merge → post-merge verification
```

Until ratified, this document has `PROPOSED` status and does not by itself activate Level 2.

Arena may prepare and implement the approved follow-up work only after the ADR reaches `RATIFIED` status and the corresponding work item is approved.

## 21. Relationship to Other Documents

This ADR does not replace `docs/ARCHITECTURE.md`.

`docs/ARCHITECTURE.md` remains the project's technical law. The Level 2 definition and factory gate rules remain authoritative.

`docs/adr/README.md` remains the catalog of confirmed ADRs and architectural history. ADR-0015 must be added there only after ratification, preserving the distinction between proposed and confirmed architectural history.

ADR-0014 remains the ratified Booking boundary and is not modified by this ADR.

## 22. Final Decision

> **The project has earned Factory Gate #1 through three independently delivered Class A Business Systems. Level 2 — Component Factory is proposed for activation, with factory capabilities introduced incrementally and without changing existing business boundaries, data ownership, contract rules, compatibility rules or the prohibition on `latest`.**

**ADR-0015 status: PROPOSED.**
