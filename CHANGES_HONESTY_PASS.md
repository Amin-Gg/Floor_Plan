# CHANGES — Honesty & Contract Pass (unified)

Implements the **Core** subset of the risk-review/fix-plan across both services,
and unifies them into one package. All edits surgical/additive; no deterministic
verdict logic (the 4 agents / SpatialGraph / `run_compliance`) was modified.

## Section 1 (vision) — root of this repo

**Created**
- `services/room_taxonomy.py` — canonical room-category normalizer (EN/Persian/
  broad → `room_*`; unmappable → `needs_review`, never guessed). Mirrors the
  engine's `ingest/category_normalizer.py`. (Issue 2)

**Changed (additive)**
- `services/bim_builder.py` — emits the versioned canonical contract:
  `schema_version` + `units` + a `scale` block; normalizes room categories;
  adds `_assess_scale()` (plausibility-based scale confidence, flags the
  uncalibrated default). (Issues 16, 4, 2)
- `routes/visualization_routes.py` — records scale `source` (explicit
  `scale_source` form field or inferred) and passes the scale block to the
  builder. (Issue 4)
- `export/ifc_exporter.py` — writes `ScaleMmPerPixel/ScaleSource/ScaleConfidence`
  into `Pset_SimsysContract` (the engine already reads these). (Issues 4, 16)
- `schemas.py` — adds `scale_source` to the `/analyze` request. (Issue 4)
- `readme.md` — new "Canonical BIM contract (v1) & honest flags" section.
  (Issues 16/2/4 docs; detector-scope honesty was already in place — Issues 1/13)

**Already satisfied (no change needed)**
- Provenance Psets `Source/Confidence/NeedsReview/ReviewReason/NameSource` are
  already written and round-trip (Issue 11). Units are already consistent
  (`area_m2`/`m²` vs `mm`) (Issue 15). Detector scope is already documented
  honestly (Issues 1/13).

## Section 2 (compliance engine) — `compliance-engine/`

- Swapped in the **honesty-pass engine**: canonical normalizer (Issue 2),
  clause-coverage accounting with `UNSUPPORTED`/`BLOCKED_BY_MISSING_DATA` + report
  table (Issue 8), clause-corpus health/fail-fast (Issue 9), public
  `POST /analyze-ifc` (Issue 3), `schema_version` (Issue 16).
- **New this pass:** `ingest/review_prepass.py` gains a **scale-confidence gate** —
  when the IFC's `ScaleConfidence` is below `SCALE_CONFIDENCE_THRESHOLD` (default
  0.5), every dimensional element is flagged and its PASS/FAIL is downgraded to
  NEEDS_REVIEW. Backward-compatible: inactive for IFCs without scale provenance.
  (Issue 4, engine side)

## Cleanup
- Deleted `build/` and `dist/` (PyInstaller/egg build artifacts — regeneratable,
  nothing references them).
- Kept `interface/` — it is used by Section-1's own round-trip tests, not dead.

## Verified
- Builder emits schema/units/scale + canonical categories + scale confidence.
- End-to-end contract: Section-1 build → IFC export (passes the §A7 gate) → engine
  load reads scale → low-confidence scale flags all elements for review.
- Engine pytest-style suite green (normalizer, coverage, clause-health, round-trip,
  deterministic regression).

## Deferred (by choice — not in this Core pass)
- Geometry-reliability heuristics: room-extraction confidence/suspicious-room
  flags (Issue 5), host-wall binding scoring (Issue 6), exterior-classification
  confidence (Issue 7). The engine already downgrades any `needs_review` element,
  so the system stays honest without these; they improve detection quality.
- Interactive scale calibration UI and a learned room-segmentation model (Issues 4/5
  stretch). Test-suite pytest refactor (Issue 14). Separate Dockerfiles polish (Issue 12).
