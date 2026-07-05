# FloorPlanTo3D API — Section 1 (Mask R-CNN → BIM → IFC)

Turns a **photograph of a residential floor plan** into structured `bim_data`
(walls, doors, windows, rooms — all in millimetres) and a downloadable **IFC4**
file that opens in Revit, ArchiCAD, FreeCAD/Bonsai, Solibri, and any IFC4 viewer.
The IFC output is the input to a **separate** downstream project that checks the
model against the Iranian National Building Regulations (Mabhas).

> **Scope of this repository = Section 1 only:** the vision API that produces
> the 3D/BIM data. The Mabhas compliance engine and the supplementary YOLO
> detector are **separate phases** and are not part of this running service
> (see *Project phases* below).

---

## What the running model actually detects

The live detector is **Mask R-CNN** (Matterport, Keras/TensorFlow), configured
for **4 classes**: `background + wall + window + door`
(`config/settings.py → NUM_CLASSES = 4`).

| Element | How it is produced | Detected by the model? |
|---------|--------------------|------------------------|
| Walls   | Mask R-CNN masks → skeletonise → centerlines, junctions, thickness | ✅ yes |
| Doors   | Mask R-CNN boxes/masks → orientation, size, host wall | ✅ yes |
| Windows | Mask R-CNN boxes/masks → size, host wall | ✅ yes |
| Rooms   | **Geometric** flood-fill of the wall mask + watershed split; **type/name from OCR only** | ⚠️ derived, not detected |
| Stairs / balcony / parking / terrace / railing / closet | code branches exist but **never fire** with a 4-class model | ❌ no (Section 2 / Mask2Former) |

**Practical consequence for code-checking:** scope your rules to walls, doors,
windows, and OCR-named rooms. Rooms with no OCR label are emitted with
`name_source: "none"` and `needs_review: true` — treat those as
`NEEDS_REVIEW`, not as compliant. There is no stair/guard/egress geometry from
this model.

---

## Canonical BIM contract (v1) & honest flags

The `bim_data` (and the IFC built from it) now carries a versioned, honest
contract that the compliance engine reads directly:

- **`schema_version: "bim-canonical-v1"`** + **`units: {length: mm, area: m2}`** —
  one explicit contract; the engine validates the version (Issue 16).
- **`scale: {mm_per_pixel, source, confidence, needs_review, reasons}`** — the
  pixel→mm scale, *where it came from* (`user` / `ocr` / `calibration` /
  `default`), and a plausibility-based confidence. An **uncalibrated default
  (1 mm/px) is flagged** (confidence 0.3); implausible door/room dimensions lower
  it. When confidence is low, the engine downgrades **all** dimensional verdicts
  to `NEEDS_REVIEW` — a wrong scale can never produce a false PASS/FAIL (Issue 4).
- **Canonical room categories** — every room is normalized to `room_bedroom /
  room_kitchen / room_bathroom / room_living / room_toilet / room_entry /
  room_storage` via `services/room_taxonomy.py` (English + Persian + broad-bucket
  aliases). Anything unmappable keeps its raw label and is marked
  `needs_review` — **never guessed** (Issue 2). `bim_data._category_summary`
  reports canonical / normalized / unmapped counts.
- **Per-element provenance** — `source / confidence / needs_review /
  review_reason` ride through to `Pset_SimsysProvenance` in the IFC, so the
  engine can defer uncertain elements (Issue 11). These flags are now set by
  geometry-reliability heuristics:
  - **Rooms** (Issue 5) — `confidence` + `review_reasons` from a quality check
    (implausible area, thin/sliver polygon, image-border contact, missing OCR
    type, degenerate polygon). Suspicious rooms are flagged, never trusted.
  - **Doors & windows** (Issue 6) — host-wall binding carries
    `host_wall_confidence`, `host_wall_distance_mm`, and `candidate_host_walls`;
    openings with a far or ambiguous host are flagged.
  - **Exterior walls** (Issue 7) — `exterior_confidence` + `exterior_reasons`;
    a window on a low-confidence-exterior wall inherits review so it can't drive
    a hard natural-light / ventilation verdict.

  When any element is flagged, the compliance engine downgrades the verdicts that
  depend on it to `NEEDS_REVIEW` — a brittle detection can never become a false
  PASS/FAIL.

`services/room_taxonomy.py` mirrors the engine's `ingest/category_normalizer.py`
— keep the two `ALIASES` dicts in sync when adding vocabulary from real plans.

## Pipeline

```
image ─► validate / resize ─► Mask R-CNN .detect() ─► {walls, windows, doors}
                                     │
   ┌─────────────────────────────────┼─────────────────────────────────────┐
   │ wall masks → skeleton → segments → junctions → wall params (mm)        │
   │ door/window boxes → orientation, size, nearest host wall               │
   │ PaddleOCR → space names (Persian / English)                           │
   │ flood-fill wall mask → room polygons → bind OCR name (or needs_review) │
   └─────────────────────────────────┬─────────────────────────────────────┘
                                     ▼
                       BimDataBuilder → bim_data (mm)
                                     ▼
                  (POST /export/ifc) ifc_exporter → .ifc (IFC4)
```

---

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/analyze` | Upload an image → `bim_data` JSON + saved visualisation + analysis report |
| POST | `/export/ifc` | Convert a prior `/analyze` result (or uploaded `bim_json`) → IFC4 file |
| GET  | `/export/ifc/parameters` | List building-height parameters and defaults |
| GET  | `/health` | Model + system diagnostics (reports `num_classes: 4`) |
| POST | `/analyze_accuracy` | Accuracy/diagnostics route |

Interactive docs: `http://localhost:8080/openapi/swagger`
(Note: `/analyze` and `/analyze_accuracy` use `@bp.route`, so they run but do
not appear in the Swagger UI — call them directly.)

---

## Quick start

```bash
# 1) Python 3.10 or 3.11 (TensorFlow 2.13 does NOT support 3.12+)
python3.11 -m venv venv && source venv/bin/activate

# 2) System lib for paddle, then deps
sudo apt-get install -y libgomp1
pip install -r requirements.txt
#    Verify the two load-bearing pins:
python -c "import numpy; assert numpy.__version__ == '1.24.3', numpy.__version__"
python -c "import ifcopenshell, ifcopenshell.api.geometry; print('ifcopenshell', ifcopenshell.version)"

# 3) Place the Mask R-CNN weights (see weights/README.md)
#    weights/maskrcnn_15_epochs.h5   (~244 MB, 4-class)

# 4) Run
python application.py                      # development
APP_ENV=production gunicorn --config gunicorn.conf.py application:application
```

Smoke test (recommended after install):

```bash
python smoke_test.py --health-only                 # imports + health + openapi
python smoke_test.py --image plan.png --scale 10.0 # full /analyze + /export/ifc
```

---

## Building parameters (heights, mm)

Pass per-request as a JSON form field `building_params`, or rely on the
Iranian-residential defaults:

| Parameter | Default (mm) |
|-----------|-------------:|
| `wall_height` | 2800 |
| `door_height` | 2100 |
| `window_height` | 1200 |
| `window_sill_height` | 900 |
| `floor_thickness` | 200 |

---

## Project phases

- **Section 1 (this repo, active):** Mask R-CNN vision API → `bim_data` → IFC4.
- **Section 2 (later, not wired):** supplementary **YOLO** detector
  (`yolo_best.pt`) for columns / railings / staircases, merged at the
  `bim_data` layer. Design lives in `config/yolo_classes.py`,
  `models/yolo_detector.py`, `services/yolo_elements.py`. `build_yolo_elements`
  is **not called yet** — do not expect YOLO output until Section 2 is built.
- **Downstream (separate project):** the Mabhas compliance engine
  (deterministic checks + RAG) consumes this IFC/`bim_data`. It is **not** in
  this repository.

A shelved **Mask2Former** path also exists in the codebase (15-class taxonomy
in `config/classes.py`, kept for that future work). It does **not** drive the
running service.

---

## Review fixes applied (see CHANGES_REVIEW_FIXES.md)

This checkout has had the Critical/High review items addressed: the
`ifcopenshell` pin (C1), a visible weight-loader warning (C2), corrected
`weights/README.md` (C3), explicit 4-class scope (H1), `needs_review` room
flags (H2), accurate `model_mode` reporting (M3), and a clarifying banner on
`config/classes.py` (M2). The supplementary YOLO integration (H3) is
intentionally deferred to Section 2.
