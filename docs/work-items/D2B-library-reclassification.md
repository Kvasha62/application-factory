# D2=B Work Item — Component Reclassification of `idempotency` and `saga`

- **Status:** Proposed — awaiting owner approval
- **Level:** C — platform architecture (`docs/ARCHITECTURE.md` §34.1: changes component boundaries and factory behavior)
- **Governance basis:** ADR-0021 §4 — "The implementation work following ratification MUST, as a separate Level-C change set where required"; ADR-0017 §44
- **Decision basis:** ADR-0021 (ratified 2026-10-05); owner decision **D2=B**, separated from the F-5 implementation slice by owner decision **D-A** (PR #138 comment `5991426192`)
- **Implementation authorization:** None until this work item is explicitly approved by the owner

This work item exists because ADR-0021 §4 assigns the D2=B consequences to a **separate** Level-C change set, and ADR-0021 §5 bounds the F-5 slice to "only the minimum mechanism required to satisfy the selected D1 decision". It is therefore deliberately **not** part of `docs/work-items/F-5-runtime-dependency-enforcement.md`.

## 1. Objective

Classify `idempotency` and `saga` as **shared in-process libraries** rather than independently deployed Level-0 runtime components, and make that classification consistent and machine-verifiable across the registry, the derived catalog, the canonical documentation and any affected Platform Instance definition.

The classification matches the code as it exists: all four registry edges to `idempotency` are `kind=internal-consumer-surface` and none is `kind=api`; `idempotency` publishes only `IdempotencyGuard` / `IdempotencyRecord` / `IdempotencyConflict` (`src/idempotency/__init__.py`) and is consumed by direct import; `saga` has no `src/saga/api.py`, no published contract surface and no runtime dependents in `src/`.

## 2. In scope

1. **Registry schema.** Extend the `class` enumeration in `factory/registry/schema/component_registry.schema.json` so a shared-library classification is representable. The current enumeration is `["business_system", "platform_service", "computational_service", "streaming_pipeline"]` and contains no library class.
2. **Canonical registry.** Classify `idempotency` and `saga` consistently in `factory/registry/component_registry.json`; both are currently `class: "platform_service"` with `lifecycle.deployable: false` and `publishability_blockers: ["deployment_artifact_absent", "migrations_absent"]`.
3. **Derived catalog.** Apply the same classification in `factory/catalog/component_catalog.json`, which is derived from the registry (`derived_from`) and carries a per-entry `entry_digest` that changes with the classification.
4. **Canonical documentation.** Update the canonical documentation that currently states the Level-0 component set — `docs/DOCUMENTATION_BASELINE.md` §4 records nine Level 0 components including `idempotency` and `saga` — and the applicability of the component Definition of Done in `docs/ARCHITECTURE.md` §30, which requires a deployment artifact and migrations from every component.
5. **Platform Instance membership.** Remove `idempotency` from Platform Instance membership where it is currently represented as an independently deployed member. It is a member of `factory/platform_instance/example_instance.json` (7 members).
6. **Instance identity.** Recompute and explicitly record the affected Platform Instance digest rather than mutating the existing one in place. Removing `idempotency` from that instance changes its digest, because the digest is the `sha256` of the canonical JSON of the instance content (`src/platform_instance/instance.py`, `compute_instance_digest`). Per ADR-0016 §5 and §7 the result is a **new** Platform Instance, not an edited one.
7. **Machine-verifiable boundaries.** Update tests and boundary checks so the new classification is machine-verifiable rather than documented only.

## 3. Explicitly out of scope

- The F-5 runtime dependency mechanism (network transport, endpoint binding, provider-side listener, start order) — that is `docs/work-items/F-5-runtime-dependency-enforcement.md`.
- New published contracts, network contracts or deployment modules for `idempotency` or `saga`.
- Any change to business or data ownership.
- Any change to the rule that the environment must provide a runtime binding for every instance member (`src/deployment_operations/environment.py`, `validate_environment`). The contradiction is resolved by **membership**: a shared library is not an instance member and therefore needs no binding. The rule itself is not weakened.
- Publication of artifacts, OCI/GHCR changes, workflow changes, production changes.
- Migrations. No migration declaration exists anywhere in `src/`; introducing them is not part of this classification.

## 4. Acceptance criteria

The change set is acceptable only if all are demonstrated:

1. The registry schema can represent a shared-library classification, and the schema remains valid for every existing entry.
2. `idempotency` and `saga` carry that classification in the canonical registry, and no other component's classification changes.
3. The derived catalog is consistent with the registry, and its entry digests are recomputed rather than left stale.
4. `docs/DOCUMENTATION_BASELINE.md` and any other canonical document stating the Level-0 component set agree with the registry.
5. No Platform Instance declares `idempotency` or `saga` as an independently deployed member.
6. The affected Platform Instance digest is recomputed and the resulting instance is recorded as a new instance with its own digest; the prior instance document is not edited in place.
7. The classification is enforced by tests, so a later re-introduction of a library as an independently deployed member fails a check rather than passing review.
8. The environment-binding completeness rule is unchanged and still refuses an instance member with no binding.
9. Component identity semantics are unchanged: `component_id` is stable, never reused, and no floating selector is introduced (registry `conventions.identity`, `conventions.versioning`).
10. Quality Gate passes.
11. Independent review is recorded.
12. Owner approval is recorded before merge.

## 5. Deliverables

- Registry schema, registry and catalog changes on a dedicated branch/work item.
- Canonical documentation updates.
- The recomputed Platform Instance artifact, recorded as a new instance.
- Tests enforcing the classification.
- Quality Gate result.
- Independent review result.
- Owner approval record.

## 6. Governance gate

This work item is **Proposed**. It is a separate Level-C change set as required by ADR-0021 §4 and a separate implementation slice as required by ADR-0017 §44.

**No implementation, merge, or release action is authorized until the owner explicitly approves this work item.**

After approval, the change set must remain bounded by this document and ADR-0021 §4. Any material scope expansion requires a new owner decision.

This work item is independent of the F-5 implementation slice: the F-5 instance `{tenant_authority, identity}` contains neither `idempotency` nor `saga`.

## 7. References

- ADR-0021 §4 — D2=B consequences are a separate Level-C change set; §5 — the F-5 slice is bounded to the D1 mechanism.
- ADR-0017 §44 — separate approved work item and acceptance criteria for later implementation slices.
- ADR-0016 §5, §7 — a Platform Instance is immutable; a different composition is a different instance.
- `docs/ARCHITECTURE.md` §30 (component Definition of Done), §34.1 (Level C).
- `docs/work-items/F-5-runtime-dependency-enforcement.md` — the F-5 implementation work item, from which this change set was separated.
- Permanent PR #138 — owner-gate and Arena communication channel; owner decisions D2=B and D-A recorded in comments `5988005048` and `5991426192`.
