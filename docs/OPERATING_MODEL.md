# Operating Model

**Status:** PROPOSED
**Version:** 1.0.0
**Date:** 2026-09-08

## 1. Purpose

This document defines the operational model for developing `application-factory` through coordinated work of ChatGPT and Arena with GitHub as the single source of truth.

This document does not replace `docs/ARCHITECTURE.md`, does not define system architecture, and does not create architectural laws.

## 2. Source of Truth

GitHub is the sole source of truth for project work.

Information is considered part of the project only when it is recorded in the repository or its GitHub work artifacts.

The authoritative roles of GitHub artifacts are:

- `docs/ARCHITECTURE.md` — technical constitution and sole active architectural law;
- `docs/adr/` — architectural decisions and architectural history;
- GitHub Issues — approved work items, requirements, findings, and decisions requiring tracking;
- Pull Requests — proposed repository changes and review context;
- commits and merged history — factual record of changes;
- canonical project documentation — current documented state of the project.

ChatGPT conversations are not a substitute for GitHub records.

## 3. Roles

### 3.1 ChatGPT

ChatGPT acts as architect, technical product manager, and reviewer.

Responsibilities:

- define and clarify work objectives;
- decompose work into executable GitHub Issues;
- identify applicable architectural rules and ADRs;
- review proposed architectural changes;
- review Arena results for architectural and contractual conformance;
- decide whether a discovered architectural gap requires an ADR or architectural change;
- accept, reject, or request correction of results.

ChatGPT does not silently bypass the repository's architectural governance.

### 3.2 Arena

Arena acts as the implementation and verification executor.

Responsibilities:

- execute approved GitHub Issues;
- inspect the repository and relevant canonical documentation;
- implement requested changes;
- test and verify its work;
- report results through GitHub;
- identify contradictions, risks, missing requirements, and architectural gaps;
- propose changes when the current architecture prevents correct implementation.

Arena must not independently ratify or redefine architectural law.

## 4. Communication Protocol

The normal communication path is:

```text
ChatGPT
   ↓
GitHub Issue / ADR / PR review
   ↓
Arena
   ↓
GitHub PR / Issue comment / verification evidence
   ↓
ChatGPT review
```

Important project decisions must be transferred from conversation into GitHub before they are treated as project truth.

## 5. Work Item Lifecycle

A normal implementation task follows:

```text
PROPOSED
   ↓
READY
   ↓
IN PROGRESS
   ↓
IMPLEMENTED
   ↓
REVIEW
   ↓
ACCEPTED
   ↓
MERGED
```

If execution is blocked:

```text
IN PROGRESS
   ↓
BLOCKED
   ↓
clarification / architectural decision
   ↓
IN PROGRESS
```

The exact GitHub labels or project-board states may evolve without changing this conceptual lifecycle.

## 6. Task Definition Standard

A task assigned to Arena should be sufficiently self-contained to execute from GitHub without relying on hidden chat context.

A substantive Issue should normally contain:

1. Goal.
2. Context.
3. Applicable source-of-truth documents and sections.
4. Scope of work.
5. Constraints and prohibitions.
6. Acceptance criteria.
7. Expected verification.
8. Expected deliverables.

The task must explicitly identify architectural constraints when they matter.

## 7. Pull Request Standard

A PR produced by Arena should make clear:

- which Issue it implements;
- what changed;
- what was verified;
- which tests or checks were performed;
- which documentation was changed;
- whether any architectural gap was discovered;
- whether an ADR is required;
- any remaining risks or limitations.

A PR is a proposed change until it has passed the required review and is merged.

## 8. Arena Report Standard

Arena reports through GitHub, not through an undocumented parallel channel.

A substantive completion report should include:

- status: completed / blocked / partial;
- implemented scope;
- files or components changed;
- verification performed and results;
- applicable architecture/ADR references;
- deviations from the task, if any;
- discovered risks or gaps;
- follow-up work, if required;
- PR reference.

A claim of completion without corresponding GitHub evidence is not considered sufficient project evidence.

## 9. Architectural Gap Protocol

If Arena discovers that correct implementation requires a change to architectural law, Arena must not silently modify the law.

The required path is:

1. Record the gap in GitHub.
2. Explain the conflict or missing rule.
3. Propose the smallest viable resolution.
4. Create or update an ADR when required by `ARCHITECTURE.md`.
5. Wait for architectural disposition before implementing the conflicting change.
6. Continue implementation only under the resulting approved rules.

This prevents implementation pressure from silently changing the constitution.

## 10. Documentation Governance

The project follows the documentation rules established by `ARCHITECTURE.md` and the documentation baseline.

In particular:

- one topic has one canonical document;
- historical documents must not be fabricated;
- obsolete documents should be removed or explicitly retired through the normal governance process;
- architectural claims must point to the canonical architecture or an ADR;
- duplicate competing versions are prohibited.

`OPERATING_MODEL.md` is an operational document. It must not become a second architecture document.

## 11. Completion Rule

A task is complete only when all applicable conditions are satisfied:

- requested implementation or documentation work is present;
- acceptance criteria are satisfied;
- verification has been performed;
- required documentation is synchronized;
- required ADRs exist and are ratified when applicable;
- the result is reviewed;
- the approved PR is merged.

Until merge, the change remains proposed work rather than repository truth on the default branch.

## 12. Authority and Conflict Resolution

When sources conflict, use the following precedence:

1. Current `docs/ARCHITECTURE.md`.
2. Ratified ADRs that govern the relevant architectural decision.
3. Current canonical project documentation.
4. Approved GitHub Issues and PR requirements.
5. Conversation context.

If a lower-level artifact conflicts with a higher-level source, the conflict must be surfaced rather than silently resolved by implementation.

## 13. Non-Goals

This document does not:

- define application architecture;
- select a technology stack;
- authorize premature factory mechanics;
- replace ADR governance;
- grant Arena authority to change architectural law;
- require implementation mechanisms that are not otherwise justified by the current project stage.

## 14. Initial Operating Principle

The project operates according to one simple rule:

> **ChatGPT formulates and reviews. GitHub records. Arena executes and reports. GitHub preserves the result.**

This document itself becomes operational project truth only after review and merge through the normal GitHub process.
