# Step 1 (IFC) → Step 2 (engine) ingestion

This wires the **enriched `plan.ifc`** produced by Step 1 (the vision pipeline)
into this compliance engine, per the *Two-Step IFC Interface* spec. Step 2 now
consumes **only an IFC file path** — everything it needs is in the file.

```
plan.ifc
   │  ingest.ifc_to_bim_data            (B1) reconstruct the agents' bim_data dict
   │                                          + schema_version, units, scale  (Issue 16)
   ▼
bim_data
   │  ingest.normalize_room_categories  (Issue 2) OCR/Persian/broad labels → room_*;
   │                                          unmappable → needs_review (never guessed)
   │  ingest.apply_review_prepass        (B2) flag uncertain elements (confidence / NeedsReview)
   ▼
run_compliance(bim_data, clauses)        the four agents + SpatialGraph — UNCHANGED
   │  ingest.downgrade_flagged_findings  (B2) PASS/FAIL on a flagged element → NEEDS_REVIEW
   │  services.build_coverage            (Issue 8) clause-level coverage accounting
   ▼
ComplianceResult + coverage → report_generator (HTML / PDF / BCF)
```

No agent, `SpatialGraph`, `orchestrator`, or `run_compliance` was modified.
Normalization, review pre-pass, and coverage all run in the ingest/pipeline/report
layers around the unchanged deterministic spine. The loader output matches the
exact `bim_data` shape those modules already consume.

## Honesty & coverage (Issues 2, 8, 9, 16)

* **Canonical room taxonomy (Issue 2).** `ingest/category_normalizer.py` maps
  English ("Kitchen"), Persian ("آشپزخانه"), and broad labels to the canonical
  `room_*` strings the agents match on. Ambiguous buckets ("Service",
  "Accommodation", "Unknown") and anything unmatched are **flagged
  `needs_review` rather than guessed**, so an unknown room can never silently
  pass or fail a rule. Edit the `ALIASES` dict in that file for new vocabulary.
* **Clause coverage (Issue 8).** `services/coverage.py` reclassifies every
  finding/clause into five honest classes — `PASS`, `FAIL`, `NEEDS_REVIEW`,
  `UNSUPPORTED` (no automatic check exists) and `BLOCKED_BY_MISSING_DATA`
  (checkable, but the required element is absent). The report and CLI print a
  clause-coverage table; unsupported clauses are listed, never silently omitted.
  This is a side-by-side accounting — the agents' `Verdict` enum is unchanged.
* **Clause-corpus health (Issue 9).** `load_clauses(path, required=True)` fails
  fast on an empty/missing corpus; `/health` reports `clause_count` +
  `clause_status`; job submission is rejected on an empty corpus unless
  `ALLOW_EMPTY_CLAUSES=1` (test mode).
* **Versioned contract (Issue 16).** The loader stamps `schema_version:
  "bim-canonical-v1"`, `units {length: mm, area: m2}`, and a `scale` block.

## Run it

```bash
# one IFC → console summary + HTML/BCF/PDF reports
python -m ingest.run_ifc path/to/plan.ifc --clauses data/mabhas_clauses.json --out ifc_reports

# options
python -m ingest.run_ifc plan.ifc --threshold 0.6   # review-confidence cutoff (env REVIEW_CONFIDENCE_THRESHOLD)
python -m ingest.run_ifc plan.ifc --no-reports       # verdicts only
python -m ingest.run_ifc plan.ifc --json             # full result as JSON
```

The CLI prints the verdict summary, how many elements were flagged / verdicts
downgraded for low confidence, the **room categories the plan contained**, and
the report paths. Exit code is non-zero if any hard `FAIL` is present (CI-friendly).

Programmatic entry points:

```python
from ingest import run_ifc_compliance
result, bim_data = run_ifc_compliance("plan.ifc", clauses)   # → ComplianceResult, dict

# or, for the API/Celery layer (mirrors run_pipeline, returns report file names):
from api.pipeline import run_pipeline_from_ifc, load_clauses
out = run_pipeline_from_ifc("plan.ifc", load_clauses("data/mabhas_clauses.json"), out_dir="ifc_reports")
```

HTTP API (Issue 3) — IFC is a first-class public compliance path:

```bash
# upload an enriched plan.ifc; returns {job_id, status}
curl -F file=@plan.ifc -F plan_name="Unit 3B" http://localhost:8000/analyze-ifc
curl http://localhost:8000/jobs/<job_id>                 # poll → summary + coverage
curl http://localhost:8000/jobs/<job_id>/report/html -o report.html
curl http://localhost:8000/health                        # clause_count + clause_status
```

`POST /analyze-ifc` saves the upload, runs B1→B2→agents→coverage→reports on a
worker (Celery if a broker is set, else a background thread), exactly like
`POST /analyze` does for `bim_data`.

## Confidence / honest-degradation (§B2)

Each reconstructed element carries provenance from the IFC (`Source`,
`Confidence`, `NeedsReview`, `ReviewReason`). Before the agents run, the pre-pass
flags any element with `NeedsReview=true` or `Confidence < threshold`. After the
agents run, **any PASS/FAIL whose `element_id` is a flagged element is forced to
NEEDS_REVIEW**, with the reason appended — an uncertain detection never yields a
silent (possibly wrong) verdict. Counts surface as `_review_summary.flagged_count`
and `_review_summary.downgraded_count`.

(The Step-1 model does not yet emit per-element confidence, so today everything
comes through at `Confidence=1.0` and nothing is downgraded — except rooms whose
name came from failed OCR, which arrive `NeedsReview=true`. The field is already
in the contract, so when the detector starts scoring elements, this turns on with
no further wiring.)

## Category vocabulary — now normalized (Issue 2)

The agents match rooms by canonical category strings: `room_bedroom`,
`room_kitchen`, `room_bathroom`, `room_living`, `room_toilet`, `room_entry`,
`room_storage` (see `OBJECT_MAP` in `services/numeric_checker.py` and the category
maps in `topology_agent` / `safety_agent`). Step 1's running 4-class model + OCR
path emits whatever the OCR space-name dictionary yields — English, Persian, or a
broad bucket — **not** necessarily `room_*`.

`ingest/category_normalizer.py` now bridges that seam: it runs first in the IFC
pipeline and resolves each room's label to a canonical `room_*` (trying the
category, then the OCR name, then the Persian `local_name`). Anything it cannot
confidently resolve — broad buckets like "Service"/"Accommodation"/"Unknown", or
an unrecognized label — is **flagged `needs_review` and left unmapped, never
guessed**, so it surfaces for review instead of silently mis-checking. The CLI's
`Rooms:` line and the report show canonical / normalized / unmapped counts;
`_category_summary.unmapped_raw` lists the exact labels that need a new alias.
Add new vocabulary by editing the `ALIASES` dict in that one file.

## Round-trip contract guarantee (§B3)

`tests/test_ifc_roundtrip_verdicts.py` asserts the interface is lossless for
verdicts:

```
run_compliance(bim_data).summary == run_compliance(ifc_to_bim_data(export(bim_data))).summary
```

Self-contained fixtures (no Step-1 exporter needed at test time):
`tests/fixtures/sample_plan_bim.json` (source plan) and
`tests/fixtures/sample_plan.ifc` (its Step-1 export). Regenerate with
`tests/fixtures/regen_sample_plan.py` if the exporter or plan changes.

```bash
python -m pytest tests/test_ifc_roundtrip_verdicts.py eval/test_verdict_regression.py -q
```

## Dependency

`ifcopenshell>=0.8.0,<0.9` added to `requirements.txt` (reads the IFC). Tested on
ifcopenshell 0.8.5.

## Files added / changed

```
ingest/                         NEW package (Step 2 IFC entry)
  __init__.py
  ifc_to_bim_data.py            B1 — IFC → bim_data (inverse of Step-1 exporter)
  review_prepass.py             B2 — pre-pass flagging + honest-degradation post-pass
  ifc_pipeline.py               run_ifc_compliance(ifc_path, clauses, …)
  run_ifc.py                    CLI:  python -m ingest.run_ifc plan.ifc
api/pipeline.py                 + run_pipeline_from_ifc(...)   (appended; nothing removed)
requirements.txt                + ifcopenshell pin
tests/test_ifc_roundtrip_verdicts.py   NEW — §B3 verdict-equality + §B2 downgrade unit test
tests/fixtures/sample_plan.ifc         NEW — sample Step-1 IFC
tests/fixtures/sample_plan_bim.json    NEW — its source bim_data
tests/fixtures/regen_sample_plan.py    NEW — regenerator
docs/IFC_INGESTION.md           NEW — this file
```

Deferred (next milestone): **Lane 1 (IDS / IfcTester)** — author `.ids` specs for
the clean numeric clauses, run `ifctester` on the IFC, map results to the
`Finding` format, and merge with the agent (Lane 2) findings into one report.
Needs `ifctester` added to the environment.
