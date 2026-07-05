# Floor Plan → IFC → Mabhas-4 Compliance — Unified System

One repository, two services that share a **versioned IFC/BIM contract**:

```
 2D floor-plan photo
        │  Section 1 — vision API  (this repo's root: Flask + Mask R-CNN)
        │     image → bim_data (mm) → IFC4   (export/ifc_exporter.py)
        ▼
   plan.ifc  ── enriched, versioned (schema_version, canonical room_*, scale
        │        provenance, per-element confidence in Pset_SimsysProvenance)
        │  Section 2 — compliance engine  (compliance-engine/: FastAPI + agents)
        │     IFC → canonical bim_data → 4 deterministic agents → coverage → report
        ▼
   Mabhas-4 compliance report  (HTML / PDF / BCF) + honest coverage table
```

The two services are **deployed separately** (different ML/BIM dependency stacks)
and talk only through the IFC file — see Issue 12 in the review. Each has its own
`requirements.txt` and `Dockerfile`.

## Run it end-to-end

**Section 1 — produce the IFC** (root of this repo):

```bash
pip install -r requirements.txt              # needs the Mask R-CNN .h5 in weights/
python application.py                         # Flask on :5000
# upload an image + a real scale, then export the model:
#   POST /analyze     (form: image=@plan.png, scale_factor_mm_per_pixel=25.0, scale_source=user)
#   POST /export/ifc  → returns plan.ifc
python smoke_test.py --image plan.png --scale 25.0   # or run the whole thing at once
```

**Section 2 — check the IFC** (`compliance-engine/`):

```bash
cd compliance-engine
pip install -r requirements.txt
# CLI: one IFC → console summary + HTML/BCF/PDF reports
python -m ingest.run_ifc /path/to/plan.ifc --clauses data/mabhas_clauses.json --out reports
# or the HTTP API (IFC is a first-class endpoint):
CLAUSES_PATH=data/mabhas_clauses.json uvicorn api.main:app
#   POST /analyze-ifc  (file=@plan.ifc)  → {job_id}
#   GET  /jobs/{id}     → summary + coverage ;  GET /jobs/{id}/report/html
```

## The honest contract (what makes this defensible)

- **Versioned BIM** — `schema_version: "bim-canonical-v1"`, explicit `units`, and a
  `scale` block; the engine validates the version. (Issues 16)
- **Scale provenance + confidence** — every model records *where* its scale came
  from and a plausibility confidence. An **uncalibrated default scale is flagged**,
  and the engine **downgrades all dimensional verdicts to NEEDS_REVIEW** when scale
  is untrusted — a wrong scale never yields a false PASS/FAIL. (Issue 4)
- **One canonical room taxonomy** — `room_*` everywhere; unmappable labels are
  flagged `needs_review`, never guessed. Same mapping on both sides
  (`services/room_taxonomy.py` ↔ `compliance-engine/ingest/category_normalizer.py`).
  (Issue 2)
- **Honest coverage** — the report distinguishes `PASS / FAIL / NEEDS_REVIEW /
  UNSUPPORTED / BLOCKED_BY_MISSING_DATA` and prints a clause-coverage table.
  Unsupported clauses are listed, never silently omitted. (Issue 8)
- **End-to-end provenance** — per-element `source / confidence / needs_review`
  ride through `Pset_SimsysProvenance`; uncertain detections are deferred. (Issue 11)
- **Clause-corpus health** — the engine fails fast / reports `degraded` rather than
  running compliance against zero clauses. (Issue 9)

## Honest scope (what this system claims)

> The system implements a **semi-automated** pipeline that converts 2D floor plans
> into BIM/IFC data and automatically checks the **subset of Mabhas 4 supported by
> the extracted geometry and room data** (walls, doors, windows, room polygons, OCR
> room names, user building parameters). Unsupported or low-confidence clauses are
> **explicitly flagged for human review**. The running detector is a 4-class Mask
> R-CNN (wall/window/door + background); stairs/parking/balcony detection and the
> YOLO/Mask2Former paths are **not** in the runtime pipeline.

## Where to read more

- Section 1 details & API: [`readme.md`](readme.md)
- Section 2 ingestion, coverage, endpoints: [`compliance-engine/docs/IFC_INGESTION.md`](compliance-engine/docs/IFC_INGESTION.md)
- What changed in this pass: [`CHANGES_HONESTY_PASS.md`](CHANGES_HONESTY_PASS.md)
- Full review & remaining (deferred) work: the risk-review/fix-plan document
