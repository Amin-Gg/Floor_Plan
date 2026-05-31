# Floor Plan to Mabhas Compliance

An end-to-end system that turns a photograph of a residential floor plan into a
formal Iranian building-code compliance report. The pipeline runs in two
connected stages: a Mask2Former-based vision model that produces structured
`bim_data` and an IFC4 file, and a deterministic compliance engine that checks
that data against the digitised National Building Regulations (مقررات ملی
ساختمان / Mabhas) and issues a Persian-RTL municipal control letter.

The project was developed as a master's thesis and is designed for both
academic defence and a downstream SaaS product. Scope: residential occupancy
group **M-4** (1–2 household, max 3 storeys).

---

## Architecture

```
              ┌────────────────────────────────────────────────────────────┐
              │                  STAGE 1 — Vision + BIM                    │
              │                                                            │
   photograph │      Mask2Former (Swin-Large, fine-tuned)                  │
   ─────────► │            ↓                                               │
              │      bim_data JSON  (walls, doors, windows, rooms,         │
              │                     stairs, balconies, terraces — all mm)  │
              │            ↓                                               │
              │      IFC4 export  (Revit / ArchiCAD / FreeCAD)             │
              └─────────────────────────────┬──────────────────────────────┘
                                            │
                                            ▼  bim_data
              ┌────────────────────────────────────────────────────────────┐
              │              STAGE 2 — Mabhas Compliance Engine            │
              │                                                            │
              │   ┌───────────────┐                                        │
              │   │ Spatial graph │  (NetworkX, Shapely)                   │
              │   └──────┬────────┘                                        │
              │          │           ┌──── numeric checker  ────┐          │
              │          ├──────────►│      topology agent      │          │
              │          │           │      opening agent       │          │
              │          │           │      safety agent        │          │
              │          │           └──────────┬───────────────┘          │
              │          ▼                      ▼                          │
              │   RAG (pgvector)  ──►  Orchestrator (LangGraph)            │
              │   324 Mabhas clauses           │                           │
              │                                ▼                           │
              │            Human review queue ──► Final findings           │
              │                                ▼                           │
              │      Municipal compliance letter (HTML + PDF + BCF)        │
              └────────────────────────────────────────────────────────────┘
```

### Design principle — deterministic spine, AI on the wings

Every PASS / FAIL verdict in the compliance engine is produced by deterministic
Python. The LLM is restricted to three roles: offline classification of the
Mabhas corpus (once, with human review), optional advisory notes on
NEEDS_REVIEW items, and the narrative paragraph of the report. **The LLM cannot
override a deterministic verdict.** This keeps every result reproducible,
traceable to a specific Mabhas article, and defensible — three properties
non-negotiable in a compliance context.

When a clause cannot be verified from the plan alone (site conditions,
interpretive requirements), the engine returns `NEEDS_REVIEW` and routes the
item to a human review queue. It never guesses.

---

## Repository structure

```
Floor_Plan/
├── application.py              Flask + OpenAPI entry point (Stage 1)
├── Dockerfile                  Production image, two-stage build
├── gunicorn.conf.py            Production server config
├── requirements.txt            Stage 1 dependencies (ML + Flask)
├── train_mask2former.py        HuggingFace fine-tuning script
├── evaluate.py                 mAP evaluation (torchmetrics)
│
├── config/                     Class definitions, settings, constants
│   └── classes.py              ← single source of truth for class IDs
├── models/                     Mask2Former inference engine
├── routes/                     Flask APIBlueprints (analyse / export / health)
├── analysis/                   Wall, room, door, window, stair, junction
├── image_processing/           Image loading and mask processing
├── export/
│   └── ifc_exporter.py         IFC4 generation via ifcopenshell
├── services/                   bim_data builder, JSON service, analysis report
├── utils/                      Geometry, validators, inference executor
├── visualization/              Wall visualisation
├── notebooks/                  Colab smoke test
├── weights/                    Model checkpoints (mounted in production)
│
└── Compliance Engine/          Stage 2 — Mabhas checker
    ├── README.md
    ├── requirements.txt        Stage 2 dependencies
    ├── classification/         Mabhas .docx → structured JSON corpus
    ├── RAG/                    pgvector schema, embeddings, retriever
    ├── services/               Spatial graph, 4 agents, orchestrator,
    │                           report generator, review queue
    ├── api/                    FastAPI + Celery async job queue
    ├── tests/                  62 tests across both stages
    ├── data/                   594-clause Mabhas corpus + sample
    └── sample_output/          A real generated municipal letter
                                (compliance_report.html / .pdf,
                                 compliance_issues.bcf)
```

---

## Quick start

### Stage 1 — the ML + BIM service

```bash
pip install -r requirements.txt

# development
python application.py
# Swagger UI: http://localhost:8080/openapi/swagger

# production
APP_ENV=production \
FLOORPLAN_MODEL_PATH=./weights/mask2former-floorplan-finetuned \
ALLOW_COCO_FALLBACK=false \
gunicorn --config gunicorn.conf.py application:application
```

### Stage 2 — the compliance engine

```bash
cd "Compliance Engine"
pip install -r requirements.txt
pip install -U fonttools          # see "Setup notes" below

# run the full test suite — no database or API key required
python -m pytest tests/ -q

# run the engine on a bim_data dict
python -c "
from services.orchestrator import run_compliance
from services.report_generator import generate_reports
import json

with open('data/mabhas_clauses.json') as f:
    clauses = [c for c in json.load(f) if not c.get('skip_category')]

# bim_data is whatever Stage 1 produced
result = run_compliance(my_bim_data, clauses)
paths = generate_reports(result.to_dict(),
                         {'plan_name': 'Plan_04',
                          'occupancy_fa': 'مسکونی — گروه M-4'},
                         out_dir='out/')
print(paths)   # {'html': ..., 'pdf': ..., 'bcf': ...}
"
```

### Run with Docker

```bash
docker build -t floorplan3d-api:1.0 .

# CPU-only
docker run -p 8080:8080 \
    -e APP_ENV=development \
    -v /path/to/weights:/app/weights:ro \
    -v /tmp/outputs:/app/outputs \
    floorplan3d-api:1.0

# GPU (requires nvidia-container-toolkit on the host)
docker run -p 8080:8080 --gpus all \
    -e APP_ENV=production \
    -e FLOORPLAN_MODEL_PATH=/app/weights/mask2former-floorplan-finetuned \
    -v /opt/floorplan/weights:/app/weights:ro \
    -v /opt/floorplan/outputs:/app/outputs \
    floorplan3d-api:1.0
```

The model weights are mounted as a read-only volume rather than baked into
the image, so one image works for development, staging, and production by
pointing at different weight folders.

---

## Stage 1 — Vision + BIM

### Model

Fine-tuned **Mask2Former** with a Swin-Large backbone, trained on annotated
architectural floor plans. The generic COCO base model is loaded only during
development (`ALLOW_COCO_FALLBACK=true`); production requires the fine-tuned
checkpoint.

### Class mapping

All class definitions live in `config/classes.py` — the single source of truth.

| Project ID | Training ID | Name     |
|:----------:|:-----------:|----------|
| 1          | 0           | wall     |
| 2          | 1           | window   |
| 3          | 2           | door     |
| 4          | 3           | stairs   |
| 5          | 4           | parking  |
| 6          | 5           | balcony  |
| 7          | 6           | terrace  |

Project IDs (1–7) appear in COCO annotation files, the `bim_data` output, and
the API. Training IDs (0–6) are used inside the model head (contiguous,
0-indexed).

### Training

```bash
python train_mask2former.py \
    --dataset_dir ./dataset \
    --output_dir  ./weights/mask2former-floorplan-finetuned \
    --epochs      50 \
    --batch_size  2 \
    --grad_accum  8 \
    --fp16
```

Dataset format is COCO instance segmentation with the 7 categories above.
Recommended annotation tool: [CVAT](https://cvat.ai), export as COCO
instance segmentation.

### Evaluation

```bash
python evaluate.py --checkpoint ./weights/mask2former-floorplan-finetuned
python evaluate.py --checkpoint ./weights/mask2former-floorplan-finetuned \
    --find_best_threshold
```

Set `DETECTION_MIN_CONFIDENCE` in `config/settings.py` to the best value found.

### API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/analyze` | upload a floor-plan image → `bim_data` JSON |
| `POST` | `/analyze_accuracy` | reliability analysis on a plan |
| `POST` | `/export/ifc` | convert `bim_data` to IFC4 file |
| `GET`  | `/export/ifc/parameters` | list all `building_params` with defaults |
| `GET`  | `/health` | server and model status |
| `GET`  | `/openapi/swagger` | interactive API documentation |

### IFC export

```bash
curl -X POST http://localhost:8080/export/ifc \
  -H "Content-Type: application/json" \
  -d '{
    "analysis_file": "plan_abc12345.json",
    "building_params": {
      "wall_height": 3000,
      "door_height": 2100,
      "window_sill_height": 900,
      "window_height": 1200,
      "floor_thickness": 200,
      "project_name": "Block 4 - Unit 12"
    }
  }' --output model.ifc
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `FLOORPLAN_MODEL_PATH` | *(none)* | path to the fine-tuned checkpoint directory |
| `ALLOW_COCO_FALLBACK` | `true` | allow the generic COCO model in dev; **set `false` in production** |
| `APP_ENV` | `development` | `development` / `production` / `testing` |
| `APP_CORS_ORIGINS` | `*` | comma-separated allowed origins in production |
| `GUNICORN_WORKERS` | `1` | gunicorn workers (one per GPU recommended) |
| `INFERENCE_WORKERS` | `1` | concurrent inference threads per worker |
| `INFERENCE_TIMEOUT` | `90` | seconds before a slow inference is aborted |

---

## Stage 2 — Mabhas Compliance Engine

### The four agents

Each agent is a deterministic Python specialist with a narrowly scoped domain.
All findings flow through one shared `Finding` type with a three-state verdict
(PASS / FAIL / NEEDS_REVIEW).

| Agent | Checks |
|---|---|
| **Numeric checker** (`services/numeric_checker.py`) | Room areas, ceiling heights, room dimensions, door widths — any measurable threshold mapped through `OBJECT_MAP` |
| **Topology agent** (`services/topology_agent.py`) | Adjacency and reachability rules through the door graph (the headline check: kitchen must not connect directly to a bathroom) |
| **Opening agent** (`services/opening_agent.py`) | Glazing-to-floor ratios, natural-light presence; site-dependent rules (open space, light wells) are conservatively flagged for review |
| **Safety agent** (`services/safety_agent.py`) | Egress reachability, stair presence, balcony/terrace guard requirements — highest bar for an automatic verdict because life-safety errors are the most dangerous |

The **orchestrator** (`services/orchestrator.py`) builds the spatial graph
once, runs all four agents in parallel via LangGraph (with an identical
sequential fallback if LangGraph is absent), merges their findings, and
optionally annotates `NEEDS_REVIEW` items with advisory notes using RAG
context. Verified to be fully offline-capable.

### The RAG layer

The Mabhas corpus (594 clauses across 8 files of the regulations) is
classified offline by `classification/mabhas_classify.py` using GPT-OSS-120B
into structured JSON: each clause is tagged with rule type (numeric / spatial
/ definition / exception), applicable occupancy groups, height groups, and
structured `entities`. The result is embedded with
`intfloat/multilingual-e5-large` and stored in PostgreSQL + pgvector. Under
the M-4 residential scope, 324 clauses are ingested. Adding occupancy groups
later requires no re-classification — only a wider ingest filter.

### The output — Iranian municipal letter

`services/report_generator.py` produces three files from one fixed,
data-bound template:

- **HTML** — a self-contained Persian-RTL **اخطار رفع نواقص ساختمانی**
  (defect rectification notice) in the style of an Iranian municipal control
  document, with the شهرداری تهران letterhead, document meta strip
  (شماره / تاریخ / پیوست with a Jalali date), narrative paragraph with
  inline colored counts, مشخصات ملک property table, four metric cards in
  Persian numerals, proportional compliance bar, defects table with the
  five-column structure (ردیف / بند مقررات / شرح حکم / مغایرت مشاهده‌شده /
  وضعیت), signature block, and a Persian methodology footer.
- **PDF** — rendered FROM the HTML by WeasyPrint, so the two cannot drift.
- **BCF 2.1** — one issue topic per FAIL and NEEDS_REVIEW (PASS findings
  excluded; BCF tracks issues, not what passes). Opens directly in Revit,
  BIMcollab, and That Open Engine.

The Spectral font is included as a sibling `spectral_fonts.css`, embedded as
base64 `@font-face` rules so the report is fully portable.

A real generated example is in `Compliance Engine/sample_output/`.

### The async web service

`Compliance Engine/api/` exposes the engine as a FastAPI service:

| Method | Path | Description |
|---|---|---|
| `POST` | `/analyze` | submit `bim_data` + meta → returns a job id |
| `GET`  | `/jobs/{id}` | poll job status, includes the result when completed |
| `GET`  | `/jobs/{id}/report/{kind}` | download the report (`kind` = `html` / `pdf` / `bcf`) |
| `GET`  | `/health` | liveness and which execution mode is active |

The service runs in two modes selected automatically. Without a broker, jobs
run in a background thread with in-memory status mirrored atomically to disk
— no infrastructure required, perfect for development and the test suite.
With `CELERY_BROKER_URL` set, the same code dispatches to a Celery worker for
true async processing across machines. Switching modes is one environment
variable; no code change.

### The human review queue

`services/review_queue.py` manages the `NEEDS_REVIEW` items that the engine
deliberately could not auto-verify. Two safety properties define it:
deterministic verdicts are never touched (only `NEEDS_REVIEW` items enter the
queue, and the test suite confirms FAIL counts are unchanged after any number
of decisions), and decisions never leak across plans (auto-applying one
building's verdict to another would risk propagating a wrong call). Reviewer
decisions persist to a JSON store with atomic writes and merge back into the
result for the final signed report. `suggestions()` surfaces clauses
repeatedly decided the same way as candidates to encode as deterministic
rules later — the safe form of feedback to the rule registry.

### Tests

```bash
cd "Compliance Engine"
python -m pytest tests/ -q
```

62 tests across nine suites cover the spatial graph, each of the four agents,
the orchestrator (both LangGraph and sequential paths), the report generator
(HTML structure, real PDF header, BCF zip), the review queue (persistence,
cross-plan safety), and the FastAPI endpoints. The suite needs no database
or external API to run.

---

## Setup notes

**WeasyPrint and fontTools.** WeasyPrint 68.x depends on `fontTools ≥ 4.62`
for font subsetting. If PDF generation fails with `expected 0 <= int <= 122,
found: 123`, run `pip install -U fonttools`.

**The Mabhas corpus.** `data/mabhas_clauses.json` is pre-classified and ready
to use; the classifier script in `classification/` only needs to be re-run if
new Mabhas sections are added.

**The RAG index.** Provisioning steps are in `Compliance Engine/RAG/`. A Colab
notebook (`mabhas_pipeline.ipynb`) automates the full ingest end-to-end. The
connection uses keyword arguments rather than a URL string to handle
special characters in passwords and managed-Postgres SSL requirements.

---

## Status

| Component | State |
|---|---|
| Stage 1 — Mask2Former training, inference, IFC export | done; production-ready API |
| Stage 2 — Mabhas corpus + RAG index | done; 324 clauses live in pgvector |
| Stage 2 — Spatial graph + 4 agents + orchestrator | done; 62 tests pass |
| Stage 2 — Report generator (HTML + PDF + BCF) | done; Iranian municipal letter template |
| Stage 2 — FastAPI + Celery async service | done |
| Stage 2 — Human review queue | done |
| Stage 1 ↔ Stage 2 integration on real model output | pending GPU training run |
| Web frontend + 3D viewer | pending (frontend integrates with both APIs) |

---