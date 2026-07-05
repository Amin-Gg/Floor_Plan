# PROJECT_CHANGES.md

Engineering pass on the Floor_Plan pipeline (Section 1: Flask photo→IFC
service + Section 2: `compliance-engine/` FastAPI Mabhas engine), continuing
from the code-review findings. Date: 2026-07-05.

**Note on baseline.** The working tree already contained a set of fixes
matching the earlier review (applied outside this session): the `api/main.py`
docstring fix, the 50 MB chunked upload cap on `/analyze-ifc`,
`parse_building_params` / `BuildingParamsError` in `api/pipeline.py`,
`building_params` threading through `submit_ifc_job → _execute_ifc →
run_pipeline_from_ifc`, `python-multipart` in requirements, `ingest` in the
pyproject packages list, and a partial `param_is_user_supplied` helper in
`BimAdapter`. This pass verified those, completed the missing halves, and
fixed what remained. Everything below was verified by running the code, not
just by reading it.

---

## 1. Summary of what was fixed

### Deployment / runtime bugs (review round 1–2)
1. **Celery worker import failure** (`ModuleNotFoundError: No module named
   'services'` on every job). Root cause: Celery loads `api.tasks` via
   `import_from_cwd()`, which puts the engine root on `sys.path` only
   *temporarily*; the module-level `if path not in sys.path` guard therefore
   skipped inserting, and Celery removed the path before task execution.
   Fix: `_ensure_paths()` in `api/pipeline.py`, re-asserted at **call time**
   inside both pipeline entry points. Verified with a live Redis + worker.
2. **Stale job status in Celery mode** (`GET /jobs/{id}` said "queued"
   forever while the worker's disk state said "completed"). Fix:
   `get_job()` in `api/tasks.py` now reads disk **first** (worker truth) and
   merges it over the submitting process's in-memory entry; memory-only
   fields (e.g. `plan_name`) survive; partial disk writes fall back to
   memory instead of raising. Reproduced before, verified fixed after.
3. **Job-id path guard**: `job_id` from the URL must match
   `^[0-9a-f]{12}$` (exactly what `uuid4().hex[:12]` mints) before it is
   joined into any filesystem path. `/jobs/..` and friends now 404.
4. **`python-multipart`** confirmed present in
   `compliance-engine/requirements.txt` (FastAPI `File()`/`Form()` raises at
   startup without it) — one duplicate line removed.
5. **Import-time Groq key requirement removed** (`rag/groq_client.py`).
   The key pool is now resolved lazily on the first actual Groq call, so
   pure vector/hybrid retrieval imports without keys, the orchestrator's
   retriever factory no longer silently strips RAG citations when keys are
   absent at import, and pytest collection works keyless.

### Stale / duplicated code (task 2)
6. **`interface/` deleted** (3 files). The root copies of
   `ifc_to_bim_data.py` and `review_prepass.py` had drifted behind the
   engine's production `ingest/` versions (missing the scale-confidence
   gate, the contract Pset reading, and `downgrade_flagged_findings`), so
   Section 1's green tests validated an outdated contract. Root tests now
   load the **production** engine modules through
   `tests/_engine_modules.py` (file-path loading under collision-free
   aliases — no `sys.path` games, since the engine tree contains `services/`,
   `tests/`, `api/` that collide with Section 1's own packages). All previous
   assertions pass unchanged against the production modules.
7. **Stale eval tests rewritten** (they predated two design changes):
   - `eval/test_query_transforms.py`: was mocking the removed Anthropic
     client (20 collection errors); rewritten against the production
     `groq_chat` seam with the same behavioral contracts (language routing,
     token budgets, fallbacks, one-call multi-query, counters). The
     model/temperature constraint (qwen3-32b @ 0.3) is pinned at its new
     single change point: `groq_chat`'s defaults.
   - `eval/test_crag_retriever.py`: fixtures moved to **logit space**
     (production hits carry cross-encoder logits and the retriever applies
     `score_transform="sigmoid"`), and the filter tests now assert the
     **SQL push-down** design (filters forwarded to every retrieval leg,
     no over-fetch) instead of the removed over-fetch + post-filter layer.
     Added a test that filters reach transform legs too.

### Provenance bug found live during this pass
8. **Operator params were mis-tagged ENGINE DEFAULT on the IFC path.**
   `orchestrator.run_compliance` flat-merged API-supplied params into
   `bim_data["building_params"]` while leaving the ingest's
   `"_provided": []` untouched — the operator's value *drove the verdict*
   but the report claimed it was an engine default. Fixed at the merge
   point (explicit params join `_provided` when the block carries one;
   legacy flat blocks keep their old semantics). Caught by the live
   e2e run, now pinned by two regression tests.

---

## 2. Manual 3D-modeling parameters (task 3)

### The parameter set
The values an operator must assert because a 2D floor plan cannot yield
them (all in mm):

| Parameter (Section-1 name) | Meaning | Default |
|---|---|---|
| `wall_height` | finished floor level → underside of the slab/ceiling above (== the clear ceiling height used by Mabhas room-height checks) | 2800 |
| `window_height` | clear window opening height | 1200 |
| `window_sill_height` | FFL → bottom of the window | 900 |
| `door_height` | clear door opening height | 2100 |
| `floor_thickness` | structural slab thickness | 200 |
| `storey_elevation` | storey datum (export-time only) | 0 |

Window **width** is measured from the plan per window (it is visible in
2D), so it is *not* a global manual parameter on the Section-1 side; the
engine API additionally accepts `window_width_mm` for forward
compatibility. If you want a manual per-window width *override* (e.g. for
windows whose measured width is untrusted), that is an open question below.

### Where values are provided
- **Section 1** `POST /analyze` (form field `building_params`, JSON) and
  `POST /export/ifc` (both modes) — this already existed; verified.
- **Section 2** `POST /analyze-ifc` (form field `building_params`, JSON) —
  overrides anything carried in the IFC. Accepts both vocabularies (below).

### How they are validated
- Section 1: `utils/validators.validate_building_params` (partial dicts,
  per-key ranges) and `schemas.BuildingParams` (pydantic, defaults +
  ranges). **New:** cross-field consistency in both —
  `window_sill_height + window_height ≤ wall_height` and
  `door_height ≤ wall_height` (absent keys resolve to exporter defaults,
  because those are the values the exporter will actually use).
- Section 2: `api/pipeline.parse_building_params` — whitelist with ranges,
  typo rejection (a silently-dropped `celing_height_mm` would leave a
  default driving a verdict the operator believes they configured).
  **New:** the same cross-field check (enforced only when the involved
  keys are supplied together — missing ones may legitimately come from the
  IFC Pset), and **alias normalization**: Section-1 spellings
  (`wall_height`, …) are accepted and normalized to the engine's canonical
  `_mm` vocabulary, so the web UI can post one parameter dict to either
  service unchanged.

### How defaults are handled (the honesty rule)
Every parameter always has a value (defaults above), and a defaulted value
still produces a real PASS/FAIL verdict — that behavior was deliberate and
is preserved. What changed is **provenance**: the system now tracks *which*
values the operator actually asserted, end to end:
- `BimDataBuilder` embeds `bim_data["building_params"]` with a
  `"_provided": [...]` list of operator-supplied keys.
- The exporter writes `BuildingParamsProvided` into the IFC contract Pset.
- `BimAdapter` exposes `param_is_user_supplied()`, and room-height verdict
  messages now say either
  `[ceiling height = 3000 mm, user building parameter — not measured]` or
  `[ceiling height = 2800 mm, ENGINE DEFAULT — not user-confirmed; supply
  building_params.wall_height to assert the real value]`.
  A compliance report never claims a default was a user input.

### How they are passed into the pipeline (the contract choice)
The **IFC contract Pset is the primary mechanism** — this follows the
project's own interface spec ("Step 2 receives a path to a plan.ifc and
nothing else"), rather than inventing a parallel side-channel:
`export/ifc_exporter.py` writes `WallHeightMm`, `DoorHeightMm`,
`WindowHeightMm`, `WindowSillHeightMm`, `FloorThicknessMm` +
`BuildingParamsProvided` into `Pset_SimsysContract` on the IfcProject;
`ingest/ifc_to_bim_data.py` reads them back into
`bim_data["building_params"]`, mapping `WallHeightMm →
ceiling_height_mm` (the one value the engine cannot recover from
2D-derived geometry). The API form field on `/analyze-ifc` is the
*override* channel (validated at the boundary, wins over the Pset).
Precedence everywhere: **explicit API arg > IFC Pset / embedded block >
defaults**.

### How they affect the final model / verdicts
- 3D geometry: wall extrusion height, window opening height + placement Z
  (sill), door opening height, slab thickness — all exporter geometry uses
  the merged parameter set (this path already existed; verified).
- Compliance: door/window heights ride in the element geometry through the
  IFC; the wall/ceiling height now survives via the Pset and drives
  room-height verdicts. Verified live: posting
  `{"wall_height": 2250}` to `/analyze-ifc` flips the corpus summary from
  PASS 16 / FAIL 11 to PASS 15 / FAIL 12, with all three room-height
  findings tagged "user building parameter".

---

## 3. File inventory

### Modified (root project)
- `schemas.py` — cross-field `model_validator` on `BuildingParams`
- `utils/validators.py` — cross-field checks in `validate_building_params`
- `services/bim_builder.py` — `provided_keys` tracking; embeds the
  `building_params` block (+ `_provided`) in `bim_data`
- `export/ifc_exporter.py` — `_PARAM_PSET_MAP`; param precedence
  (arg > embedded block > defaults); writes params + provenance into
  `Pset_SimsysContract`
- `tests/test_ifc_roundtrip.py`, `tests/test_provenance_gate.py` — import
  the production engine modules via `tests/_engine_modules.py`
- `CLAUDE.md` — `interface/` reference updated to the single source of truth

### Modified (compliance-engine)
- `api/tasks.py` — disk-first `get_job` merge; `_JOB_ID_RE` guard
- `api/pipeline.py` — `_ensure_paths()` at call time; param alias
  normalization + cross-field check in `parse_building_params`; stale
  `BuildingParamsJson` comment corrected to the implemented mechanism
- `api/main.py` — (baseline work verified: docstrings, upload cap,
  `building_params` form field wired to `submit_ifc_job`)
- `rag/groq_client.py` — lazy key pool (`_keys()`), no import-time raise
- `ingest/ifc_to_bim_data.py` — reads the contract Pset params +
  `BuildingParamsProvided`; `WallHeightMm → ceiling_height_mm` mapping
- `services/numeric_checker.py` — `BimAdapter` honors `_provided`,
  aliases `wall_height`/`wall_height_mm` → `ceiling_height_mm`, tracks
  operator-supplied keys; honest default/user tags in room-height messages
- `services/orchestrator.py` — provenance-preserving param merge (fix #8)
- `tests/test_numeric_property_routing.py` — default-tag assertion updated
- `eval/test_query_transforms.py` — rewritten (Groq seam)
- `eval/test_crag_retriever.py` — logit fixtures; push-down filter tests
- `requirements.txt` — one duplicate `python-multipart` line removed
- `pyproject.toml` — (baseline: `ingest` in packages; verified)

### Created
- `tests/_engine_modules.py` (root) — production-module loader for tests
- `tests/test_manual_params_flow.py` (root, 12 tests) — validation,
  builder block, exporter Pset, production-loader roundtrip
- `compliance-engine/tests/test_building_params.py` (16 tests) — adapter
  provenance/aliases, honest tagging, synthetic-IFC Pset roundtrip,
  orchestrator-merge regressions
- `compliance-engine/tests/test_job_store.py` (25 tests) — disk-over-memory
  status, job-id guard incl. traversal probes, `/analyze-ifc`
  `building_params` boundary validation and threading
- `PROJECT_CHANGES.md` — this file

### Deleted
- `interface/__init__.py`, `interface/ifc_to_bim_data.py`,
  `interface/review_prepass.py` — stale duplicates of
  `compliance-engine/ingest/` (task 2; see fix #6)
- `compliance-engine/eval/test_query_transforms.py` (old version) —
  replaced, not patched: it mocked a client that no longer exists

### Excluded from the ZIP (not deleted from your machine)
`__pycache__/`, `.pytest_cache/`, and `graphify-out/` (contains only an
AST cache from an interrupted graphify run keyed to your local Windows
paths — re-run `/graphify .` to regenerate; the cache makes it fast).

---

## 4. Test commands and results

All runs on Python 3.12, engine runs with `GROQ_API_KEYS` **unset** (proves
keyless import/collection works).

| Command (from repo root) | Result |
|---|---|
| `python3 -m pytest tests/` | **69 passed** (57 before + 12 new) |
| `cd compliance-engine && python3 -m pytest` | **234 passed** (was 166 passed, 5 failed, 20 errors) |
| `cd compliance-engine && python3 -m pytest tests/` | 79 passed |
| `cd compliance-engine && python3 -m pytest eval/` | 155 passed |

Live end-to-end (not in the suites):
- **Thread mode**: `/analyze-ifc` on the sample fixture — default run
  PASS 16 / FAIL 11 / NEEDS_REVIEW 314 with 3 "ENGINE DEFAULT" tags;
  `building_params={"wall_height": 2250}` run PASS 15 / FAIL 12 with 3
  "user building parameter" tags and 0 default tags. HTML report 200.
- **Celery mode** (real Redis + worker, the exact documented invocation):
  same submission → worker completes, the **submitting** process's
  `GET /jobs/{id}` reports "completed" (staleness fix), report downloads
  (183 KB), summary PASS 15 / FAIL 12, tags correct. Traversal probe
  `/jobs/..` → 404.

No test results are approximated; every number above is from an actual run.

---

## 5. Known remaining limitations

1. **PDF reports** require WeasyPrint (system libs); absent, the service
   logs it and `/report/pdf` returns 404 while HTML/BCF work. Unchanged
   behavior, just noted.
2. **Multi-uvicorn-worker deployments**: job state is per-process memory +
   shared disk. The disk-first fix makes status correct as long as all
   workers share `RESULTS_DIR` (same container/volume). Separate machines
   need a shared volume — or move the store to Redis (open question 3).
3. `eval/` DB/embedding-dependent tests mock their backends; the true
   retrieval-quality evals still need the pgvector DB and keys (by design).
4. Engine window/door **height** rules measure the element fields that Step
   1 stamps from the global parameters — a per-element "this specific
   window is 1.6 m" override does not exist anywhere in the pipeline yet.
5. `classification/mabhas_classify.py` (offline one-time tool) still has a
   placeholder hardcoded key and Windows path; left untouched as out of
   scope, but recommend `os.getenv` before reuse.

## 6. Operator decisions (2026-07) — all four resolved and implemented

1. **Ceiling height stays a single global parameter** per building
   (single-storey scope). No per-room overrides. No code change needed.
2. **Per-window width overrides** — implemented (see Round 2 §B).
3. **Defaulted parameters no longer produce PASS/FAIL** — implemented
   (Round 2 §A): unasserted ceiling → NEEDS_REVIEW with instructions.
4. **Job storage migrated to Redis** — implemented (Round 2 §C).

---

# Round 2 (same date): decisions implemented + Docker refresh

## A. Strict default policy (decision 3)

`services/numeric_checker.py`: when `ceiling_height_mm` was NOT asserted by
the operator (via the API field, the IFC contract Pset's
`BuildingParamsProvided`, or a legacy flat `building_params` dict), every
room-height check now returns **NEEDS_REVIEW per room** with the message
"ceiling height not asserted — … Supply building_params.wall_height (mm) to
assert the real floor-to-slab height and get a PASS/FAIL verdict". The
engine default is never used for a verdict. Asserted values keep producing
real PASS/FAIL tagged `user building parameter — not measured`.

Live verification (sample corpus, 328 clauses): without params the summary
moved from PASS 16 / FAIL 11 / REVIEW 314 to **PASS 15 / FAIL 9 / REVIEW
317** — exactly the three room-height findings, each carrying the
instructive note; with `wall_height` asserted the three return as real
verdicts. Updated tests: `test_numeric_property_routing.py`,
`test_building_params.py` (4 assertions flipped to the new policy).

## B. Per-window width overrides (decision 2)

Windows are not standardized: plan-extracted widths are per-window defaults
the operator can override individually.

- **Input**: new `window_overrides` field on `POST /export/ifc` (both
  modes), shape `{"Window_3": {"width": 1400}}` — keyed by the window ids
  from the /analyze response. Documented in `GET /export/ifc/parameters`.
- **Validation**: `services/bim_builder.apply_window_overrides` — unknown
  window ids, unknown fields (heights/sills stay global by design), and
  out-of-range widths (300–5000 mm) are rejected loudly; a silently
  skipped typo would fake an applied override.
- **Provenance**: every window carries `width_source` ("measured" |
  "user"); the exporter writes it to `Pset_SimsysProvenance.WidthSource`
  and the engine's ingest reads it back — verified by an IFC roundtrip
  test. `bim_data["window_overrides_applied"]` lists what was overridden.
- Files: `services/bim_builder.py`, `export/ifc_exporter.py`,
  `routes/export_routes.py`, `compliance-engine/ingest/ifc_to_bim_data.py`,
  +7 tests in `tests/test_manual_params_flow.py`.

## C. Redis job store (decision 4)

New `compliance-engine/api/job_store.py` with two backends behind one
interface; `api/tasks.py` now routes all job state through it and
`api/main.py` serves report bytes from it:

- **RedisJobStore** (production): job status = Redis hash (JSON-encoded
  fields), finished reports = byte blobs, **and the uploaded IFC itself** =
  a blob stashed at submit and fetched (single-consumer, then deleted) by
  the worker — so API replicas and workers share **no volume at all**.
  Everything carries `JOB_TTL_SECONDS` (default 7 days; jobs age out).
- **LocalJobStore** (dev / `JOB_STORE=local` / no broker): exactly the
  previous semantics — in-process dict + `status.json` mirror, disk wins
  on read, artifacts from the job directory.
- Selection: `JOB_STORE=local` force > `JOB_STORE_REDIS_URL` >
  redis-scheme `CELERY_BROKER_URL` > local; unreachable Redis degrades to
  local with a loud warning.

Live proof of container-grade isolation: worker and API given different
scratch dirs AND the API-side upload file deleted immediately after submit
— the worker fetched the IFC from Redis, completed the job, and the 183 KB
report downloaded from Redis (`RESULTS_DIR` never shared). Thread mode
without a broker re-verified on LocalJobStore.

Tests: `tests/test_job_store.py` rewritten against the backends (36 tests,
FakeRedis stub — CI needs no broker): staleness semantics, artifacts, TTLs,
upload stash/fetch single-consumer, backend selection incl. degradation,
id-guard unchanged.

## D. Docker refresh (new task)

- **`docker-compose.yml` (new)**: `redis` (AOF, `volatile-ttl` eviction so
  memory pressure evicts oldest jobs, never broker structures),
  `compliance-api`, `compliance-worker` (same image, celery command,
  `celery inspect ping` healthcheck, `--scale compliance-worker=N`),
  `floorplan-api` (Section 1, weights mounted ro, GPU block ready to
  uncomment). Job store on Redis **DB 1**, broker on DB 0, so flushing one
  never nukes the other. No shared volumes between api and worker.
- **`.env.example` (new)**: all compose variables with comments.
- **`compliance-engine/Dockerfile`**: env contract updated
  (JOB_STORE_REDIS_URL / JOB_TTL_SECONDS / JOB_STORE; RESULTS_DIR
  re-documented as scratch), api-vs-worker roles documented at the CMD.
  No stage changes — the build itself was already correct.
- **Root `Dockerfile`**: unchanged structurally (working GPU build);
  orchestration now documented in compose.
- **`.dockerignore` (both)**: `+.env` (never bake credentials),
  root also `+graphify-out/`.

## E. Round 2 test results (all from actual runs)

| Command | Result |
|---|---|
| `python3 -m pytest tests/` (root) | **76 passed** (69 → 76) |
| `cd compliance-engine && python3 -m pytest` (keyless) | **245 passed** (234 → 245) |

Live e2e: thread mode (LocalJobStore) completed + report 200; Celery mode
with disjoint filesystems completed + report 200 from Redis; strict-default
run verified (§A numbers); user-param run verified (3 user tags, 0 default).

## F. Remaining limitations after Round 2

1. PDF reports still require WeasyPrint system libs (compose image has
   them; bare-metal dev may not).
2. Redis persistence: AOF is on in compose, but a Redis wipe loses
   in-flight jobs — acceptable for ephemeral jobs; resubmit is cheap.
3. Per-window overrides currently cover **width** only, by design
   (heights/sills are global single-storey parameters). The structure
   (`{"id": {field: value}}`) extends without a contract change.
4. The engine's window-width verdict messages do not yet distinguish
   measured vs user-asserted widths (the provenance IS carried in
   bim_data/IFC; surfacing it in messages is a small follow-up if wanted).
