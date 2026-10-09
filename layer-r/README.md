# `layer-r/` — Layer R source of truth (owner-side)

**Status: CONTROL SCAFFOLDING — no source payload yet.** This tree exists to put
the owner-maintained Layer R implementation under Git and CI control, so that
the code running on the Running Platform host can be tied to one immutable
commit and drift can be detected without mutation. It is **not** production
evidence, and its mere existence implies no production deployment, no D1–D8
closure, and no change to any ADR or contract.

Authorized by the project owner: PR #138 comment `6077226985`
("bring Layer R under Git and CI control"). Inventory and reconciliation:
PR #138 comment `6077487927`. Scope decisions taken by the owner for the
Phase 2 changeset (this tree): **mechanisms only** — the implementation payload
arrives with the host export (step M1); the tree lives **in this repository**
as source of truth and is deployed on the host **outside the application-factory
checkout** (the D5 loader rule of PR #156 is preserved untouched).

## What is Layer R

The production runtime layer of the S4 model
(`docs/arena/S4-PRODUCTION-RUNTIME-BOOTSTRAP-RUNBOOK.md` §0.1): the cell
manager, the identity producer and the owner-side adapters (`RuntimeAdapter`,
`RunningPlatformIdentitySource`). It is **owner-side** code: Deployment &
Operations reaches it only through the two declared seams and never imports it
into its own authority boundary. Keeping that ownership is a hard constraint of
this tree — see "Ownership rules" below.

## Layout

```text
layer-r/
  README.md              this file
  SOURCE_MANIFEST.json   committed manifest: path → sha256, class, host_path, origin
  source/                class A — version-controlled implementation (created at M1)
  config/                class B/F — deployable configuration, host templates (M1)
```

`source/` and `config/` are intentionally empty until the real host source is
exported (M1). **No stand-in, rehearsal or double is kept here**: the rehearsal
of PR #144 (`src/running_platform_rehearsal/`, explicitly not production), the
test double of PR #156 (`tests/_layer_r_fixtures/`) and the local Docker stack
(`src/running_platform/local_identity_service.py`, `LOCAL DOCKER /
NON-PRODUCTION`) are separate artifacts and are not Layer R source of truth.

## File classification (enforced by the manifest tooling)

| Class | Meaning | In Git? |
|---|---|---|
| `source` | implementation code | yes, hashed in `SOURCE_MANIFEST.json` |
| `deployable-configuration` | declared deployment inputs (e.g. environment binding values) | yes — **never** secrets |
| `host-specific-template` | template of a host-bound file (systemd unit with placeholders) | yes as template |
| *(secret / credential)* | token, key, password | **never** — env var at process boundary only (RF-9) |
| *(mutable state)* | `<PLATFORM_STATE_DIR>` (`platform.json`, `members/`, …) | **never** |
| *(generated / runtime data)* | logs, journals, `__pycache__` | **never** |

`scripts/layer_r_manifest.py verify` refuses unlisted files, hash drift,
missing files, non-UTF-8 content, unclassified files and secret-looking
content. `scripts/layer_r_export_bundle.py` refuses to export anything
secret-looking at all.

## Path and symlink policy (enforced by all three tools)

- Manifest `path` values must be canonical relative POSIX file paths under
  exactly `layer-r/source/` or `layer-r/config/` — segment-wise, not
  prefix-wise (`layer-r/source-evil/` does not match). Absolute paths, `..` /
  `.` segments, empty segments, backslashes, drive letters, trailing slashes
  and any non-canonical spelling are refused; duplicates after normalization
  are impossible because ambiguous spellings are refused first.
- Manifest `host_path` values (and every path the drift check touches) follow
  the same canonical-relative rule under the host cell root.
- **Symlinks and other non-regular files are forbidden** anywhere in
  `layer-r/source`, `layer-r/config` and an export payload: nothing behind a
  symlink is ever followed, hashed or copied, and a resolved path must stay
  inside the expected root. The exporter aborts before copying a single file
  on any violation; the drift check reports `unsafe` and stays read-only.
- **The supplied root itself must not be a symlink** — the tree root, the
  drift-check host root and the export cell root alike. A symlinked root is
  refused *before* any walk, read or hash (`is_dir` alone would follow the
  link): `verify`/`generate` report it as `<root>`, the drift check raises
  `DriftCheckError`, and the exporter raises `ExportRefused`. Symlinked
  *tracked roots* (`layer-r/source`, `layer-r/config`) are refused the same
  way.

## Ownership rules (must hold for every future commit here)

1. **Layer R stays owner-side.** Nothing in `src/deployment_operations` or
   `src/running_platform` may import from `layer-r/`; the D&O composition seam
   loads the owner's factory through the explicit declaration only
   (PR #156 `--layer-r <path|module>:<factory>` semantics; the runbook §10
   step-10 composition root is the other documented form).
2. **No runtime authority moves into D&O.** This tree is storage, review and CI
   for owner code — not a re-homing of runtime ownership (ADR-0020 §4).
3. **No secrets, mutable state or production data** are ever committed
   (classification above; tooling enforces).
4. **The deployed copy lives outside the checkout.** The host serves
   `<RP_CELL_HOME>` (e.g. `/srv/running-platform/cell`); the D5 loader's
   "outside this repository" rule is satisfied by that deployment, while Git
   remains the source of truth the host is installed from.
5. **No production evidence is created here.** Acceptance evidence stays on the
   owner's evidence path (runbook §11 / Issue #128); CI output is wiring
   evidence only.

## Release binding — deployed source ↔ immutable commit

1. **Release** = one Git commit (optionally tagged) at which
   `python -m pytest tests/` and `python scripts/layer_r_manifest.py verify`
   are green in CI. The manifest at that commit *is* the specification of the
   deployed bytes.
2. **Install** (owner, on the host): copy `layer-r/source` + `layer-r/config`
   of that commit into `<RP_CELL_HOME>`, outside the checkout. Record on the
   host (in the state area, never in Git): the commit SHA and the SHA-256 of
   the manifest file.
3. **Prove it** at any later time:

```bash
git -C ~/application-factory show <commit>:layer-r/SOURCE_MANIFEST.json > /tmp/m.json
python3 ~/application-factory/scripts/layer_r_drift_check.py \
    --host-root /srv/running-platform/cell --manifest /tmp/m.json
```

Exit `0` with `verdict: IN SYNC` means the host serves exactly the bytes of
that commit. **CI alone never proves the running host is synchronized** — only
this host-run, read-only drift check does.

## Drift check and rollback

- `scripts/layer_r_drift_check.py` — read-only: reports `in-sync` / `changed` /
  `missing` / `extra` per file; writes nothing to the host; exit `0` clean,
  `1` drift, `2` usage/read error.
- **Rollback** = install the previous release commit on the host (owner action)
  and re-run the drift check against that commit's manifest. Git-side rollback
  is an ordinary revert. No automated host remediation exists or is wanted.

## Staged migration (M0 → M4)

| Step | What | Who |
|---|---|---|
| M0 | this scaffolding lands (CI green) | Arena (Phase 2) |
| M1 | `scripts/layer_r_export_bundle.py` on the host; files placed under `source/`+`config/`; `layer_r_manifest.py generate` with explicit classes; import PR | owner runs export; import reviewed |
| M2 | CI green on the payload; boundary conformance suite pointed at the real source | CI + Arena |
| M3 | release commit; owner installs from the tree to `<RP_CELL_HOME>`; records commit+manifest hash on the host | owner |
| M4 | baseline drift check clean; loop = change → PR → CI → release → install → drift check | owner + Arena |

## CI checks vs owner-host evidence

| Check | Where | Proves | Proves **not** |
|---|---|---|---|
| `tests/test_layer_r_manifest.py` (manifest policy, tools) | CI | tree ↔ manifest consistency at the commit | anything about the host |
| `tests/test_layer_r_boundary.py` (adapter/producer boundary conformance) | CI | fixtures conform / violations are caught; at M1+: the real source conforms | that the host runs it |
| Ruff / Black / `compileall` / full `pytest tests/` (existing Quality Gate) | CI | the whole repository stays conformant | host synchronization |
| `layer_r_drift_check.py` | **owner host, read-only** | host bytes == manifest of one commit | — |
| Runbook §11 step evidence (I1a–I3, A1–A12) | owner host | production acceptance | — (out of scope of this tree) |

## Boundary conformance target hook

`tests/test_layer_r_boundary.py` runs its checks against labelled in-test
fixtures in CI. After M1 the same suite can be pointed at the real payload via
`LAYER_R_CONFORMANCE_TARGET=<absolute-path.py>:<factory>` (D5 declaration
form); with the variable unset, the hook tests skip and CI stays green.
