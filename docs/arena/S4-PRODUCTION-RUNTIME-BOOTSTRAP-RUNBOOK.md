# S4 — Production Runtime Bootstrap Runbook (owner-executable)

**Status:** PLAN / RUNBOOK — not executed. No bootstrap, deployment or production mutation has been performed.
**Revision:** 2 — ownership correction (`production runtime ownership ≠ D&O orchestration`).
**Supersedes:** the runbook published as PR #138 comment `5978947440` (revision 1), which made `deploy()` the de facto owner of the production runtime lifecycle.
**Anchor convention:** an unqualified `file.py:NNN` anchor means `src/deployment_operations/file.py:NNN`. Anchors outside that package are qualified (`tenant_authority/…`, `running_platform/…`, `platform_instance/…`).
**Normative sources:** ADR-0016 §4, §18; ADR-0017; ADR-0019; ADR-0020 §4, §5; `docs/ARCHITECTURE.md` §37, §37.1.
**Preserved seam (unchanged):** `Running Platform → owner producer → owner-side adapter → ActualPlatformSnapshot → existing validation → project_actual_identity → existing compute_instance_digest → D_actual → comparison with expected instance_digest`.

---

## 0. Ownership model — normative for this runbook

Two layers, two owners, two seams. Everything else in this document is derived from this section.

### 0.1 Layer R — Running Platform (the production runtime layer)

**Owner:** the Running Platform owner (`<RP_OWNER_USER>` / `<RP_OWNER_HANDLE>`, ADR-0020 §4).

Layer R is a **standing** production runtime layer. It is created, started, supervised and kept alive by its own owner, **before** any Deployment & Operations call and **independently of** whether such a call ever happens. It owns:

| Layer R owns | Meaning |
|---|---|
| The runtime substrate | The supervisor and the member slots; the existence and lifetime of member runtimes |
| Runtime lifecycle machinery | create / start / stop / restart / health **of the runtime itself**, under Layer R's own policy |
| The platform state directory | `<PLATFORM_STATE_DIR>` — actual membership, actual composition, actual manifest/configuration/branding/extension state |
| The Platform Identity Surface | The producer that establishes the nine actual identity-bearing facts (ADR-0020 §4, §7) |
| Survival | Layer R outlives any D&O process; the death of a D&O caller ends nothing in Layer R |

**Layer R is not created by `deploy()`, is not owned by `deploy()`, and does not cease to exist when a D&O operation ends.**

### 0.2 Layer O — Deployment & Operations (the orchestration layer)

**Owner:** `<DNO_USER>`.

Layer O owns orchestration and verification only (ADR-0016 §4; ADR-0017; ADR-0020 §5):

| Layer O owns | Meaning |
|---|---|
| Provisioning, deployment | Realizing one accepted Platform Instance **into** Layer R |
| Deployment state, journals | The authoritative record of what one operation did and claimed |
| Migration orchestration | Ordering component-defined migrations for one deployment |
| **Runtime operational control** | "operational control of the running platform" (ADR-0016 §4, line 79); "starting and stopping runtime elements, health/readiness establishment, restarts and comparable operational actions" (ADR-0016 §18) — issued **through the seam**, recorded in deployment state |
| Health/readiness **evaluation** | Judging what Layer R reports; Layer R produces the report |
| Upgrade, rollback, reconciliation | Explicit orchestration operations over the seam |
| Identity **verification** | Comparing `D_actual` with `D_expected`; never producing actual identity (ADR-0020 §4, §5) |

**Layer O does not own the runtime.** "Operational control" (ADR-0016 §18) is the right to *issue* operational actions through the seam; it is not ownership of the runtime's existence. `deploy()` is one orchestration operation, not a lifecycle owner.

### 0.3 The two seams

Both are constructor-injected parameters of the existing public API; neither is a new contract.

| Seam | Contract | Injection point | Direction |
|---|---|---|---|
| **Runtime / control seam** | `RuntimeAdapter` — `materialize`, `migrate`, `start`, `attach`, `request`, `stop` (`runtime.py:221-263`, "The seam between the deployment operation and its environment") | `deploy(request, runtime=…)` (`deployment.py:639-646`); `attach(request, runtime=…)` (`deployment.py:1391`); `restart(request, runtime=…)` (`restart.py:361-367`) | Layer O → Layer R: requests only |
| **S4 identity seam** | `PlatformIdentityProvider` (`platform_identity.py:289-299`), realized by `OwnerSuppliedPlatformIdentityProvider` over `RunningPlatformIdentitySource.observe(binding) -> ActualPlatformSnapshot` (`platform_identity_source.py:52-57`, `:60-111`) | `deploy(..., identity_provider=…)` (`deployment.py:643`); `reconcile(..., identity_provider=…)` (`reconciliation.py:179-182`); `restart(..., identity_provider=…)` (`restart.py:365`) | Layer R → Layer O: evidence only |

**The complete list of things Layer O can do to the runtime** — verified by enumeration of every adapter call inside `deploy()`:

| Call | Anchor |
|---|---|
| `adapter.materialize(element)` | `deployment.py:950` |
| `adapter.start(element)` | `deployment.py:1024` |
| `adapter.request(handle, OP_START)` | `deployment.py:1039` |
| `adapter.migrate(element)` | `deployment.py:1104` |
| `adapter.request(handle, OP_PROBE)` | `deployment.py:1222` |
| `adapter.stop(handle)` | `deployment.py:921` (fail-closed) and `Deployment.stop()` `:344-346` |

Because that list is exhaustive, ownership of the runtime can be relocated to Layer R **without changing D&O**: Layer O only ever issues those six requests, and the RP-owned adapter decides what each one means inside Layer R.

**Added by the approved F-1 slice — one request, and nothing else.** `deployment_operations.attach` (`deployment.py:1391`) issues exactly one further request, `adapter.attach(element)` (`:1518`), which binds a runtime element Layer R **already** supervises. It creates, starts, stops, restarts and migrates nothing, so it moves no ownership: the list above stays the complete list of things Layer O can do *to* the runtime, and `attach` only restores Layer O's reference to it.

### 0.4 Why `deploy()` is not the runtime owner

1. `deploy()` never touches a member process itself. Every runtime effect is one of the six adapter calls above; the default `LocalProcessRuntime` is only a **default**, replaced by injection at `deployment.py:769` (`adapter: RuntimeAdapter = runtime or LocalProcessRuntime(source_paths=paths)`).
2. `RuntimeHandle.process` is dereferenced **only inside `LocalProcessRuntime`** (`runtime.py:954`, `:998`). Nothing in `deployment.py`, `restart.py` or `reconciliation.py` reads it, so an RP-owned adapter may define its own handle payload — the handle is a **reference to a Layer R runtime element**, not a process Layer O owns.
3. `Deployment.stop()` is documented as "An operational action, not a lifecycle change" (`deployment.py:312-333`) and is idempotent. Under this runbook it means: *release Layer O's attachment and stop claiming a Running Platform in the record*. Whether a member runtime actually terminates is Layer R's policy decision.
4. The platform's existence is therefore not a function of any Layer O object's lifetime — which §12.1 proves operationally (I1a, I1b, I2, I3).

**Consequence, and a hard requirement on the RP-owned adapter:** `deploy()` calls `adapter.stop(handle)` for every handle when any stage fails (`deployment.py:916-921`). If `stop` destroyed runtime, a failed orchestration attempt would tear down Layer R. **`stop` MUST be detach-only.** See §7.

---

## 1. Recommended bootstrap architecture (ONE)

**R1 — standing, owner-operated runtime cell with an injected RP-owned runtime adapter.**

One host the owner controls. Two OS users. **Layer R is stood up first and runs standalone**: a supervised cell manager plus the producer, both owned by `<RP_OWNER_USER>`. Layer O then *attaches* to it through the runtime seam and verifies it through the S4 seam. First production composition: **one member, `tenant_authority` 0.1.0** (§5).

No container orchestrator, database or listening port is required: component stores are in-memory dataclasses (`tenant_authority/store.py:37-43`), health is answered in-process through `fastapi.testclient.TestClient` (`runtime_worker.py:1367-1382`), and the runtime dependencies are `fastapi`, `pydantic` and `httpx2` (`pyproject.toml:5-9`).

Rejected: driving the cell from a CI runner (factory-side automation would become the identity source, ADR-0020 §4); letting `deploy()` spawn and own the members (revision 1's error, and the reason for this revision).

---

## 2. Prerequisite checklist (owner)

| # | Prerequisite | Requirement | Evidence |
|---|---|---|---|
| P1 | Host | One Linux host/VM the owner administers; no inbound exposure needed | `uname -a` |
| P2 | Python | **≥ 3.13** (`pyproject.toml:4`) | `python3.13 -V` |
| P3 | Checkout | Clean checkout pinned to a recorded SHA | `git rev-parse HEAD` |
| P4 | Install | Project installed for that interpreter | `python3.13 -m pip show application-factory` |
| P5 | Two users | `<DNO_USER>` (Layer O) and `<RP_OWNER_USER>` (Layer R) | `id` of both |
| P6 | Three directories | `<RUNTIME_ROOT>` (Layer O), `<PLATFORM_STATE_DIR>` (Layer R), `<RP_CELL_HOME>` (Layer R: cell manager, producer, adapter) | `ls -ld` |
| P7 | Permissions | `<RP_OWNER_USER>` cannot read `<RUNTIME_ROOT>`; `<DNO_USER>` cannot write `<PLATFORM_STATE_DIR>` | Two refused operations (§10 step 3) |
| P8 | Supervision | The cell manager **and** the producer run under a supervisor owned by `<RP_OWNER_USER>` | Unit files + `systemctl status` |
| P9 | No secrets in files | No secret value in any repository or evidence file | `grep -rIl` over `<RP_CELL_HOME>` |

---

## 3. Ownership and process boundaries

| Path / resource | Layer O (`<DNO_USER>`) | Layer R (`<RP_OWNER_USER>`) |
|---|---|---|
| `<RUNTIME_ROOT>/operations/**` — deployment records and journals, **contain `instance_digest`** | **RW** | **none** |
| `<RUNTIME_ROOT>/deployments/**` — `runtime.json` (carries `platform_id`, `manifest`, `configuration`, `instance_digest`), `runtime.log` | **RW** | **none** |
| `<PLATFORM_STATE_DIR>` — actual membership, composition, manifest/configuration/branding/extension state, producer answers | **none** | **RW** |
| `<RP_CELL_HOME>` — cell manager, producer, RP-owned adapter | **none** (imports the adapter at call time) | **RW** |
| **Member runtimes (the platform)** | **attachment only** — via the six adapter calls of §0.3 | **created, supervised, lifetime-owned** |
| **Runtime lifecycle (create/start/stop/restart of the runtime itself)** | **issues requests through the seam; owns no lifetime** | **owns** |
| Expected state (`request.json`, `manifest.json`, `instance.json`) | **R** | **none** |
| Credentials | env var at call time (`--secret KEY=ENVVAR`, `__main__.py:51-63`) | env var of the Layer R units only |

The `<RUNTIME_ROOT>` denial is the load-bearing control: after a real run it contains a complete copy of expected identity (`runtime.json` keys include `instance_digest`, `platform_id`, `manifest`, `configuration`; `operations/<sha256(deployment_id)>.json` carries the platform instance). A producer that could read it could "observe" `D_expected` — forbidden by ADR-0019 §36 and ADR-0020 §6.

---

## 4. Lifecycle operations — who owns which

| Operation | Owner | How it happens |
|---|---|---|
| Bring the runtime layer into existence | **Layer R** | Cell manager started by its supervisor (§10 step 4). No D&O involvement |
| Keep the layer alive / restart it after failure | **Layer R** | Supervisor policy (`Restart=`), Layer R's own health of its own runtime |
| Create or attach a member runtime element | **Layer R** (executes) | On Layer O's `adapter.start(element)` request |
| Bind pinned content into a member slot | **Layer R** (executes) | On Layer O's `adapter.materialize(element)` request; digest verified against the pin |
| Run component migrations for one deployment | **Layer O orchestrates**, Layer R executes | `adapter.migrate(element)` (ADR-0016 §14, §18) |
| Establish health/readiness facts | **Layer R** | The member's own `/health`, `/ready` (`tenant_authority/api.py:142-153`) |
| **Evaluate** health/readiness and decide acceptance | **Layer O** | `adapter.request(handle, OP_PROBE)` → `deployment.py:1222` |
| Verify identity correspondence | **Layer O** | S4 seam; `deployment.py:898-912` |
| Stop claiming a Running Platform (record) | **Layer O** | `Deployment.stop()` → `mark_stopped` |
| Decide whether a member runtime terminates | **Layer R** | Its own policy, on a detach request |
| Upgrade / rollback / reconcile | **Layer O orchestrates** | `upgrade(..., runtime=…, identity_provider=…)`, `rollback(...)`, `reconcile(..., identity_provider=…)`; reconciliation "takes no runtime action of any kind" (`reconciliation.py:179-186`) |
| Produce actual identity facts | **Layer R** | The producer (ADR-0020 §4) |

**What is allowed through the S4 seam:** exactly one direction — Layer R supplies independently grounded actual evidence for one evaluation; Layer O validates it, projects it through the existing canonicalization and compares. Nothing else crosses it: no control, no lifecycle command, no expected state, no credential.

**What `deploy()` can no longer mean:** creation of the runtime, ownership of a member's lifetime, authority over whether the platform survives, or the source of any actual identity fact.

---

## 5. Minimal production scope for the first proof

**One member: `tenant_authority` 0.1.0.**

Verified reason: `build_deployment` exists in exactly seven packages (`authorization_service`, `booking_service`, `commerce_service`, `identity_service`, `learning_service`, `records_service`, `tenant_authority`); the Composer resolves the full declared dependency closure and never drops a dependency (`composer/errors.py:43`); and `build_elements` raises `RuntimeProcessError` for any instance component with no runtime binding (`runtime.py:277-281`). Current Composer resolutions at `main@795f3bd`: `tenant_authority` → 1 member (startable); `identity` 0.4.0 → 2 (`identity` + `tenant_authority`, both startable); `authorization` → 3; `records` → 4; `commerce` → 4. `idempotency` is a D2=B shared in-process library, not a runtime member; `saga` is likewise a shared library. Registry dependencies remain `tenant_authority: []`, `identity: [tenant_authority]`, `authorization: [identity, tenant_authority]`, `commerce/booking/learning: [authorization, idempotency, identity]`; those `idempotency` edges are `kind=internal-consumer-surface`, not deployed membership.

| Element | Value | Owner |
|---|---|---|
| Member runtime | One supervised runtime element speaking the member protocol (`start`/`migrate`/`probe`/`stop`, `runtime_worker.py:1438-1448`) | **Layer R** |
| Health | `/health` → `{status, component_id, version, platform_id}`; `/ready` → `{status, component_id}` (`tenant_authority/api.py:142-153`) | **Layer R** produces, Layer O evaluates |
| Migrations | None declared by any component (no `migration` match under `components/`) | — |
| **Actual membership** | Exactly one entry in the Layer R member registry; `membership_established=true` only on clean enumeration | **Layer R** |
| Survival | The cell manager keeps the member alive across Layer O calls and after Layer O exits | **Layer R** |
| Out of scope | This first proof remains limited to `tenant_authority`. `identity` 0.4.0 now has `identity_service.deployment.build_deployment`, but it is not included in this one-member bootstrap. `idempotency` and `saga` are D2=B shared in-process libraries, not runtime members. The preserved seven-member `factory/platform_instance/example_instance.json` is the pre-D2=B composition; the current six-member example is `factory/platform_instance/example_instance_without_shared_libraries.json`. | — |

The member runtime protocol implementation may be the shipped `deployment_operations.runtime_worker` module (a protocol implementation, not an owner) or the owner's own; **supervision and lifetime belong to Layer R either way**. If the owner wants Layer R to have no import dependency on the `deployment_operations` package, that is a separate packaging decision (finding F-4).

---

## 6. Independent producer (Layer R)

**Placement:** a process on the host, running as `<RP_OWNER_USER>`, supervised, started by Layer R's own supervisor — **not** spawned by any D&O call, not a child of a Layer O process, holding no Layer O handle.

**The nine identity-bearing surfaces and the source of each:**

| # | Surface | Source Layer R reads | Status |
|---|---|---|---|
| 1 | `platform_id` | `<PLATFORM_STATE_DIR>/platform.json`, written by the cell when the member actually started — **not** `/health`'s `platform_id`, which is `config.platform_id` (`tenant_authority/api.py:148` → `tenant_authority/engine.py:93-95`) and therefore a runtime copy of desired specification, excluded by ADR-0019 §36 | new Layer R capability |
| 2 | Complete membership | `<PLATFORM_STATE_DIR>/members/` — one entry per member actually present; clean termination required | new Layer R capability |
| 3 | `component_id` + `component_version` | The member's own `/health` body (`tenant_authority/api.py:146-147`) | observable immediately |
| 4 | Artifact identity | The actual bound artifact unit; today the only registry value is `artifact_type "none"` / `digest null` for all nine components, so the actual value is the canonical proved-absence tuple (ADR-0019 §11) | observable immediately, as proved absence |
| 5 | Manifest identity + `manifest_state` | `<PLATFORM_STATE_DIR>/manifest.json` — the actually active Manifest; `manifest_state` from that record, never `ready`/`realized`/`running` | new Layer R capability |
| 6 | Identity-bearing configuration | Actually active values of `platform_id` / `current_platform_id` (`environment.py:51`) read from the running member — not Layer O's `effective_configuration` (`environment.py:135`), excluded by ADR-0020 §14 | new Layer R capability |
| 7 | Golden Bundle + inventory completeness | `<PLATFORM_STATE_DIR>/golden_bundle.json`; for the first composition the value is `null` **and** `golden_bundle_inventory_established` must still be `true` | new Layer R capability |
| 8 | Branding | `<PLATFORM_STATE_DIR>/branding.json`; absence recorded explicitly (`state:"ABSENT"`), never omitted | new Layer R capability |
| 9 | Extensions | `<PLATFORM_STATE_DIR>/extensions.json`; no component contract exposes an extension point, so absence must still be recorded explicitly | new Layer R capability |

**Two values that must never be confused.** The *deployment id* is the durable, expected-derived record identity — `dep-{platform_id}-{environment_id}-{first 12 hex of instance_digest}-a{attempt}` (`state.py:146-165`) — and the *binding basis* is the evaluating side's own note of which record a binding was derived from (`deployment-record:<deployment_id>`, `deployment.py:254-256`). Both are expected-derived, both stay on the Layer O side, and **neither is a correlation value**. The *evaluation handle* H is a different value in a different role: a fresh opaque random nonce — `ev-` + 32 hex characters = 128 bits (`deployment.py:194-213`) — issued by the evaluating operation for exactly one evaluation. H names no platform, no environment and no attempt, carries no digest or digest prefix, states no position and is never parsed to derive one. **H is the only correlation value that crosses this seam.**

**What the producer categorically never receives from Layer O or expected state:** the deployment id, the binding basis, or any expected-derived value, or any part or derivative of these; the Platform Instance document or its `instance_digest`; the Manifest document; expected component bindings; expected configuration; expected Golden Bundle; expected branding/extensions; the `DeploymentRecord`; the deployment request; `platform_id`, `environment_id`, `attempt`; any path under `<RUNTIME_ROOT>`; any credential. Its entire input per evaluation is `{"op":"observe","handle":"<H>"}` — the handle of §8 and nothing else — and its answer must state that same handle in its `correlation` statement (§10 step 11).

---

## 7. The RP-owned runtime adapter (the runtime seam implementation)

Layer R implements `RuntimeAdapter` (`runtime.py:221-263`). Semantics are fixed by §0.4 and §4:

| Method | Required Layer R semantics | Forbidden |
|---|---|---|
| `materialize(element)` | Bind the pinned content into the member slot **inside Layer R**; verify the copy's digest against the pin; report honestly when there is no content for `artifact_type: none` | Inventing an artifact identity |
| `migrate(element)` | Execute the component-defined migrations in a Layer R execution context | Editing desired state (ADR-0016 §18) |
| `start(element)` | **Attach** to an existing Layer R member runtime, or ask the cell manager to create one under Layer R's own supervision; return a handle that is a **reference** to the Layer R element | Returning a process whose lifetime Layer O controls; creating a runtime outside Layer R's supervision |
| `request(handle, op, timeout=…)` | Forward `OP_START` / `OP_PROBE` to the Layer R element within the deadline | Synthesizing an answer; consulting expected state |
| `stop(handle)` | **Detach**: release Layer O's reference and let Layer R apply its own policy to the member | Killing the layer, the supervisor, the producer, or another deployment's runtime |
| `attach(element)` | Return a handle to the member Layer R **already supervises** for exactly this element, or refuse (`runtime.py:238`) | Creating, starting, restarting or migrating anything; returning a handle for an element Layer R does not supervise |

Two verified facts make this Level A:
- `RuntimeHandle.process` is dereferenced only inside `LocalProcessRuntime` (`runtime.py:954`, `:998`), so the handle payload is Layer R's to define — **no D&O change**.
- `deploy()`'s fail-closed path calls `adapter.stop(handle)` for every handle (`deployment.py:916-921`), so a detach-only `stop` is what keeps a failed orchestration attempt from destroying Layer R.

**Mandatory:** the RP-owned adapter is injected as `runtime=`. Using the default `LocalProcessRuntime` (`deployment.py:769`) would make Layer O the runtime owner and is a STOP condition (§14).

---

## 8. Evaluation protocol (S4 identity seam)

| Property | Rule |
|---|---|
| Fresh handle | The evaluating operation issues a new opaque random handle per evaluation (≥128 bits — `ev-` + 32 hex characters, `deployment.py:194-213`), caches nothing and re-observes every time; the owner-side adapter transports the handle unchanged and must never substitute, reuse, pre-issue, extend or derive one |
| Correlation | The producer states, as its own facts, the binding identity it answered (handle, scope, sequence, target) and its attribution (authority, basis, instant); the owner-side adapter stops a document whose handle statement is missing, foreign or not the handle it transported — it holds no expected values and is therefore never the seam's authority — and D&O establishes the full correspondence in-band (`require_correlated_evidence`) |
| Foreign rejection | An answer stating another handle, another binding space, another position or another platform ⇒ refusal; a handle the evaluation never issued can never establish correspondence |
| Stale rejection | An answer for an earlier binding (an earlier attempt or position), `freshness_current=false`, or an incomplete correlation ⇒ refusal (ADR-0019 §26; ADR-0020 §18) |
| Freshness | `freshness_current=true` only for the observation taken under the current handle |
| Hard timeout | The adapter enforces its own deadline **after** the transport returns; a timeout is a refusal, never a match |
| Fail-closed | Missing surface, missing basis, non-normative provenance, `UNKNOWN` presence, incomplete membership or inventory ⇒ `UNAVAILABLE`. `MISMATCH` is never downgraded; `UNAVAILABLE` is never upgraded (ADR-0019 §28) |

Correlation is not delegated to the owner-side adapter. D&O itself fails closed: `deployment_operations.platform_identity.require_correlated_evidence(binding, evidence)` runs at every identity seam (deploy, attach, restart, reconcile) before `D_actual` is computed, requires the evaluation binding to state its semantics (authority, basis, target, scope, sequence, instant), requires the observation to state its own binding identity and attribution, and refuses any disagreement as `UNAVAILABLE` — an equal handle alone establishes nothing. The adapter remains the place where a document is stopped before it crosses the boundary, but the seam no longer depends on it.

---

## 9. Artifact / Manifest / Golden Bundle

**Minimum for the first composition:** nothing beyond proved absence. All nine registry entries carry `artifact_type "none"` with `digest: null`; the composed instance's `golden_bundle` is `null`. There is no sealed digest to resolve and no bundle to look up.

**`ATTESTED` is sufficient** — ADR-0019 §24–§25 route (a): attestation of the complete actual identity, grounded in the producer's own observations, with no new authority and no second canonicalization. Level A preserved. (Executed confirmation in §16.)

**Exact Level C ADR triggers** — stop and ratify first if any becomes necessary:
1. A component publishes a **sealed artifact** ⇒ ADR-0019 §10 requires measuring the digest and resolving it through a **unique** authoritative publication record (0 ⇒ unavailable, >1 ⇒ unavailable). No such store exists: `PublicationRecords` are written "to publication-records.json **inside the uploaded evidence archive**" and "Reconciliation is NOT part of this workflow" (`.github/workflows/publish-component-artifacts.yml:6-8`).
2. A composition pins a **certified Golden Bundle** ⇒ ADR-0019 §20 requires the complete authoritative inventory; `factory/golden_bundle/` holds only `example_bundle.json`.
3. The actual **Manifest** must be resolved to an authoritative publication for `manifest_state` (ADR-0019 §14); `factory/platform_manifest/` holds only `schema/` + `README.md`.

Required decision if any fires: *"Establish a Factory-side authoritative publication/inventory authority for artifact, Manifest and Golden Bundle resolution with the 0/1/>1 uniqueness semantics of ADR-0019 §10/§14/§20, and define its ownership boundary relative to ADR-0020 §5."* None is triggered by the first production composition.

---

## 10. Runbook — steps the owner performs

Placeholders: `<HOST> <DNO_USER> <RP_OWNER_USER> <RUNTIME_ROOT> <PLATFORM_STATE_DIR> <RP_CELL_HOME> <ENVIRONMENT_ID> <PLATFORM_ID> <RP_OWNER_HANDLE> <BOUND_SECONDS> <SHA> <REPO_URL>`. `<ENVIRONMENT_ID>` must be concrete — a floating selector is rejected.

**Ordering rule:** steps 1–5 stand up Layer R **with no Layer O involvement**. Only from step 6 does Layer O appear, and it only ever attaches.

### Step 0 — pin and check out
```bash
git clone <REPO_URL> ~/application-factory && cd ~/application-factory
git checkout <SHA>                 # record this SHA as evidence
python3.13 -V                      # >= 3.13 (P2)
python3.13 -m pip install .
# Verify that Starlette's TestClient dependency is available before bootstrap.
python3.13 -c 'from fastapi import FastAPI; from fastapi.testclient import TestClient; app = FastAPI(); TestClient(app); print("TestClient OK")'
```

### Step 1 — users and directories
```bash
sudo useradd --system --home-dir <RP_CELL_HOME> --shell /usr/sbin/nologin <DNO_USER>
sudo useradd --system --home-dir <RP_CELL_HOME> --shell /usr/sbin/nologin <RP_OWNER_USER>
sudo install -d -o <DNO_USER>      -g <DNO_USER>      -m 0700 <RUNTIME_ROOT>
sudo install -d -o <RP_OWNER_USER> -g <RP_OWNER_USER> -m 0700 <PLATFORM_STATE_DIR>
sudo install -d -o <RP_OWNER_USER> -g <RP_OWNER_USER> -m 0750 <RP_CELL_HOME>
```

### Step 2 — write the Layer R components in `<RP_CELL_HOME>` (owner code, outside the repository)
- `cell_manager` — supervises member runtimes, maintains `<PLATFORM_STATE_DIR>/members/`, owns runtime lifecycle.
- `producer` — answers `{"op":"observe","handle":…}` with the nine-surface document of §10 step 11.
- `rp_runtime_adapter` — the `RuntimeAdapter` implementation of §7.
- `s4_adapter` — the `RunningPlatformIdentitySource` implementation of §8.

### Step 3 — prove the boundary (this output is evidence)
```bash
sudo -u <RP_OWNER_USER> sh -c 'ls <RUNTIME_ROOT>'        ; echo "exit=$?"   # expect Permission denied
sudo -u <DNO_USER>        sh -c 'touch <PLATFORM_STATE_DIR>/x' ; echo "exit=$?"  # expect Permission denied
```

### Step 4 — start Layer R standalone
```bash
sudo systemctl start rp-cell-manager rp-identity-producer     # units owned by <RP_OWNER_USER>
systemctl status rp-cell-manager rp-identity-producer --no-pager
```
**At this point no Layer O process exists and none has ever run.** Layer R must be up and its member registry populated by the cell manager's own supervision.

### Step 5 — Layer R standalone proof (no D&O anywhere)
Ask the producer for one observation directly. **At this point nothing has been realized into the layer**, so the correct answer is a well-formed nine-surface document with `membership_established: true` and an **empty** member set — and the S4 consumer must refuse it as incomplete (`ActualIdentityUnavailable: "actual component membership is empty"`, `platform_identity.py:603-605`).

That refusal is part of the proof, not a failure: it shows that (a) Layer R and its identity surface exist and answer with **zero Layer O involvement**, and (b) absence of members is reported honestly rather than fabricated into a complete identity. The complete-identity form of this proof is **I1b** (§12.1), which can only run after the first attach at step 13 — a specific realized composition is orchestration output, while the layer that hosts it and the facts about it belong to Layer R.

### Step 6 — compose the expected side (Layer O inputs)
```bash
cd ~/application-factory && export PYTHONPATH=$PWD/src
cat > /tmp/request.json <<JSON
{"\$schema":"factory/composer/schema/composition_request.schema.json",
 "components":[{"component_id":"tenant_authority","component_version":"0.1.0"}],
 "configuration":{"tenant_authority":{"platform_id":"<PLATFORM_ID>"}},
 "manifest":{"manifest_id":"<PLATFORM_ID>","manifest_version":"1.0.0"}}
JSON
python3.13 -m composer validate /tmp/request.json
python3.13 -m composer compose  /tmp/request.json --out /tmp/manifest.json    # state "draft"
```

### Step 7 — Platform Manifest lifecycle act `draft → validated`
An instance may only be assembled from `validated`/`approved`/`published`/`deployed` (`platform_instance/validation.py:73`); there is no CLI for the act, so record it explicitly and recompute the digest with the existing canonicalizer:
```python
# /tmp/advance.py — uses the repository's own compute_manifest_digest; no second canonicalization
import json, sys
from platform_manifest.manifest import compute_manifest_digest
from platform_manifest.validation import validate_document
p = sys.argv[1]; d = json.load(open(p))
d["lifecycle"] = {"state": "validated"}
d["validation_attestation"] = {"validated_by": "<RP_OWNER_HANDLE>",
                               "validated_at": "<UTC_TIMESTAMP>",
                               "checks": ["schema", "registry", "composition"]}
d["manifest_digest"] = compute_manifest_digest(d)
json.dump(d, open(p, "w"), indent=2, sort_keys=True)
print(validate_document(d))          # expect: []
```
```bash
python3.13 /tmp/advance.py /tmp/manifest.json
python3.13 -m platform_manifest validate /tmp/manifest.json
```

### Step 8 — assemble, validate and digest the Platform Instance
```bash
python3.13 -m platform_instance assemble /tmp/manifest.json --platform-id <PLATFORM_ID> --output /tmp/instance.json
python3.13 -m platform_instance validate /tmp/instance.json /tmp/manifest.json
python3.13 -m platform_instance digest   /tmp/instance.json      # → D_expected; record it
```
The instance carries `configuration: {"tenant_authority": {"platform_id": "<PLATFORM_ID>"}}` and `golden_bundle: null` — the producer must report configuration as `PRESENT` and the bundle as `ABSENT` with complete inventory, or `D_actual` cannot match.

### Step 9 — the environment document (exact accepted key set: `environment_id`, `runtime_root`, `bindings[{component_id, deployment_module, deployment_factory, migrations?, import_paths?}]`, `configuration_overlay?`, `artifact_cache?`, `python_executable?`, `probe_timeout_seconds?` — `environment.py:188-267`)
```json
{
  "environment_id": "<ENVIRONMENT_ID>",
  "runtime_root": "<RUNTIME_ROOT>",
  "bindings": [
    { "component_id": "tenant_authority",
      "deployment_module": "tenant_authority.deployment",
      "deployment_factory": "build_deployment" }
  ],
  "probe_timeout_seconds": 60.0
}
```
Save as `<RP_CELL_HOME>/environment.json`. **Do not add `configuration_overlay.tenant_authority.platform_id`** — the accepted instance pins that key and the overlay is rejected (`environment.py:405`).

### Step 10 — the Layer O composition root (attaches; never creates)
```python
from deployment_operations import DeploymentRequest, InstanceReference, deploy
from deployment_operations.platform_identity_source import OwnerSuppliedPlatformIdentityProvider

deployment = deploy(
    request,
    runtime=RpRuntimeAdapter(cell),                       # §7 — runtime ownership stays in Layer R
    identity_provider=OwnerSuppliedPlatformIdentityProvider(S4Adapter(producer, timeout=<BOUND_SECONDS>)),
)
# Layer O does NOT call deployment.stop() here; and even if it does, §7 makes stop a detach.
```
The CLI is deliberately not used: it cannot pass a provider (`deployment_operations/__main__.py:129`) or a runtime adapter, and adding those there would create a new D&O entry point and put Level A at risk.

**The CLI is also the last code-level residue of the superseded model, and a concrete reason not to use it for Layer R.** Its default is to stop the runtime when the operation returns — `--keep-running` means *"leave the platform's runtime elements running after the operation"* (`deployment_operations/__main__.py:82-86`), applied as `if not arguments.keep_running: deployment.stop()` (`:145-146`). That framing makes the platform's survival a property of **what the caller chose not to do**, i.e. exactly the ownership error this revision corrects. Under the corrected model: Layer R's survival is its own supervisor's policy and is not a flag; `deployment.stop()` means detach (§0.4, §7); and because the CLI can only use the default `LocalProcessRuntime`, its default would **actually terminate** Layer R's members. Revision 1 cited `--keep-running` as the survival mechanism; revision 2 does not, and that removal is deliberate.

### Step 11 — producer answer schema
One JSON object per evaluation, mirroring the field set the existing owner-surface contract validates (`running_platform/owner_state.py:61-152`):
```json
{
  "platform_id": "<actual>", "freshness_current": true,
  "correlation": {"token": "<echo of H>", "scope": "<the binding space this platform serves>",
                  "sequence": <this answer's position in that space>, "target": "<the platform actually observed>",
                  "authority": "<who established this observation>", "basis": "<on what basis>",
                  "established_at": "<when>"},
  "provenance": "ATTESTED", "membership_established": true,
  "components": [{"component_id": "tenant_authority", "component_version": "0.1.0",
                  "artifact_identity": {"artifact_type": "none", "digest": null,
                                        "pinned": false, "canonical_form": null}}],
  "manifest": {"manifest_id": "<actual>", "manifest_version": "<actual>", "manifest_digest": "<actual>"},
  "manifest_state": "validated",
  "configuration":  {"state": "PRESENT", "value": {"tenant_authority": {"platform_id": "<actual>"}}},
  "golden_bundle":  {"state": "ABSENT"},  "golden_bundle_inventory_established": true,
  "extensions":     {"state": "ABSENT"},  "branding": {"state": "ABSENT"}
}
```
**The `handle` in the request is the opaque per-evaluation handle H of §8 — never the deployment id and never the binding basis.** The producer receives H (§6: it is the only correlation value crossing this seam) and must return it in `correlation.token` as an echo, stating alongside it its own binding identity (`scope`, `sequence`, `target`) and its attribution (`authority`, `basis`, `established_at`); D&O refuses the answer as `UNAVAILABLE` unless that statement agrees with the binding the evaluation established (`require_correlated_evidence`, `platform_identity.py:339`) — an equal handle alone establishes nothing. The producer must refuse to emit, and the adapter refuse to accept, any document containing `instance_digest`, `expected_instance`, the deployment id or any expected-derived material (`running_platform/owner_state.py:72`). The `correlation` statement is the producer's own fact: the request carries nothing but H, so no expected identity can be echoed back into it.

### Step 12 — negative control (must fail closed)
Run the composition root with the S4 seam unavailable (producer stopped or answering a foreign handle). Expected: `IdentityVerificationFailed: identity/version/digest verification failed; the platform is not deployed` — **and Layer R must still be running afterwards** (check `systemctl status` and re-ask the producer). This proves a failed orchestration attempt destroys nothing it does not own.

### Step 13 — positive attach
Run with both seams. Expected: `lifecycle=realized`, `ready=True`, `identity_verified=True`, `deployed=True`, `deployment_id = dep-<PLATFORM_ID>-<ENVIRONMENT_ID>-<12 hex>-a1`.

### Step 14 — independence proofs I1b, I2 and I3 (§12.1)
Perform the three proofs in this order, each with its evidence recorded verbatim:

1. **I1b — actual identity without D&O.** Ensure no Layer O process is running (`pgrep -u <DNO_USER>` → empty). Ask the producer directly (step 6 command) and feed the answer through the S4 seam. Expected: a complete nine-surface actual identity and `MATCH` against `D_expected`, with no Layer O participation.
2. **I2 — Layer O's death ends nothing.** Kill any Layer O process (`pkill -u <DNO_USER>`). Re-check: Layer R units still active (`systemctl status rp-cell-manager rp-identity-producer`), and the producer still answers a complete current identity.
3. **I3 — Layer O's stop is a claim, not a kill.** Call `Deployment.stop()` on the `Deployment` object held by the Layer O process that performed step 13 — or, since the approved F-1 slice, on a handle re-bound by `deployment_operations.attach` in a **fresh** Layer O process (§15, F-1). A cold handle that holds no runtime element now **refuses** instead of writing `stopped` (`deployment.py:335`). Expected: the deployment record reaches `mark_stopped` (Layer O no longer claims a Running Platform, `deployment.py:312-333`) **and** the member runtime's outcome follows Layer R's own policy, recorded by the cell manager. Record what Layer R did and why — that decision belongs to Layer R, not to Layer O.

If any of the three fails, stop: the bootstrap has silently rebuilt revision 1 (§14, item 12).

### Step 15 — acceptance matrix (§12.2), then record everything on Issue #128 (owner-side; Arena's Issues access is read-only) and post the D8 authorization there.

---

## 11. Expected evidence per step, and where it is stored

| Step | Expected evidence | Stored at |
|---|---|---|
| 0 | `<SHA>`, `python3.13 -V`, install line | #128 record |
| 1 | `id` of both users; `ls -ld` of the three directories with modes | #128 record |
| 2 | The four Layer R files; a static check that the producer imports nothing from `deployment_operations` except published identity contracts | `<RP_CELL_HOME>` |
| 3 | Two `Permission denied` outputs with non-zero exits | #128 record (independence proof) |
| 4 | Supervisor status for both Layer R units, **with no Layer O process running** (`pgrep -u <DNO_USER>` empty) | `…/evidence/step4.txt` |
| 5 | The producer's standalone answer with zero Layer O involvement: nine surfaces, `membership_established: true`, empty member set, and the S4 seam's `ActualIdentityUnavailable` refusal | `…/step5.txt` — **the I1a proof** |
| 6 | "valid and composable"; "draft platform manifest written"; member list `["tenant_authority"]` | `…/step6.txt` |
| 7 | `validate_document` → `[]`; "manifest is valid"; the `manifest_digest` | `…/step7.txt` |
| 8 | "platform instance is valid"; **`D_expected`** | `…/step8.txt` |
| 9 | Successful `load_environment` (id, binding, timeout, empty overlay) | `…/step9.txt` |
| 10 | The composition root file | `<RP_CELL_HOME>` |
| 12 | The fail-closed exception **plus** post-failure `systemctl status` and a fresh producer answer | `…/step12.txt` |
| 13 | `deployment_id`, lifecycle/ready/identity_verified/deployed, `D_expected` | `…/step13.txt` |
| 14 | I1b, I2 and I3 transcripts | `…/step14.txt` |
| 15 | All acceptance transcripts, including timeout and non-forwarding | `…/acceptance/` |

No evidence file may contain a secret value, an expected-state document, or content obtained by reading `<RUNTIME_ROOT>` from Layer R.

---

## 12. Independence proofs and acceptance matrix

### 12.1 Independence proofs — these are what make the ownership split executable rather than rhetorical

| # | Proof | Procedure | Required result |
|---|---|---|---|
| **I1a** | **The runtime layer and its identity surface exist without D&O** | Step 5: Layer R started by its supervisor; no Layer O process has ever run; ask the producer directly | A well-formed nine-surface document with `membership_established: true` and an empty member set; the S4 seam refuses it as incomplete (`platform_identity.py:603-605`). Layer R answered; nothing was fabricated |
| **I1b** | **The Running Platform's actual identity is established without D&O** | After step 13, with **no Layer O process running**: ask the producer directly and evaluate the answer through the S4 seam | A complete, current, self-consistent nine-surface actual identity; `D_actual == D_expected` ⇒ `MATCH`, with no Layer O participation |
| **I2** | **Layer O's death ends nothing** | After step 13, kill the Layer O process | Layer R units still active; the producer still answers a complete current identity; `pgrep -u <DNO_USER>` empty |
| **I3** | **Layer O's stop is a claim, not a kill** | Call `Deployment.stop()` | The record becomes `mark_stopped` (Layer O no longer claims a Running Platform, `deployment.py:312-333`), while the member runtime's fate follows **Layer R's** policy — recorded as such by the cell manager |

I1a, I1b, I2 and I3 are the acceptance evidence for "Running Platform ≠ D&O orchestration". If any fails, the bootstrap has silently rebuilt revision 1 and must stop.

### 12.2 Acceptance matrix

| # | Case | Injected condition | Required result | Enforced at |
|---|---|---|---|---|
| A1 | MATCH | Complete, current, correctly correlated actual identity | `MATCH` → `identity_verified` → `realized` | `deployment.py:898-915` |
| A2 | Drift → MISMATCH | One identity-bearing value changed | `MISMATCH`, never `UNAVAILABLE` | `platform_identity.py:493-494` |
| A3 | Stale → refusal | `freshness_current=false`, an earlier handle, or an answer for an earlier binding | `UNAVAILABLE` | `require_correlated_evidence`; `validate_actual_evidence` |
| A4 | Foreign correlation → refusal | An answer stating another handle (never issued, or issued for an earlier evaluation), another binding space, another position or another platform | `UNAVAILABLE`, never `MATCH` | `require_correlated_evidence` (in D&O, in-band) **and** the owner-side adapter |
| A5 | Incomplete → refusal | `membership_established=false`, incomplete bundle inventory, any `UNKNOWN` field | `UNAVAILABLE` | `_validate_components`, `_validate_golden_bundle_inventory` |
| A6 | Duplicate → refusal | The same `component_id` twice | `UNAVAILABLE` | `_validate_components` |
| A7 | Extra / missing member → MISMATCH | A member in the cell absent from the answer, or an expected member absent | `MISMATCH`, never silently ignored | projection + digest equality (`test_extra_actual_component_is_mismatch:273`, `test_missing_expected_component_is_mismatch:285`) |
| A8 | Expected-state contamination → refusal | Surface carries `instance_digest`/`expected_instance`, or echoes `D_expected` | refusal | `running_platform/owner_state.py:72`; plus the step 12 negative control |
| A9 | Timeout → refusal | Producer stalls past `<BOUND_SECONDS>`, then answers late | refusal; never acceptance | owner-side adapter |
| A10 | Boundary non-forwarding | Tap every byte adapter→producer | No expected-derived value, no secret, no `<RUNTIME_ROOT>` path crosses | owner-side adapter |
| **A11** | **Runtime-ownership non-forwarding** | Inspect the RP-owned adapter | `stop` is detach-only; no Layer O code path terminates a Layer R runtime; a failed `deploy()` leaves Layer R intact (step 12) | §7 + step 12 |
| **A12** | **Re-binding touches nothing** | A fresh Layer O process calls `attach` for the realized deployment | Only `adapter.attach` is issued — no `materialize`/`start`/`stop`/`migrate`; the same Layer R element stays alive; one `platform_attached` action is recorded; a cold `stop()` without a handle refuses | §7, §15 F-1 |

---

## 13. RF-1…RF-11 closure map

Unchanged in substance from revision 1; the owner column now reflects the two-layer split.

| RF | Closed by | Status after this runbook |
|---|---|---|
| RF-1 | §0–§9 change no contract, boundary or intake; the owner ticks Level A in D7 | **READY AFTER BOOTSTRAP** |
| RF-2 | §10 step 10 composition root (attaches only) + `<RP_OWNER_USER>` ownership of both adapters; the public API already threads `runtime=` and `identity_provider=` (`deployment.py:639-646`, `upgrade.py:235→292`, `rollback.py:246→303`, `reconciliation.py:179-182`) | **READY AFTER BOOTSTRAP** |
| RF-3 | §10 steps 2 and 11 — `{"op","handle"}` out, one nine-surface document back | **READY AFTER BOOTSTRAP** |
| RF-4 | §8 — per-evaluation handle, no caching, `<BOUND_SECONDS>` | **READY AFTER BOOTSTRAP** (value is owner input) |
| RF-5 | Steps 0–5 (D1 environment, scope = the cell's own member registry, production status) | **OWNER DECISION + OWNER ACTION** |
| RF-6 | `<RP_OWNER_USER>` + `<RP_OWNER_HANDLE>` (D2); separation proven at step 3 and by I1a–I3 | **OWNER DECISION** |
| RF-7 | Step 4 producer + cell manager (D3) | **OWNER DECISION + OWNER ACTION** |
| RF-8 | §6 table (D4), one class = `ATTESTED` | **OWNER DECISION** (confirm as written) |
| RF-9 | Credentials §9-of-revision-1 semantics retained: environment-scoped secret or host credential store, env-var delivery only, `repr=False` in records (`environment.py:103`), no secret in evidence; enumeration of existing secrets stays **BLOCKED (403)** | **OWNER DECISION** |
| RF-10 | Step 15 — signed D8 **on #128** | **BLOCKED for Arena** (read-only Issues access) |
| RF-11 | Step 15 — record location + acceptance evidence (i) non-forwarding, (ii) foreign/stale refusal, (iii) bounded wait, **plus (iv) the I1a–I3 independence proofs** | **BLOCKED for Arena** |

**New in revision 2:** RF-11 now also requires the I1a–I3 proofs, because runtime-ownership separation is itself an acceptance fact and not a wording preference.

---

## 14. STOP conditions

Implementation of Issue #128 stays blocked while any of these holds:

1. Steps 0–5 are incomplete — Layer R does not exist standalone, or I1a has not been evidenced.
2. **The runtime seam is not injected** — i.e. the default `LocalProcessRuntime` (`deployment.py:769`) or the default S1 identity provider (`deployment.py:761-767`) would be used. Either makes Layer O the runtime owner and reintroduces revision 1's defect.
3. The RP-owned adapter's `stop` is not detach-only, or any Layer O path can terminate a Layer R runtime, its supervisor or its producer.
4. The signed D8 authorization is not on Issue #128 (RF-10).
5. The first Issue #128 proof must remain scoped to exactly `{tenant_authority}`; a broader composition is outside this runbook and requires a separate owner-approved bootstrap decision. Although `identity` 0.4.0 now has a deployment module, this runbook has not executed that broader composition; `idempotency` and `saga` are D2=B shared libraries, not standalone runtime members.
6. The membership scope is defined wider than the cell's own member registry (AG-4) ⇒ Level C ADR first.
7. The provenance pattern is finer than "one class for all nine" without moving to the S5 seam (AG-3) ⇒ Level C ADR first.
8. Any §9 trigger fires — sealed artifact, certified Golden Bundle, Manifest publication lookup ⇒ Level C ADR first.
9. The producer can read `<RUNTIME_ROOT>`, or receives the deployment id, the binding basis, expected state, a secret, any expected-derived value, or any part or derivative of these (ADR-0019 §36; ADR-0020 §6). The opaque per-evaluation handle of §8 is the **only** correlation value that crosses this seam, and it is the only thing the producer receives per evaluation (§6, §10 step 11).
10. Any design makes Layer O, `DeploymentRecord`, the deployment request, the runtime spec or the CLI the source of actual identity (ADR-0020 §4).
11. A second digest, canonicalization or identity authority appears anywhere in the cell.
12. Any statement or step in this runbook is read as making `deploy()` the creator or owner of the runtime — §0.4 governs.

**Becomes READY FOR IMPLEMENTATION when:** steps 0–15 are complete with the §11 evidence; I1a–I3 pass; A1–A11 pass (A9–A11 can only exist after Layer R exists); D1–D8 are recorded with D8 signed on #128; and the findings of §15 are dispositioned by the owner.

**PR #144** remains REHEARSAL / NOT PRODUCTION, open, draft, unmerged, head `835e85eb78cbfcc8bf991d21faa50846d7f43ba8`; `main` contains no `running_platform_rehearsal` paths.

---

## 15. Findings and dispositions (reported; not implemented by this runbook)

| # | Finding | Evidence | What it needs |
|---|---|---|---|
| **F-1 — CLOSED (implemented, Level B, no Level C ADR).** Re-binding is now an explicit operation: `deployment_operations.attach(request, *, runtime, identity_provider)` (`deployment.py:1391`) fresh-reads the authoritative record, re-verifies the exact instance, binds only elements Layer R already supervises through `RuntimeAdapter.attach` (`runtime.py:238`, `LocalProcessRuntime` refuses at `:927`), re-verifies the actual identity through the unchanged S4 seam, records one `platform_attached` operational action, and creates/starts/stops/restarts/migrates nothing. Cold `Deployment.stop()` fails closed (`deployment.py:335`). Tests: `tests/test_deployment_operations_attach.py`. *Original finding, kept for history:* **No re-attach contract.** `Deployment(` is constructed in exactly one place — the return of `deploy()` (`deployment.py:926`) — and there is no `attach` / `reattach` / `resume` / `adopt` API anywhere in `src/deployment_operations/`. `state.py:1262 load_record` returns a `DeploymentRecord`, not a `Deployment`, and `ReconciliationRequest.deployment` requires a `Deployment` (`reconciliation.py:136`). So a **fresh** Layer O process cannot resume orchestration of an already-standing Layer R from persisted state | grep for `Deployment(` and for `def attach|reattach|resume|adopt` | Either a new contract function (e.g. `attach(deployment_id, *, runtime, identity_provider) -> Deployment`) or an owner ruling on who re-attaches. **A contract change ⇒ its own work item; if it alters the deployment model, a Level C ADR.** Not implemented here |
| **F-2** | **One textual hook could be misread.** `docs/ARCHITECTURE.md:130` says the Running Platform "получается из Platform Instance стадией Deployment & Operations", and ADR-0016 §4/§18 give D&O the transition and "operational control". Read strictly these do **not** make D&O the owner of the runtime layer, so no ADR change is strictly required for this runbook | `ARCHITECTURE.md:130`; ADR-0016 line 79, §18 | A one-line Level A documentation clarification that *realization into a standing runtime layer ≠ ownership of that layer*. Recommended, not performed |
| **F-3** | **Runbook placement.** This file lives under `docs/arena/`, which is not listed in `docs/DOCUMENTATION_BASELINE.md` §2 canonical sources | `DOCUMENTATION_BASELINE.md` §2 | Owner decides whether to register Arena runbooks in the baseline. Not modified here (scope) |
| **F-4** | **Layer R's import posture.** If Layer R reuses the shipped `deployment_operations.runtime_worker` protocol, it imports a D&O package module. That is a protocol implementation, not ownership, but the owner may prefer zero such dependency | `runtime_worker.py`; §5 | Owner's packaging decision |
| **F-5 — IMPLEMENTATION COMPLETE (software scope; no production bootstrap implied).** `identity` 0.4.0 now has a deployment module and its F-5 dependency path was implemented by PR #150 (merged at `42ca157…`). `idempotency` and `saga` are D2=B shared in-process libraries, not independently deployed members; the current six-member D2=B example excludes them, while the original seven-member instance remains unchanged. This runbook remains an unexecuted plan for the one-member `{tenant_authority}` proof. | PR #150 / `docs/work-items/F-5-runtime-dependency-enforcement.md` §6.2; PR #151 / D2B work item | No component-module gap remains; Issue #128 bootstrap gates remain as stated in §14 and have not been executed |

---

## 16. Verification status of this revision

**Verified in the repository [V]:** every anchor cited above — `deploy()`'s injectable `runtime` and `identity_provider` (`deployment.py:639-646`), the six adapter call sites inside `deploy()` (`:921, :950, :1024, :1039, :1104, :1222`), the default adapter (`:769`) and default identity provider (`:761-767`), `Deployment.stop()` semantics (`:311-360`, with the adapter call at `:346`), the single `Deployment(` construction inside `deploy()` (`:926`), `RuntimeAdapter` (`runtime.py:221-263`), `RuntimeHandle.process` dereferenced only at `runtime.py:954, :998`, `restart(..., runtime=…, identity_provider=…)` (`restart.py:361-367, :379`), `reconcile(..., identity_provider=…)` and "takes no runtime action of any kind" (`reconciliation.py:179-200`), `ReconciliationRequest.deployment: Deployment` (`:136`), `state.py:1262 load_record`, the environment loader key set and overlay rules (`environment.py:188-267, :405`), `ASSEMBLABLE_MANIFEST_STATES` (`platform_instance/validation.py:73`), the CLI's provider-less `deploy(request)` call (`deployment_operations/__main__.py:129`), its `--keep-running` default-to-stop semantics (`:82-86`, `:145-146`), `_secrets` (`:51-63`), ADR-0016 §18 and line 79, `ARCHITECTURE.md:130`, `.github/workflows/publish-component-artifacts.yml:6-8`.

**Executed previously on this chain (revision 1, re-confirmed as the mechanism this revision relies on):** the full factory chain `composer validate → compose → lifecycle act → platform_manifest validate → platform_instance assemble/validate/digest` producing `D_expected = sha256:90531f5d8623de140ec2b9361a8dd414a87643837fd160402d123195b6dd6634`; `load_environment` on the exact §9 document; `deploy(..., identity_provider=OwnerSuppliedPlatformIdentityProvider(...))` with `provenance="ATTESTED"` reaching `lifecycle=realized, ready=True, identity_verified=True, deployed=True`; the negative control failing closed; the Composer closure table as it stood in revision 1 (§5; those counts are historical and are superseded by the current-main resolutions now listed there); the `<RUNTIME_ROOT>` layout and `runtime.json` key list of §3; the S4 seam probe (MATCH / drift MISMATCH / stale / incomplete / `UNKNOWN` / duplicate / contamination refused). That probe also recorded **foreign token → `MATCH`**: under the superseded model the seam compared nothing and the token was the derived deployment id. That observation is what ADR-0020 §18 correlation now closes — see §8, where D&O refuses it in-band.

**Implemented since revision 2 (approved Level B F-1 slice):** `deployment_operations.attach` and `RuntimeAdapter.attach`, with the cold-stop guard; covered by `tests/test_deployment_operations_attach.py` (15 tests) and by the full suite. No Level C ADR was created, and the S4 ownership model is unchanged.

**Not executed, and deliberately so:** no bootstrap step. No host, user, directory, permission, unit, producer, adapter, deployment or credential was created; no Layer R exists. I1a–I3 and A9–A11 can only be executed by the owner after steps 1–5.
