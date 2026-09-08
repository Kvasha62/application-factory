# Application Factory

Application Factory is a platform for developing independent,
self-contained applications that can later be integrated into
larger customer-specific platforms.

## Core principle

Each application must be independently:

* developed;
* run locally;
* tested;
* visually inspected;
* accepted;
* versioned.

## Team

* Owner — Oleg
* Architect / Project Manager — ChatGPT
* Executor — Arena

## Development model

Applications are developed independently and integrated only
through defined contracts and APIs.

## Architecture law

`docs/ARCHITECTURE.md` is the technical law of this project.
It defines component boundaries, data ownership, contracts,
versioning, migrations, tenant isolation and the rules that both
developers and AI coding agents must follow.

Architecture changes are made through ADRs:

```text
docs/adr/          — new ADRs (ADR-0009 and later)
docs/adr.md        — historical ADRs (ADR-0001…0008)
```

Change levels are defined in `docs/ARCHITECTURE.md` §34.1:

* **Level A — implementation.** No ADR required.
* **Level B — local architecture.** Component-level ADR if the decision is significant.
* **Level C — platform architecture.** ADR required.
* **Level D — constitutional change.** ADR + impact analysis + migration plan + explicit ratification of a new ARCHITECTURE.md version.

`docs/ARCHITECTURE.md` must not be edited silently.

## Status

Foundation stage.

Current state: standalone mode (ARCHITECTURE.md §4.1) — the factory
gates (Component Catalog, Golden Bundles, Composer, Release Train)
are not enabled yet, and building them before the gates are reached
would violate LAW-13.

Architecture review of version 1.1.0: `docs/architecture-review-1.1.0.md`.
Proposed constitutional amendments await ratification in
`docs/adr/ADR-0010-architecture-gap-review.md`.
