# ADR-0020 — Platform Identity Surface for Running Platform

**Status:** RATIFIED

**Ratified:** 2026-09-26 by project owner

**Decision level:** C

**Scope:** Deployment & Operations / Running Platform identity evidence

**Related:** ADR-0016, ADR-0017, ADR-0018, ADR-0019

---

## 1. Decision

The Running Platform MUST expose an owner-correct identity surface from which the actual identity of the Running Platform can be independently established.

This surface is named:

```text
Platform Identity Surface
```

Its purpose is to provide actual identity-bearing facts about the Running Platform to an independent verifier.

The Platform Identity Surface is not itself the Platform Instance canonicalization mechanism.

It does not compute:

```text
D_expected
```

and does not redefine:

```text
D_actual
```

The existing Platform Instance canonicalization remains authoritative under ADR-0019.

The semantic relationship is:

```text
Running Platform
        ↓
Platform Identity Surface
        ↓
Actual Identity Evidence
        ↓
ADR-0019 validation
        ↓
Actual Platform Instance
        ↓
existing canonicalization
        ↓
D_actual
```

---

## 2. Problem

ADR-0019 establishes the requirement that correspondence between a Running Platform and an expected Platform Instance requires independently established actual identity:

```text
CORRESPONDS :=
    PROOF_VALID
    ∧ D_actual == D_expected
```

The current architecture provides several independent actual observations:

- component health;
- component identity/version;
- runtime execution evidence;
- execution-content verification;
- deployment operational state.

It does not currently define a single owner-correct architectural surface through which the complete identity of the Running Platform can be independently established.

Without such a surface, an implementation may be tempted to reconstruct actual identity from expected-state records such as:

- deployment requests;
- DeploymentRecord;
- expected Platform Instance;
- expected Manifest;
- expected artifact declarations;
- expected configuration;
- expected Golden Bundle.

That would violate ADR-0019 because expected state would become evidence of actual state.

The missing architectural boundary is therefore:

```text
actual Running Platform
        ↓
owner-correct actual identity source
```

---

## 3. Decision Scope

This ADR establishes the architectural responsibility for a Platform Identity Surface.

It defines:

- ownership;
- semantic purpose;
- minimum identity coverage;
- separation from expected state;
- relationship with Factory authority;
- relationship with Deployment & Operations;
- provenance requirements;
- freshness requirements;
- completeness requirements;
- fail-closed behavior.

It does not define a mandatory transport technology.

---

## 4. Ownership

The Running Platform owns the actual identity facts exposed by the Platform Identity Surface.

The Platform Identity Surface therefore belongs semantically to:

```text
Running Platform
```

not to:

```text
Deployment request
DeploymentRecord
Platform Instance
Factory
deployment_operations verifier
```

Deployment & Operations may collect and verify evidence from the surface.

Deployment & Operations MUST NOT become the source of truth for actual platform identity merely because it orchestrates the deployment.

---

## 5. Separation of Responsibilities

The architecture has four distinct identity responsibilities.

### Factory

Factory owns authoritative published definitions and metadata, including applicable:

- component publications;
- artifact publications;
- Manifest publications;
- Golden Bundle inventory.

Factory answers:

```text
What does this published identity mean?
```

It does not by itself prove:

```text
What is running now?
```

### Platform Instance

Platform Instance defines the expected composed identity.

It provides:

```text
D_expected
```

### Running Platform

Running Platform provides actual identity-bearing facts through the Platform Identity Surface.

It answers:

```text
What is actually running?
```

### Deployment & Operations

Deployment & Operations verifies the evidence and performs correspondence evaluation.

It answers:

```text
Does the actual Running Platform correspond to this expected Platform Instance?
```

---

## 6. Expected State Is Not Actual Evidence

The Platform Identity Surface MUST NOT be implemented as an echo of expected deployment state.

The following are not actual identity evidence merely because they are stored or transmitted by Deployment & Operations:

- deployment request values;
- expected Platform Instance values;
- DeploymentRecord values copied from the request;
- expected component bindings;
- expected Manifest;
- expected configuration;
- expected Golden Bundle;
- expected artifact metadata.

The architectural direction is:

```text
EXPECTED
Platform Instance
      │
      ▼
D_expected


ACTUAL
Running Platform
      │
      ▼
Platform Identity Surface
      │
      ▼
actual evidence
      │
      ▼
D_actual
```

These two domains meet only at the final correspondence comparison.

---

## 7. Minimum Identity Coverage

The Platform Identity Surface MUST provide, directly or through independently correlated subordinate actual surfaces, sufficient evidence to establish every identity-bearing field participating in the existing Platform Instance canonicalization.

At minimum, the architecture must provide a path for establishing:

```text
platform identity
component membership
component identity
artifact identity
Manifest identity
identity-bearing configuration
Golden Bundle identity
branding
extensions
```

The exact field set remains governed by the existing Platform Instance canonicalization.

This ADR MUST NOT create a second canonical field list.

---

## 8. Complete Component Membership

The Platform Identity Surface MUST provide a mechanism by which complete actual component membership can be established within the existing D&O ownership boundary.

The surface MUST make it possible to distinguish:

```text
actual components = expected components
```

from:

```text
expected components ⊂ actual components
```

Health checks against only expected components are insufficient to establish complete membership.

The absence of an expected additional component from the response set is not, by itself, proof that the additional component does not exist.

An independently established additional identity-bearing component MUST NOT be ignored.

If complete membership cannot be established, ADR-0019 correspondence proof is unavailable.

---

## 9. Component-Level Identity

For every actual component participating in Platform Instance identity, the architecture MUST provide independently grounded actual:

```text
component_id
component_version
```

Component `/health` or an equivalent component-owned identity surface MAY provide these facts.

Such component-level evidence is subordinate evidence for Platform Identity.

It does not require every component to independently publish the complete Platform Instance identity.

---

## 10. Platform-Level Identity

Component-level health is not sufficient to establish the complete Platform identity.

The Platform Identity Surface MUST provide or compose evidence for platform-level facts that cannot safely be inferred from individual component health.

These include, where identity-bearing:

```text
complete membership
Manifest
identity-bearing configuration
Golden Bundle
branding
extensions
```

The architecture therefore distinguishes:

```text
component identity surface
```

from:

```text
platform identity surface
```

A component identity surface MUST NOT be overloaded with unrelated platform-level identity semantics merely to avoid defining the platform-level boundary.

---

## 11. Artifact Identity Boundary

The Platform Identity Surface MUST NOT redefine artifact identity.

Artifact identity remains governed by ADR-0018 and existing Factory artifact semantics.

For actual artifact identity, the architectural evidence chain is:

```text
actual bound artifact
        ↓
independent measurement
        ↓
authoritative Factory publication
        ↓
unique resolution
        ↓
actual artifact identity
```

The Platform Identity Surface may expose or identify the actual artifact binding, but it MUST NOT replace the Factory artifact authority.

---

## 12. Execution Content Is Not Platform Identity

F3B execution-content evidence remains separate.

The following may establish execution integrity:

- bound files;
- content digests;
- loaded modules;
- runtime paths;
- foreign-content refusal;
- worker-side verification.

They do not establish complete Platform Instance identity.

The Platform Identity Surface MUST NOT reinterpret an execution-content digest as:

```text
components[].artifact.digest
```

or as:

```text
D_actual
```

---

## 13. Manifest

The Platform Identity Surface MUST provide a mechanism for establishing the actual active Manifest independently of the expected Manifest.

The actual Manifest may be identified through:

- a Running Platform reference;
- an authoritative runtime surface;
- a trusted attestation;
- another independently grounded mechanism.

The following are not sufficient by themselves:

```text
deployment request
DeploymentRecord
expected Platform Instance
expected Manifest
```

The actual Manifest MUST ultimately resolve to the authoritative Manifest publication according to ADR-0019.

---

## 14. Identity-Bearing Configuration

The Platform Identity Surface MUST provide a mechanism for establishing actual values of configuration fields that participate in Platform Instance identity.

The architecture MUST distinguish:

```text
configuration requested
```

from:

```text
configuration actually active
```

An effective configuration produced by Deployment & Operations is not automatically actual identity evidence.

Operational configuration that does not participate in Platform Instance identity does not need to become part of the identity surface solely because it is observable.

---

## 15. Golden Bundle

The Platform Identity Surface MUST provide a mechanism for establishing which Golden Bundle, if any, is actually active.

Resolution against Factory authority remains governed by ADR-0019.

The actual surface MUST NOT merely repeat the expected Golden Bundle identifier.

The actual bundle evidence and complete authoritative Factory inventory are separate evidence domains:

```text
Running Platform
      ↓
actual active bundle
      ↓
Factory authoritative inventory
      ↓
ADR-0019 resolution
```

Incomplete Factory inventory MUST NOT be interpreted as zero matches.

---

## 16. Branding and Extensions

Where branding or extensions participate in existing Platform Instance canonicalization, the Platform Identity Surface MUST provide actual evidence for those values.

Expected branding or expected extensions are not actual evidence.

Actual absence must be independently established when omission changes identity.

---

## 17. Provenance

Identity-bearing evidence exposed by the Platform Identity Surface MUST be independently grounded.

ADR-0019 provenance classes remain authoritative:

```text
MEASURED
TRANSITIVE
ATTESTED
```

The Platform Identity Surface does not create a fourth provenance class.

Every evidence item must be attributable to:

```text
what was established
how it was established
which Running Platform it describes
when it was established
```

---

## 18. Freshness

Platform Identity Surface evidence MUST be correlated with the Running Platform identity being evaluated.

Evidence from an earlier or different identity-bearing binding MUST NOT be accepted as current actual identity evidence.

Freshness MAY be established through an implementation-specific mechanism such as:

- generation;
- binding identifier;
- runtime identity;
- nonce;
- attestation context;
- equivalent correlation mechanism.

This ADR does not mandate one transport or freshness technology.

It does mandate the semantic property:

```text
evidence describes this Running Platform
```

---

## 19. Uniqueness

The Platform Identity Surface MUST preserve multiplicity until uniqueness has been established.

Implementations MUST NOT silently collapse actual evidence using a map keyed by identity before checking duplicates.

For identity-bearing records where uniqueness is required:

```text
0 records
    → unavailable

1 record
    → candidate

>1 records
    → unavailable
```

unless an independently governed correlation rule proves that the observations represent the same actual fact.

---

## 20. Consistency

All identity-bearing facts obtained through the Platform Identity Surface MUST describe one coherent Running Platform.

Contradictions between:

- component membership;
- component identity;
- artifact identity;
- Manifest;
- configuration;
- Golden Bundle;
- branding;
- extensions

make actual identity unavailable.

The verifier MUST NOT select a preferred value merely because one source is more convenient.

---

## 21. Failure Semantics

The Platform Identity Surface MUST support fail-closed semantics.

If the surface cannot establish a required identity-bearing fact, the verifier MUST NOT manufacture that fact from expected state.

The result is:

```text
PROOF_VALID = false
D_actual = unavailable
```

and ADR-0019 correspondence cannot be asserted.

A proven complete actual identity whose digest differs from expected identity remains:

```text
MISMATCH
```

The Surface MUST NOT collapse:

```text
UNAVAILABLE
```

into:

```text
MISMATCH
```

or:

```text
VERIFIED
```

---

## 22. No Host-Wide Identity Discovery

The Platform Identity Surface does not imply unrestricted host scanning.

Actual identity is established within the existing D&O ownership boundary.

A host-wide scan, broader ownership model, or discovery of unrelated processes is outside this ADR.

Changing the ownership boundary requires a separate architectural decision.

---

## 23. No Second Canonicalization

The Platform Identity Surface does not define a new identity digest.

After evidence validation, ADR-0019 continues to require:

```text
validated actual identity
        ↓
existing Platform Instance canonicalization
        ↓
D_actual
```

This ADR introduces no:

```text
platform_identity_digest
running_platform_digest
runtime_digest
```

as an alternative identity.

---

## 24. Relationship to ADR-0019

ADR-0019 remains authoritative for:

- actual identity semantics;
- identity coverage;
- canonicalization;
- provenance;
- artifact identity;
- Manifest identity;
- configuration closure;
- Golden Bundle resolution;
- freshness;
- consistency;
- fail-closed correspondence.

This ADR adds the architectural ownership boundary required to make those semantics implementable without turning Deployment & Operations into an identity source of truth.

In short:

```text
ADR-0019
    defines what must be proven

ADR-0020
    defines who owns the actual identity surface
    from which that proof can be established
```

---

## 25. Relationship to ADR-0016

ADR-0016 remains authoritative for Deployment & Operations.

The Platform Identity Surface is part of the actual Running Platform evidence boundary required to satisfy the existing identity-correspondence responsibility.

This ADR does not modify:

- deployment lifecycle;
- `ready`;
- `realized`;
- `deployed`;
- provisioning;
- runtime management;
- health/readiness semantics.

---

## 26. Relationship to ADR-0017

ADR-0017 remains unchanged.

The Platform Identity Surface does not add a tenth Deployment & Operations capability.

Deployment & Operations consumes and verifies actual identity evidence as part of its existing responsibilities.

The Surface itself belongs to the Running Platform.

---

## 27. Relationship to ADR-0018

ADR-0018 remains authoritative for:

- closed executable artifact identity;
- Factory artifact boundary;
- artifact digest;
- `artifact_type=none`;
- separation of artifact identity from runtime dependencies.

This ADR does not redefine artifact semantics.

---

## 28. Relationship to F3B

F3B remains the execution-content integrity mechanism.

The Platform Identity Surface does not replace:

- pre-start verification;
- worker-side verification;
- execution binding;
- TOCTOU protection;
- foreign-content refusal.

Conversely, F3B does not replace the Platform Identity Surface.

The two evidence domains remain separate:

```text
F3B
    → execution content integrity

Platform Identity Surface
    → actual Platform Instance identity evidence
```

---

## 29. Implementation Freedom

This ADR is technology-neutral.

The Platform Identity Surface MAY be implemented through:

- a runtime API;
- a local runtime protocol;
- a platform-owned identity document;
- an attestation mechanism;
- a combination of component and platform surfaces;
- another mechanism satisfying the semantic requirements.

The implementation MUST NOT select a mechanism that changes:

- identity coverage;
- ownership;
- provenance;
- freshness;
- uniqueness;
- completeness;
- fail-closed semantics.

Transport is an implementation choice.

Identity semantics are architectural.

---

## 30. Consequences

### Positive consequences

The architecture gains a clear distinction between:

```text
expected identity
actual identity
execution integrity
operational state
```

It becomes possible to implement ADR-0019 without using Deployment & Operations records as an accidental identity authority.

The verifier can remain a verifier rather than becoming a second Factory.

### Costs

The Running Platform acquires an explicit identity-surface responsibility.

Additional runtime evidence and correlation mechanisms may be required.

Some identity-bearing fields that currently exist only in expected state will require new actual-state observation mechanisms.

Actual membership completeness becomes an explicit architectural requirement.

---

## 31. Non-Goals

This ADR does not:

- define a concrete HTTP endpoint;
- define a concrete Python interface;
- define a serialization format;
- define a wire protocol;
- define an attestation technology;
- redefine Platform Instance canonicalization;
- redefine artifact identity;
- redefine F3B;
- redefine deployment lifecycle;
- require host-wide scanning;
- authorize implementation of Issue #90;
- alter ADR-0016, ADR-0017, ADR-0018, or ADR-0019 semantics.

---

## 32. Implementation Authorization

This ADR is a governance decision only while its status is:

```text
PROPOSED
```

No implementation may treat this document as authorization to change the repository architecture.

Implementation may begin only after:

1. this ADR is ratified;
2. the ratified ADR is published;
3. the implementation work item is confirmed to conform to this ADR and ADR-0019;
4. the owner authorizes implementation according to the project's governance process.

---

## 33. Required Implementation Invariant

Any implementation authorized under this ADR MUST preserve:

```text
Running Platform
    ↓
independent actual identity evidence
    ↓
ADR-0019 validation
    ↓
existing Platform Instance canonicalization
    ↓
D_actual
```

and:

```text
CORRESPONDS
iff
PROOF_VALID
and
D_actual == D_expected
```

No implementation may replace an unavailable actual fact with:

- expected state;
- omission;
- inferred absence;
- arbitrary fallback;
- first/last match;
- floating selector;
- execution-content digest.

---

## 34. Decision Boundary

The architectural boundary established by this ADR is:

```text
                Factory
                   │
          authoritative publications
                   │
                   │
Running Platform ──┼── Platform Instance
       │           │          │
       │           │          │
       ▼           │          ▼
Platform Identity  │      D_expected
Surface            │
       │           │
       ▼           │
Actual Evidence ───┘
       │
       ▼
ADR-0019 validation
       │
       ▼
D_actual
       │
       ▼
D_actual == D_expected
```

The critical ownership rule is:

```text
Factory
    owns published definitions

Platform Instance
    owns expected composed identity

Running Platform
    owns actual runtime identity facts

Deployment & Operations
    verifies correspondence
```

---

## 35. Status and Next Step

**RATIFIED — 2026-09-26 by project owner.**

The project owner approved this architectural decision on 2026-09-26.

This document is the repository representation of that ratified decision.

The ratification establishes the Platform Identity Surface ownership and semantic boundary described above.

It does not by itself authorize implementation of Issue #90.

Implementation remains a separate step requiring an approved implementation work item that conforms to ADR-0019 and this ADR.

The following remain unchanged:

- ADR-0016 §10;
- ADR-0017;
- ADR-0018;
- ADR-0019;
- the existing Platform Instance canonicalization;
- the existing artifact identity contract.

---
