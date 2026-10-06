# Arena Task — O4-C Running Platform Target Discovery

**Status:** authorized task  
**Mode:** READ-ONLY DISCOVERY  
**Base:** current `main`  
**Canonical governance gate:** Issue #128  
**Related architecture:** ADR-0019, ADR-0020  
**Do not merge this task branch unless separately authorized.**

## Objective

Resolve the current O4-C blocker by determining whether the repository already contains a concretely identified **real Running Platform target** suitable for O4-D independent verification.

Required target tuple:

1. **Environment** — concrete Running Platform environment.
2. **Owner** — concrete party/system responsible for actual Running Platform state.
3. **Producer** — concrete owner-side component/system producing actual Platform Identity evidence.
4. **Evidence Channel** — concrete channel/interface/artifact through which D&O receives that evidence.

Do not invent, infer, or promote any of these from intention-only documentation.

## Scope

Inspect current `main` and relevant governance history, including at minimum:

- `factory/environments/`
- environment bindings and authoritative environment artifacts
- `docs/OPERATING_MODEL.md`
- `docs/arena/S4-PRODUCTION-RUNTIME-BOOTSTRAP-RUNBOOK.md`
- `src/running_platform/owner_state.py`
- `src/running_platform/identity_sources.py`
- `src/deployment_operations/platform_identity.py`
- `src/deployment_operations/platform_identity_source.py`
- relevant production/bootstrap manifests, composition requests, and environment-specific artifacts
- Issue #128 and related governance records
- PR #144 and relevant production-identity integration history where needed.

## Classification

For every candidate target, classify each tuple element as exactly one:

- **CONFIRMED** — supported by concrete repository/governance evidence.
- **DECLARED** — stated as intended/configured, but not evidence of a real external Running Platform.
- **REHEARSAL** — explicitly test/stage/rehearsal only.
- **MISSING** — no concrete evidence found.

Clearly distinguish **target definition** from **real production evidence**. A repository environment binding or manifest may define a target without proving that the corresponding real Running Platform exists or is currently producing owner-supplied actual identity.

## Independence checks

Verify that any candidate production path:

- does not derive actual identity from `D_expected`, Platform Instance, DeploymentRecord, runtime bindings, or other expected-state copies;
- does not introduce host-wide discovery;
- does not introduce a second canonicalizer or second identity authority;
- preserves the existing D&O acceptance seam;
- has a concrete owner-side source of actual facts;
- can provide provenance, correlation, freshness, multiplicity/completeness, and contradiction/fail-closed evidence required by ADR-0020.

## Special prohibition

**Do NOT treat PR #144 as production evidence.**

PR #144 is explicitly REHEARSAL / NOT PRODUCTION, draft, and not a real environment/owner/producer proof.

Do not merge it, modify it, or convert it into production evidence.

## Deliverable

Do not modify source code, ADRs, Issues, PR metadata, workflows, production configuration, or runtime state.

Return a report through the canonical Arena communication channel (PR #138) containing:

### A. Target table

| Element | Value | Classification | Exact evidence |
|---|---|---|---|
| Environment | ... | ... | ... |
| Owner | ... | ... | ... |
| Producer | ... | ... | ... |
| Evidence Channel | ... | ... | ... |

### B. Evidence map

For every CONFIRMED item give:

- exact file/path or GitHub object;
- exact ref/SHA where applicable;
- relevant section/symbol;
- why it proves the claim.

For DECLARED/REHEARSAL/MISSING items explain why they do not satisfy O4-C.

### C. O4-C verdict

Return exactly one:

- **READY FOR O4-D** — all four tuple elements are concretely established and independently verifiable; or
- **BLOCKED** — at least one required element remains unconfirmed.

If BLOCKED, identify the exact missing owner decision(s), without making them yourself.

### D. O4-D preparation

If READY FOR O4-D, provide a precise read-only verification plan for the actual evidence.

If BLOCKED, provide the minimal owner decision record required to unblock:

`Environment → Owner → Producer → Evidence Channel`

## Evidence discipline

Every conclusion must be backed by exact refs/SHA, file paths, commands/searches, and observed results.

Do not claim production execution, real external producer existence, owner approval, CI success, deployment acceptance, or O4 closure unless directly evidenced.

**No code or governance mutations are authorized by this task.**
