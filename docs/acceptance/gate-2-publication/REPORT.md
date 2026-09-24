Evidence status:     COMPLETE
Evidence source:     CURRENT
Date:                2026-09-24T19:33:39Z
Repository:          Kvasha62/application-factory
Branch:              arena/01a0b7dd-application-factory
HEAD:                bbcdb9512ada90b86eaadd68f0279a4296daadfa
origin/main:         399836608ca99b6ea8b84808d694d9d27db25066

========================================================================
Acceptance Gate #2 — F-3B source_package/v1 D&O Runtime Integration
========================================================================

RESULT: PASSED

------------------------------------------------------------------------
Claims verified by this evidence:
------------------------------------------------------------------------

1. source_package reaches actual runtime execution through start/request

   test_e2e_source_package_runtime_start_and_request:
   - Real LocalProcessRuntime adapter (not mocked)
   - Real subprocess.Popen via adapter.start()
   - Real runtime_worker subprocess
   - Real build_deployment() called from generated entrypoint
   - Real contract_app() returning FastAPI app
   - request("start") returns {"status": "ok"} with execution evidence
   - Module "deployment" confirmed loaded in execution evidence

2. Health/readiness probing through real worker

   test_e2e_source_package_runtime_health_probe:
   - Same real worker subprocess
   - request("start") then request("probe")
   - /health returns {"status": "healthy", "component": "source_package_test"}
   - /ready returns {"status": "ready"}
   - Component-specific value proves response from actual generated entrypoint

3. Runtime outside-boundary import rejected by worker boundary enforcement

   test_e2e_outside_boundary_import_refused_at_runtime:
   - evil.py placed in bound_element.workspace (untrusted location)
   - Dynamic import via importlib.import_module with name assembled from
     fragments at runtime — invisible to execution_closure bytecode analysis
   - _VebFinder.find_spec("evil") → refusal_for("evil") iterates _untrusted
   - _resolve_under("evil", workspace) finds evil.py → returns refusal
   - ExecutionBoundaryError raised with boundary-specific message
   - Worker returns {"status": "error", "error": "<boundary refusal text>"}
   - Assertion checks exact _VebFinder phrases:
     * "refuses to load" (line 777 of runtime_worker.py)
     * "never a source of trusted" (lines 775-776)
   - These strings are unique to boundary enforcement; cannot match
     ModuleNotFoundError, SyntaxError, AttributeError, or generic failure

4. Boundary chain verified:
   Factory canonical.json
   → artifact.digest authentication (SHA256 of canonical.json bytes)
   → physical tree verification (content_digest per entry)
   → VerifiedArtifactBoundary
   → execution_closure(boundary) with _assert_in_boundary
   → BoundModules (executable=True, owned=True)
   → LocalProcessRuntime
   → runtime_worker subprocess
   → worker re-digest at load time (_BoundLoader.exec_module)
   → worker import enforcement (_VebFinder)

5. Identity separation maintained:
   - artifact.digest = SHA256(canonical.json bytes)
   - entry.content_digest = SHA256(file bytes)
   - No conflation in any source_package path

6. TOCTOU verified:
   - _verify_before_execution() re-reads before start()
   - _BoundLoader re-digests at load time
   - Same enforcement as artifact_type=none

7. artifact_type=none regression: 153 existing tests pass, unchanged

------------------------------------------------------------------------
Test suites executed:
------------------------------------------------------------------------

tests/test_f3b_source_package_integration.py:  33 passed
tests/test_deployment_operations.py:          153 passed
tests/test_factory_artifact_source_package.py: 44 passed
tests/test_factory_artifact_container_image.py: 27 passed
tests/test_component_registry.py:             124 passed
                                             -----
Total:                                        381 passed

------------------------------------------------------------------------
Quality checks:
------------------------------------------------------------------------

black --check:     PASSED
ruff check:        PASSED
compileall:        PASSED
git diff --check:  PASSED

------------------------------------------------------------------------
Scope:
------------------------------------------------------------------------

This evidence publication created ONLY new files under:
  docs/acceptance/gate-2-publication/

No production code, tests, ADRs, schemas, contracts, registry,
manifests, instances, or deployment state was modified.

Pre-existing working tree preserved.