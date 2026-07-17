# Stage 8 Baseline Record (Phase 0)

**Date:** 2026-07-10
**Baseline artifact:** `compliance-engine-stage8.zip` (kept unchanged)
**Git baseline:** commit `9c015ba0ef276bb34d911160bbf237eccddefb9a`, tag `stage8-zip-baseline` — a sanitized source baseline derived from the reviewed ZIP, verifiable with `git show stage8-zip-baseline --stat`. All remediation work is on branch `remediation/stage8` branched from that commit.
**Purpose:** Frozen reference point required by the remediation plan before any behavior-changing refactor begins.

---

## 1. Test baseline (reproduced, junitxml-verified)

| Run | Command | Result |
|---|---|---|
| Core suite (as shipped) | `pytest -q tests` | **219 passed, 0 failed, 0 errors, 0 skipped** (~1.6 s) |
| Full suite (as shipped) | `pytest -q` | **374 total, 1 failed** — `eval/test_verdict_regression.py::test_advisory_text_differs_proving_test_has_teeth` |
| Full suite (after REM-TEST-001 fix + audit corrections) | `pytest -q` | **375 passed, 0 failed, 0 errors, 0 skipped** (374 baseline tests + 1 new `test_advisory_eligibility_product_decision` pinning the Stage 7 advisory-eligibility contract) |

Note: the plan reported "373 passed, 1 failed" — same run, same single failure; total collected is 374.

Known cosmetic issue (pre-existing, not fixed in Phase 0): a test triggers a
`PytestUnraisableExceptionWarning` (`KeyError` in a `__del__`/GC path) and the
final `-q` summary line is occasionally swallowed on some terminals. Use
`--junitxml` for authoritative counts. Track as a P2 cleanup item.

## 2. Environment

- Python 3.12.3
- **Complete environment freeze:** `docs/STAGE8_PIP_FREEZE.txt` (authoritative —
  `requirements.txt` uses broad lower bounds like `networkx>=3.0`, so the
  freeze, not the requirements file, defines the reproduced environment).
- **Saved raw test artifacts** (JUnit XML): `docs/test-results/phase0_core.xml`,
  `phase0_full_before_fix.xml`, `phase0_full_after_fix.xml`.
- The full suite collects and passes **without** `torch` / `sentence-transformers` /
  `psycopg2` / DB / network — all heavy imports are lazy or mocked in tests.
  `conftest.py` pins `CRAG_ENABLED=0`, `GRAPH_ENABLED=0`.

## 3. REM-TEST-001 — root cause and resolution (product decision)

**Failure:** `test_advisory_text_differs_proving_test_has_teeth` asserted at least one
`[AI note: …]`-annotated NEEDS_REVIEW finding in both runs; both were empty.

**Root cause:** Stage 7 (2026-07) deliberately excluded `unsupported=True`
NEEDS_REVIEW findings (engine-limitation items: unmapped vocabulary,
unsupported comparator/unit) from the LLM advisory pass
(`validation/compliance/runner.py:_llm_review_interpretive`) to stop spending LLM
budget where a note cannot help the reviewer. The shared fixture
(`tests/test_orchestrator.py` BIM/CLAUSES) produces exactly one NEEDS_REVIEW
finding — clause `O1`, `window_area` unmapped — which is `unsupported=True`.
After Stage 7 it is correctly skipped, leaving zero annotatable findings and
making the eval test's teeth check fail.

**Decision (plan §21, outcome 2):** The Stage 7 exclusion is correct product
behavior and is retained. The eval test was updated, not weakened: a genuinely
interpretive clause (`C1`, a conditional numeric rule — conditional rules yield
NEEDS_REVIEW with `unsupported=False` per `numeric_checker._check_entity` step 1)
was added **locally to the eval clause set** (`CLAUSES_EVAL = CLAUSES + [_INTERPRETIVE_CLAUSE]`).
The LLM pass annotates it, notes demonstrably differ across the two mock
retrievers, and the deterministic-verdict invariant is asserted over a strictly
larger finding set. The shared core fixture is untouched; all 219 core tests
unchanged.

**Contract clarification recorded:** advisory notes target *interpretive*
NEEDS_REVIEW findings only; *unsupported* findings intentionally receive no note.
The unified Finding contract (REM-ARCH-002) must preserve the
`unsupported` distinction.

## 4. Current entry points and their divergence (pre-remediation)

| Entry point | Location | Stages executed |
|---|---|---|
| `run_pipeline(bim_data, clauses, out_dir, meta)` | `api/pipeline.py:316` | compliance → coverage → reports. **No schema, no quality.** |
| `run_pipeline_from_ifc(ifc_path, clauses, out_dir, …, building_params)` | `api/pipeline.py:362` | schema → ingest → prepass → quality → compliance → coverage → reports |
| `run_ifc_compliance(ifc_path, clauses, …)` | `ingest/ifc_pipeline.py:62` | inner engine for the IFC path |

This is the divergence REM-ARCH-003 removes: the raw `bim_data` path bypasses
Quality entirely.

## 5. Current API request/report shapes (compatibility reference)

### `POST /analyze` (`api/main.py:95`) — asynchronous
- Body: `AnalyzeRequest` — `bim_data: Dict[str, Any]` (must include a `rooms`
  list; may embed a flat `building_params` dict), `meta: Optional[dict]`.
  **Clauses are NOT part of the request** — they are loaded once in the task
  layer from the configured corpus (`CLAUSES_PATH` → `mabhas_clauses.json`,
  `api/tasks.py:31,40`); an empty corpus rejects submission with 503
  (`EmptyClauseCorpusError`) unless `ALLOW_EMPTY_CLAUSES=1`.
- Flat `building_params` validated at the boundary by
  `parse_building_params` (`BuildingParamsError` → 400).
- **Immediate response:** `AnalyzeResponse` — `job_id: str`, `status: str`
  (`"queued"`). Nothing else.
- Job **result** (available via `GET /jobs/{job_id}` when completed) is the
  `run_pipeline` return dict: `summary` (PASS/FAIL/NEEDS_REVIEW counts),
  `coverage`, `duration_s`, `n_findings`, `reports` (basename map:
  json/html/pdf/bcf). Artifacts via `GET /jobs/{job_id}/report/{kind}`.

### `POST /analyze-ifc` (`api/main.py:120`) — asynchronous
- Multipart: `file` (IFC upload, capped at `MAX_IFC_UPLOAD_MB`, default 50),
  optional `plan_name: Form`, optional `building_params: Form` (JSON string,
  flat dict).
- Same job pattern: immediate `{job_id, status}`, poll `GET /jobs/{job_id}`,
  fetch `GET /jobs/{job_id}/report/{kind}`.

### Report payload (pre-v1, produced by `services/report_generator.generate_reports`)
- Input: `result.to_dict()` (`summary`, `findings`, `by_agent`), `meta`, `coverage`, optional `stages={"schema": …, "quality": …}` (IFC path only).
- Finding dict shape: `article_id`, `object_id`, `verdict`, `message`, `unsupported`, agent fields. Schema stage uses a separate `SchemaFinding` shape (`ingest/schema_validator.py:50`).
- No `report_schema_version`, no stable finding IDs, no unified stage status — targets of REM-REPORT-001/002.

### Manual inputs (pre-v1)
- Flat dict only (e.g. `{"wall_height_mm": 3200, "window_width_mm": 1200}`),
  parsed by `parse_building_params`.
- **Per-window/door/wall overrides are NOT implemented in this baseline.**
  `apply_window_overrides` exists only as a comment in
  `ingest/ifc_to_bim_data.py:329` — there is no such callable in the
  repository. (An earlier draft of this document wrongly claimed the
  function existed; corrected per the Phase 0 independent audit.) Phase 2
  (REM-INPUT-001/002) introduces per-element overrides from scratch through
  the versioned manual-input merger — do not plan Phase 2 around wiring an
  existing function.

## 6. Deprecation plan for old entry points

1. Phase 2 (PR 3): all three entry points become thin wrappers that build a
   `PipelineRequest` and delegate to `run_validation_pipeline`. Wrappers emit
   `DeprecationWarning` and are covered by equivalence tests.
2. One transition release: wrappers + flat `building_params` accepted
   (converted to `defaults`, deprecation note in report metadata).
3. Phase 9 (PR 12): wrappers and flat-input parsing removed after callers
   (`api/main.py`, `api/tasks.py`, scripts) migrate.

## 7. Phase 0 exit gate status

- [x] Core suite reproducibly passes (219/219).
- [x] Eval failure root-caused; fixed with explicit product decision (not skipped/deleted).
- [x] Full suite green: 375/375.
- [x] ADR-001/002/003 written (`docs/adr/`).
- [x] Current API request/report shapes documented (this file, §5).
- [x] Remediation branch created and sanitized source baseline tagged: branch `remediation/stage8`, baseline commit `9c015ba0ef276bb34d911160bbf237eccddefb9a`, tag `stage8-zip-baseline`. Full history transferable via `phase0.bundle` (`git clone phase0.bundle` or fetch into an existing repo).
- [x] Independent-audit corrections applied (API contract docs fixed, false `apply_window_overrides` claim removed, SHA-256 fake-LLM fingerprint, explicit advisory-eligibility assertions, ADR-001/002 identity determinism specified, ADR-003 parse-context requirement, full pip freeze + JUnit artifacts included).
