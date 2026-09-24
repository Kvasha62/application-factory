Evidence status:     COMPLETE
Evidence revision:   v2
Evidence source:     CURRENT
Date:                2026-09-25
Repository:          Kvasha62/application-factory
Branch:              arena/01a0b7dd-application-factory
HEAD:                bbcdb9512ada90b86eaadd68f0279a4296daadfa
origin/main:         399836608ca99b6ea8b84808d694d9d27db25066

Prior evidence:
  v1 commit:  f96e22c93b4a9dbc8f3f51efb9d87cb95214fefe
  v1 tag:     gate-2-evidence
  v1 status:  immutable, not modified by this revision

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
   - _VebFinder.find_spec("evil") -> refusal_for("evil") iterates _untrusted
   - _resolve_under("evil", workspace) finds evil.py -> returns refusal
   - ExecutionBoundaryError raised with boundary-specific message
   - Worker returns {"status": "error", "error": "<boundary refusal text>"}
   - Assertion checks exact _VebFinder phrases (runtime_worker.py):
     * "never a source of trusted component" (line 770)
     * "boundary refuses to load it" (line 771)
   - These strings are unique to boundary enforcement; cannot match
     ModuleNotFoundError, SyntaxError, AttributeError, or generic failure

4. Boundary chain verified:
   Factory canonical.json
   -> artifact.digest authentication (SHA256 of canonical.json bytes)
   -> physical tree verification (content_digest per entry)
   -> VerifiedArtifactBoundary
   -> execution_closure(boundary) with _assert_in_boundary
   -> BoundModule (executable=True, owned=True)
   -> LocalProcessRuntime
   -> runtime_worker subprocess
   -> worker re-digest at load time (_BoundLoader.exec_module)
   -> worker import enforcement (_VebFinder)

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
Test suites — historical execution evidence:
------------------------------------------------------------------------

These results were obtained during the evidence creation session
(2026-09-24) and are documented in the v1 evidence. They represent
the actual test execution that validated Gate #2.

  tests/test_f3b_source_package_integration.py:  33 passed
  tests/test_deployment_operations.py:          153 passed
  tests/test_factory_artifact_source_package.py: 44 passed
  tests/test_factory_artifact_container_image.py: 27 passed
  tests/test_component_registry.py:             124 passed
  Total:                                        381 passed

Verification status: HISTORICALLY REPORTED

------------------------------------------------------------------------
Test suites — independent re-execution:
------------------------------------------------------------------------

Independent re-execution was attempted in this validation environment.

Available tools:
  python3:  3.11.2
  pytest:   NOT INSTALLED
  black:    NOT INSTALLED
  ruff:     NOT INSTALLED
  compileall: AVAILABLE

Since pytest is not installed, independent re-execution of test suites
is NOT AVAILABLE IN THIS ENVIRONMENT.

Verification status: NOT INDEPENDENTLY RE-EXECUTED

The historical results cannot be confirmed or denied by independent
re-execution. They are documented as historically reported.

------------------------------------------------------------------------
Quality checks — historical execution evidence:
------------------------------------------------------------------------

These results were obtained during the evidence creation session
(2026-09-24) and are documented in the v1 evidence.

  black --check:     HISTORICALLY REPORTED PASS
  ruff check:        HISTORICALLY REPORTED PASS
  compileall:        HISTORICALLY REPORTED PASS
  git diff --check:  HISTORICALLY REPORTED PASS

------------------------------------------------------------------------
Quality checks — independent re-execution:
------------------------------------------------------------------------

compileall:
  Command:  python3 -m compileall -q src tests
  Result:   CURRENTLY VERIFIED PASS (exit 0)

git diff --check:
  Command:  git diff --check
  Result:   CURRENTLY VERIFIED PASS (exit 0, no whitespace errors)

black:
  NOT AVAILABLE (not installed in current environment)

ruff:
  NOT AVAILABLE (not installed in current environment)

------------------------------------------------------------------------
Scope:
------------------------------------------------------------------------

The evidence commit itself contains exactly the files listed in
DIFF.txt. No production code, tests, ADRs, schemas, contracts,
registry, manifests, instances, or deployment state was modified
by this evidence revision.

Pre-existing working tree preserved.