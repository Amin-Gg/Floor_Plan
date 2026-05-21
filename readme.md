# FloorPlanTo3D API — v2.0 (Mask2Former)

AI-powered floor plan analysis pipeline. Accepts a photograph of a floor plan
and returns structured BIM data in millimetres plus a downloadable IFC4 file
ready for Revit, ArchiCAD, and FreeCAD.

---

## Architecture

```
Photo of floor plan
        ↓
POST /analyze
  Mask2Former Swin-Large (fine-tuned on floor plans)
  Detects: wall, window, door, stairs, parking, balcony, terrace
        ↓
bim_data JSON  (walls, doors, windows, rooms, stairs, slabs — all in mm)
        ↓
POST /export/ifc
  Generates IFC4 file via ifcopenshell
        ↓
Open in Revit / ArchiCAD / FreeCAD → complete 3D model
        ↓
Run Dynamo scripts for Mabhas building code compliance
```

---

## Model

This project uses **Mask2Former** (Swin-Large backbone), fine-tuned on
annotated architectural floor plan images.

- Base model: `facebook/mask2former-swin-large-coco-instance`
- Fine-tuned checkpoint: stored in `./weights/mask2former-floorplan-finetuned/`
- Weights format: HuggingFace `safetensors` (not `.h5`)

> **Note:** The generic COCO base model is loaded only during development
> (`ALLOW_COCO_FALLBACK=true`). Production requires the fine-tuned checkpoint.

---

## Class Mapping

All class definitions live in **`config/classes.py`** — the single source of
truth. Do not redefine classes in any other file.

| Project ID | Training ID | Name     |
|:----------:|:-----------:|----------|
| 1          | 0           | wall     |
| 2          | 1           | window   |
| 3          | 2           | door     |
| 4          | 3           | stairs   |
| 5          | 4           | parking  |
| 6          | 5           | balcony  |
| 7          | 6           | terrace  |

**Project IDs (1-7):** used in COCO annotation files, bim_data JSON, API output.  
**Training IDs (0-6):** used inside the model head (0-indexed, contiguous).

---

## Dataset Format

COCO Instance Segmentation JSON with the 7 categories above.

```
dataset/
    train/
        images/           ← floor plan images (.png / .jpg)
        annotations.json  ← COCO-format annotations
    val/
        images/
        annotations.json
```

Recommended annotation tool: **CVAT** (https://cvat.ai) — export as COCO
instance segmentation.

---

## Setup

```bash
pip install -r requirements.txt
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FLOORPLAN_MODEL_PATH` | *(none)* | Path to fine-tuned checkpoint directory |
| `ALLOW_COCO_FALLBACK` | `true` | Allow generic COCO model in dev; **set to `false` in production** |
| `APP_ENV` | `development` | `development` / `production` / `testing` |
| `APP_CORS_ORIGINS` | `*` | Comma-separated allowed origins in production |
| `GUNICORN_WORKERS` | `1` | Number of Gunicorn workers (1 per GPU recommended) |
| `INFERENCE_WORKERS` | `1` | Concurrent inference threads per worker |
| `INFERENCE_TIMEOUT` | `90` | Seconds before a slow inference is aborted |

---

## Training

```bash
python train_mask2former.py \
    --dataset_dir ./dataset \
    --output_dir  ./weights/mask2former-floorplan-finetuned \
    --epochs      50 \
    --batch_size  2 \
    --grad_accum  8 \
    --fp16
```

After training, set `FLOORPLAN_MODEL_PATH=./weights/mask2former-floorplan-finetuned`.

---

## Evaluation

```bash
# Compute mAP@50 and mAP@50:95 on the validation set
python evaluate.py --checkpoint ./weights/mask2former-floorplan-finetuned

# Find the optimal confidence threshold
python evaluate.py --checkpoint ./weights/mask2former-floorplan-finetuned \
    --find_best_threshold
```

Then set `DETECTION_MIN_CONFIDENCE` in `config/settings.py` to the best value.

---

## Running the API

Development:
```bash
python application.py
# Swagger UI: http://localhost:8080/openapi/swagger
```

Production:
```bash
APP_ENV=production \
FLOORPLAN_MODEL_PATH=./weights/mask2former-floorplan-finetuned \
ALLOW_COCO_FALLBACK=false \
gunicorn --config gunicorn.conf.py application:application
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/analyze` | Upload floor plan image → bim_data JSON |
| `POST` | `/analyze_accuracy` | Accuracy/reliability analysis |
| `POST` | `/export/ifc` | Convert bim_data to IFC4 file |
| `GET`  | `/export/ifc/parameters` | List all building_params with defaults |
| `GET`  | `/health` | Server and model status |
| `GET`  | `/openapi/swagger` | Interactive API docs |

---

## IFC Export

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

---

## Project Structure

```
├── application.py           Flask/OpenAPI app factory
├── train_mask2former.py     HuggingFace Trainer fine-tuning script
├── evaluate.py              mAP evaluation (torchmetrics)
├── gunicorn.conf.py         Production server configuration
├── config/
│   ├── classes.py           ← Single source of truth for all class IDs
│   ├── settings.py          Environment-based configuration
│   └── constants.py         File paths and thresholds
├── models/
│   └── mask_rcnn_model.py   Mask2Former inference engine
├── routes/                  Flask blueprints (APIBlueprint for Swagger)
├── analysis/                Wall, room, stair, slab, junction analysis
├── export/
│   └── ifc_exporter.py      IFC4 file generation via ifcopenshell
├── utils/
│   ├── error_handlers.py    Centralised error handling
│   ├── validators.py        Request validation
│   └── inference_executor.py ThreadPoolExecutor for non-blocking inference
├── dataset/                 Training data (user-provided)
└── weights/                 Model checkpoints
```
