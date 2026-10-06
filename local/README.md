# Local Docker Running Platform stack — `LOCAL DOCKER / NON-PRODUCTION`

This directory contains a **local, non-production** Running Platform stand-in so
the acceptance path of Deployment & Operations (`deploy` → identity seam →
acceptance/refusal) can be exercised against a **real owner-side process**
instead of D&O reading an ambient `<runtime_root>/running_platform_identity.json`.

Everything produced by this stack is labelled `LOCAL DOCKER / NON-PRODUCTION`.
It is **not** production evidence, **not** a GitHub Environment, **not** an
owner-authority proof, and it creates no production identity, no credential and
no registry artifact.

## Topology

```
┌──────────────────────────────┐        HTTP (one opaque token)        ┌────────────────────────────┐
│ running-platform             │ ◀──────────────────────────────────── │ dno                        │
│  owner-side producer         │                                       │  deployment_operations     │
│  own authoritative state     │ ────────────────────────────────────▶ │  .deploy(...)              │
│  (its own directory)         │        owner identity surface         │  identity seam + refusal   │
└──────────────────────────────┘                                       └────────────────────────────┘
     local/Dockerfile.running-platform                                    local/Dockerfile.dno
     src/running_platform/local_identity_service.py                       local/dno/run_deployment.py
     state: local/work/rp/identity.json                                   src/running_platform/http_owner_state.py
```

* The producer is a stdlib-only service. The D&O side reaches it through
  `HttpRunningPlatformOwnerStateReader` → `OwnerStateSnapshotSource` →
  `OwnerSuppliedPlatformIdentityProvider` — the shipped seams; no D&O contract
  is changed, and no ambient identity file is involved.
* The boundary accepts exactly one opaque evaluation binding. A request that
  carries anything else is refused and recorded.
* The declared bound (`--identity-timeout`) is enforced by the reader; a stalled
  producer leads to a refusal, never to acceptance.

## Run it

```bash
# 1. prepare the platform definition (Factory side) and the owner-side state
python local/tools/prepare_local_platform.py --work-dir local/work

# 2a. process mode (no container engine needed): separate OS processes
python local/run_local_evidence.py --mode process --out local/evidence

# 2b. docker mode: the same two sides as containers of local/compose.yaml
python local/run_local_evidence.py --mode docker  --out local/evidence
```

`run_local_evidence.py` runs the scenarios `correct`, `foreign`, `stale`,
`wrong-correlation`, `contradictory`, `delay` and `unavailable`, and writes the
transcripts (owner-side audit, D&O result, checks) under `--out`.

Manual Docker usage:

```bash
export AF_WORK_DIR=$PWD/local/work
RP_SCENARIO=correct docker compose -f local/compose.yaml up -d --build running-platform
docker compose -f local/compose.yaml run --rm --no-deps dno
docker compose -f local/compose.yaml logs --no-log-prefix running-platform
docker compose -f local/compose.yaml down -v
```

## The owner-side identity state

`local/work/rp/identity.json` is the producer's authoritative local state: the
platform id, the manifest identity, the component set, and the configuration
this platform serves — prepared from the same platform definition the Factory
composed and assembled (see `local/tools/prepare_local_platform.py`). It is a
**seeded stand-in**: this producer does not yet measure a live platform runtime,
which is precisely the production gap that remains open (O4-C D3/D4).

`identity-foreign.json` is a second, deliberately different platform state used
to exercise foreign-identity refusal.

## Scenarios

| scenario | owner-side behaviour | D&O outcome |
| --- | --- | --- |
| `correct` | serves its authoritative state | accepted (`MATCH`) |
| `foreign` | serves another platform's state | refused (digest mismatch) |
| `stale` | `freshness_current: false` | refused (stale surface) |
| `wrong-correlation` | answers with another evaluation's token | refused (foreign correlation) |
| `contradictory` | configuration contradicts the served `platform_id` | refused (contradiction) |
| `delay` | stalls longer than the declared bound | refused within the bound |
| `unavailable` | answers `503` | refused |

## Tests

* `tests/test_local_identity_transport.py` — the producer/consumer contract over
  a real process boundary, including a real `deploy()` acceptance and refusal;
  needs no container engine and no credentials.
* `tests/test_local_docker_stack.py` — the Docker launch path; opt in with
  `AF_LOCAL_DOCKER=1` when a container engine is available (it is skipped, not
  faked, otherwise).

## What this stack does and does not establish

It establishes, locally and reproducibly: a real owner-side producer process
physically separate from D&O; an explicit transport boundary; the shipped
acceptance/refusal logic exercised against real evidence; a bounded wait; and a
record of exactly what crosses the boundary.

It does **not** establish: a production environment, a production Running
Platform owner, a production identity producer, a production carrier/transport,
credentials or access configuration, or any production acceptance run. Those
remain open in O4-C (D1–D6) and must be answered with independently checkable
production evidence — never with this stack.
