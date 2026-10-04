# Control Plane (Slice 1) — REHEARSAL OF ARCHITECTURE, NOT A SUBSYSTEM

> Owner authorization: PR #138 comment `5969644992` ratifies the Slice 0
> Decision Package (`5969585498`) and authorizes Slice 1 strictly inside
> the §12 boundary of package `5969585498`: persistence in
> `factory/control_plane/**` only. This package is a **rehearsal of
> architecture, not a subsystem** — it has no API, no intake channels and
> no runtime authority.

## What Slice 1 contains

| Domain | Document | Mutability |
|---|---|---|
| Project | `projects/*.json` | mutable container (title, status, current pointers) |
| Requirements | `requirements/*.json` — **RequirementsVersion** | immutable, append-only predecessor chain |
| Configuration (aggregate) | `configurations/*_index.json` — current pointer only | mutable `current` field |
| Configuration (version) | `configurations/*_c<N>.json` — **ConfigurationVersion** | immutable, append-only predecessor chain |

Documents are built only through `factory_control_plane.documents`
builders, which validate and settle the SHA-256 digest before anything is
written. There is no update path for an immutable version: any technical
change creates a **new** ConfigurationVersion/RequirementsVersion with a
new digest and a `predecessor` link.

## Design decisions (ratified)

- **Requirements are the canonical convergence target.** Every intake
  channel (form / NL / import / API — Slice 2+) normalizes into the
  structured RequirementsVersion shape; NL text is never stored as a
  parallel source of truth in this package.
- **Selector-free, exact-version configuration only.** Every entry in
  `components` pins an exact selector-free version (no `^`, `~`, `*`,
  ranges); validation refuses range operators.
- **Registry stays canonical.** Component identity, versions and
  lifecycle live exclusively in `factory/registry/component_registry.json`.
  This package only reads it (`registry_reference.py`); every
  ConfigurationVersion reference is cross-checked **on the authoritative
  validation/build path** (`new_configuration_version` →
  `configuration_version_errors` → `component_reference_errors`) and any
  disagreement — unknown id, version mismatch, lifecycle state other than
  `registered`, or an unreadable registry — fails closed. Configuration
  documents carry references, never component facts (no `dependencies`,
  `lifecycle`, `artifact`, availability data).
- **Template/Variant references** are explicit nullable fields
  (`template_ref`, `variant_ref`); the Template/Variant domains
  themselves are not implemented in Slice 1.
- **Approval semantics, attachment points only.** Each
  ConfigurationVersion carries `attachments: {proposal, approval, build,
  release}` — all `null` in Slice 1. The Proposal/Approval/Build/Release
  subsystems are not implemented; nothing here grants approval
  authority.

## Desired vs Actual

Slice 1 models **Desired State exclusively**: what should exist (intent
and its technical configuration). It deliberately does **not** read,
store or reason about **Actual State** (deployments, runtime health,
observed identity, drift). Proposal/Approval/Build/Release attachments,
intake endpoints, storage adapters, the control-plane API and any
Actual-state readers are future work outside this package and outside
Slice 1.

## Digest

- Algorithm: SHA-256 over the canonical JSON bytes (sorted keys, compact
  separators `,`/`:`, UTF-8, `ensure_ascii=False`, NaN/Infinity
  forbidden) of the entire immutable version document **minus its
  `digest` field**; rendered as `sha256:<64 lowercase hex>`.
- Identity: the digest covers the full version payload including
  `schema_version`, so a declared-schema change changes the digest.
- Key-order independence: byte-identical regardless of construction or
  parse order.
- Non-substitutability: control-plane digests stand only for
  Requirements/Configuration version documents — they are a separate
  scheme from Manifest/Instance/D&O/artifact digests and are never used
  in place of them.
- Mutable documents (Project, configuration index) carry no digest;
  pointers store the digest of the immutable version they reference.

## Running the tests

```bash
python -m pytest factory/control_plane/tests
```

The repository Quality Gate (`pytest tests/` via
`pyproject.toml:testpaths`) does not collect this subtree; changing
`testpaths` would require editing `pyproject.toml`, which lies outside
the authorized `factory/control_plane/**` boundary. This limitation is
reported explicitly in the Slice 1 implementation report.

## Slice 2 — composition gate (S2-1 / S2-2 / S2-3)

Slice 2 adds the three ratified contracts *additively*. Slice 1 semantics,
schemas and documents are untouched: a `configuration/v1` document stays valid
with its original digest and is never rewritten.

| Contract | Schema | Builder / verifier |
|---|---|---|
| `control-plane/configuration/v2` — the shape the existing Composition Request actually accepts | `schema/configuration_version_v2.schema.json` | `configuration_v2.new_configuration_version_v2`, `configuration_v2.generate_request_payload` |
| `control-plane/proposal/v1` — immutable validation report (findings + state) for one exact configuration | `schema/proposal_version.schema.json` | `proposals.new_proposal_version`, `proposals.verify_proposal_version` |
| `control-plane/approval/v1` — immutable ledger record bound to one exact `(configuration_id, digest)` + proposal digest | `schema/approval_record.schema.json` | `approvals.new_approval_record`, `approvals.new_approval_revocation`, `approvals.ApprovalLedger` |
| `control-plane/composition-request/v1` — the exact Composer payload plus its provenance | `schema/composition_request_record.schema.json` | `composition_requests.new_request_record`, `composition_requests.verify_request_record` |

Boundaries this slice keeps:

- **v1 fails closed.** A `configuration/v1` version is valid, and composition
  refuses it deterministically (`CP-CONF-V1-NOT-PROJECTABLE`); there is no
  conversion, coercion or compatibility shim.
- **Approval is a ledger, not a flag.** Records are immutable after creation;
  revocation appends. There is no update or delete path, and effectiveness is
  always derived from the record, the exact configuration, the exact proposal
  and the chain — never stored.
- **Warnings need an explicit acknowledgement; errors cannot be acknowledged
  or bypassed.** `info` needs nothing. The acknowledged finding ids are part of
  the digest-covered record, so an approval records exactly what was accepted.
- **RBAC attachment point only.** `approvals.AuthorityPolicy` (with the
  permissive `AllowAllAuthority`) is the single seam a future authorization
  layer plugs into; no policy engine, roles or sessions are implemented here.
- **The Composer stays the only composer.** Projection and diagnostics are
  delegated to its public surface; the request record nests the payload
  verbatim and keeps control-plane provenance outside it.
- **Publication is untouched.** Composition ends at a `draft` manifest; the
  manifest lifecycle remains the sole publication authority.

The RBAC seam, the deferred proposal features (alternatives, conflicts,
selection) and the repository Quality Gate's `testpaths` are reported as
out-of-scope for this slice in the implementation report on PR #138.

## S2 trust-boundary hardening (authorized separately)

The S2 hardening addendum makes the verification APIs fail closed on incomplete
proof inputs. Before Approval creation/effectiveness or Composition Request
creation/verification, callers supply the exact Requirements, Project,
Configuration and Proposal documents. The Proposal is schema/digest checked,
its typed `(id, digest)` references and project are cross-checked, and its full
canonical contents are re-derived from those inputs. A Proposal predecessor is
also supplied separately when one is declared. Approval effectiveness further
requires the exact Approval record to be a member of a verified append-only
ledger; a revocation must target one exact earlier, not-yet-revoked record.

The hardening preserves the existing schemas, finding codes, error/warning/info
acknowledgement rules and shipped example bytes/digests. It adds no
Requirements v1 or Configuration v2 semantics, authentication, persistence,
network access or Composer behavior. The guarantee is data binding and
re-derivation, not proof of a human actor's identity: the in-memory ledger must
come from the trusted in-process caller.

**Quality Gate addendum:** the earlier `testpaths` note above records the Slice 1
boundary decision at that time. The separately authorized S2 hardening adds an
explicit `python -m pytest factory/control_plane/tests` step to both the local
PowerShell gate and CI, and includes the Control Plane package in compile
coverage. `pyproject.toml:testpaths` remains unchanged.
