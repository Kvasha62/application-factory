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
  This package only reads it (`registry_reference.py`) to cross-check
  references; configuration documents carry references, never component
  facts (no `dependencies`, `lifecycle`, `artifact`, availability data).
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
