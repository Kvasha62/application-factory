# ADR-0019 — Running Platform Identity Correspondence

**Status:** RATIFIED
**Ratified:** 2026-09-26 by project owner
**Decision level:** C
**Scope:** Deployment & Operations / Platform Instance identity
**Related:** ADR-0016, ADR-0017, ADR-0018

---

## 1. Decision

A Running Platform corresponds to an expected Platform Instance only when the identity of the actual Running Platform has been independently established and corresponds to that specific expected Platform Instance.

The normative correspondence predicate is:

```text
CORRESPONDS :=
    PROOF_VALID
    ∧ D_actual == D_expected
```

If actual identity cannot be established completely and validly:

```text
D_actual = unavailable
```

and correspondence MUST NOT be asserted.

If actual identity is independently established and:

```text
D_actual != D_expected
```

the result is a proven identity mismatch.

---

## 2. Problem

ADR-0016 distinguishes desired Platform Instance state from actual Running Platform state and requires verification that the actual Running Platform corresponds to a specific Platform Instance.

Existing deployment evidence can establish substantial properties of the expected Platform Instance and its realization, including component realization, execution-content integrity, health, readiness, and desired-input integrity.

Those properties do not by themselves establish the complete identity of the actual Running Platform.

The missing semantic closure is:

```text
Actual Running Platform
        ↓
complete actual identity evidence
        ↓
existing Platform Instance canonicalization
        ↓
D_actual
        ↓
D_actual == D_expected
```

---

## 3. Expected Platform Instance Identity

The expected Platform Instance is the desired identity against which the actual Running Platform is evaluated.

The fields participating in that identity are determined by the existing Platform Instance canonicalization.

This ADR does not establish a second field list or alternative identity schema.

The authoritative question is which content is included by the existing:

```text
instance_content_for_digest
```

and the existing canonicalization contract.

---

## 4. Actual Platform Instance Identity

The actual Platform Instance identity is established from independently grounded evidence about the actual Running Platform.

Expected Platform Instance data MUST NOT be copied into actual identity.

The actual identity is established by determining the actual values of all identity-bearing content and applying the existing Platform Instance canonicalization.

This produces:

```text
D_actual
```

The expected Platform Instance already provides:

```text
D_expected
```

Correspondence requires equality between these independently established values.

---

## 5. Identity Coverage

Actual identity proof MUST cover every field that participates in the existing Platform Instance canonicalization.

An optional property may be omitted from actual identity proof only when actual evidence independently establishes its absence.

Expected absence is not actual absence.

Silence is not evidence.

The existing canonicalization excludes only:

```text
instance_digest
$schema
```

No additional identity exclusions are introduced by this ADR.

---

## 6. Existing Canonicalization Is Authoritative

The existing Platform Instance canonicalization is authoritative for both expected and actual identity.

The canonicalization contract includes the existing rules for:

- content selection;
- canonical JSON representation;
- ordering;
- serialization;
- digest calculation.

The implementation mechanism or function names are not themselves architectural semantics.

A second canonicalization, alternative digest formula, or independent identity representation MUST NOT be introduced by implementation.

Any change to the existing Platform Instance canonicalization requires a separate architectural decision.

---

## 7. Actual Provenance

Every actual identity-bearing value MUST have provenance grounded in actual Running Platform evidence.

Valid provenance may be:

- direct actual evidence;
- measured actual evidence followed by a unique authoritative metadata resolution;
- trusted attestation of the complete actual identity where such attestation is independently grounded in actual evidence.

Expected Platform Instance values MUST NOT become actual provenance merely because they are expected.

---

## 8. Provenance Classes

Actual evidence MAY distinguish between:

### MEASURED

A value is established by measuring the actual identity-bearing content or binding being evaluated.

For artifact identity, the digest is measured from the actual bound canonical artifact unit before any publication lookup is performed.

### TRANSITIVE

A value is derived from independently measured actual evidence through a unique authoritative publication or registry record.

Transitive resolution does not replace the measurement that establishes the identity-bearing digest.

### ATTESTED

A trusted authority attests to the complete actual identity represented by `D_actual`.

Attestation is not a mechanism for independently filling one missing identity-bearing field.

An attestation MUST NOT be used to turn expected data into actual data.

The authority and attestation mechanism are implementation concerns unless separately governed; this ADR does not delegate new identity semantics to an implementation work item.

---

## 9. Artifact Identity

`components[].artifact.digest` identifies the sealed artifact unit under the existing Factory artifact contract.

It is not the digest of:

- a loaded source file;
- a shared library;
- an execution closure;
- an arbitrary directory;
- an arbitrary runtime file set;
- an F3B execution-content set.

Artifact identity remains the identity of the sealed canonical artifact representation defined by existing Factory semantics.

---

## 10. Actual Artifact Digest Provenance

For an actual non-null artifact digest, the digest MUST first be measured from the actual bound canonical artifact unit under the existing Factory representation.

Only after that measurement MAY the measured digest be used to resolve authoritative publication metadata.

The lookup MUST be unique.

The lookup does not itself establish the artifact digest.

The normative sequence is:

```text
actual bound canonical artifact unit
        ↓
measure digest
        ↓
authoritative publication lookup by measured digest
        ↓
unique record
        ↓
artifact metadata
```

The result is:

```text
0 matching authoritative records
    → D_actual = unavailable

1 matching authoritative record
    → unique publication metadata for the independently measured artifact

>1 matching authoritative records
    → actual artifact identity unavailable
```

A component identifier/version lookup MUST NOT establish a non-null actual artifact digest by itself.

---

## 11. `artifact_type=none`

`artifact_type=none` denotes the absence of a sealed artifact identity only when that absence is independently established from actual evidence.

Silence does not establish `none`.

The expected Platform Instance MUST NOT be used to copy:

- the `artifact_type=none` tuple;
- `digest=null`;
- `pinned=false`;
- `canonical_form=null`;
- or any part of that tuple

into actual identity.

Where actual evidence establishes that no sealed artifact unit is present, the existing canonical representation is:

```text
artifact_type = none
digest = null
pinned = false
canonical_form = null
```

If neither presence nor absence of a sealed artifact identity is independently established, actual artifact identity is unavailable.

---

## 12. Execution Content Identity

Execution-content evidence is distinct from Platform Instance artifact identity.

F3B execution evidence may establish:

- which files were bound;
- which bytes were measured;
- which modules were loaded;
- whether loaded bytes match the binding;
- whether foreign or unbound content entered execution.

Such evidence is required where applicable for execution integrity.

It does not establish or substitute for:

```text
artifact.digest
```

The digest of a loaded file, execution closure, runtime module set, or execution-content set MUST NOT be used as:

```text
components[].artifact.digest
```

Execution-content evidence does not constitute `D_actual` and does not independently establish Platform Instance identity.

Any future decision changing this relationship requires a separate ratified ADR.

---

## 13. Manifest Identity Closure

If Manifest identity participates in Platform Instance identity, actual correspondence MUST establish the authoritative actual Manifest.

The actual Manifest identity includes the applicable:

```text
manifest_id
manifest_version
manifest_digest
manifest_state
```

The expected Manifest, request Manifest, or runtime copy of desired Manifest data MUST NOT be used as the actual Manifest anchor.

---

## 14. Manifest Uniqueness

Actual Manifest resolution MUST use the authoritative publication corresponding to the independently established actual Manifest identity.

The normative cases are:

```text
0 authoritative publications
    → actual Manifest identity unavailable

1 authoritative publication
    → unique actual Manifest

>1 authoritative publications
    → actual Manifest identity unavailable
```

When a unique publication exists:

```text
manifest_digest
```

MUST be established from that publication according to the existing Manifest canonicalization.

`manifest_state` MUST be taken from that authoritative publication.

`ready`, `realized`, `running`, deployment state, or equivalent operational state MUST NOT substitute for `manifest_state`.

The implementation MUST NOT resolve ambiguity using:

- `latest`;
- `current`;
- request order;
- runtime order;
- first match;
- last match;
- arbitrary precedence.

---

## 15. Configuration Closure

Configuration values participating in Platform Instance identity MUST be established from actual evidence.

If a configuration value is included in Platform Instance canonicalization, its actual value MUST be established before `D_actual` can be computed.

An effective configuration hash MUST NOT replace the Platform Instance digest where effective configuration contains environment overlays or values outside Platform Instance identity.

Actual configuration identity follows the existing Platform Instance canonicalization.

---

## 16. Configuration Consistency

Conflicting configuration evidence makes actual identity proof unavailable.

An implementation MUST NOT:

- choose one conflicting value;
- prefer expected configuration;
- prefer runtime configuration;
- discard conflicting observations;
- silently normalize conflicts;
- fall back to the expected value.

Actual configuration evidence must be internally consistent before it participates in `D_actual`.

---

## 17. Component Membership

The actual Platform Instance component set MUST be established independently within the applicable D&O ownership boundary.

The expected component list is not sufficient evidence of actual membership.

The following is insufficient:

```text
EXPECTED_COMPONENT_SET
    ⊆
MANAGED_ACTUAL_SET
```

Actual membership must also establish that no additional identity-bearing component exists within the relevant ownership boundary.

An independently established extra member MUST NOT be silently ignored.

---

## 18. Actual Component Identity and Independent Membership

Each actual component participating in Platform Instance identity MUST have independently grounded:

- `component_id`;
- `component_version`;
- artifact identity;
- applicable identity-bearing metadata.

Actual component identity MAY be established:

- directly from verified actual evidence;
- transitively through a unique authoritative publication after independently measured artifact identity;
- through a valid attestation of actual identity.

The expected component entry MUST NOT be copied into the actual component set.

---

## 19. Membership Completeness

Actual component membership MUST be complete within the ownership boundary relevant to the correspondence claim.

Silence is not evidence of absence.

If actual membership cannot be established completely within that boundary, actual identity is unavailable:

```text
D_actual = unavailable
```

This ADR does not require a host-wide scan.

A broader ownership boundary is not created by implementation choice. Defining a different ownership boundary requires a separate architectural decision.

---

## 20. Golden Bundle Closure

Golden Bundle identity is determined from the complete actual component membership, actual component versions, and actual artifact identities according to existing Factory Golden Bundle semantics.

Complete actual inventory means the entire authoritative Factory Golden Bundle inventory against which the actual pin identity is evaluated.

It is not:

- a candidate set selected from expected Platform Instance;
- a search limited to expected `golden_bundle`;
- a search limited to expected bundle identifier;
- a search limited to expected bundle membership;
- a search limited to expected Instance digest;
- an equivalent expected-state selector.

A prefilter is allowed only if semantically equivalent to complete authoritative inventory for actual pin identity and MUST NOT exclude any Golden Bundle whose:

- component membership;
- component versions;
- artifact identities

are equal to the actual inventory used to resolve the actual pin identity.

Incomplete actual inventory means:

```text
D_actual = unavailable
```

The normative cases are:

```text
complete inventory + 0 matches
    → explicit uncertified/null bundle state

complete inventory + 1 match
    → unique actual Golden Bundle

complete inventory + >1 matches
    → unavailable

incomplete/unavailable inventory
    → unavailable
```

Incomplete inventory MUST NOT be interpreted as zero matches or as a unique match.

No implementation work item may introduce a different zero-match, fallback, precedence, or disambiguation rule.

---

## 21. Branding Closure

If branding participates in Platform Instance identity, actual branding MUST be established from actual Running Platform evidence.

Expected branding is not actual branding.

If branding is absent from actual identity, that absence MUST be independently established.

Operational irrelevance does not establish actual absence.

---

## 22. Extensions Closure

If extensions participate in Platform Instance identity, actual extension membership and identity MUST be established independently.

An extension MUST NOT be omitted merely because it has no execution effect.

Expected extension absence is not actual extension absence.

The actual extension set must be complete within the applicable ownership boundary.

---

## 23. Actual Evidence Cannot Be Contaminated by Expected State

Expected state is the comparison target, not a source of actual evidence.

Implementation MUST NOT:

- copy missing actual component identity from expected components;
- copy missing actual Manifest identity from expected Manifest;
- copy missing actual configuration from expected configuration;
- infer actual branding absence from expected branding absence;
- infer actual extension absence from expected extension absence;
- select a Golden Bundle using expected identity as if it were actual evidence;
- use expected artifact identity as measured actual artifact identity.

Expected and actual identity remain separate until the final correspondence comparison.

---

## 24. Proof Models

Two proof models are architecturally valid.

### Reconstruction

```text
Actual Running Platform
        ↓
component evidence
        ↓
execution evidence
        ↓
artifact evidence
        ↓
Manifest evidence
        ↓
configuration evidence
        ↓
composition metadata
        ↓
Actual Identity
        ↓
existing Platform Instance canonicalization
        ↓
SHA256
        ↓
D_actual
```

### Attestation

```text
Running Platform
        ↓
actual evidence
        ↓
trusted composition authority
        ↓
attestation(D_actual)
        ↓
D_actual
```

Reconstruction is descriptive, not mandatory.

Attestation may establish the same `D_actual`, provided the attestation is grounded in actual evidence and covers the complete actual identity.

---

## 25. Attestation Constraints

Attestation MAY establish `D_actual` when the attestation is grounded in actual Running Platform evidence and covers the complete identity required by this ADR.

Attestation MUST NOT be used to fill an individual missing identity-bearing field from expected state.

An attestation that merely repeats:

```text
D_expected
```

without independent actual grounding is not actual identity evidence.

This ADR does not establish a new attestation authority or governance mechanism.

---

## 26. Evidence Freshness

Actual identity evidence is current only when it describes the same identity-bearing binding being evaluated.

Evidence becomes stale when the identity-bearing state it describes has changed, including changes to:

- component membership;
- bound artifact unit;
- identity-bearing configuration;
- composition metadata.

Evidence from a different binding, different composition, or earlier identity-bearing state MUST NOT be treated as evidence for the current correspondence decision.

The mechanism by which implementation detects such changes is implementation-defined, but the semantic condition is not.

Stale evidence makes actual identity proof unavailable.

---

## 27. Evidence Inconsistency

Any internally inconsistent identity evidence MUST make actual identity proof unavailable.

Examples include:

- conflicting component versions;
- conflicting artifact identities;
- conflicting Manifest identity;
- conflicting configuration identity;
- conflicting component membership;
- conflicting Golden Bundle resolution;
- contradictory actual evidence and attestation.

The implementation MUST NOT resolve conflicts through arbitrary precedence.

---

## 28. Fail-Closed Correspondence

Correspondence MUST fail closed.

The following states MUST NOT produce a positive correspondence result:

```text
actual identity unavailable
actual identity incomplete
actual evidence stale
actual evidence inconsistent
artifact identity unavailable
Manifest identity unavailable
component membership incomplete
Golden Bundle inventory incomplete
D_actual unavailable
```

If independently established:

```text
D_actual != D_expected
```

the result is a proven mismatch.

Mismatch MUST NOT be converted into unavailable.

Unavailable MUST NOT be converted into match.

---

## 29. Relationship to Deployed

ADR-0016 §10 remains authoritative.

This ADR does not create a new definition of `deployed`.

The identity-correspondence condition clarified here is:

```text
complete actual identity coverage
∧ valid actual provenance
∧ internally consistent evidence
∧ current evidence
∧ D_actual available
∧ D_actual == D_expected
```

This condition is part of the correspondence verification already required by ADR-0016 §10.

Health/readiness and other deployment conditions remain governed by ADR-0016.

---

## 30. Relationship to ADR-0016

ADR-0016 remains authoritative for Deployment & Operations semantics.

This ADR closes the identity-correspondence gap between:

```text
actual Running Platform
```

and:

```text
specific expected Platform Instance
```

It does not modify ADR-0016 §10.

It does not establish an independent deployed model.

---

## 31. Relationship to ADR-0017

ADR-0017 remains unchanged.

This ADR clarifies the actual-identity correspondence required by the existing ADR-0016 §10 contract.

It does not add a new independent D&O responsibility or alter the deployment model defined by ADR-0017.

Existing ADR-0017 realization and verification requirements remain in force.

---

## 32. Relationship to ADR-0018

ADR-0018 remains authoritative for closed executable artifact identity.

In particular:

```text
components[].artifact.digest
```

continues to identify the sealed executable artifact under the existing Factory contract.

Execution-content evidence remains separate from sealed artifact identity.

This ADR does not redefine artifact canonicalization.

---

## 33. Ownership Boundary

Actual component membership and identity are evaluated within the existing D&O ownership boundary relevant to the correspondence claim.

The ownership boundary is not expanded by implementation choice.

A broader ownership boundary, host-wide scan, or different scope of actual inventory requires a separate architectural decision.

Within the applicable boundary:

- actual membership must be independently established;
- extra members must not be ignored;
- incomplete membership must fail closed.

---

## 34. Zero-Match Semantics

Zero-match results MUST preserve the distinction between:

```text
no actual match exists
```

and:

```text
actual identity could not be established
```

For Golden Bundle resolution:

```text
complete inventory + 0 matches
    → explicit uncertified/null bundle state
```

whereas:

```text
incomplete inventory
    → unavailable
```

Likewise:

```text
0 authoritative Manifest publications
```

does not establish a unique actual Manifest.

No implementation may collapse these states for convenience.

---

## 35. Consequences

This ADR makes explicit that successful execution, health, readiness, or execution-content integrity do not by themselves prove complete Platform Instance identity.

The architecture therefore distinguishes:

```text
desired identity
actual identity
execution integrity
health/readiness
lifecycle state
```

The result is a fail-closed identity correspondence decision rather than an inference from partial realization evidence.

---

## 36. Implementation Freedom

This ADR defines semantic requirements, not a mandatory implementation mechanism.

Implementation MAY use transport or storage mechanisms such as:

- runtime APIs;
- deployment records;
- event records;
- deployment state;
- attestations;
- composition metadata;
- authoritative registries.

However, a transport or storage record is actual identity evidence only when the value carried by that record already has independent actual provenance under this ADR.

In particular, the following do not become actual evidence merely by being recorded:

- `DeploymentRecord.instance_digest`;
- request payload values;
- runtime copies of desired specification;
- expected Platform Instance fields.

An existing deployment record containing `D_expected` does not establish `D_actual`.

Implementation freedom MUST NOT redefine identity semantics.

---

## 37. Explicit Non-Decisions

The following decisions are closed:

- execution-content evidence does not become Platform Instance identity;
- expected state is not a source of actual identity evidence;
- this ADR does not introduce a second canonicalization;
- this ADR does not modify ADR-0016 §10;
- implementation work items may not define new identity semantics by implementation choice.

Questions outside the scope of this ADR include:

- whether every component must expose a complete Platform Instance;
- whether a runtime API is required for every identity-bearing field;
- whether local reconstruction is required;
- whether a particular attestation authority is required;
- whether a broader ownership boundary is required.

These questions MUST NOT be used to weaken or bypass the identity and fail-closed rules established here.

Descriptive implementation examples cannot reopen closed architectural decisions.

---

## 38. Decision Boundary

The semantic boundary is:

```text
Expected Platform Instance
        │
        │ comparison target
        ▼
D_expected

Actual Running Platform
        │
        │ independently grounded evidence
        ▼
Actual Identity
        │
        │ existing canonicalization
        ▼
D_actual

Correspondence
        │
        ▼
D_actual == D_expected
```

The implementation work item operates inside this boundary.

It may choose mechanisms for obtaining and transporting evidence but may not redefine what constitutes actual identity, actual provenance, completeness, freshness, or correspondence.

A new identity rule or canonicalization requires a separate architectural decision.

---

## 39. Required Implementation Invariant

Any implementation of this ADR MUST preserve:

```text
CORRESPONDS
iff
    PROOF_VALID
    and
    D_actual == D_expected
```

`PROOF_VALID` requires, at minimum:

```text
actual evidence independently grounded
∧ coverage of every identity-bearing field
∧ independently established absence for omitted optional identity fields
∧ valid provenance for actual component identities
∧ complete artifact state
∧ complete actual membership within the applicable ownership boundary
∧ complete authoritative Golden Bundle inventory
∧ normative Golden Bundle resolution
∧ unique Manifest where required
∧ internally consistent evidence
∧ current evidence
∧ existing Platform Instance canonicalization
∧ D_actual available
```

No implementation may replace unavailable evidence with expected state, omission, fallback, or inferred equivalence.

---

## 40. Status

**RATIFIED — 2026-09-26 by project owner.**

The semantic decision represented by ADR-0019 was ratified by the project owner on 2026-09-26 in Issue #89.

This document is the repository representation of that ratified decision.

The ratification does not authorize implementation by itself. Implementation remains a separate step requiring an approved implementation work item.

The following remain unchanged:

- ADR-0016 §10;
- ADR-0017;
- ADR-0018;
- the existing Platform Instance canonicalization;
- the existing artifact identity contract.

The implementation work item MUST implement the semantics of this ADR and MUST NOT redefine identity semantics.

---

